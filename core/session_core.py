"""统一持久化会话核心。

设计：
- 常驻一个反检测浏览器（cloakbrowser），内部维护最多两个会话槽位：
    普通对话（normal）与识图对话（vision）。
- 每个槽位：惰性创建（首次使用时）、空闲超时自动销毁（服务器端删除会话）、
  可手动重置（关闭旧会话开启新会话）、可随时切换。
- 所有页面操作通过 asyncio.Lock 串行化，避免并发命令互相干扰。
- 会话元数据持久化到 conversations.json，插件重启后仍可切换回未销毁的会话。
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
    CONVERSATIONS_FILE,
    DEEPSEEK_BASE,
    DEFAULT_VISION_PROMPT,
    IDLE_TIMEOUT_SECONDS,
    MODE_LABELS,
    MODE_NORMAL,
    MODE_VISION,
    MAX_IMAGE_BYTES,
    STATE_FILE,
    VISION_WELCOME_TEXT,
    AuthError,
    ConversationError,
)

logger = logging.getLogger("astrbot")

# 答案文本稳定检测参数
STABLE_CHECKS = 18      # 连续 N 次文本无变化视为输出完成
STABLE_INTERVAL = 0.5   # 每次检测间隔（秒）


@dataclass
class Conversation:
    """单个 DeepSeek 会话槽位的元数据"""
    mode: str                       # normal | vision
    session_id: str = ""
    url: str = ""                   # 会话页面 URL（用于切换回来）
    message_count: int = 0          # 用户已发送消息数
    created_at: float = 0.0
    last_active: float = 0.0
    destroyed: bool = False         # True 表示已销毁（超时/重置），下次使用需重建

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Conversation":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in allowed})


class DeepSeekSessionCore:
    """统一持久化会话核心（全异步）"""

    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self.bearer_token = None
        self.lock = asyncio.Lock()      # 串行化所有页面操作
        self.conversations: dict[str, Conversation] = {}
        self.current_mode = MODE_NORMAL
        self._closing = False
        self._load_conversations()

    # ───────────────────────── 对外 API ─────────────────────────

    async def send_message(self, mode: str, text: str, thinking: bool = False) -> str:
        """在指定模式的会话中发送文本消息（无会话则自动创建）"""
        async with self.lock:
            self._check_closing()
            conv = await self.ensure_conversation_locked(mode)
            result = await self._send_query(text, thinking)
            self._mark_active(conv)
            return result

    async def send_image_message(
        self, mode: str, text: str, image_paths: list, thinking: bool = False
    ) -> str:
        """在识图会话中发送图片（可附带文字）"""
        async with self.lock:
            self._check_closing()
            conv = await self.ensure_conversation_locked(mode)
            await self._upload_images(image_paths)
            result = await self._send_query(text or DEFAULT_VISION_PROMPT, thinking)
            self._mark_active(conv)
            return result

    async def ensure_conversation(self, mode: str) -> Conversation:
        """确保指定模式的会话存在（创建或切换到它）"""
        async with self.lock:
            self._check_closing()
            return await self.ensure_conversation_locked(mode)

    async def switch_conversation(self, mode: str) -> Conversation:
        """切换到指定模式的会话（不存在则创建）"""
        async with self.lock:
            self._check_closing()
            conv = await self.ensure_conversation_locked(mode)
            self.current_mode = mode
            logger.info(f"🔀 [Session] 已切换到{MODE_LABELS.get(mode, mode)}。")
            return conv

    async def reset_conversation(self, mode: str) -> Conversation:
        """重置：关闭旧会话（服务器端删除）并开启同一模式的新会话"""
        async with self.lock:
            self._check_closing()
            old = self.conversations.get(mode)
            if old and not old.destroyed:
                await self._delete_session_best_effort(old)
                old.destroyed = True
                logger.info(f"🔄 [Session] 已关闭旧的{MODE_LABELS.get(mode, mode)}。")
            conv = await self.ensure_conversation_locked(mode)
            self.current_mode = mode
            return conv

    async def destroy_conversation(self, mode: str) -> bool:
        """销毁指定模式的会话（空闲超时调用）。返回是否真的有会话被销毁"""
        async with self.lock:
            conv = self.conversations.get(mode)
            if not conv or conv.destroyed:
                return False
            await self._delete_session_best_effort(conv)
            conv.destroyed = True
            self._save_conversations()
            logger.info(f"⏳ [Session] {MODE_LABELS.get(mode, mode)}已销毁。")
            return True

    def get_conversation(self, mode: str) -> Conversation | None:
        return self.conversations.get(mode)

    def conversation_summary(self, mode: str) -> dict:
        """供 /ais session 使用"""
        conv = self.conversations.get(mode)
        now = time.time()
        if not conv or conv.destroyed:
            return {"mode": mode, "label": MODE_LABELS.get(mode, mode), "exists": False}
        remain = max(0, int(IDLE_TIMEOUT_SECONDS - (now - conv.last_active)))
        return {
            "mode": mode,
            "label": MODE_LABELS.get(mode, mode),
            "exists": True,
            "session_id": conv.session_id or "未知",
            "message_count": conv.message_count,
            "created_at": time.strftime("%m-%d %H:%M", time.localtime(conv.created_at)),
            "last_active": time.strftime("%m-%d %H:%M", time.localtime(conv.last_active)),
            "idle_remain": remain,
            "is_current": (mode == self.current_mode),
        }

    def list_summary(self) -> list[dict]:
        return [self.conversation_summary(m) for m in (MODE_NORMAL, MODE_VISION)]

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

    def _load_conversations(self):
        try:
            if os.path.exists(CONVERSATIONS_FILE):
                with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for mode, d in data.items():
                    if mode in (MODE_NORMAL, MODE_VISION):
                        self.conversations[mode] = Conversation.from_dict(d)
                logger.info(f"📂 [Session] 已加载会话元数据: {len(self.conversations)} 个槽位")
        except Exception as e:
            logger.warning(f"⚠️ [Session] 加载会话元数据失败: {e}")

    def _save_conversations(self):
        try:
            with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
                json.dump({m: c.to_dict() for m, c in self.conversations.items()}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"⚠️ [Session] 保存会话元数据失败: {e}")

    def _mark_active(self, conv: Conversation):
        conv.message_count += 1
        conv.last_active = time.time()
        self.current_mode = conv.mode
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

    async def ensure_conversation_locked(self, mode: str) -> Conversation:
        """（持锁调用）确保会话存在并让页面处于该会话"""
        conv = self.conversations.get(mode)
        if not conv or conv.destroyed or not conv.url:
            conv = await self._create_conversation(mode)
            self.conversations[mode] = conv
            self._save_conversations()
            return conv
        # 会话已存在：导航回其页面；页面失效则自动重建
        try:
            await self._goto_session(conv)
        except ConversationError:
            logger.warning(f"⚠️ [Session] {MODE_LABELS.get(mode, mode)}页面已失效，自动重建...")
            conv.destroyed = True
            conv = await self._create_conversation(mode)
            self.conversations[mode] = conv
            self._save_conversations()
        self.current_mode = mode
        return conv

    async def _init_browser(self):
        """按需初始化浏览器"""
        if not os.path.exists(STATE_FILE):
            raise AuthError("本地未找到登录凭证。")

        logger.info("🚀 [Session] 正在启动持久化搜索内核...")
        self.browser = await launch_async(headless=True, humanize=True)
        self.context = await self.browser.new_context(storage_state=STATE_FILE)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(30000)

        def sniff_auth_token(request):
            auth_header = request.headers.get("authorization")
            if auth_header and auth_header.startswith("Bearer"):
                self.bearer_token = auth_header

        self.page.on("request", sniff_auth_token)

    async def _create_conversation(self, mode: str) -> Conversation:
        """开启新模式的新会话（识图模式需点击模式入口）"""
        if not self.browser:
            await self._init_browser()

        await self._open_new_chat()
        if mode == MODE_VISION:
            await self._switch_to_vision()

        conv = Conversation(mode=mode, created_at=time.time(), last_active=time.time())
        conv.url = self.page.url
        conv.session_id = self._extract_session_id(self.page.url)
        logger.info(f"🟢 [Session] 已创建{MODE_LABELS.get(mode, mode)}（url={conv.url}）。")
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
                        if ((x === '开启新对话' || x === '新建对话') && e.children.length === 0) {
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
        """点击「识图模式」入口并验证切换成功"""
        in_vision = await self.page.evaluate(
            """() => {
                const t = document.body.innerText || '';
                if (t.indexOf(%s) >= 0) return true;
                for (const e of document.querySelectorAll('div,span,button')) {
                    const x = (e.innerText || '').trim();
                    if (x === '识图模式') {
                        const c = '' + e.className;
                        if (c.indexOf('active') >= 0 || c.indexOf('selected') >= 0 ||
                            e.getAttribute('aria-selected') === 'true') return true;
                    }
                }
                return false;
            }""" % json.dumps(VISION_WELCOME_TEXT)
        )
        if in_vision:
            return

        for attempt in range(3):
            clicked = await self.page.evaluate(
                """() => {
                    const nodes = [...document.querySelectorAll('div,span,button')];
                    for (const e of nodes) {
                        const x = (e.innerText || '').trim();
                        if (x === '识图模式' && e.children.length === 0) {
                            e.click();
                            return 'clicked';
                        }
                    }
                    return 'not_found';
                }"""
            )
            if clicked == "not_found":
                raise ConversationError(
                    "未找到「识图模式」入口。可能当前账号未开放识图功能，或网页 UI 已更新。"
                )
            await asyncio.sleep(1)
            ok = await self.page.evaluate(
                "() => (document.body.innerText || '').indexOf(%s) >= 0" % json.dumps(VISION_WELCOME_TEXT)
            )
            if ok:
                return
        raise ConversationError("「识图模式」切换失败，请稍后重试。")

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

    async def _toggle_thinking(self, enable: bool):
        """尝试开启/关闭 DeepSeek 深度思考模式"""
        if not enable:
            return
        try:
            btn = self.page.locator(".ds-toggle-button").filter(has_text="深度思考")
            await btn.wait_for(state="visible", timeout=30000)
            await btn.click()
            await asyncio.sleep(1)
        except Exception:
            pass

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
                send_btn = self.page.locator(
                    "div[role=button].ds-button--primary"
                ).filter(has_text="发送").last
                if await send_btn.count() == 0:
                    send_btn = self.page.locator(
                        "div[role=button].ds-button--primary"
                    ).last
                if await send_btn.count() > 0:
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

    async def _send_query(self, text: str, thinking: bool) -> str:
        """在当前页面发送提问并等待回答（不含会话状态维护）"""
        await self._toggle_thinking(thinking)
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

        previous_text = ""
        stable_count = 0
        while True:
            current_text = await answer_locator.last.inner_text()
            if current_text == previous_text and len(current_text) > 0:
                stable_count += 1
            else:
                stable_count = 0
                previous_text = current_text

            if stable_count >= STABLE_CHECKS:
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

        # 等待 blob 缩略图出现（上传成功标志）
        for retry in range(3):
            ok = await self._wait_blob_thumbnails(len(existing), timeout=30)
            if ok:
                await asyncio.sleep(1)
                return
            logger.warning(f"⚠️ [Session] 上传缩略图未出现（第 {retry + 1} 次），重试注入...")
            try:
                await file_input.set_input_files(existing)
            except Exception:
                pass
        raise ConversationError("图片上传超时，请重试。")

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
