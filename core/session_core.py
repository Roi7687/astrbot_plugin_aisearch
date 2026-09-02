"""统一持久化会话核心。

设计：
- 常驻一个反检测浏览器（cloakbrowser），本地维护多个会话记录（普通/识图混合），
  每个会话有一个本地递增 id（1、2、3...），可随时列出（/ais list）与按 id 切换（/ais switch <id>）。
- 会话惰性创建（首次使用时）；空闲超时自动销毁（服务器端删除会话）；
  已销毁会话保留记录，按 id 切换时自动重建。
- 所有页面操作通过 asyncio.Lock 串行化，避免并发命令互相干扰。
- 会话元数据持久化到 conversations.json（含 id 计数器），插件重启后仍可切回未销毁的会话。
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass

from markdownify import markdownify as md
from cloakbrowser import launch_async

from .config import (
    BROWSER_LOCALE,
    BROWSER_TIMEZONE,
    CONVERSATIONS_FILE,
    DEEPSEEK_BASE,
    DEFAULT_VISION_PROMPT,
    IDLE_TIMEOUT_SECONDS,
    MODE_LABELS,
    MODES,
    MODE_NORMAL,
    MODE_VISION,
    MAX_IMAGE_BYTES,
    STATE_FILE,
    VISION_DEBUG_SHOT,
    VISION_WELCOME_TEXT,
    AuthError,
    ConversationError,
)

logger = logging.getLogger("astrbot")

# 答案文本稳定检测参数
STABLE_CHECKS = 18      # 连续 N 次文本无变化视为输出完成
STABLE_INTERVAL = 0.5   # 每次检测间隔（秒）

# 识图模式入口标签：中文正常命中；英文兜底（服务器为英文系统且语言强制失败时 UI 为英文）
VISION_ENTRY_LABELS = ("识图模式", "Image Mode", "Vision Mode", "Image Recognition")
# 模糊匹配关键词（部分改版入口带描述文字，如「识图模式 · 实验性」）
VISION_ENTRY_FUZZY = ("识图",)
# 可能收纳模式入口的「+ / 更多」菜单按钮文本（窄视口/改版下入口在弹出菜单里）
VISION_EXPAND_LABELS = ("更多", "More", "+", "＋")


@dataclass
class Conversation:
    """单个 DeepSeek 会话的元数据（local_id 由插件本地计数）"""
    local_id: int = 0               # 本地会话 id（1, 2, 3...）
    mode: str = MODE_NORMAL         # normal | vision
    session_id: str = ""
    url: str = ""                   # 会话页面 URL（用于切换回来）
    message_count: int = 0          # 用户已发送消息数
    created_at: float = 0.0
    last_active: float = 0.0
    destroyed: bool = False         # 历史兼容：v2.2.11 起销毁即删除记录，此字段仅短暂用于页面失效重建路径

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Conversation":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in allowed})


class DeepSeekSessionCore:
    """统一持久化会话核心（全异步，多会话 + 本地 id）"""

    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self.bearer_token = None
        self.lock = asyncio.Lock()      # 串行化所有页面操作
        self.conversations: dict[int, Conversation] = {}  # local_id -> 会话
        self.next_id = 1                # 本地会话 id 计数器
        self.current_id: int | None = None   # 当前会话 id
        self._closing = False
        self._load_conversations()

    # ───────────────────────── 对外 API ─────────────────────────

    @property
    def current_conversation(self) -> Conversation | None:
        """当前会话（可能为 None）"""
        if self.current_id is None:
            return None
        return self.conversations.get(self.current_id)

    @property
    def current_mode(self) -> str:
        """当前会话模式（无会话时为 normal）"""
        conv = self.current_conversation
        return conv.mode if conv else MODE_NORMAL

    async def send_message(
        self, text: str, mode: str | None = None
    ) -> tuple[bool, str]:
        """在当前会话（或指定模式会话）发送文本消息。

        mode=None 保持当前会话（无会话自动创建普通会话）；
        mode='vision' 会复用最近的识图会话（无则新建）。
        返回 (是否新建了会话, 回答文本)。
        """
        async with self.lock:
            self._check_closing()
            conv, created = await self.ensure_current_locked(mode)
            result = await self._send_query(text)
            self._mark_active(conv)
            return created, result

    async def send_image_message(
        self, text: str, image_paths: list
    ) -> tuple[bool, str]:
        """上传图片到**全新的识图会话**并发送提问。返回 (是否新建了会话, 回答文本)。

        v2.2.9 起每次传图都新建识图会话：多张图堆在同一个 DeepSeek 识图会话
        中容易导致回复异常；旧会话保留在列表中（/ais list 可见，/ais switch <id> 可切回）。
        注意：此处不能调用 new_conversation()（其内部持锁，会与当前锁死锁），
        直接 _create_conversation + _register。
        """
        async with self.lock:
            self._check_closing()
            conv = await self._create_conversation(MODE_VISION)
            self._register(conv)
            await self._upload_images(image_paths)
            result = await self._send_query(text or DEFAULT_VISION_PROMPT)
            self._mark_active(conv)
            return True, result

    async def ensure_mode(self, mode: str) -> tuple[Conversation, bool]:
        """确保当前会话为指定模式：复用该模式最近活跃会话，没有则新建。
        返回 (会话, 是否新建)。"""
        async with self.lock:
            self._check_closing()
            return await self.ensure_current_locked(mode)

    async def switch_conversation(self, local_id: int) -> tuple[Conversation, bool]:
        """按本地 id 切换会话；id 不存在（含已销毁被移除记录）报错。
        返回 (会话, 是否重建了会话)。"""
        async with self.lock:
            self._check_closing()
            conv = self.conversations.get(local_id)
            if not conv:
                raise ConversationError(
                    f"找不到会话 #{local_id}，请先发送 /ais list 查看可用会话。"
                )
            try:
                await self._goto_session(conv)
            except ConversationError:
                logger.warning(f"⚠️ [Session] 会话 #{local_id} 页面已失效，自动重建...")
                conv.destroyed = True
                new_conv = await self._create_conversation(conv.mode)
                new_conv.local_id = conv.local_id
                new_conv.created_at = conv.created_at
                self.conversations[local_id] = new_conv
                self.current_id = local_id
                self._save_conversations()
                return new_conv, True
            self.current_id = local_id
            self._save_conversations()
            return conv, False

    async def new_conversation(self, mode: str = MODE_NORMAL) -> Conversation:
        """创建新会话并设为当前（v2.2.10 起总是新建普通会话，
        不再继承上一轮会话的模式——识图由发送图片自动触发，新建识图会话无意义）。
        旧会话保留在列表中，可按 id 切回。"""
        async with self.lock:
            self._check_closing()
            conv = await self._create_conversation(mode)
            self._register(conv)
            return conv

    async def destroy_conversation(self, local_id: int) -> bool:
        """销毁空闲超时的会话（由空闲计时器调用）：服务器端删除 + 本地记录一并移除。

        v2.2.11 起不再保留已销毁的记录（用户无法再访问，且避免
        conversations.json 无限膨胀）；销毁的是当前会话时重置 current_id。
        v2.2.12 起**锁内权威校验真实空闲时间**：若计时器到点后会话仍有新活动
        （例如一次提问耗时跨越了计时终点，等待锁期间刚完成提问），放弃销毁；
        全部会话销毁后 id 计数归 1，避免无限增长。
        返回是否真的有会话被销毁。
        """
        async with self.lock:
            conv = self.conversations.get(local_id)
            if not conv or conv.destroyed:
                return False
            # 真实空闲校验（会话记录仍在且最近 300s 内有活动 → 不销毁）
            if time.time() - conv.last_active < IDLE_TIMEOUT_SECONDS:
                return False
            await self._delete_session_best_effort(conv)
            del self.conversations[local_id]
            if self.current_id == local_id:
                self.current_id = None
            if not self.conversations:
                self.next_id = 1  # v2.2.12：无存活会话时 id 计数归 1
            self._save_conversations()
            logger.info(
                f"⏳ [Session] 会话 #{local_id}（{MODE_LABELS.get(conv.mode, conv.mode)}）已销毁并移除记录。"
            )
            return True

    def get_conversation(self, local_id: int) -> Conversation | None:
        return self.conversations.get(local_id)

    def conversation_summary(self, local_id: int) -> dict:
        """单个会话摘要（供 /ais list 使用）"""
        conv = self.conversations.get(local_id)
        now = time.time()
        if not conv:
            return {"local_id": local_id, "exists": False}
        remain = (
            0
            if conv.destroyed
            else max(0, int(IDLE_TIMEOUT_SECONDS - (now - conv.last_active)))
        )
        return {
            "local_id": local_id,
            "mode": conv.mode,
            "label": MODE_LABELS.get(conv.mode, conv.mode),
            "exists": True,
            "destroyed": conv.destroyed,
            "session_id": conv.session_id or "未知",
            "message_count": conv.message_count,
            "created_at": time.strftime("%m-%d %H:%M", time.localtime(conv.created_at)),
            "last_active": time.strftime("%m-%d %H:%M", time.localtime(conv.last_active)),
            "idle_remain": remain,
            "is_current": (local_id == self.current_id),
        }

    def list_summary(self) -> list[dict]:
        """全部会话摘要（按本地 id 升序）"""
        return [self.conversation_summary(i) for i in sorted(self.conversations)]

    def is_browser_alive(self) -> bool:
        return self.browser is not None

    async def close(self):
        """安全关闭浏览器（插件卸载 / 登录凭证刷新后调用）"""
        async with self.lock:
            self._closing = True
            if self.browser:
                try:
                    await self.browser.close()
                except Exception as e:
                    logger.warning(f"⚠️ [Session] 关闭浏览器异常: {e}")
                self.browser = None
                self.context = None
                self.page = None
            # 会话元数据保留（destroyed 状态不变），下次使用重建浏览器
            self._closing = False
            logger.info("🔴 [Session] 浏览器已关闭。")

    # ───────────────────────── 内部实现 ─────────────────────────

    def _check_closing(self):
        if self._closing:
            raise ConversationError("会话正在关闭中，请稍后重试")

    def _register(self, conv: Conversation):
        """为新会话分配本地 id 并登记为当前会话"""
        if not self.conversations:
            # v2.2.12：没有任何存活会话时 id 从 1 重新开始，避免无限增长
            self.next_id = 1
        conv.local_id = self.next_id
        self.next_id += 1
        self.conversations[conv.local_id] = conv
        self.current_id = conv.local_id
        self._save_conversations()
        logger.info(
            f"🟢 [Session] 已创建会话 #{conv.local_id}（{MODE_LABELS.get(conv.mode, conv.mode)}）。"
        )

    def _latest_of_mode(self, mode: str, alive_only: bool = True) -> Conversation | None:
        """返回指定模式最近活跃的会话（alive_only=True 时排除已销毁）"""
        cands = [c for c in self.conversations.values() if c.mode == mode]
        if alive_only:
            cands = [c for c in cands if not c.destroyed]
        return max(cands, key=lambda c: c.last_active) if cands else None

    def _load_conversations(self):
        try:
            if not os.path.exists(CONVERSATIONS_FILE):
                return
            with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "conversations" in data:
                # 新格式：{"next_id": N, "current_id": id, "conversations": {id: {...}}}
                self.next_id = int(data.get("next_id") or 1)
                self.current_id = data.get("current_id")
                for k, d in data["conversations"].items():
                    conv = Conversation.from_dict(d)
                    if conv.local_id:
                        self.conversations[conv.local_id] = conv
            elif isinstance(data, dict):
                # 旧格式 {"normal": {...}, "vision": {...}}：迁移为本地 id（normal=1, vision=2）
                for mode in MODES:
                    if mode in data:
                        conv = Conversation.from_dict(
                            {**data[mode], "local_id": self.next_id}
                        )
                        self.conversations[self.next_id] = conv
                        self.next_id += 1
                alive = [c for c in self.conversations.values() if not c.destroyed]
                if alive:
                    self.current_id = max(alive, key=lambda c: c.last_active).local_id
                if self.conversations:
                    logger.info("📂 [Session] 已迁移旧版双槽位会话数据为本地 id 索引。")
            # v2.2.11 起销毁即删除记录：清理历史遗留的 destroyed 数据并立即落盘
            stale = [i for i, c in self.conversations.items() if c.destroyed]
            for i in stale:
                del self.conversations[i]
            changed = bool(stale)
            if stale:
                logger.info(f"🧹 [Session] 已清理 {len(stale)} 条已销毁的会话记录。")
            # v2.2.12：无任何存活会话时 id 计数归 1（避免无限增长）
            if not self.conversations and self.next_id != 1:
                self.next_id = 1
                changed = True
                logger.info("🧹 [Session] 无存活会话，id 计数已归 1。")
            if self.current_id not in self.conversations:
                self.current_id = None
            if changed:
                self._save_conversations()
            logger.info(
                f"📂 [Session] 已加载会话元数据: {len(self.conversations)} 个会话（下一个 id={self.next_id}）"
            )
        except Exception as e:
            logger.warning(f"⚠️ [Session] 加载会话元数据失败: {e}")

    def _save_conversations(self):
        try:
            with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "next_id": self.next_id,
                        "current_id": self.current_id,
                        "conversations": {
                            str(k): c.to_dict() for k, c in self.conversations.items()
                        },
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.warning(f"⚠️ [Session] 保存会话元数据失败: {e}")

    def _mark_active(self, conv: Conversation):
        conv.message_count += 1
        conv.last_active = time.time()
        self.current_id = conv.local_id
        # 发送后页面 URL 会更新为真实会话地址（/a/chat/s/<uuid>），回写记录
        try:
            url = self.page.url if self.page else ""
            if url and ("/s/" in url or "/a/chat/" in url):
                conv.url = url
                sid = self._extract_session_id(url)
                if sid:
                    conv.session_id = sid
        except Exception:
            pass
        self._save_conversations()

    async def ensure_current_locked(self, mode: str | None = None) -> tuple[Conversation, bool]:
        """（持锁调用）确保当前会话就绪：返回 (会话, 是否新建)"""
        conv = self.current_conversation
        if conv and not conv.destroyed and (mode is None or conv.mode == mode):
            # 当前会话模式匹配（或无需指定）：导航回其页面，失效则移除记录并新建
            try:
                await self._goto_session(conv)
                return conv, False
            except ConversationError:
                logger.warning(f"⚠️ [Session] 会话 #{conv.local_id} 页面已失效，移除记录并新建...")
                del self.conversations[conv.local_id]
                if self.current_id == conv.local_id:
                    self.current_id = None
                self._save_conversations()
        if mode is not None:
            # 指定模式：复用该模式最近活跃会话
            cand = self._latest_of_mode(mode)
            if cand:
                try:
                    await self._goto_session(cand)
                    self.current_id = cand.local_id
                    self._save_conversations()
                    return cand, False
                except ConversationError:
                    logger.warning(f"⚠️ [Session] 会话 #{cand.local_id} 页面已失效，移除记录并新建...")
                    del self.conversations[cand.local_id]
                    if self.current_id == cand.local_id:
                        self.current_id = None
                    self._save_conversations()
            conv = await self._create_conversation(mode)
            self._register(conv)
            return conv, True
        # 无当前会话：创建普通会话
        conv = await self._create_conversation(MODE_NORMAL)
        self._register(conv)
        return conv, True

    async def _init_browser(self):
        """按需初始化浏览器"""
        if not os.path.exists(STATE_FILE):
            raise AuthError("本地未找到登录凭证。")

        logger.info("🚀 [Session] 正在启动持久化搜索内核...")
        # 强制中文 UI：不指定 locale 时浏览器继承服务器系统语言（Linux 多为英文），
        # DeepSeek 会返回英文界面，「识图模式」等中文文本匹配将全部失败
        self.browser = await launch_async(
            headless=True,
            humanize=True,
            locale=BROWSER_LOCALE,
            timezone=BROWSER_TIMEZONE,
        )
        self.context = await self.browser.new_context(
            storage_state=STATE_FILE,
            locale=BROWSER_LOCALE,
            timezone_id=BROWSER_TIMEZONE,
        )
        self.page = await self.context.new_page()
        self.page.set_default_timeout(30000)

        def sniff_auth_token(request):
            auth_header = request.headers.get("authorization")
            if auth_header and auth_header.startswith("Bearer"):
                self.bearer_token = auth_header

        self.page.on("request", sniff_auth_token)

    async def _create_conversation(self, mode: str) -> Conversation:
        """开启指定模式的新会话（识图模式需点击模式入口）。id 由 _register 分配"""
        if not self.browser:
            await self._init_browser()

        await self._open_new_chat()
        if mode == MODE_VISION:
            await self._switch_to_vision()

        conv = Conversation(mode=mode, created_at=time.time(), last_active=time.time())
        conv.url = self.page.url
        conv.session_id = self._extract_session_id(self.page.url)
        return conv

    async def _open_new_chat(self):
        """打开一个全新的空白对话页"""
        await self.page.goto(DEEPSEEK_BASE + "/")
        await self.page.wait_for_load_state("networkidle")
        await asyncio.sleep(3)  # 等待 React 渲染

        # 风控提示检查（便于排查）
        try:
            body_text = await self.page.evaluate("() => document.body.innerText.slice(0, 200)")
            if "使用环境异常" in body_text:
                raise ConversationError(
                    "DeepSeek 网页端触发了环境风控（使用环境异常）。请稍后重试，或重新执行 /cloak登录 刷新凭证。"
                )
        except ConversationError:
            raise
        except Exception:
            pass

        # 等待输入框出现，确保聊天页真正可用（慢速服务器/弱网下更稳妥）
        try:
            await self.page.locator("textarea").first.wait_for(
                state="visible", timeout=30000
            )
        except Exception:
            logger.warning("⚠️ [Session] 新对话页输入框未出现，继续尝试...")

        # 若当前不是空白新对话（有历史消息），点击「开启新对话」
        has_messages = await self.page.evaluate(
            "() => document.querySelectorAll('.ds-markdown').length > 0"
        )
        if has_messages:
            clicked = await self.page.evaluate(
                """() => {
                    const nodes = [...document.querySelectorAll('div,span,button,[role=button]')];
                    for (const e of nodes) {
                        const x = (e.innerText || '').trim();
                        if ((x === '开启新对话' || x === '新建对话' || x === 'New chat' || x === 'New Chat') && e.children.length === 0) {
                            e.click();
                            return 'clicked';
                        }
                    }
                    return 'not_found';
                }"""
            )
            if clicked == "not_found":
                logger.warning("⚠️ [Session] 未找到「开启新对话」按钮，尝试直接导航。")
            await asyncio.sleep(2)

    async def _switch_to_vision(self):
        """点击「识图模式」入口并验证切换成功。

        多策略查找：精确标签（中/英）→ 模糊文本 → 展开「+ / 更多」菜单；
        全部失败时保存页面截图与文本片段留档，便于排查 UI 变化。
        """
        if await self._in_vision_state():
            return

        for attempt in range(3):
            clicked = await self._click_vision_entry()
            if clicked == "clicked":
                await asyncio.sleep(1)
                if await self._in_vision_state():
                    return
                continue
            # 未找到入口：第一次先尝试展开「+ / 更多」菜单（窄视口/改版下入口在弹出菜单里）
            if attempt == 0:
                expanded = await self._expand_vision_menu()
                if expanded:
                    logger.info("🔎 [Session] 已展开「更多」菜单，重新查找识图模式入口...")
                    await asyncio.sleep(1.2)
                    continue
            # 三次都找不到（慢速服务器页面未渲染完也会在这里兜住）才留档报错
            if attempt == 2:
                await self._dump_vision_failure()
                raise ConversationError(
                    "未找到「识图模式」入口。可能当前账号未开放识图功能、网页 UI 已更新，"
                    "或浏览器被识别为英文环境（已自动尝试中文/英文入口与「更多」菜单）。"
                    "已保存页面截图 vision_debug.png 到插件目录，请查看后反馈。"
                )
        raise ConversationError("「识图模式」切换失败，请稍后重试。")

    async def _in_vision_state(self) -> bool:
        """判断当前页面是否已处于识图模式：欢迎语出现，或入口元素处于激活态"""
        return await self.page.evaluate(
            """() => {
                const t = document.body.innerText || '';
                if (t.indexOf(%s) >= 0) return true;
                const labels = %s;
                for (const e of document.querySelectorAll('div,span,button')) {
                    const x = (e.innerText || '').trim();
                    if (labels.indexOf(x) >= 0) {
                        const c = '' + e.className;
                        if (c.indexOf('active') >= 0 || c.indexOf('selected') >= 0 ||
                            e.getAttribute('aria-selected') === 'true') return true;
                    }
                }
                return false;
            }""" % (
                json.dumps(VISION_WELCOME_TEXT),
                json.dumps(list(VISION_ENTRY_LABELS)),
            )
        )

    async def _click_vision_entry(self) -> str:
        """查找并点击识图模式入口（精确标签优先，模糊文本兜底）。返回 'clicked' / 'not_found'"""
        return await self.page.evaluate(
            """() => {
                const exact = %s, fuzzy = %s;
                // 在候选元素中挑「最深」（children 最少）且文本最短的叶节点，避免点中父容器
                const pick = (pred) => {
                    let best = null;
                    for (const e of document.querySelectorAll('div,span,button,[role=button]')) {
                        if (!pred(e)) continue;
                        if (!best) { best = e; continue; }
                        const leaf = e.children.length === 0;
                        const bestLeaf = best.children.length === 0;
                        if (leaf && !bestLeaf) best = e;
                        else if (leaf === bestLeaf &&
                                 (e.innerText || '').length < (best.innerText || '').length) best = e;
                    }
                    return best;
                };
                let el = pick(e => {
                    const x = (e.innerText || '').trim();
                    return exact.indexOf(x) >= 0;
                });
                if (!el) {
                    // 改版后入口可能带描述文字（如「识图模式 · 实验性」）：模糊匹配短文本
                    el = pick(e => {
                        const x = (e.innerText || '').trim();
                        return fuzzy.some(k => x.indexOf(k) >= 0) && x.length <= 20;
                    });
                }
                if (!el) return 'not_found';
                try { el.scrollIntoView({block: 'center'}); } catch (err) {}
                el.click();
                return 'clicked';
            }""" % (
                json.dumps(list(VISION_ENTRY_LABELS)),
                json.dumps(list(VISION_ENTRY_FUZZY)),
            )
        )

    async def _expand_vision_menu(self) -> bool:
        """点击可能收纳模式入口的「+ / 更多」菜单按钮，返回是否点击了候选按钮"""
        return await self.page.evaluate(
            """() => {
                const labels = %s;
                const cands = [...document.querySelectorAll('div,span,button,[role=button]')].filter(e => {
                    const x = (e.innerText || '').trim();
                    if (labels.indexOf(x) >= 0) return true;
                    const aria = e.getAttribute('aria-label') || '';
                    const cls = '' + e.className;
                    return /更多|more|add|plus/i.test(aria) ||
                           /(^|\\s)(ds-)?(add|plus|more)[\\w-]*/i.test(cls);
                });
                if (!cands.length) return false;
                let best = cands[0];
                for (const e of cands) {
                    const leaf = e.children.length === 0;
                    const bestLeaf = best.children.length === 0;
                    if (leaf && !bestLeaf) best = e;
                    else if (leaf === bestLeaf &&
                             (e.innerText || '').length < (best.innerText || '').length) best = e;
                }
                try { best.scrollIntoView({block: 'center'}); } catch (err) {}
                best.click();
                return true;
            }""" % json.dumps(list(VISION_EXPAND_LABELS))
        )

    async def _dump_vision_failure(self):
        """识图入口查找失败时留档：保存页面截图与文本片段，便于排查 UI 变化"""
        try:
            await self.page.screenshot(path=VISION_DEBUG_SHOT)
            snippet = await self.page.evaluate(
                "() => (document.body.innerText || '').slice(0, 600).replace(/\\s+/g, ' ')"
            )
            logger.warning(
                f"⚠️ [Session] 未找到识图模式入口，截图已保存: {VISION_DEBUG_SHOT}\n"
                f"    页面文本片段: {snippet}"
            )
        except Exception as e:
            logger.warning(f"⚠️ [Session] 识图失败现场留档异常: {e}")

    async def _goto_session(self, conv: Conversation):
        """导航回已存在的会话页面"""
        if not self.browser:
            await self._init_browser()
        if not conv.url:
            raise ConversationError("会话页面地址缺失，请重置会话。")
        if "/s/" not in conv.url:
            # 尚无会话地址（创建后未发过消息）：按模式重新进入新对话页
            await self._open_new_chat()
            if conv.mode == MODE_VISION:
                await self._switch_to_vision()
            return
        await self.page.goto(conv.url)
        await self.page.wait_for_load_state("networkidle")
        input_box = self.page.locator("textarea").first
        await input_box.wait_for(state="visible", timeout=20000)
        await asyncio.sleep(1)

    # ── 消息发送与等待 ──

    async def _locate_by_text(
        self, selectors: tuple, text: str, visible_timeout: float = 2.0
    ):
        """在候选 CSS 容器中查找「文本包含指定文字」的第一个可见元素。

        兼容 cloakbrowser humanize：其 isolated-world resolver 不支持
        .filter(has_text=...) 链式 locator，改用纯 CSS 定位 + all_inner_texts
        过滤 + 尾部 .nth()。找不到返回 None。
        """
        for selector in selectors:
            loc = self.page.locator(selector)
            try:
                texts = await loc.all_inner_texts()
            except Exception:
                continue
            idx = next((i for i, t in enumerate(texts) if t and text in t), None)
            if idx is None:
                continue
            btn = loc.nth(idx)
            try:
                await btn.wait_for(state="visible", timeout=visible_timeout)
            except Exception:
                continue
            return btn
        return None

    async def _type_and_send(self, text: str):
        """在输入框中输入文本并发送（Enter 优先，按钮兜底）"""
        input_box = self.page.locator(
            "textarea[name='search'], textarea[placeholder*='发送消息'], textarea"
        ).first
        await input_box.wait_for(state="visible", timeout=30000)
        await input_box.click()
        await asyncio.sleep(0.3)
        await input_box.press_sequentially(text, delay=30)
        await asyncio.sleep(0.3)

        # 发送：先 Enter，再检测输入框是否清空
        await input_box.press("Enter")
        cleared = await self._wait_textarea_cleared(6)
        if not cleared:
            try:
                send_btn = await self._locate_by_text(
                    ("div[role=button].ds-button--primary", "div[role=button]", "button"),
                    "发送",
                )
                if send_btn is not None:
                    await send_btn.click()
            except Exception:
                pass
            await self._wait_textarea_cleared(6)

    async def _wait_textarea_cleared(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = await self.page.evaluate(
                "() => { const ta = document.querySelector('textarea'); return ta ? ta.value : ''; }"
            )
            if not value:
                return True
            await asyncio.sleep(0.4)
        return False

    async def _send_query(self, text: str) -> str:
        """在当前页面发送提问并等待回答（不含会话状态维护）。

        等待策略（v2.2.7 回归简单版）：发送后等待对话框最后一条回复出现，
        随后对其做**文本稳定检测**（连续 9 秒无变化视为输出完成）。
        不做回复块计数 / 生成标志 / 补发重试等复杂判断，避免在部分环境下
        卡死或误判；保留 300 秒总上限防止异常情况下无限等待。
        深度思考 / 联网搜索跟随 DeepSeek 网页端设置（不自动操作开关）。
        """
        await self._type_and_send(text)

        # 发送后的快速错误检测
        await asyncio.sleep(2)
        rejected = await self.page.evaluate(
            """() => {
                const t = document.body.innerText || '';
                return t.indexOf('违反使用规范') >= 0 || t.indexOf('消息未能发送') >= 0;
            }"""
        )
        if rejected:
            return "⚠️ DeepSeek 拒绝了这条消息（内容违反使用规范或发送失败），请调整后重试。"

        answer_locator = self.page.locator(
            "div.ds-markdown.ds-assistant-message-main-content"
        )
        try:
            await answer_locator.last.wait_for(state="visible", timeout=30000)
        except Exception:
            # UI 变动兜底：退回通用 .ds-markdown 选择器
            answer_locator = self.page.locator(".ds-markdown")
            try:
                await answer_locator.last.wait_for(state="visible", timeout=30000)
            except Exception:
                raise ConversationError("等待 AI 回答超时，请稍后重试。")

        # 文本稳定检测：连续 9 秒无变化视为输出完成
        previous_text = ""
        stable_count = 0
        total_deadline = time.time() + 300  # 总上限，防止异常情况下无限等待
        while True:
            try:
                current_text = await answer_locator.last.inner_text()
            except Exception:
                current_text = ""
            if current_text == previous_text and len(current_text) > 0:
                stable_count += 1
            else:
                stable_count = 0
                previous_text = current_text

            if stable_count >= STABLE_CHECKS:
                break
            if time.time() > total_deadline:
                break
            await asyncio.sleep(STABLE_INTERVAL)

        # 清理引用角标
        try:
            await self.page.evaluate(
                "() => document.querySelectorAll('.ds-markdown-cite').forEach(el => el.remove())"
            )
        except Exception as e:
            logger.warning(f"⚠️ [Session] 清理 DOM 引用角标异常: {e}")

        raw_html = await answer_locator.last.inner_html()
        return md(raw_html, heading_style="ATX")

    # ── 图片上传 ──

    async def _upload_images(self, image_paths: list):
        """将本地图片上传到识图会话（Playwright set_input_files 注入）"""
        existing = [p for p in image_paths if p and os.path.exists(p)]
        if not existing:
            raise ConversationError("没有可用的图片文件（可能已过期或下载失败）。")

        # 找图片文件输入框：优先 accept 含 image 的，退而取第一个 input[type=file]
        file_input = self.page.locator("input[type=file][accept*='image']").first
        if await file_input.count() == 0:
            file_input = self.page.locator("input[type=file]").first
        if await file_input.count() == 0:
            raise ConversationError("未找到图片上传入口，可能网页 UI 已更新。")

        await file_input.set_input_files(existing)
        logger.info(f"📤 [Session] 已注入 {len(existing)} 张图片，等待上传完成...")

        # 阶段 1：等待 blob 缩略图出现（文件已被读取并挂载到输入区）
        thumbnails_ok = False
        for retry in range(3):
            ok = await self._wait_blob_thumbnails(len(existing), timeout=30)
            if ok:
                thumbnails_ok = True
                break
            logger.warning(f"⚠️ [Session] 上传缩略图未出现（第 {retry + 1} 次），重试注入...")
            try:
                await file_input.set_input_files(existing)
            except Exception:
                pass
        if not thumbnails_ok:
            raise ConversationError("图片上传超时，请重试。")

        # 阶段 2（关键）：缩略图出现 ≠ 上传完成——慢速网络下文件可能仍在
        # 上传到 DeepSeek 服务器，此时发送提问会导致 AI 收不到图片。
        # 双路确认上传完成：上传网络请求全部响应 或 DOM 上传中标志消失。
        finished = await self._wait_upload_finished(len(existing), timeout=90)
        if not finished:
            raise ConversationError("图片上传未完成（网络较慢或上传失败），请稍后重试。")
        logger.info("✅ [Session] 图片上传完成，开始发送提问...")

    async def _wait_upload_finished(self, count: int, timeout: float) -> bool:
        """等待图片上传真正完成。

        双路确认：
        ① 监听上传网络请求（POST/PUT 且 URL 含 upload/file），等待全部响应完成；
        ② 兜底：DOM 上传中标志（进度条 / 百分比 / uploading 类）消失。
        """
        net_ok = await self._wait_upload_network_done(count, timeout=timeout)
        if net_ok:
            await asyncio.sleep(1)  # 让 UI 完成附件挂载
            return True
        dom_ok = await self._wait_no_upload_marker(timeout=30)
        if dom_ok:
            await asyncio.sleep(1)
            return True
        return False

    async def _wait_upload_network_done(self, count: int, timeout: float) -> bool:
        """监听上传网络请求（POST/PUT 且 URL 含 upload/file），等待全部完成"""
        done_count = 0
        done_event = asyncio.Event()

        def is_upload_request(req) -> bool:
            try:
                if req.method not in ("POST", "PUT"):
                    return False
                url = (req.url or "").lower()
                return "upload" in url or "/file" in url or "/files" in url
            except Exception:
                return False

        def on_response(resp):
            nonlocal done_count
            try:
                if is_upload_request(resp.request):
                    done_count += 1
                    if done_count >= count:
                        done_event.set()
            except Exception:
                pass

        def on_request_failed(req):
            nonlocal done_count
            try:
                if is_upload_request(req):
                    done_count += 1
                    if done_count >= count:
                        done_event.set()
            except Exception:
                pass

        self.page.on("response", on_response)
        self.page.on("requestfailed", on_request_failed)
        try:
            await asyncio.wait_for(done_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self.page.remove_listener("response", on_response)
            self.page.remove_listener("requestfailed", on_request_failed)

    async def _wait_no_upload_marker(self, timeout: float) -> bool:
        """兜底检测：页面无上传中标志（进度条 / 百分比 / uploading 类，排除回答正文）"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            ok = await self.page.evaluate(
                """() => {
                    const inMd = e => !!(e.closest && e.closest('.ds-markdown'));
                    const els = [...document.querySelectorAll('div,span')];
                    // 上传进度 class（uploading / progress 且与上传相关）
                    const busyClass = els.some(e => {
                        if (inMd(e)) return false;
                        const c = '' + e.className;
                        return /uploading|is-uploading|upload-progress|uploading-progress/.test(c);
                    });
                    // 百分比文本（如 45%），排除回答正文
                    const busyPct = els.some(e => {
                        if (inMd(e)) return false;
                        return /^\\s*\\d{1,3}%\\s*$/.test(e.innerText || '');
                    });
                    return !(busyClass || busyPct);
                }"""
            )
            if ok:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _wait_blob_thumbnails(self, count: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            n = await self.page.evaluate(
                """() => {
                    let n = 0;
                    for (const im of document.querySelectorAll('img')) {
                        if ((im.src || '').startsWith('blob:')) n++;
                    }
                    return n;
                }"""
            )
            if n >= count:
                return True
            await asyncio.sleep(0.3)
        return False

    # ── 会话清理 ──

    def _extract_session_id(self, url: str) -> str:
        m = re.search(r"/(?:a/chat/s/|s/)([0-9a-fA-F-]{8,})", url)
        return m.group(1) if m else ""

    async def _delete_session_best_effort(self, conv: Conversation):
        """通过 DeepSeek API 静默删除服务器端会话"""
        sid = conv.session_id or self._extract_session_id(conv.url)
        if not sid or not self.context:
            return
        try:
            delete_api = DEEPSEEK_BASE + "/api/v0/chat_session/delete"
            headers = {"content-type": "application/json", "x-client-platform": "web"}
            if self.bearer_token:
                headers["authorization"] = self.bearer_token
            await self.context.request.post(
                delete_api, data={"chat_session_id": sid}, headers=headers
            )
            logger.info(f"🗑 [Session] 已删除服务器端会话 {sid}")
        except Exception as e:
            logger.warning(f"⚠️ [Session] 删除服务器端会话失败: {e}")
