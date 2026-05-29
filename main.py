import os
import asyncio
import threading
import logging
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

# 功能模块
from .core.config import QR_FILE, AuthError
from .core.search_core import DeepSeekSearchCore
from .core.multi_search_core import MultiSearchCore
from .core.login_core import CloakAuthenticator

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
    MULTI_SEARCH_TIMEOUT = 300  # 多轮对话空闲超时（秒），测试用

    def __init__(self, context: Context):
        super().__init__(context)
        self.search = DeepSeekSearchCore()
        self.multi_search = MultiSearchCore()
        self._session_expiry_task = None
        self.want_session = False
        self._closing = False              # 防止超时关闭与新命令竞态
        self._session_expired_notify = False  # 超时后下次命令时通知用户

    # ── 参数解析 ──

    @staticmethod
    def _parse_thinking_flag(query: str) -> tuple:
        """解析 -t 参数，返回 (thinking: bool, clean_query: str)"""
        q = query.strip()
        if q.startswith("-t "):
            return True, q[3:].strip()
        if q == "-t":
            return True, ""
        return False, q

    # ── 单轮搜索（/ais）──

    @filter.command("ais", alias={'搜索'})
    async def ai_search_command(self, event: AstrMessageEvent, query: GreedyStr):
        thinking, clean_query = self._parse_thinking_flag(query)
        if not clean_query:
            yield event.plain_result("⚠️ 缺少搜索词。用法示例：/ais 今天有什么大新闻\n💡 添加 -t 参数启用深度思考：/ais -t 今天有什么大新闻")
            return

        try:
            result = await self.search.execute_search(clean_query, thinking=thinking)
            kb = self._build_keyboard(
                ("🔍 继续搜索", "/ais "),
                ("🧠 深度搜索", "/ais -t "),
            )
            if not await self._try_send_with_keyboard(event, result, kb):
                yield event.plain_result(result)
        except AuthError:
            yield event.plain_result("⚠️ 未检测到登录凭证，或凭证已失效。\n👉 请发送指令 `/cloak登录` 重新获取二维码验证。")
        except Exception as e:
            yield event.plain_result(f"❌ 检索失败: {e}")

    # ── 多轮对话（/mais）──

    @filter.command("mais", alias={'多轮搜索'})
    async def ai_multi_search_command(self, event: AstrMessageEvent, query: GreedyStr):
        thinking, clean_query = self._parse_thinking_flag(query)
        if not clean_query:
            yield event.plain_result("⚠️ 缺少搜索词。用法示例：/mais 今天有什么大新闻\n💡 添加 -t 参数启用深度思考：/mais -t 今天有什么大新闻")
            return

        # 超时通知：上次会话因超时结束时，告知用户
        if self._session_expired_notify:
            self._session_expired_notify = False
            yield event.plain_result("⏳ 上一轮多轮对话已因长时间无活动自动结束，已为您开启新会话。")

        try:
            self.want_session = True
            self._closing = False  # 重置关闭标志（新命令开始）

            # 首次调用时启动持久会话
            if not self.multi_search.is_active:
                await self.multi_search.start_session()

            result = await self.multi_search.send_message(clean_query, thinking=thinking)
            self._reset_session_expiry()
            kb = self._build_keyboard(
                ("💬 继续对话", "/mais "),
                ("🧠 深度对话", "/mais -t "),
            )
            if not await self._try_send_with_keyboard(event, result, kb):
                yield event.plain_result(result)
        except AuthError:
            await self._close_multi_session()
            yield event.plain_result(
                "⚠️ 未检测到登录凭证，或凭证已失效。\n👉 请发送指令 `/cloak登录` 重新获取二维码验证。"
            )
        except RuntimeError as e:
            if "会话正在关闭" in str(e):
                return  # 超时关闭中断，静默退出
            yield event.plain_result(f"❌ 多轮对话检索失败: {e}")
        except Exception as e:
            yield event.plain_result(f"❌ 多轮对话检索失败: {e}")

    # ── 多轮对话会话管理 ──

    def _reset_session_expiry(self):
        """重置空闲超时计时器"""
        if self._session_expiry_task:
            self._session_expiry_task.cancel()
        self._session_expiry_task = asyncio.ensure_future(self._session_expiry_waiter())

    async def _session_expiry_waiter(self):
        """等待超时后自动关闭会话"""
        try:
            await asyncio.sleep(self.MULTI_SEARCH_TIMEOUT)
            if self.want_session:
                logger.info("⏳ [MultiSearch] 多轮对话会话空闲超时，正在关闭...")
                self._closing = True
                self.multi_search.closing = True
                self._session_expired_notify = True  # 下次命令时通知用户
                await self._close_multi_session()
        except asyncio.CancelledError:
            pass  # 计时器被正常重置，忽略

    async def _close_multi_session(self):
        """关闭多轮对话会话"""
        self.want_session = False
        if self._session_expiry_task:
            self._session_expiry_task.cancel()
            self._session_expiry_task = None
        await self.multi_search.close()

    # ── QQ 键盘按钮 ──

    @staticmethod
    def _build_keyboard(*buttons: tuple) -> dict:
        """构建 QQ 键盘按钮数据，参数为 (label, data) 元组列表"""
        return {
            "content": {
                "rows": [{
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
                }],
            },
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
            elif type_name == "C2CMessage":
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

    # ── 登录（/cloak登录）──
    # 登录需等待用户扫码（2+ 分钟），使用后台线程避免阻塞事件循环

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
                yield event.plain_result("✅ 扫码成功！凭证已刷新。\n👉 搜索内核已重置，现在您可以随时使用 `/ais` 功能了。")
            else:
                yield event.plain_result("❌ 登录超时或失败，请重新执行指令。")

        except Exception as e:
            yield event.plain_result(f"❌ 插件调度异常: {e}")
        finally:
            if os.path.exists(QR_FILE):
                os.remove(QR_FILE)

    # ── 帮助（/aishelp）──

    @filter.command("aishelp", alias={'ai帮助'})
    async def help_command(self, event: AstrMessageEvent):
        help_text = (
            "🔍 AI搜索插件 帮助\n\n"
            "📋 命令列表：\n"
            "• /ais <搜索词> — 单轮 AI 联网搜索\n"
            "• /mais <搜索词> — 多轮对话搜索，支持上下文连续\n"
            "• /aishelp — 显示本帮助信息\n"
            "• /cloak登录 — 微信扫码登录 DeepSeek\n\n"
            "💡 参数说明：\n"
            "• -t — 启用深度思考模式，AI 会进行更深入的推理分析\n\n"
            "📌 使用示例：\n"
            "  /ais 今天有什么大新闻\n"
            "  /ais -t 解释量子纠缠的原理\n"
            "  /mais 帮我对比 Python 和 Go\n"
            "  /mais -t 详细分析这个算法的复杂度"
        )
        yield event.plain_result(help_text)