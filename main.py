import asyncio
import inspect
import logging
import os
import re
import threading
import time

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
    CARD_COVER_HINT,
    DEFAULT_IMAGE_PROMPT,
    DEFAULT_QUOTE_PROMPT,
    AuthError,
    ImagePrepareError,
    ConversationError,
)
from .core.login_core import CloakAuthenticator
from .core.quote_parser import (
    build_prompt,
    card_cover_refs,
    clean_dump_text,
    extract_quote_images,
    extract_quote_text,
    find_reply,
    quote_sender,
)
from .core.session_core import DeepSeekSessionCore

try:  # AstrBot >= 4.4 官方引用消息解析工具（跨平台：内嵌 chain / 远程 get_msg / 合并转发）
    from astrbot.core.utils.quoted_message import (
        extract_quoted_message_images as _fw_quote_images,
        extract_quoted_message_text as _fw_quote_text,
    )
except Exception:  # pragma: no cover - 旧版 AstrBot 无该模块时降级为本地解析
    try:
        from astrbot.core.utils.quoted_message_parser import (
            extract_quoted_message_images as _fw_quote_images,
            extract_quoted_message_text as _fw_quote_text,
        )
    except Exception:
        _fw_quote_text = None
        _fw_quote_images = None

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
    """AI搜索插件：统一 /ais 指令，持久化会话；文本与图片在同一会话中处理。"""

    def __init__(self, context: Context):
        super().__init__(context)
        self.session = DeepSeekSessionCore()
        self._timer_tasks: dict[int, asyncio.Task] = {}   # local_id -> 空闲计时任务

    async def terminate(self):
        """插件禁用/重载时清理：取消空闲计时器 + 关闭浏览器内核。

        必须显式清理（v2.3.1）：Star 基类的 terminate 是空实现，不清理会导致
        重载后**旧实例的计时器仍在运行**——其到点后销毁的会话可能正是新实例
        正在对话的那个（2026-10-11 日志实测：旧实例 01:16:13 销毁的会话，
        新实例 01:17 仍在其中提问），且旧浏览器进程永不退出。
        """
        for task in self._timer_tasks.values():
            if task and not task.done():
                task.cancel()
        self._timer_tasks.clear()
        try:
            await self.session.close()
        except Exception as e:
            logger.warning(f"⚠️ [AIS] 插件卸载时关闭浏览器失败: {e}")

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
            # /ais list — 查看会话列表（带本地 id）
            yield event.plain_result(self._format_session_list())
            return
        if action == "switch":
            # /ais switch <id>（按本地 id 切换）；无参或参数无效时显示列表
            local_id = payload.get("local_id")
            if local_id is None:
                yield event.plain_result(self._format_session_list())
                return
            try:
                conv, rebuilt = await self.session.switch_conversation(local_id)
                self._arm_idle_timer(conv.local_id)
                extra = (
                    "（原会话已关闭，已自动重建）"
                    if rebuilt
                    else f"（已发 {conv.message_count} 条）"
                )
                yield event.plain_result(
                    f"🔀 已切换到会话 #{conv.local_id}{extra}。\n直接发送消息即可在该会话中提问。"
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
                    f"🔄 已开启新会话（会话 #{conv.local_id}）。\n"
                    f"旧会话保留在列表中：/ais list 可查看，/ais switch <id> 可切回。"
                )
            except AuthError:
                yield event.plain_result(self._auth_error_text())
            except Exception as e:
                yield event.plain_result(f"❌ 新建会话失败: {e}")
            return

        # —— 提问（send）——
        text = payload["text"]
        try:
            created, result = await self._ask(event, text)
            result = await self._respond(event, created, result)
            if result:
                yield event.plain_result(result)
        except AuthError:
            yield event.plain_result(self._auth_error_text())
        except ImagePrepareError as e:
            yield event.plain_result(f"⚠️ {e}")
        except ConversationError as e:
            yield event.plain_result(f"❌ 搜索失败: {e}")
        except Exception as e:
            yield event.plain_result(f"❌ 搜索失败: {e}")

    # ═════════════════════ 消息处理：文本 / 图片 → 当前会话 ═════════════════════

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """@机器人（或私聊）发送文本、图片或**引用消息** → 自动进入 AI 搜索对话。

        v2.3.0：DeepSeek 官方已把「快速 / 专家 / 识图模式」合并为统一对话
        （图片理解内置于同一会话），因此文本与图片走同一条处理路径——
        图片在当前会话中上传提问，保持上下文连续。
        v2.3.1：引用（回复）消息中的**文本与图片**一并提取，
        `event.get_message_str()` 拿不到被引用的内容（它只在 Reply 组件里）。
        仅在需要切换/新建会话时才使用 /ais switch、/ais new 等指令。
        跳过：指令消息、机器人自己发出的消息（部分平台会回传，防止自我回复循环）。
        """
        if event.is_stopped() or self._is_self_message(event):
            return
        if self._is_command_message(event):
            return  # 指令消息（/ais、/cloak登录 等）由对应指令处理器处理

        own_raw = self._clean_image_text(event.get_message_str() or "")
        own_covers = card_cover_refs(own_raw)  # 直接发送的卡片：封面图随提问上传
        own_text = self._shrink_dump_text(own_raw, "当前")
        images = list(await self._collect_images(event)) + own_covers
        has_quote = find_reply(event.get_messages()) is not None

        if not own_text and not images and not has_quote:
            return  # 空消息（无文字、无图片、无引用）→ 放行给默认 LLM

        # 群聊需要 @ 机器人或唤醒词；私聊直接触发。
        # 放在解析引用之前：群里的无关引用消息不会触发远程拉取。
        is_at = getattr(event, "is_at_or_wake_command", False) or event.is_private_chat()
        if not is_at:
            return

        # 解析引用消息（文本 + 图片 + 卡片封面）。文本引用受 AUTO_TRIGGER 约束，
        # 引用中的图片与直接发的图片一样始终处理。
        quote_ctx = await self._collect_quote(event)
        images.extend(quote_ctx[1])
        images.extend(quote_ctx[3])
        if not images and not AUTO_TRIGGER:
            return

        prompt = self._build_query_prompt(
            own_text, quote_ctx, bool(images), bool(own_covers)
        )
        if not prompt:
            return  # 引用内容为空且自己没有文字 → 放行给默认 LLM
        logger.debug(
            f"📝 [AIS] 本轮提问（{len(prompt)} 字"
            f"{f'，图片 {len(images)} 张' if images else ''}）: {prompt[:200]}"
        )

        event.stop_event()  # 阻止默认 LLM 响应

        try:
            created, result = await self._send_query(prompt, images)
            result = await self._respond(event, created, result)
            if result:
                yield event.plain_result(result)
        except AuthError:
            yield event.plain_result(self._auth_error_text())
        except ImagePrepareError as e:
            yield event.plain_result(f"⚠️ {e}")
        except ConversationError as e:
            yield event.plain_result(f"❌ 搜索失败: {e}")
        except Exception as e:
            yield event.plain_result(f"❌ 搜索失败: {e}")

    # ═════════════════════ 提问组装（文本 + 引用消息 + 图片） ═════════════════════

    @staticmethod
    def _build_query_prompt(
        own_text: str, quote_ctx: tuple, has_images: bool, own_card_cover: bool = False
    ) -> str:
        """把「用户自己的话 + 被引用内容」组装成本轮提问（引用在前、自己的话在后）。

        卡片带封面时补一行提示（`CARD_COVER_HINT`），让模型侧重描述封面画面并
        结合卡片摘要作答（v2.3.3）。
        """
        quote_text, _quote_images, sender, quote_covers = quote_ctx
        own = own_text
        if own_card_cover:
            own = f"{own}\n{CARD_COVER_HINT}" if own else CARD_COVER_HINT
        return build_prompt(
            own,
            quote_text,
            sender,
            has_images=has_images,
            default_image_prompt=DEFAULT_IMAGE_PROMPT,
            default_quote_prompt=DEFAULT_QUOTE_PROMPT,
            quote_hint=CARD_COVER_HINT if quote_covers else "",
        )

    async def _ask(self, event: AstrMessageEvent, own_text: str) -> tuple[bool, str]:
        """/ais 指令入口：组装提问（含引用消息文本/图片）并提交给当前会话。"""
        own_raw = self._clean_image_text(own_text or "")
        own_covers = card_cover_refs(own_raw)
        own_text = self._shrink_dump_text(own_raw, "指令")
        quote_ctx = await self._collect_quote(event)
        images = (
            list(await self._collect_images(event))
            + own_covers
            + list(quote_ctx[1])
            + list(quote_ctx[3])
        )
        prompt = self._build_query_prompt(
            own_text, quote_ctx, bool(images), bool(own_covers)
        )
        logger.debug(
            f"📝 [AIS] 指令提问（{len(prompt)} 字"
            f"{f'，图片 {len(images)} 张' if images else ''}）: {prompt[:200]}"
        )
        return await self._send_query(prompt, images)

    async def _send_query(self, prompt: str, images: list) -> tuple[bool, str]:
        """按有无图片分发到会话内核；图片全部解析失败时抛 ImagePrepareError。"""
        if not images:
            return await self.session.send_message(prompt)
        paths = await self._prepare_image_paths(images)
        if not paths:
            raise ImagePrepareError("图片下载/解析失败，无法提交。")
        return await self.session.send_image_message(prompt, paths)

    async def _collect_quote(self, event: AstrMessageEvent) -> tuple[str, list, str, list]:
        """提取引用消息（Reply）→ (被引用文本, 被引用图片, 发送者昵称, 卡片封面)。

        QQ 群里「引用一条消息 + @机器人 + 自己的话」时，被引用内容只在 Reply
        组件中（chain / message_str），`get_message_str()` 只返回自己的话。
        不做这一步，DeepSeek 只会收到「回答一下这个问题」这类指代不明的话。

        文本/图片优先用 AstrBot 官方解析工具（能处理远程 get_msg、合并转发等
        跨平台场景），失败或旧版本没有该工具时回退到本地 `quote_parser`。
        被引用内容是卡片消息时，额外返回封面图 URL（v2.3.3，交给上传链路）。
        """
        try:
            reply = find_reply(event.get_messages())
        except Exception:
            reply = None
        if reply is None:
            return "", [], "", ""

        sender = quote_sender(reply)

        text = ""
        if _fw_quote_text is not None:
            try:
                text = str(await _fw_quote_text(event, reply) or "").strip()
            except Exception as e:
                logger.debug(f"[AIS] 引用文本解析（AstrBot 工具）失败，改用本地解析: {e}")
        if not text:
            text = extract_quote_text(reply)

        # 内嵌 chain 里的图片（QQ 官方平台的引用图片在此）；没有才走远程兜底
        images = extract_quote_images(reply)
        if not images and _fw_quote_images is not None:
            try:
                images = list(await _fw_quote_images(event, reply) or [])
            except Exception as e:
                logger.debug(f"[AIS] 引用图片解析（AstrBot 工具）失败: {e}")

        # 封面图 URL 必须在清理前取出（清理后卡片转储已变成描述文本）
        covers = card_cover_refs(text)
        text = self._shrink_dump_text(text, "引用")

        if text or images or covers:
            logger.debug(
                f"🔗 [AIS] 解析到引用消息：文本={text[:80]!r}，图片 {len(images)} 张，"
                f"卡片封面 {len(covers)} 张，发送者={sender or '未知'}"
            )
        return text, images, sender, covers

    @staticmethod
    def _shrink_dump_text(text: str, origin: str) -> str:
        """QQ 消息文本转储 → 简洁描述（卡片消息 v2.3.2 / 合并转发 v2.3.3）。

        QQ 官方平台把卡片消息与合并转发都渲染成多行文本转储，里面混着图标、
        封面等超长签名 URL 与 `=== 消息 N ===` 这类结构性标记；这里解析成
        「卡片类型 + 来源 + 标题 + 摘要」或「谁说了什么」的清单。
        识别失败时原样返回，不会丢信息。
        """
        cleaned = clean_dump_text(text)
        if cleaned != text:
            logger.debug(
                f"🃏 [AIS] 解析{origin}消息转储：{len(text)} 字 → 清理后 "
                f"{len(cleaned)} 字: {cleaned[:160]}"
            )
        return cleaned

    # ═════════════════════ 统一回复 ═════════════════════

    async def _respond(self, event: AstrMessageEvent, created: bool, result: str) -> str:
        """统一回复 AI 搜索结果：刷新空闲计时，附「已开启新会话」前缀与键盘按钮。
        返回最终文本；已通过键盘消息发送时返回空串（调用方无需再 yield）。"""
        conv = self.session.current_conversation
        if conv:
            self._arm_idle_timer(conv.local_id)
        if created and conv:
            result = (
                f"🟢 已开启新会话（会话 #{conv.local_id}）。\n\n{result}"
            )
        kb = self._build_keyboard(
            ("💬 继续对话", "/ais "),
            ("🔀 切换会话", "/ais switch"),
        )
        if await self._try_send_with_keyboard(event, result, kb):
            return ""
        return result

    # ═════════════════════ 空闲超时（静默销毁） ═════════════════════

    def _arm_idle_timer(self, local_id: int):
        """重置指定会话的空闲计时器（每次提问/切换/新建后调用）"""
        task = self._timer_tasks.get(local_id)
        if task and not task.done():
            task.cancel()
        self._timer_tasks[local_id] = asyncio.ensure_future(self._idle_waiter(local_id))

    async def _idle_waiter(self, local_id: int):
        """空闲超时后静默销毁会话（不再主动推送通知，避免刷屏）。

        v2.2.12 起计时以**真实空闲时间**（last_active）为准：
        计时器到点后二次校验，若 sleep 期间会话仍有活动（例如一次提问耗时
        跨越了计时终点、提问刚结束），则按最后活跃时间重新计时而非销毁；
        destroy_conversation 内部还有锁内权威校验兜底，杜绝「对话中被销毁」。
        """
        try:
            await asyncio.sleep(IDLE_TIMEOUT_SECONDS)
            conv = self.session.get_conversation(local_id)
            if not conv or conv.destroyed:
                return  # 会话已不存在（被销毁或记录被移除）
            if time.time() - conv.last_active < IDLE_TIMEOUT_SECONDS - 1:
                # sleep 期间有新活动：重新计时，不销毁
                self._arm_idle_timer(local_id)
                return
            logger.info(f"⏳ [AIS] 会话 #{local_id} 空闲超时，静默销毁。")
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
        """获取**当前消息**里的图片组件（兼容新旧 AstrBot API）。

        引用消息里的图片不在这里处理，由 `_collect_quote()` 统一提取
        （v2.3.1 起合并到引用解析，避免同图被重复收集）。
        """
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
        """将待上传图片转换为本地文件路径（自动下载、过大则压缩）。

        元素可能是 Image 组件（当前消息图片 / 引用 chain 中的图片），
        也可能是字符串引用（AstrBot 官方解析工具返回的 URL / base64:// / 本地路径）。
        """
        paths = []
        for img in images:
            try:
                path = None
                if isinstance(img, str):
                    path = await self._resolve_image_ref(img)
                else:
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

    async def _resolve_image_ref(self, ref: str):
        """把字符串形式的图片引用落成本地文件路径。

        支持 AstrBot 引用解析工具返回的三种形态：
        `base64://<数据>`、`http(s)://...` 与本地绝对路径。
        """
        ref = (ref or "").strip()
        if not ref:
            return None
        if ref.startswith("base64://"):
            return await self._save_base64_image(ref[len("base64://") :])
        if ref.startswith(("http://", "https://")):
            return await self._download_image(ref)
        if os.path.exists(ref):
            return ref
        return None

    async def _save_base64_image(self, payload: str):
        """base64 图片数据 → 本地文件（按魔数补正确扩展名，便于识别 MIME）"""
        import base64

        from .core.config import PLUGIN_DIR

        try:
            data = base64.b64decode(payload, validate=False)
        except Exception as e:
            logger.warning(f"⚠️ [AIS] base64 图片解码失败: {e}")
            return None
        if not data:
            return None
        tmp = os.path.join(PLUGIN_DIR, "ais_tmp")
        os.makedirs(tmp, exist_ok=True)
        ext = self._sniff_image_ext(data)
        dest = os.path.join(tmp, f"ais_b64_{int(time.time() * 1000)}{ext}")
        with open(dest, "wb") as f:
            f.write(data)
        return dest

    @staticmethod
    def _sniff_image_ext(data: bytes) -> str:
        """按文件头判断图片格式（无法识别时用 .img，交给 DeepSeek 自行判断）"""
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return ".gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        if data.startswith(b"BM"):
            return ".bmp"
        return ".img"

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
        """格式化会话列表（紧凑 markdown 表格风格，供 /ais list / /ais switch 无参时使用）。

        v2.2.10 起：只显示未销毁（空闲超时已关闭）的会话——已销毁的会话
        用户不会关心，仍可通过 /ais switch <id> 切换时自动重建。
        """
        items = [
            it for it in self.session.list_summary()
            if it["exists"] and not it["destroyed"]
        ]
        if not items:
            return (
                "📭 还没有任何会话。\n"
                "直接发送消息（或 @机器人）即可开始提问！"
            )
        lines = [f"📋 会话列表（共 {len(items)} 个，👉 为当前）:", ""]
        for it in items:
            marker = "👉" if it["is_current"] else "  "
            lines.append(
                f"{marker} {it['local_id']} ｜ {it['message_count']} 条"
                f" ｜ 最后活跃 {it['last_active']}"
            )
        lines += [
            "",
            "💡 /ais switch <id> 切换会话 ｜ /ais new 新建",
        ]
        return "\n".join(lines)

    @staticmethod
    def _usage_text() -> str:
        return (
            "⚠️ 用法：\n"
            "• 直接发送消息（@机器人 或私聊）即可自动联网提问，无需指令\n"
            "• @机器人 + 图片（或私聊发图）→ 自动解析图片并回答\n"
            "• 引用一条消息 + @机器人 提问 → 被引用的文字/图片一并送给 AI\n"
            "• /ais <问题> — 在当前会话提问\n\n"
            "📌 管理：/ais list（列表） ｜ /ais switch <id>（切换） ｜ "
            "/ais new（新建） ｜ /ais help（帮助）"
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "🔍 AI搜索插件 帮助\n\n"
            "⚡ 自动触发：\n"
            "• 群聊中 @机器人 发送文字（或私聊直接发送）→ 自动联网提问，无需 /ais 指令\n"
            "• 群聊中 @机器人 发送图片（可附带文字）→ 自动理解图片并回答\n"
            "• 引用（回复）一条消息再 @机器人 提问 → 被引用消息的文本与图片一并送给 AI\n"
            "• 文字与图片在同一会话中处理，上下文连续\n"
            "• 深度思考默认自动开启；智能搜索跟随 DeepSeek 网页端设置\n"
            "• 仅切换/新建会话时才需要指令（/ais switch、/ais new）\n\n"
            "📋 统一指令 /ais（别名：搜索）：\n"
            "• /ais <问题> — 在当前会话联网提问（无会话自动创建）\n"
            "• /ais new（或 reset/重置）— 开启新会话，旧会话保留在列表中\n"
            "• /ais list（或 列表/状态/session）— 查看会话列表（👉 为当前，已关闭的不显示）\n"
            "• /ais switch <id>（或 切换）— 按本地 id 切换会话，如 /ais switch 2\n"
            "• /ais help（或 帮助）— 显示本帮助\n\n"
            "⏳ 会话空闲超时（默认 300 秒）后自动关闭（静默，不推送通知）；"
            "已关闭的会话按 id 切换时会自动重建。\n\n"
            "📌 其他：\n"
            "• /cloak登录 — 微信扫码登录 DeepSeek\n"
            "• 示例：@机器人 今天有什么大新闻\n"
            "  @机器人 + 图片 这张图里有什么\n"
            "  /ais list\n"
            "  /ais switch 2"
        )

    @staticmethod
    def _auth_error_text() -> str:
        return "⚠️ 未检测到登录凭证，或凭证已失效。\n👉 请发送指令 /cloak登录 重新获取二维码验证。"
