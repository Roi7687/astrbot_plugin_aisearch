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
3. `clean_card_text()` —— **卡片消息（小程序 / 音乐 / 图文…）文本转储清理**
   （v2.3.2，见下）；
4. `build_prompt()` —— 组装本轮真正的提问：
   **被引用内容在前（带 `[引用消息]` 标记），用户自己的话在后**，
   自己的话/图片缺失时用默认提示词补位，保证 prompt 永不为空。

**为什么要清理卡片消息（v2.3.2）**

QQ 官方平台对「卡片消息」（小程序 / 音乐 / 图文 / 大图…）的 `content` 字段是
腾讯渲染好的多行文本转储，引用它或直接发它时都会原样进入消息文本：

    [卡片消息] 小程序
    摘要: [QQ小程序]《42中队》官方预告片「宣誓」，游戏现已开放愿望单
    source: 哔哩哔哩
    source_logo: https://open.gtimg.cn/open/app_icon/...png?t=1791448425
    title: 《42中队》官方预告片「宣誓」，游戏现已开放愿望单
    preview: https://qq.ugcimg.cn/v1/<超长签名串>

整段送给模型会被资源 URL 带偏——实测（2026-10-11 11:49）模型整段回答都在
解释 `preview` 那条 CDN 链接，完全没回答「这张卡片是什么」。`clean_card_text()`
解析出**卡片类型 / 来源 / 标题 / 摘要**，丢弃图标、封面等资源 URL；
识别失败或清理后无有效信息时**原样返回**（绝不让信息变少）。

组件识别全部按**类名**做鸭子类型判断（`type(comp).__name__`），因此本模块
不 import astrbot，测试可直接构造同名假组件。
"""

from __future__ import annotations

import re
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


# ═════════════════════ QQ 卡片消息文本转储清理（v2.3.2） ═════════════════════

CARD_HEADER_RE = re.compile(r"^\[(?P<kind>[^\[\]]{1,16})\]\s*(?P<subtype>.*)$")
"""卡片首行：``[卡片消息] 小程序`` / ``[链接分享]`` / ``[音乐分享] 网易云音乐`` …"""

CARD_FIELD_RE = re.compile(
    r"^(?P<label>[A-Za-z][A-Za-z0-9_ ]{0,23}|摘要|标题|来源|描述|链接)\s*[:：]\s*(?P<value>.+)$"
)
"""字段行：``摘要: xxx`` / ``source_logo: https://…`` / ``标题：xxx``（中英文冒号都兼容）"""

CARD_LABEL_NAMES = {
    # 有信息量的字段 → 中文标签
    "title": "标题",
    "标题": "标题",
    "摘要": "摘要",
    "desc": "摘要",
    "description": "摘要",
    "summary": "摘要",
    "描述": "摘要",
    "source": "来源",
    "来源": "来源",
    "app": "来源",
    "appname": "来源",
    "from": "来源",
    "url": "链接",
    "link": "链接",
    "jumpurl": "链接",
    "跳转链接": "链接",
    "链接": "链接",
    # 纯资源字段 → 丢弃（图标 / 封面等 URL，正是把模型带偏的噪音）
    "preview": None,
    "source_logo": None,
    "sourcelogo": None,
    "icon": None,
    "logo": None,
    "cover": None,
    "image": None,
    "img": None,
    "thumb": None,
    "thumbnail": None,
}

def parse_card_dump(text: str) -> dict | None:
    """解析 QQ 卡片消息的文本转储。

    只保留**认识**的字段（标题 / 摘要 / 来源 / 链接），其余（appid、type、
    签名串等）与图标、封面一并丢弃——不认识的内容宁可走「原文兜底」，
    也不要塞进 prompt 当噪音。

    返回 ``{"kind", "subtype", "fields": [(标签, 值), ...]}``；
    不是卡片、或清理后没有任何有效字段时返回 None（调用方保留原文）。
    """
    if not text or not text.strip():
        return None
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return None

    header = CARD_HEADER_RE.match(lines[0])
    if not header:
        return None

    fields: list[tuple[str, str]] = []
    for line in lines[1:]:
        field = CARD_FIELD_RE.match(line)
        if not field:
            continue
        value = field.group("value").strip()
        if not value:
            continue
        key = field.group("label").strip().lower().replace(" ", "")
        name = CARD_LABEL_NAMES.get(key, "")
        if not name:
            continue  # 未知字段与资源字段（图标 / 封面 / appid…）：丢弃
        fields.append((name, value))

    if not fields:
        return None
    return {
        "kind": header.group("kind").strip(),
        "subtype": header.group("subtype").strip(),
        "fields": fields,
    }


def clean_card_text(text: str) -> str:
    """把卡片消息文本转储清理成简洁可读的描述；识别失败时**原样返回**。

    例（实测 2026-10-11 11:49 的引用卡片，415 字 → 90 字）::

        [卡片消息] 小程序                                       QQ 小程序卡片（来源：哔哩哔哩）
        摘要: [QQ小程序]《42中队》官方预告片「宣誓」…      →   摘要：[QQ小程序]《42中队》官方预告片「宣誓」…
        source: 哔哩哔哩
        source_logo: https://open.gtimg.cn/…（丢弃）
        title: 《42中队》官方预告片「宣誓」…（与摘要重复，丢弃）
        preview: https://qq.ugcimg.cn/v1/…（丢弃）
    """
    parsed = parse_card_dump(text)
    if not parsed:
        return text

    kind = parsed["kind"]
    subtype = parsed["subtype"]
    fields = list(parsed["fields"])

    # 首行：卡片类型 + 来源
    if "卡片" in kind:
        head = f"QQ {subtype}卡片" if subtype else "QQ 卡片消息"
    elif subtype:
        head = f"{kind}（{subtype}）"
    else:
        head = kind
    source = next((v for name, v in fields if name == "来源"), "")
    if source:
        head = f"{head}（来源：{source}）"
        fields = [(n, v) for n, v in fields if n != "来源"]

    # 标题与摘要互相包含时只保留信息量更大的那条
    title = next((v for name, v in fields if name == "标题"), "")
    summary = next((v for name, v in fields if name == "摘要"), "")
    dropped: set[str] = set()
    if title and summary and (title in summary or summary in title):
        dropped.add("标题" if len(title) <= len(summary) else "摘要")

    lines = [head]
    lines += [
        f"{name}：{value}" for name, value in fields if name not in dropped
    ]
    return "\n".join(lines)


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
