import asyncio
import inspect
import logging
import os
import re
import threading

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .core.command_parser import parse_ais_command
from .core.config import (
    AUTO_TRIGGER,
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
        self._timer_tasks: dict[int, asyncio.Task] = {}   # local_id -> 空闲计时任务

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
        if action == "list":
            # /ais list — 查看全部会话（带本地 id），列表页可 /ais list <id> 直接切换
            yield event.plain_result(self._format_session_list())
            return
        if action == "switch":
            # /ais switch <id>（按本地 id 切换）｜ /ais switch 识图/普通（按模式切换）
            local_id = payload.get("local_id")
            mode = payload.get("mode")
            if local_id is None and mode is None:
                yield event.plain_result(self._format_session_list())
                return
            try:
                if local_id is not None:
                    conv, rebuilt = await self.session.switch_conversation(local_id)
                    self._arm_idle_timer(conv.local_id)
                    label = MODE_LABELS.get(conv.mode, conv.mode)
                    extra = (
                        "（原会话已关闭，已自动重建）"
                        if rebuilt
                        else f"（已发 {conv.message_count} 条）"
                    )
                    yield event.plain_result(
                        f"🔀 已切换到会话 #{conv.local_id}（{label}{extra}）。\n直接发送 /ais 即可在该会话中提问。"
                    )
                else:
                    conv, created = await self.session.ensure_mode(mode)
                    self._arm_idle_timer(conv.local_id)
                    label = MODE_LABELS.get(conv.mode, conv.mode)
                    prefix = "🟢 已新建" if created else "🔀 已切换到"
                    yield event.plain_result(
                        f"{prefix}会话 #{conv.local_id}（{label}）。\n直接发送 /ais 即可在该会话中提问。"
                    )
            except AuthError:
                yield event.plain_result(self._auth_error_text())
            except ConversationError as e:
                yield event.plain_result(f"❌ 切换会话失败: {e}")
            except Exception as e:
                yield event.plain_result(f"❌ 切换会话失败: {e}")
            return
        if action == "new":
            # /ais new — 开启新会话（旧会话保留在列表中，可按 id 切回）
            try:
                conv = await self.session.new_conversation()
                self._arm_idle_timer(conv.local_id)
                yield event.plain_result(
                    f"🔄 已开启新的{MODE_LABELS.get(conv.mode, conv.mode)}（会话 #{conv.local_id}）。\n"
                    f"旧会话保留在列表中：/ais list 可查看，/ais switch <id> 可切回。"
                )
            except AuthError:
                yield event.plain_result(self._auth_error_text())
            except Exception as e:
                yield event.plain_result(f"❌ 新建会话失败: {e}")
            return

        # —— 提问（send）——
        mode = payload.get("mode") or None  # "vision" 或 None（保持当前会话）
        text = payload["text"]
        thinking = payload["thinking"]
        try:
            created, result = await self.session.send_message(text, thinking, mode)
            result = await self._respond(event, created, result)
            if result:
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
        if (
            event.is_stopped()
            or self._is_self_message(event)
            or self._is_command_message(event)
        ):
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

        try:
            paths = await self._prepare_image_paths(images)
            if not paths:
                yield event.plain_result("⚠️ 图片下载/解析失败，无法识图。")
                return

            created, result = await self.session.send_image_message(text, paths)
            result = await self._respond(event, created, result)
            if result:
                yield event.plain_result(result)
        except AuthError:
            yield event.plain_result(self._auth_error_text())
        except ConversationError as e:
            yield event.plain_result(f"❌ 识图失败: {e}")
        except Exception as e:
            yield event.plain_result(f"❌ 识图失败: {e}")

    # ═════════════════════ 自动触发：@机器人/私聊文本消息 ═════════════════════

    @filter.event_message_type(EventMessageType.ALL)
    async def on_auto_message(self, event: AstrMessageEvent):
        """@机器人（或私聊）发送文本 → 自动进入 AI 搜索对话，无需 /ais 指令。

        仅在需要切换/新建会话时才使用 /ais switch、/ais new 等指令。
        跳过：指令消息（AstrBot 指令 filter 已激活的消息）、图片消息（交给识图）、
        机器人自己发出的消息（部分平台会回传，防止自我回复循环）。
        """
        if not AUTO_TRIGGER or event.is_stopped() or self._is_self_message(event):
            return

        text = self._clean_image_text(event.get_message_str() or "")
        if not text:
            return
        if text.startswith("/") or self._is_command_message(event):
            return  # 指令消息（/ais、/cloak登录 等）由对应指令处理器处理
        if await self._collect_images(event):
            return  # 图片消息交给 on_image_message（识图模式）

        # 群聊需要 @ 机器人或唤醒词；私聊直接触发
        is_at = getattr(event, "is_at_or_wake_command", False) or event.is_private_chat()
        if not is_at:
            return

        event.stop_event()  # 阻止默认 LLM 响应

        try:
            created, result = await self.session.send_message(text)
            result = await self._respond(event, created, result)
            if result:
                yield event.plain_result(result)
        except AuthError:
            yield event.plain_result(self._auth_error_text())
        except ConversationError as e:
            yield event.plain_result(f"❌ 搜索失败: {e}")
        except Exception as e:
            yield event.plain_result(f"❌ 搜索失败: {e}")

    # ═════════════════════ 统一回复 ═════════════════════

    async def _respond(self, event: AstrMessageEvent, created: bool, result: str) -> str:
        """统一回复 AI 搜索结果：刷新空闲计时，附「已开启新会话」前缀与键盘按钮。
        返回最终文本；已通过键盘消息发送时返回空串（调用方无需再 yield）。"""
        conv = self.session.current_conversation
        if conv:
            self._arm_idle_timer(conv.local_id)
        if created and conv:
            result = (
                f"🟢 已开启新的{MODE_LABELS.get(conv.mode, conv.mode)}"
                f"（会话 #{conv.local_id}）。\n\n{result}"
            )
        kb = self._build_keyboard(
            ("💬 继续对话", "/ais "),
            ("🧠 深度对话", "/ais -t "),
            ("🖼 识图对话", "/ais -v "),
            ("🔀 切换会话", "/ais switch"),
        )
        if await self._try_send_with_keyboard(event, result, kb):
            return ""
        return result

    # ═════════════════════ 空闲超时（静默销毁） ═════════════════════

    def _arm_idle_timer(self, local_id: int):
        """重置指定会话的空闲计时器"""
        task = self._timer_tasks.get(local_id)
        if task and not task.done():
            task.cancel()
        self._timer_tasks[local_id] = asyncio.ensure_future(self._idle_waiter(local_id))

    async def _idle_waiter(self, local_id: int):
        """空闲超时后静默销毁会话（不再主动推送通知，避免刷屏）"""
        try:
            await asyncio.sleep(IDLE_TIMEOUT_SECONDS)
            conv = self.session.get_conversation(local_id)
            if conv and not conv.destroyed:
                label = MODE_LABELS.get(conv.mode, conv.mode)
                logger.info(f"⏳ [AIS] 会话 #{local_id}（{label}）空闲超时，静默销毁。")
                await self.session.destroy_conversation(local_id)
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _is_self_message(event: AstrMessageEvent) -> bool:
        """判断消息是否由机器人自己发出（部分平台会把机器人自己的消息回传，
        不过滤会导致自我回复循环刷屏）"""
        try:
            sender = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
            return bool(sender) and sender == self_id
        except Exception:
            return False

    @staticmethod
    def _is_command_message(event: AstrMessageEvent) -> bool:
        """判断消息是否被 AstrBot 识别为某插件的指令（含内置指令）。

        关键背景：waking_check 阶段已将 wake_prefix（默认 /）从
        message_str 剥离（"/cloak登录" 到达插件时已是 "cloak登录"），
        因此不能靠文本前缀判断指令。改用 activated_handlers——
        AstrBot 在唤醒检查时已把匹配的指令 handler 写入事件，
        其中带 Command 类 filter（CommandFilter / CommandGroupFilter）。

        必须让路的原因：自动触发若误拦截指令消息并 stop_event()，
        StarRequestSubStage 会因 is_stopped 跳过后续所有 handler，
        导致 /cloak登录、/ais 等指令彻底失效。
        """
        try:
            activated = event.get_extra("activated_handlers", []) or []
            for handler in activated:
                for f in getattr(handler, "event_filters", []) or []:
                    if "Command" in type(f).__name__:
                        return True
        except Exception:
            pass
        return False

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

    def _format_session_list(self) -> str:
        """格式化全部会话列表（带本地 id，供 /ais list / /ais switch 无参时使用）"""
        items = self.session.list_summary()
        if not items:
            return (
                "📭 还没有任何会话。\n"
                "直接发送 /ais 开始提问吧！"
            )
        lines = [f"📋 会话列表（共 {len(items)} 个，👉 为当前）:", ""]
        for it in items:
            marker = "👉" if it["is_current"] else "  "
            status = "⏸ 已关闭" if it["destroyed"] else f"{it['message_count']} 条"
            lines.append(
                f"{marker} #{it['local_id']}  {it['label']} ｜ {status} ｜ 最后活跃 {it['last_active']}"
            )
        lines += [
            "",
            "💡 切换：/ais switch <id>（如 /ais switch 2），或 /ais list <id>",
            "💡 新建：/ais new（旧会话保留，可随时切回）",
        ]
        return "\n".join(lines)

    @staticmethod
    def _usage_text() -> str:
        return (
            "⚠️ 用法：\n"
            "• 直接发送消息（@机器人 或私聊）即可自动联网提问，无需指令\n"
            "• /ais <问题> — 在当前会话提问\n"
            "• /ais -t <问题> — 强制开启深度思考\n"
            "• /ais -v <问题> — 切到识图会话提问\n\n"
            "📌 管理：/ais list（列表） ｜ /ais switch <id>（切换） ｜ "
            "/ais new（新建） ｜ /ais help（帮助）"
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "🔍 AI搜索插件 帮助\n\n"
            "⚡ 自动触发：\n"
            "• 群聊中 @机器人 发送文字（或私聊直接发送）→ 自动联网提问，无需 /ais 指令\n"
            "• 群聊中 @机器人 发送图片 → 自动进入识图模式\n"
            "• 深度思考与联网搜索默认固定开启\n"
            "• 仅切换/新建会话时才需要指令（/ais switch、/ais new）\n\n"
            "📋 统一指令 /ais（别名：搜索）：\n"
            "• /ais <问题> — 在当前会话联网提问（无会话自动创建）\n"
            "• /ais -t <问题> — 强制开启深度思考模式\n"
            "• /ais -v <问题> — 切到「识图模式」会话提问（无则自动创建）\n"
            "• /ais new（或 reset/重置）— 开启新会话，旧会话保留在列表中\n"
            "• /ais list（或 列表/状态/session）— 查看全部会话（带本地 id，👉 为当前）\n"
            "• /ais switch <id>（或 切换）— 按本地 id 切换会话，如 /ais switch 2\n"
            "• /ais switch 识图/普通 — 切换到该模式最近使用的会话（无则新建）\n"
            "• /ais list <id> — 列表便捷切换（等同于 /ais switch <id>）\n"
            "• /ais help（或 帮助）— 显示本帮助\n\n"
            "🖼 识图模式：在群聊中 @机器人 并发送图片（可附带文字），"
            "或在私聊中直接发送图片，即可自动进入识图会话。\n\n"
            "⏳ 会话空闲超时（默认 300 秒）后自动关闭，并主动通知您；"
            "已关闭的会话按 id 切换时会自动重建。\n\n"
            "📌 其他：\n"
            "• /cloak登录 — 微信扫码登录 DeepSeek\n"
            "• 示例：@机器人 今天有什么大新闻\n"
            "  /ais -t 解释量子纠缠的原理\n"
            "  /ais -v 帮我看看这张图的配色\n"
            "  /ais list\n"
            "  /ais switch 2"
        )

    @staticmethod
    def _auth_error_text() -> str:
        return "⚠️ 未检测到登录凭证，或凭证已失效。\n👉 请发送指令 /cloak登录 重新获取二维码验证。"
