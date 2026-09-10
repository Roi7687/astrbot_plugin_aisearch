"""统一持久化会话核心。

设计：
- 常驻一个反检测浏览器（cloakbrowser），本地维护多个会话记录，
  每个会话有一个本地递增 id（1、2、3...），可随时列出（/ais list）与按 id 切换（/ais switch <id>）。
- v2.3.0 起 DeepSeek 官方把「快速 / 专家 / 识图模式」合并为统一对话
  （公告：「快速、专家、识图模式已合并升级」，图片理解内置于同一会话），
  插件相应取消「普通模式 / 识图模式」之分：文本与图片都在当前会话中处理。
- 会话惰性创建（首次使用时）；空闲超时自动销毁（服务器端删除会话 + 本地记录移除）。
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
    DEFAULT_IMAGE_PROMPT,
    IDLE_TIMEOUT_SECONDS,
    MAX_IMAGE_BYTES,
    STATE_FILE,
    AuthError,
    ConversationError,
)

logger = logging.getLogger("astrbot")

# 答案文本稳定检测参数
STABLE_CHECKS = 18      # 连续 N 次文本无变化视为输出完成
STABLE_INTERVAL = 0.5   # 每次检测间隔（秒）


@dataclass
class Conversation:
    """单个 DeepSeek 会话的元数据（local_id 由插件本地计数）"""
    local_id: int = 0               # 本地会话 id（1, 2, 3...）
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
    """统一持久化会话核心（全异步，多会话 + 本地 id；文本与图片共用同一会话）"""

    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self.bearer_token = None
        self._toggle_warned: set = set()   # 已警告过的模式开关名（避免日志刷屏）
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

    async def send_message(self, text: str) -> tuple[bool, str]:
        """在当前会话发送文本消息（无会话自动创建）。返回 (是否新建了会话, 回答文本)。"""
        async with self.lock:
            self._check_closing()
            conv, created = await self.ensure_current_locked()
            result = await self._send_query(text)
            self._mark_active(conv)
            return created, result

    async def send_image_message(
        self, text: str, image_paths: list
    ) -> tuple[bool, str]:
        """在**当前会话**上传图片并发送提问。返回 (是否新建了会话, 回答文本)。

        v2.3.0 起 DeepSeek 已合并普通/识图模式（图片理解内置于同一会话），
        图片直接在当前会话中上传，保持上下文连续（不再另开识图会话）；
        无会话时自动创建。
        """
        async with self.lock:
            self._check_closing()
            conv, created = await self.ensure_current_locked()
            await self._upload_images(image_paths)
            result = await self._send_query(text or DEFAULT_IMAGE_PROMPT)
            self._mark_active(conv)
            return created, result

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
                new_conv = await self._create_conversation()
                new_conv.local_id = conv.local_id
                new_conv.created_at = conv.created_at
                self.conversations[local_id] = new_conv
                self.current_id = local_id
                self._save_conversations()
                return new_conv, True
            self.current_id = local_id
            self._save_conversations()
            return conv, False

    async def new_conversation(self) -> Conversation:
        """创建新会话并设为当前。旧会话保留在列表中，可按 id 切回。"""
        async with self.lock:
            self._check_closing()
            conv = await self._create_conversation()
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
            logger.info(f"⏳ [Session] 会话 #{local_id} 已销毁并移除记录。")
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
        logger.info(f"🟢 [Session] 已创建会话 #{conv.local_id}。")

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
                # 旧格式（v2.0 双槽位）{"normal": {...}, "vision": {...}}：迁移为本地 id（1、2）
                for key in ("normal", "vision"):
                    if key in data:
                        conv = Conversation.from_dict(
                            {**data[key], "local_id": self.next_id}
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

    async def ensure_current_locked(self) -> tuple[Conversation, bool]:
        """（持锁调用）确保当前会话就绪：返回 (会话, 是否新建)"""
        conv = self.current_conversation
        if conv and not conv.destroyed:
            # 导航回当前会话页面；页面失效则移除记录并新建
            try:
                await self._goto_session(conv)
                return conv, False
            except ConversationError:
                logger.warning(f"⚠️ [Session] 会话 #{conv.local_id} 页面已失效，移除记录并新建...")
                del self.conversations[conv.local_id]
                if self.current_id == conv.local_id:
                    self.current_id = None
                self._save_conversations()
        # 无可用会话：创建新会话
        conv = await self._create_conversation()
        self._register(conv)
        return conv, True

    async def _init_browser(self):
        """按需初始化浏览器"""
        if not os.path.exists(STATE_FILE):
            raise AuthError("本地未找到登录凭证。")

        logger.info("🚀 [Session] 正在启动持久化搜索内核...")
        # 强制中文 UI：不指定 locale 时浏览器继承服务器系统语言（Linux 多为英文），
        # DeepSeek 会返回英文界面，导致按中文文本定位页面元素（深度思考开关等）失败
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

    async def _create_conversation(self) -> Conversation:
        """开启新会话（DeepSeek 已合并模式，无需切换识图入口）。id 由 _register 分配"""
        if not self.browser:
            await self._init_browser()

        await self._open_new_chat()

        conv = Conversation(created_at=time.time(), last_active=time.time())
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

    async def _goto_session(self, conv: Conversation):
        """导航回已存在的会话页面"""
        if not self.browser:
            await self._init_browser()
        if not conv.url:
            raise ConversationError("会话页面地址缺失，请重置会话。")
        if "/s/" not in conv.url:
            # 尚无会话地址（创建后未发过消息）：重新进入新对话页
            await self._open_new_chat()
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
        visible_timeout 单位为**秒**（内部换算毫秒传给 playwright——修复
        v2.2.4 起把 2.0 秒误当 2ms 导致等待必然超时的潜伏 bug）。
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
                await btn.wait_for(state="visible", timeout=int(visible_timeout * 1000))
            except Exception:
                continue
            return btn
        return None

    async def _ensure_deep_think_on(self) -> bool:
        """确保「深度思考」模式开启（幂等）。v2.2.13 起每次提问默认开启。

        定位方式与 v1.3.0 相同：文本为「深度思考」的 .ds-toggle-button。
        2026-08 真实登录态浏览器实测：该元素在当前 UI 仍存在且可点击——点击后
        class 追加 ds-toggle-button--selected、aria-pressed 变 true；**再次点击会
        关闭**，因此必须先检测激活态（已激活则不点击，避免开关被误关）。
        找不到开关时降级不阻塞提问（网页端若已记忆开启则不受影响），仅首次警告。
        """
        btn = await self._locate_by_text((".ds-toggle-button",), "深度思考")
        if btn is None:
            if "深度思考" not in self._toggle_warned:
                self._toggle_warned.add("深度思考")
                logger.warning("⚠️ [Session] 未找到「深度思考」开关，保持网页端当前设置。")
            return False
        try:
            active = await btn.evaluate(
                """(el) => {
                    const c = '' + (el.className || '');
                    const aria = el.getAttribute('aria-pressed') || el.getAttribute('aria-checked');
                    return c.indexOf('selected') >= 0 || c.indexOf('active') >= 0 || aria === 'true';
                }"""
            )
            if active:
                return True
            await btn.click()
            await asyncio.sleep(1.0)
            logger.info("🔛 [Session] 已开启「深度思考」")
            return True
        except Exception as e:
            logger.debug(f"[Session] 开启「深度思考」失败: {e}")
            return False

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

        等待策略：**发送前**记录助手回复块的基线（数量 + 最后一块文本），发送后
        轮询等待「回复块数量增加 或 最后一块文本变化」= 本轮新回复开始渲染
        （v2.2.14：修复两种误判——连续对话时旧回复仍在，直接对「最后一条」做
        稳定检测会把上一轮回复当成本轮结果；而回答过快（<2s）时发送后才采样
        基线会把新回答本身当基线。基线必须在发送前采样），随后对最新回复做
        **文本稳定检测**（连续 9 秒无变化视为输出完成，v2.2.7）。
        不做回复块计数 / 生成标志 / 补发重试等复杂判断，避免在部分环境下
        卡死或误判；保留 300 秒总上限防止异常情况下无限等待。
        v2.2.13 起每次提问前默认开启「深度思考」（幂等，无需任何参数）。
        """
        # 发送前基线：上一轮助手回复块数量与文本（此时页面静止，无本轮内容）
        answer_locator = self.page.locator(
            "div.ds-markdown.ds-assistant-message-main-content"
        )
        baseline_count = 0
        baseline_text = ""
        try:
            baseline_count = await answer_locator.count()
            if baseline_count > 0:
                baseline_text = (await answer_locator.last.inner_text()).strip()
        except Exception:
            pass

        # 默认启动深度思考（已开启时幂等跳过）
        await self._ensure_deep_think_on()
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

        total_deadline = time.time() + 300  # 总上限，防止异常情况下无限等待

        # 阶段 1：等待本轮新回复开始渲染——回复块数量增加，或最后一块文本变化。
        # 只观察 assistant 专用选择器（不匹配用户消息，避免把用户消息当回复）。
        new_deadline = time.time() + 180
        new_ready = False
        while True:
            try:
                cnt = await answer_locator.count()
                if cnt > baseline_count:
                    new_ready = True
                    break  # 出现了新的回复块
                if cnt > 0:
                    cur = (await answer_locator.last.inner_text()).strip()
                    if cur != baseline_text:
                        new_ready = True
                        break  # 最后一块文本变化（同一块内更新）
            except Exception:
                pass
            if time.time() > new_deadline or time.time() > total_deadline:
                break
            await asyncio.sleep(1)
        if not new_ready:
            raise ConversationError("等待 AI 回答超时，请稍后重试。")

        # 阶段 2：文本稳定检测——连续 9 秒无变化视为输出完成
        previous_text = ""
        stable_count = 0
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
        """将本地图片上传到当前会话（Playwright set_input_files 注入）"""
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
