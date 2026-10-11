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
3. `clean_dump_text()` —— **QQ 消息文本转储清理统一入口**：
   - `clean_card_text()`（v2.3.2）：卡片消息（小程序 / 音乐 / 图文…）
   - `clean_forward_text()`（v2.3.3）：合并转发（聊天记录，多条消息）
4. `card_cover_refs()`（v2.3.3）—— 卡片封面图 URL，交给上传链路让模型看图；
5. `build_prompt()` —— 组装本轮真正的提问：
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
    # 纯资源字段 → 丢弃（来源图标等，对回答没有帮助）
    "source_logo": None,
    "sourcelogo": None,
    "icon": None,
    "logo": None,
}

CARD_COVER_LABELS = {
    # 封面图字段 → 取出 URL 交给上传链路（v2.3.3 起让模型直接看封面）
    "preview",
    "preview_image",
    "previewimage",
    "cover",
    "cover_url",
    "coverurl",
    "image",
    "img",
    "pic",
    "picture",
    "thumb",
    "thumbnail",
}

_URL_RE = re.compile(r"^(?:https?://|base64://|data:)", re.IGNORECASE)


def _looks_like_url(value: str) -> bool:
    return bool(_URL_RE.match((value or "").strip()))


def parse_card_dump(text: str) -> dict | None:
    """解析 QQ 卡片消息的文本转储。

    只保留**认识**的字段（标题 / 摘要 / 来源 / 链接），其余（appid、type、
    签名串等）与来源图标一并丢弃；封面图单独收集到 ``covers``（v2.3.3，
    供上传给模型看图）。不认识的内容宁可走「原文兜底」，也不塞进 prompt 当噪音。

    返回 ``{"kind", "subtype", "fields": [(标签, 值), ...], "covers": [url, ...]}``；
    不是卡片、且既无有效字段也无封面时返回 None（调用方保留原文）。
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
    covers: list[str] = []
    for line in lines[1:]:
        field = CARD_FIELD_RE.match(line)
        if not field:
            continue
        value = field.group("value").strip()
        if not value:
            continue
        key = field.group("label").strip().lower().replace(" ", "")
        if key in CARD_COVER_LABELS:
            if _looks_like_url(value):
                covers.append(value)
            continue
        name = CARD_LABEL_NAMES.get(key, "")
        if not name:
            continue  # 未知字段与资源字段（图标 / appid…）：丢弃
        fields.append((name, value))

    if not fields and not covers:
        return None
    return {
        "kind": header.group("kind").strip(),
        "subtype": header.group("subtype").strip(),
        "fields": fields,
        "covers": covers,
    }


def card_cover_refs(text: str) -> list[str]:
    """卡片封面图引用（``preview`` / ``cover`` …，v2.3.3）。

    这些 URL 不进入 prompt（会把模型带偏），而是由上传链路下载后作为图片提交，
    让模型直接看封面。不是卡片时返回空列表。
    """
    parsed = parse_card_dump(text)
    return list(parsed["covers"]) if parsed else []


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
    if not parsed or not parsed["fields"]:
        # 没有可读字段（例如卡片只有封面图）→ 原样返回，别把内容压成一句空壳
        # （封面仍可由 card_cover_refs() 单独取出上传）
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


# ═════════════════════ QQ 合并转发（聊天记录）文本转储清理（v2.3.3） ═════════════════════
# 实测原文（2026-10-11 12:10 线上日志，引用一条合并转发消息）：
#     === 消息 1 ===
#     [消息内容] [群聊的聊天记录]
#     [消息类型] 引用消息
#     [关联消息]
#     --- 第1条 ---
#         [消息内容] https://www.bilibili.com/video/BV1cXae6mEny/
#         [发送者] 刺鸟/ty无限
#     --- 第2条 ---
#         [消息内容] 实战经验解析韭留美
#         [发送者] 刺鸟/ty无限
# 直接发（未引用）时的形态省去外层包装：
#     [群聊的聊天记录]
#     === 消息 1 ===
#     [消息内容] xxx
#     [发送者] yyy
# 两种形态都按「[消息内容] + [发送者]」成对提取，转成
#     QQ 合并转发消息（聊天记录，共 3 条）
#     - 刺鸟/ty无限：https://www.bilibili.com/video/BV1cXae6mEny/
#     - 刺鸟/ty无限：实战经验解析韭留美
#     - 刺鸟/ty无限：win11不能

FORWARD_HEADER_RE = re.compile(r"^(?:=+\s*消息\s*\d+\s*=+|\[群聊的聊天记录\]|\[聊天记录\])$")
FORWARD_ITEM_RE = re.compile(r"^-+\s*第\s*\d+\s*条\s*-+$")
FORWARD_CONTENT_RE = re.compile(r"^\[消息内容\]\s*(?P<value>.*)$")
FORWARD_SENDER_RE = re.compile(r"^\[发送者\]\s*(?P<value>.*)$")
FORWARD_META_RE = re.compile(r"^\[(?:消息类型|关联消息|消息时间|消息ID|消息id)\]\s*.*$")
FORWARD_CONTAINER_TITLES = {"[群聊的聊天记录]", "[聊天记录]", "[合并转发]", "[转发消息]"}
FORWARD_MAX_ITEMS = 30
"""最多列出多少条（超大转发不至于把 prompt 撑爆，超出部分标注条数）"""


def parse_forward_dump(text: str) -> dict | None:
    """解析 QQ 合并转发（聊天记录）的文本转储。

    返回 ``{"items": [{"sender", "content"}, ...], "total": int}``；
    不是合并转发、或没提取到任何一条消息时返回 None（调用方保留原文）。
    """
    if not text or not text.strip():
        return None
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    if not any(
        FORWARD_HEADER_RE.match(ln) or FORWARD_ITEM_RE.match(ln) for ln in lines
    ):
        return None

    items: list[dict[str, str]] = []
    content: str | None = None
    sender = ""

    def flush():
        nonlocal content, sender
        if content is not None:
            text_value = content.strip()
            if text_value and text_value not in FORWARD_CONTAINER_TITLES:
                items.append({"content": text_value, "sender": sender.strip()})
        content, sender = None, ""

    for line in lines:
        matched = FORWARD_CONTENT_RE.match(line)
        if matched:
            flush()
            content = matched.group("value")
            continue
        matched = FORWARD_SENDER_RE.match(line)
        if matched:
            if content is not None:
                sender = matched.group("value")
            continue
        if (
            FORWARD_HEADER_RE.match(line)
            or FORWARD_ITEM_RE.match(line)
            or FORWARD_META_RE.match(line)
        ):
            continue
        if content is not None:
            content = f"{content} {line}"  # 多行消息内容：续行合并
    flush()

    if not items:
        return None
    return {"items": items, "total": len(items)}


def clean_forward_text(text: str) -> str:
    """把合并转发转储压成「谁说了什么」的清单；识别失败时**原样返回**。"""
    parsed = parse_forward_dump(text)
    if not parsed:
        return text

    items = parsed["items"]
    total = parsed["total"]
    shown = items[:FORWARD_MAX_ITEMS]
    head = f"QQ 合并转发消息（聊天记录，共 {total} 条）"
    if total > len(shown):
        head += f"，以下为前 {len(shown)} 条"
    lines = [head]
    for item in shown:
        sender = item["sender"]
        lines.append(f"- {sender}：{item['content']}" if sender else f"- {item['content']}")
    if total > len(shown):
        lines.append(f"…（另有 {total - len(shown)} 条未列出）")
    return "\n".join(lines)


def clean_dump_text(text: str) -> str:
    """QQ 消息文本转储清理统一入口：合并转发（v2.3.3）→ 卡片消息（v2.3.2）。

    两种都不是时原样返回；两个清理函数各自保证「识别失败不改写」。
    """
    cleaned = clean_forward_text(text)
    if cleaned != text:
        return cleaned
    return clean_card_text(text)


def build_prompt(
    own_text: str,
    quote_text: str = "",
    quote_sender_name: str = "",
    has_images: bool = False,
    default_image_prompt: str = "",
    default_quote_prompt: str = "",
    quote_hint: str = "",
) -> str:
    """组装本轮提问文本。

    结构：``[引用消息（昵称）] 被引用的内容`` +（可选提示行）+ 空行 +
    ``用户自己的话``；用户自己没说话时，按「有图 → default_image_prompt」
    「只有引用 → default_quote_prompt」补默认提示词，保证返回非空字符串。
    ``quote_hint`` 用于在引用内容后补一行说明（如「随附卡片封面图」）。
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
    if quote_hint:
        head = f"{head}\n{quote_hint}"
    return f"{head}\n\n{body}" if body else head
