"""引用消息（Reply）解析与提问组装（纯函数，无第三方依赖，可直接单测）。

**为什么需要这个模块（v2.3.1）**

QQ 群里「引用一条消息 + @机器人 + 自己的一句话」时，AstrBot 的消息链形如：

    [Reply(被引用的内容), At(机器人), Plain(自己的话)]

`event.get_message_str()` **只有自己的话**，被引用的内容只存在于 `Reply`
组件里（`chain` 或 `message_str`）。插件 v2.3.0 之前只取 `get_message_str()`，
于是「回答一下这个问题」被原样发给 DeepSeek——模型看不到「这个问题」指什么，
只能回「请把你想让我回答的问题发出来」（实测日志 2026-10-11 01:17:41）。

本模块负责：
1. `find_reply()` —— 从消息链里找到第一个 Reply 组件；
2. `extract_quote_text()` / `extract_quote_images()` —— 取出被引用消息的
   文本与图片（`chain` 优先，`message_str` 兜底）；
3. `build_prompt()` —— 组装本轮真正的提问：
   **被引用内容在前（带 `[引用消息]` 标记），用户自己的话在后**，
   自己的话/图片缺失时用默认提示词补位，保证 prompt 永不为空。

组件识别全部按**类名**做鸭子类型判断（`type(comp).__name__`），因此本模块
不 import astrbot，测试可直接构造同名假组件。
"""

from __future__ import annotations

from typing import Any

REPLY_LABEL = "[引用消息]"
"""无发送者昵称时的引用标记（QQ 官方平台不提供 sender_nickname）"""

# 非文本组件 → 占位符（让模型知道「这里原本有个附件」）
_COMPONENT_PLACEHOLDERS = {
    "Image": "[图片]",
    "Face": "[表情]",
    "Record": "[语音]",
    "Video": "[视频]",
    "File": "[文件]",
    "Forward": "[转发消息]",
    "Node": "[转发消息]",
    "Nodes": "[转发消息]",
    "Json": "[卡片消息]",
    "Location": "[位置]",
    "Music": "[音乐]",
    "Poke": "[戳一戳]",
    "Dice": "[骰子]",
    "RPS": "[猜拳]",
}

_CHAIN_ATTRS = ("chain", "message")
"""被引用消息段列表可能挂在 chain（当前 AstrBot）或 message（历史字段）"""


def find_reply(components) -> Any | None:
    """返回消息链中第一个 Reply 组件；没有引用消息时返回 None。"""
    for comp in components or []:
        if type(comp).__name__ == "Reply":
            return comp
    return None


def quote_sender(reply) -> str:
    """被引用消息的发送者昵称（部分平台为空字符串）。"""
    return str(getattr(reply, "sender_nickname", "") or "").strip()


def _component_text(comp) -> str:
    """单个消息段 → 文本（非文本段转为占位符，未知段忽略）。"""
    name = type(comp).__name__
    if name == "Plain":
        return str(getattr(comp, "text", "") or "")
    if name == "At":
        nickname = str(getattr(comp, "name", "") or "").strip()
        qq = str(getattr(comp, "qq", "") or "").strip()
        return f"@{nickname or qq}" if (nickname or qq) else "@"
    if name == "AtAll":
        return "@全体成员"
    return _COMPONENT_PLACEHOLDERS.get(name, "")


def _iter_chain(reply):
    """依次尝试 chain / message 字段，返回第一个非空的被引用消息段列表。"""
    for attr in _CHAIN_ATTRS:
        chain = getattr(reply, attr, None)
        if isinstance(chain, (list, tuple)) and chain:
            return chain
    return []


def extract_quote_text(reply) -> str:
    """提取被引用消息的文本（chain 优先，`Reply.message_str` 兜底）。

    对应 QQ 官方平台：被引用文本同时写入 `chain=[Plain(...)]` 与
    `message_str`；部分平台/历史数据只有 `message_str`，故二者都要兼容。
    """
    if reply is None:
        return ""
    parts = [t for t in (_component_text(c) for c in _iter_chain(reply)) if t]
    text = " ".join(parts).strip()
    if not text:
        text = str(getattr(reply, "message_str", "") or "").strip()
    return text


def extract_quote_images(reply) -> list:
    """取出被引用消息 chain 中的 Image 组件（保持原始顺序，供上传使用）。"""
    if reply is None:
        return []
    return [c for c in _iter_chain(reply) if type(c).__name__ == "Image"]


def build_prompt(
    own_text: str,
    quote_text: str = "",
    quote_sender_name: str = "",
    has_images: bool = False,
    default_image_prompt: str = "",
    default_quote_prompt: str = "",
) -> str:
    """组装本轮提问文本。

    结构：``[引用消息（昵称）] 被引用的内容`` + 空行 + ``用户自己的话``；
    用户自己没说话时，按「有图 → default_image_prompt」「只有引用 →
    default_quote_prompt」补默认提示词，保证返回非空字符串。
    """
    own = (own_text or "").strip()
    quoted = (quote_text or "").strip()
    sender = (quote_sender_name or "").strip()

    body = own
    if not body and has_images:
        body = default_image_prompt
    if not body and quoted:
        body = default_quote_prompt

    if not quoted:
        return body

    label = f"[引用消息（{sender}）]" if sender else REPLY_LABEL
    head = f"{label} {quoted}"
    return f"{head}\n\n{body}" if body else head
