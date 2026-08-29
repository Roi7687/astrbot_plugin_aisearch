import asyncio
import inspect
import logging
import os
import re
import threading

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .core.command_parser import parse_ais_command
from .core.config import (
    QR_FILE,
    IDLE_TIMEOUT_SECONDS,
    MAX_IMAGE_BYTES,
    MODE_LABELS,
    MODE_NORMAL,
    MODE_VISION,
    AuthError,
    ConversationError,
)
from .core.login_core import CloakAuthenticator
from .core.session_core import DeepSeekSessionCore

logger = logging.getLogger("astrbot")


class LoginTaskState:
    """登录线程与 async 之间的信号桥"""

    def __init__(self):
        self.qr_path = None
        self.qr_ready = threading.Event()
        self.success = False
        self.done = threading.Event()
        self.error = None


class CloakSearchPlugin(Star):
    """AI搜索插件：统一 /ais 指令，持久化会话，支持识图模式。"""

    def __init__(self, context: Context):
        super().__init__(context)
        self.session = DeepSeekSessionCore()
        self._timer_tasks: dict[str, asyncio.Task] = {}   # mode -> 空闲计时任务
        self._notice_sources: dict[str, dict] = {}        # mode -> 主动通知来源

    # ═════════════════════ 统一指令：/ais ═════════════════════

    @filter.command("ais", alias={"搜索"})
    async def ai_search_command(self, event: AstrMessageEvent, query: GreedyStr):
        action, payload = parse_ais_command(query)

        if action == "usage":
            yield event.plain_result(self._usage_text())
            return
        if action == "help":
            yield event.plain_result(self._help_text())
            return
        if action == "session":
            info = self.session.conversation_summary(self.session.current_mode)
            yield event.plain_result(self._format_session_info(info))
            return
        if action == "list":
            yield event.plain_result(self._format_session_list())
            return
        if action == "new":
            mode = self.session.current_mode
            self._record_notice_source(mode, event)
            try:
                await self.session.reset_conversation(mode)
                self._arm_idle_timer(mode)
                yield event.plain_result(
                    f"🔄 已关闭旧会话并开启新的{MODE_LABELS.get(mode, mode)}。\n可以直接发送 /ais 开始提问。"
                )
            except AuthError:
                yield event.plain_result(self._auth_error_text())
            except Exception as e:
                yield event.plain_result(f"❌ 重置会话失败: {e}")
            return
        if action == "switch":
            mode_arg = payload.get("mode_arg")
            if not mode_arg:
                yield event.plain_result(self._format_switch_menu())
                return
            self._record_notice_source(mode_arg, event)
            try:
                await self.session.switch_conversation(mode_arg)
                self._arm_idle_timer(mode_arg)
                yield event.plain_result(
                    f"🔀 已切换到{MODE_LABELS.get(mode_arg, mode_arg)}。\n直接发送 /ais 即可在该会话中提问。"
                )
            except AuthError:
                yield event.plain_result(self._auth_error_text())
            except Exception as e:
                yield event.plain_result(f"❌ 切换会话失败: {e}")
            return

        # —— 提问（send）——
        mode = payload.get("mode") or self.session.current_mode
        text = payload["text"]
        thinking = payload["thinking"]
        self._record_notice_source(mode, event)
        try:
            conv_before = self.session.get_conversation(mode)
            created = not conv_before or conv_before.destroyed
            result = await self.session.send_message(mode, text, thinking)
            self._arm_idle_timer(mode)

            prefix = f"🟢 已开启新的{MODE_LABELS.get(mode, mode)}。\n\n" if created else ""
            result = prefix + result
            kb = self._build_keyboard(
                ("💬 继续对话", "/ais "),
                ("🧠 深度对话", "/ais -t "),
                ("🖼 识图对话", "/ais -v "),
                ("🔀 切换会话", "/ais switch"),
            )
            if not await self._try_send_with_keyboard(event, result, kb):
                yield event.plain_result(result)
        except AuthError:
            yield event.plain_result(self._auth_error_text())
        except ConversationError as e:
            yield event.plain_result(f"❌ 搜索失败: {e}")
        except Exception as e:
            yield event.plain_result(f"❌ 搜索失败: {e}")

    # ═════════════════════ 图片消息 → 识图会话 ═════════════════════

    @filter.event_message_type(EventMessageType.ALL)
    async def on_image_message(self, event: AstrMessageEvent):
        """@机器人 + 图片（或私聊图片）→ 自动进入识图会话"""
        if event.is_stopped():
            return

        images = await self._collect_images(event)
        if not images:
            return

        # 群聊需要 @ 机器人或唤醒词；私聊直接处理
        is_at = getattr(event, "is_at_or_wake_command", False) or event.is_private_chat()
        if not is_at:
            return

        text = self._clean_image_text(event.get_message_str() or "")
        event.stop_event()  # 阻止默认 LLM 重复响应
        self._record_notice_source(MODE_VISION, event)

        try:
            conv_before = self.session.get_conversation(MODE_VISION)
            created = not conv_before or conv_before.destroyed

            paths = await self._prepare_image_paths(images)
            if not paths:
                yield event.plain_result("⚠️ 图片下载/解析失败，无法识图。")
                return

            result = await self.session.send_image_message(MODE_VISION, text, paths)
            self._arm_idle_timer(MODE_VISION)

            prefix = "🟢 已开启新的识图对话。\n\n" if created else ""
            result = prefix + result
            kb = self._build_keyboard(
                ("💬 继续对话", "/ais "),
                ("🧠 深度对话", "/ais -t "),
                ("🖼 识图对话", "/ais -v "),
                ("🔀 切换会话", "/ais switch"),
            )
            if not await self._try_send_with_keyboard(event, result, kb):
                yield event.plain_result(result)
        except AuthError:
            yield event.plain_result(self._auth_error_text())
        except ConversationError as e:
            yield event.plain_result(f"❌ 识图失败: {e}")
        except Exception as e:
            yield event.plain_result(f"❌ 识图失败: {e}")

    # ═════════════════════ 空闲超时与主动通知 ═════════════════════

    def _arm_idle_timer(self, mode: str):
        """重置指定会话的空闲计时器"""
        task = self._timer_tasks.get(mode)
        if task and not task.done():
            task.cancel()
        self._timer_tasks[mode] = asyncio.ensure_future(self._idle_waiter(mode))

    async def _idle_waiter(self, mode: str):
        """空闲超时后自动销毁会话并主动通知用户"""
        try:
            await asyncio.sleep(IDLE_TIMEOUT_SECONDS)
            conv = self.session.get_conversation(mode)
            if conv and not conv.destroyed:
                logger.info(f"⏳ [AIS] {MODE_LABELS.get(mode, mode)}空闲超时，自动销毁...")
                destroyed = await self.session.destroy_conversation(mode)
                if destroyed:
                    await self._notify(
                        mode,
                        f"⏳ {MODE_LABELS.get(mode, mode)}已因 {IDLE_TIMEOUT_SECONDS} 秒无活动自动关闭。\n发送 /ais 即可开启新会话。",
                    )
        except asyncio.CancelledError:
            pass

    def _record_notice_source(self, mode: str, event: AstrMessageEvent):
        """记录会话最后使用的消息来源，用于状态变化的主动通知"""
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            self._notice_sources[mode] = {
                "umo": getattr(event, "unified_msg_origin", None),
                "bot": getattr(event, "bot", None),
                "raw": raw,
            }
        except Exception:
            pass

    async def _notify(self, mode: str, text: str):
        """主动推送消息（QQ 官方平台走 bot API，其余平台走 context.send_message）"""
        src = self._notice_sources.get(mode)
        if not src:
            return
        try:
            bot = src.get("bot")
            raw = src.get("raw")
            if bot is not None and raw is not None:
                type_name = type(raw).__name__
                if type_name == "GroupMessage":
                    await bot.api.post_group_message(
                        group_openid=raw.group_openid,
                        msg_type=2,
                        markdown={"content": text},
                        msg_id=None,
                        msg_seq=1,
                    )
                    return
                if type_name == "C2CMessage":
                    await bot.api.post_c2c_message(
                        openid=raw.author.user_openid,
                        msg_type=2,
                        markdown={"content": text},
                        msg_id=None,
                        msg_seq=1,
                    )
                    return
            umo = src.get("umo")
            if umo:
                await self.context.send_message(umo, MessageChain().message(text))
        except Exception as e:
            logger.warning(f"⚠️ [AIS] 主动通知发送失败: {e}")

    # ═════════════════════ 图片工具 ═════════════════════

    async def _collect_images(self, event: AstrMessageEvent) -> list:
        """兼容新旧 AstrBot API 获取消息中的图片组件"""
        images = []
        get_image = getattr(event, "get_image", None)
        if callable(get_image):
            try:
                res = get_image()
                if inspect.isawaitable(res):
                    res = await res
                if res:
                    images = list(res)
            except Exception:
                images = []
        if not images:
            images = [c for c in event.get_messages() if isinstance(c, Image)]
        return images

    async def _prepare_image_paths(self, images: list) -> list:
        """将图片组件转换为本地文件路径（自动下载、过大则压缩）"""
        paths = []
        for img in images:
            try:
                path = None
                convert = getattr(img, "convert_to_file_path", None)
                if callable(convert):
                    path = await convert()
                elif getattr(img, "url", None):
                    path = await self._download_image(img.url)
                if not path or not os.path.exists(path):
                    continue
                if os.path.getsize(path) > MAX_IMAGE_BYTES:
                    compressed = await self._compress_image(path)
                    if compressed:
                        path = compressed
                    else:
                        logger.warning(f"⚠️ [AIS] 图片过大且压缩失败，跳过: {path}")
                        continue
                paths.append(path)
            except Exception as e:
                logger.warning(f"⚠️ [AIS] 图片准备失败: {e}")
        return paths

    async def _download_image(self, url: str) -> str:
        """下载网络图片到临时文件"""
        import httpx

        from .core.config import PLUGIN_DIR

        tmp = os.path.join(PLUGIN_DIR, "ais_tmp")
        os.makedirs(tmp, exist_ok=True)
        dest = os.path.join(
            tmp,
            f"ais_img_{int(asyncio.get_event_loop().time() * 1000)}_{abs(hash(url)) % 10000}.img",
        )
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                f.write(resp.content)
        return dest

    async def _compress_image(self, path: str):
        """压缩图片到 1024px / JPEG q70（PIL 可选依赖）"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._compress_image_sync, path)

    @staticmethod
    def _compress_image_sync(path: str):
        try:
            from PIL import Image as PILImage, ImageOps

            im = PILImage.open(path)
            im = ImageOps.exif_transpose(im)
            im.thumbnail((1024, 1024), PILImage.LANCZOS)
            out = path + ".tmp.jpg"
            im.convert("RGB").save(out, "JPEG", quality=70)
            if os.path.getsize(out) < os.path.getsize(path):
                return out
            os.remove(out)
            return None
        except Exception:
            return None

    @staticmethod
    def _clean_image_text(s: str) -> str:
        """清理图片消息附带文本（去掉 @ 昵称与图片占位符）"""
        s = re.sub(r"@\S+\s*", "", s)
        s = re.sub(r"\[图片\]|\[表情\]|\[动画表情\]", "", s)
        return s.strip()

    # ═════════════════════ QQ 键盘按钮 ═════════════════════

    @staticmethod
    def _build_keyboard(*buttons: tuple) -> dict:
        """构建 QQ 键盘按钮数据，参数为 (label, data) 元组列表"""
        return {
            "content": {
                "rows": [
                    {
                        "buttons": [
                            {
                                "id": f"btn_{i}",
                                "render_data": {
                                    "label": label,
                                    "visited_label": label,
                                    "style": 1,
                                },
                                "action": {
                                    "type": 2,
                                    "permission": {"type": 2},
                                    "data": data,
                                    "at_bot_show_channel_list": False,
                                },
                            }
                            for i, (label, data) in enumerate(buttons)
                        ],
                    }
                ],
            }
        }

    async def _try_send_with_keyboard(
        self, event: AstrMessageEvent, text: str, keyboard: dict
    ) -> bool:
        """尝试在 QQ 官方平台发送带键盘按钮的消息，成功返回 True"""
        try:
            bot = getattr(event, "bot", None)
            raw = getattr(event.message_obj, "raw_message", None)
            if not bot or not raw:
                return False

            type_name = type(raw).__name__
            msg_id = event.message_obj.message_id

            if type_name == "GroupMessage":
                await bot.api.post_group_message(
                    group_openid=raw.group_openid,
                    msg_type=2,
                    markdown={"content": text},
                    keyboard=keyboard,
                    msg_id=msg_id,
                    msg_seq=1,
                )
                return True
            if type_name == "C2CMessage":
                await bot.api.post_c2c_message(
                    openid=raw.author.user_openid,
                    msg_type=2,
                    markdown={"content": text},
                    keyboard=keyboard,
                    msg_id=msg_id,
                    msg_seq=1,
                )
                return True
        except Exception as e:
            logger.warning(f"[Keyboard] 键盘消息发送失败，回退普通消息: {e}")
        return False

    # ═════════════════════ 登录（/cloak登录） ═════════════════════

    @filter.command("cloak登录")
    async def login_command(self, event: AstrMessageEvent):
        yield event.plain_result("🚀 正在生成二维码，请稍候...")

        task_state = LoginTaskState()
        authenticator = CloakAuthenticator()

        # 后台线程执行登录（内部有独立事件循环 + launch_async）
        threading.Thread(target=authenticator.execute_login_flow, args=(task_state,), daemon=True).start()

        try:
            # 阶段 1：等待二维码就绪
            while not task_state.qr_ready.is_set() and not task_state.done.is_set():
                await asyncio.sleep(1)

            if task_state.error or not task_state.qr_path:
                yield event.plain_result(f"❌ 获取二维码失败: {task_state.error}")
                return

            yield event.image_result(task_state.qr_path)
            yield event.plain_result("👆 请在 2 分钟内使用微信扫码完成身份验证。")

            # 阶段 2：等待扫码完成
            while not task_state.done.is_set():
                await asyncio.sleep(2)

            if task_state.success:
                # 凭证已刷新：重置浏览器内核（下次使用时加载新凭证）
                await self.session.close()
                for task in self._timer_tasks.values():
                    if task and not task.done():
                        task.cancel()
                self._timer_tasks.clear()
                yield event.plain_result(
                    "✅ 扫码成功！凭证已刷新。\n👉 现在您可以随时使用 /ais 功能了。"
                )
            else:
                yield event.plain_result("❌ 登录超时或失败，请重新执行指令。")

        except Exception as e:
            yield event.plain_result(f"❌ 插件调度异常: {e}")
        finally:
            if os.path.exists(QR_FILE):
                os.remove(QR_FILE)

    # ═════════════════════ 展示文案 ═════════════════════

    @staticmethod
    def _usage_text() -> str:
        return (
            "⚠️ 用法：\n"
            "• /ais <问题> — 在当前会话提问\n"
            "• /ais -t <问题> — 开启深度思考\n"
            "• /ais -v <问题> — 切到识图会话提问\n\n"
            "📌 管理：/ais new（重置） ｜ /ais session（状态） ｜ "
            "/ais list（列表） ｜ /ais switch（切换） ｜ /ais help（帮助）"
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "🔍 AI搜索插件 帮助\n\n"
            "📋 统一指令 /ais（别名：搜索）：\n"
            "• /ais <问题> — 在当前会话联网提问（无会话自动创建）\n"
            "• /ais -t <问题> — 开启深度思考模式\n"
            "• /ais -v <问题> — 切到「识图模式」会话提问（无则自动创建）\n"
            "• /ais new（或 reset/重置）— 关闭当前会话并开启新会话\n"
            "• /ais session（或 状态）— 查看当前会话信息\n"
            "• /ais list（或 列表）— 查看全部会话\n"
            "• /ais switch [识图/普通]（或 切换）— 切换当前会话\n"
            "• /ais help（或 帮助）— 显示本帮助\n\n"
            "🖼 识图模式：在群聊中 @机器人 并发送图片（可附带文字），"
            "或在私聊中直接发送图片，即可自动进入识图会话。\n\n"
            "⏳ 会话空闲超时（默认 300 秒）后自动关闭，并主动通知您。\n\n"
            "📌 其他：\n"
            "• /cloak登录 — 微信扫码登录 DeepSeek\n"
            "• 示例：/ais 今天有什么大新闻\n"
            "  /ais -t 解释量子纠缠的原理\n"
            "  /ais -v 帮我看看这张图的配色\n"
            "  /ais switch 识图"
        )

    @staticmethod
    def _auth_error_text() -> str:
        return "⚠️ 未检测到登录凭证，或凭证已失效。\n👉 请发送指令 /cloak登录 重新获取二维码验证。"
