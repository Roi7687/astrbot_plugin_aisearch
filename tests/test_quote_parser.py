"""引用消息解析单元测试（纯函数，无第三方依赖）

覆盖两类线上场景：
1. v2.3.1：QQ 群「引用一条消息 + @机器人 + 自己的话」时，被引用的内容
   （Reply.chain / Reply.message_str）必须进入提问文本，否则 DeepSeek 只看到
   「回答一下这个问题」这类指代不明的话；
2. v2.3.2：被引用（或直接发送）的是**卡片消息**（小程序 / 音乐 / 图文）时，
   腾讯渲染的多行文本转储里混着图标、封面等超长签名 URL，必须解析出
   卡片类型 / 来源 / 标题 / 摘要并丢弃资源 URL（实测模型会整段回答 preview 链接）。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.quote_parser import (
    REPLY_LABEL,
    build_prompt,
    card_cover_refs,
    clean_card_text,
    clean_dump_text,
    clean_forward_text,
    extract_quote_images,
    extract_quote_text,
    find_reply,
    parse_card_dump,
    parse_forward_dump,
    quote_sender,
)

DEFAULT_IMAGE_PROMPT = "请详细描述这张图片的内容。"
DEFAULT_QUOTE_PROMPT = "请针对我引用的这条消息进行回答。"

# 实测原文（2026-10-11 11:49 线上日志：引用一条 B 站小程序卡片 +「这是什么」）
QQ_CARD_DUMP = """[卡片消息] 小程序
摘要: [QQ小程序]《42中队》官方预告片「宣誓」，游戏现已开放愿望单
source: 哔哩哔哩
source_logo: https://open.gtimg.cn/open/app_icon/00/95/17/76/100951776_100_m.png?t=1791448425
title: 《42中队》官方预告片「宣誓」，游戏现已开放愿望单
preview: https://qq.ugcimg.cn/v1/vd9mc16v4u4fdiu18nu79url5qnvdhq6h0qurfl2em9inbs1l72j5g0j39i6b38s8s20ob4ukjcuolmnmlfjulu9n752u026n7q8h357l59utm035s9v8q8lkuh0l3dlgl2c8ips491q7mcv2jk7hcokrg/e9vf2tsqr6j29hl2kodb3vahlc"""

CARD_COVER_URL = (
    "https://qq.ugcimg.cn/v1/vd9mc16v4u4fdiu18nu79url5qnvdhq6h0qurfl2em9inbs1l72j5g0j39i6"
    "b38s8s20ob4ukjcuolmnmlfjulu9n752u026n7q8h357l59utm035s9v8q8lkuh0l3dlgl2c8ips491q7mcv"
    "2jk7hcokrg/e9vf2tsqr6j29hl2kodb3vahlc"
)

# 实测原文（2026-10-11 12:10 线上日志：引用一条合并转发消息 +「分析一下这段聊天记录」）
QQ_FORWARD_DUMP_QUOTED = """=== 消息 1 ===
[消息内容] [群聊的聊天记录]
[消息类型] 引用消息
[关联消息]
--- 第1条 ---
    [消息内容] https://www.bilibili.com/video/BV1cXae6mEny/
    [发送者] 刺鸟/ty无限
--- 第2条 ---
    [消息内容] 实战经验解析韭留美
    [发送者] 刺鸟/ty无限
--- 第3条 ---
    [消息内容] win11不能
    [发送者] 刺鸟/ty无限"""

# 同一批记录的「直接发送」形态（外层包装更少）
QQ_FORWARD_DUMP_DIRECT = """[群聊的聊天记录]
=== 消息 1 ===
[消息内容] https://www.bilibili.com/video/BV1cXae6mEny/
[发送者] 刺鸟/ty无限
=== 消息 2 ===
[消息内容] 实战经验解析韭留美
[发送者] 刺鸟/ty无限
=== 消息 3 ===
[消息内容] win11不能
[发送者] 刺鸟/ty无限"""


# ── 与 AstrBot 组件同名的假组件（quote_parser 按类名做鸭子类型判断）──
class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, url="http://example.com/a.png"):
        self.url = url
        self.file = url


class At:
    def __init__(self, qq="", name=""):
        self.qq = qq
        self.name = name


class Reply:
    def __init__(self, chain=None, message_str="", sender_nickname=""):
        self.chain = chain
        self.message_str = message_str
        self.sender_nickname = sender_nickname


# QQ 官方平台实测消息链（日志 2026-10-11 01:17:41）：
#   [Reply(chain=[Plain("今天星期几")]), At(机器人), Plain("回答一下这个问题")]
# 其中 event.get_message_str() 只有「回答一下这个问题」。
QQ_REPLY = Reply(chain=[Plain("今天星期几")], message_str="今天星期几")
QQ_CHAIN = [QQ_REPLY, At(qq="F9F5BCD9", name=""), Plain("回答一下这个问题")]

CASES_TEXT = [
    # (说明, reply, 期望文本)
    ("chain 中的 Plain", QQ_REPLY, "今天星期几"),
    ("chain 为空 → message_str 兜底", Reply(chain=[], message_str="昨天天气如何"), "昨天天气如何"),
    ("chain=None → message_str 兜底", Reply(chain=None, message_str="明天下雨吗"), "明天下雨吗"),
    ("chain 含图片段 → 转 [图片] 占位", Reply(chain=[Plain("看这个"), Image()]), "看这个 [图片]"),
    ("chain 含 @ → 保留 @ 文本", Reply(chain=[At(qq="123", name="小明"), Plain("在吗")]), "@小明 在吗"),
    ("chain 全为空但 message_str 有值", Reply(chain=[Plain("")], message_str="兜底文本"), "兜底文本"),
    ("无 reply → 空字符串", None, ""),
    ("chain 与 message_str 都为空 → 空字符串", Reply(chain=[], message_str=""), ""),
]


def check_text_cases(failed):
    for desc, reply, expected in CASES_TEXT:
        got = extract_quote_text(reply)
        if got != expected:
            failed.append(f"extract_quote_text [{desc}]: {got!r} != {expected!r}")


def check_find_reply(failed):
    if find_reply(QQ_CHAIN) is not QQ_REPLY:
        failed.append("find_reply: 未在消息链中找到 Reply")
    if find_reply([At(qq="1"), Plain("你好")]) is not None:
        failed.append("find_reply: 无引用时应返回 None")
    if find_reply(None) is not None:
        failed.append("find_reply: None 输入应返回 None")


def check_images(failed):
    img1, img2 = Image("a.png"), Image("b.png")
    got = extract_quote_images(Reply(chain=[Plain("图"), img1, img2]))
    if got != [img1, img2]:
        failed.append(f"extract_quote_images: 顺序/数量错误 {got}")
    if extract_quote_images(Reply(chain=[Plain("纯文本")])) != []:
        failed.append("extract_quote_images: 无图时应返回空列表")
    if extract_quote_images(None) != []:
        failed.append("extract_quote_images: None 输入应返回空列表")


def check_sender(failed):
    if quote_sender(Reply(sender_nickname=" 群友 ")) != "群友":
        failed.append("quote_sender: 应去除首尾空白")
    if quote_sender(Reply()) != "":
        failed.append("quote_sender: 无昵称应返回空字符串")


def check_prompt(failed):
    def expect(desc, got, must_contain=(), must_not_contain=(), exact=None):
        if exact is not None and got != exact:
            failed.append(f"build_prompt [{desc}]: {got!r} != {exact!r}")
            return
        for needle in must_contain:
            if needle not in got:
                failed.append(f"build_prompt [{desc}]: 缺少 {needle!r} → {got!r}")
        for needle in must_not_contain:
            if needle in got:
                failed.append(f"build_prompt [{desc}]: 不应包含 {needle!r} → {got!r}")

    # 1. 本次线上故障场景：引用「今天星期几」+ 自己的话「回答一下这个问题」
    got = build_prompt(
        "回答一下这个问题", "今天星期几", "",
        has_images=False,
        default_image_prompt=DEFAULT_IMAGE_PROMPT,
        default_quote_prompt=DEFAULT_QUOTE_PROMPT,
    )
    expect("QQ 引用+自己的话", got, must_contain=["今天星期几", "回答一下这个问题", REPLY_LABEL])
    if got.index("今天星期几") > got.index("回答一下这个问题"):
        failed.append("build_prompt: 被引用内容应排在用户自己的话之前")

    # 2. 带发送者昵称
    got = build_prompt("解释一下", "地球是圆的", "小明")
    expect("带昵称", got, must_contain=["[引用消息（小明）]", "地球是圆的", "解释一下"])

    # 3. 只引用、自己没说话 → 补默认引用提示词
    got = build_prompt(
        "", "今天星期几", "",
        has_images=False,
        default_image_prompt=DEFAULT_IMAGE_PROMPT,
        default_quote_prompt=DEFAULT_QUOTE_PROMPT,
    )
    expect("仅引用", got, must_contain=["今天星期几", DEFAULT_QUOTE_PROMPT])

    # 4. 引用 + 图片、自己没说话 → 默认图片提示词（优先于引用提示词）
    got = build_prompt(
        "   ", "看这张图", "",
        has_images=True,
        default_image_prompt=DEFAULT_IMAGE_PROMPT,
        default_quote_prompt=DEFAULT_QUOTE_PROMPT,
    )
    expect("引用+图片", got, must_contain=["看这张图", DEFAULT_IMAGE_PROMPT],
           must_not_contain=[DEFAULT_QUOTE_PROMPT])

    # 5. 无引用消息 → 原样返回用户文本（不加引用标记）
    expect("无引用", build_prompt("今天有什么大新闻"), exact="今天有什么大新闻",
           must_not_contain=[REPLY_LABEL])

    # 6. 无文字 + 有图片 + 无引用 → 默认图片提示词
    expect("仅图片", build_prompt("", has_images=True,
                                  default_image_prompt=DEFAULT_IMAGE_PROMPT),
           exact=DEFAULT_IMAGE_PROMPT)

    # 7. 全空 → 空字符串（调用方据此放行给默认 LLM）
    expect("全空", build_prompt(""), exact="")

    # 8. 引用文本与自己都没说话（引用内容解析为空）→ 空字符串（调用方放行给默认 LLM）
    expect("引用文本为空", build_prompt("   ", "  ", "", default_quote_prompt=DEFAULT_QUOTE_PROMPT),
           exact="")

    # 9. 文本与图片都来自引用时，prompt 不为空（防止误判为「空消息」被放行）
    got = build_prompt("", "看这个", "", has_images=True,
                       default_image_prompt=DEFAULT_IMAGE_PROMPT,
                       default_quote_prompt=DEFAULT_QUOTE_PROMPT)
    if not got.strip():
        failed.append("build_prompt: 引用文本+图片的 prompt 不应为空")


def check_card(failed):
    """卡片消息文本转储清理（v2.3.2）"""

    def expect(desc, got, must_contain=(), must_not_contain=(), exact=None):
        if exact is not None and got != exact:
            failed.append(f"card [{desc}]: {got!r} != {exact!r}")
            return
        for needle in must_contain:
            if needle not in got:
                failed.append(f"card [{desc}]: 缺少 {needle!r} → {got!r}")
        for needle in must_not_contain:
            if needle in got:
                failed.append(f"card [{desc}]: 不应包含 {needle!r} → {got!r}")

    # 1. 实测原文：解析出卡片类型 / 来源 / 摘要，丢掉图标与封面 URL
    cleaned = clean_card_text(QQ_CARD_DUMP)
    expect(
        "实测小程序卡片",
        cleaned,
        must_contain=["QQ 小程序卡片", "来源：哔哩哔哩", "《42中队》官方预告片「宣誓」"],
        must_not_contain=["qq.ugcimg.cn", "open.gtimg.cn", "source_logo", "preview"],
    )
    if len(cleaned) > 120:
        failed.append(f"card [实测小程序卡片]: 清理后仍过长（{len(cleaned)} 字）: {cleaned!r}")

    # 2. 标题与摘要互相包含时只保留一条（不重复占 token）
    parsed = parse_card_dump(QQ_CARD_DUMP)
    titles = [v for n, v in parsed["fields"] if n == "标题"]
    if len(titles) != 1:
        failed.append(f"card [parse_card_dump]: 标题应保留 1 条 → {parsed['fields']}")
    if cleaned.count("《42中队》官方预告片「宣誓」，游戏现已开放愿望单") != 1:
        failed.append(f"card [去重]: 标题/摘要重复内容应只出现一次 → {cleaned!r}")

    # 3. 完全相同的标题与摘要 → 只留一条（标签保留信息量更大的那条）
    same = clean_card_text(
        "[卡片消息] 音乐\n摘要: 一首歌\ntitle: 一首歌\nsource: 网易云音乐"
    )
    expect("标题摘要相同", same, must_contain=["QQ 音乐卡片", "来源：网易云音乐"],
           must_not_contain=["标题："])
    if same.count("一首歌") != 1:
        failed.append(f"card [标题摘要相同]: 重复内容应只出现一次 → {same!r}")

    # 4. 保留链接类字段（有信息量的 URL 字段按「链接」保留）
    link = clean_card_text("[链接分享]\n标题: 某篇文章\nurl: https://example.com/a")
    expect("链接分享", link, must_contain=["链接分享", "标题：某篇文章", "链接：https://example.com/a"])

    # 5. 技术字段（appid / type / 签名串）一律丢弃，来源等已知字段转中文标签
    tech = clean_card_text(
        "[卡片消息] 小程序\nappid: 1109\nappname: 哔哩哔哩\ntype: mini\nsignature: "
        "abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklmnopqrstuvwxyz0123456789"
        "abcdefghijklmnopqrstuvwxyz\njumpurl: https://b23.tv/xyz"
    )
    expect("技术字段过滤", tech,
           must_contain=["QQ 小程序卡片", "来源：哔哩哔哩", "链接：https://b23.tv/xyz"],
           must_not_contain=["appid", "type", "signature"])

    # 6. 中文冒号、缺子类型、无来源 都要兼容
    expect("中文冒号", clean_card_text("[卡片消息] 小程序\n标题：今天的天气"),
           must_contain=["QQ 小程序卡片", "标题：今天的天气"])
    expect("无子类型", clean_card_text("[卡片消息]\n标题: 标题A"),
           must_contain=["QQ 卡片消息", "标题：标题A"])

    # 7. 非卡片文本原样返回（普通消息绝不被改写）
    plain_texts = [
        "今天星期几",
        "回答一下这个问题",
        "时间: 明天\n地点: 公司",          # 有字段样式但首行不是 [xxx] → 不动
        "[图片]",                          # 单行 → 不动
        "第一行\n第二行",                   # 多行但无字段 → 不动
        "",
    ]
    for raw in plain_texts:
        if clean_card_text(raw) != raw:
            failed.append(f"card [非卡片不改写]: {raw!r} → {clean_card_text(raw)!r}")

    # 8. 卡片里只有资源 URL（清理后没有有效字段）→ 原样返回，不丢信息
    only_urls = "[卡片消息] 小程序\npreview: https://qq.ugcimg.cn/v1/abc\ntype: mini"
    if clean_card_text(only_urls) != only_urls:
        failed.append(f"card [无有效字段回退]: {clean_card_text(only_urls)!r}")

    # 9. 清理后的卡片文本进入 prompt：模型看到的是卡片描述而不是 URL
    prompt = build_prompt(
        "这是什么", clean_card_text(QQ_CARD_DUMP), "",
        default_image_prompt=DEFAULT_IMAGE_PROMPT,
        default_quote_prompt=DEFAULT_QUOTE_PROMPT,
    )
    expect("卡片进 prompt", prompt,
           must_contain=["[引用消息]", "QQ 小程序卡片", "来源：哔哩哔哩", "这是什么"],
           must_not_contain=["qq.ugcimg.cn", "open.gtimg.cn"])

    # 10. 直接发送的卡片（未引用）同样会被清理
    if clean_card_text(QQ_CARD_DUMP) == QQ_CARD_DUMP:
        failed.append("card [直接发送]: 卡片文本未被清理")

    # 11. 卡片封面图引用（v2.3.3）：URL 不进 prompt，但要能被单独取出去上传
    refs = card_cover_refs(QQ_CARD_DUMP)
    if refs != [CARD_COVER_URL]:
        failed.append(f"card [封面引用]: {refs!r} != [{CARD_COVER_URL!r}]")
    if card_cover_refs("今天星期几") != []:
        failed.append("card [封面引用]: 非卡片应返回空列表")
    if card_cover_refs("[卡片消息] 小程序\n标题: 只有标题\nsource_logo: https://a/b.png") != []:
        failed.append("card [封面引用]: 来源图标不应被当成封面")
    # 只有封面、没有信息字段：清理保持原文（不丢信息），但封面仍可取出
    cover_only = f"[卡片消息] 大图\npreview: {CARD_COVER_URL}"
    if clean_card_text(cover_only) != cover_only:
        failed.append(f"card [仅封面回退]: {clean_card_text(cover_only)!r}")
    if card_cover_refs(cover_only) != [CARD_COVER_URL]:
        failed.append("card [仅封面取图]: 只有封面时也应能取到 URL")

    # 12. 封面提示行进入 prompt（build_prompt 的 quote_hint）
    hint = "（随附卡片封面图，请侧重描述封面画面，并结合卡片摘要作答）"
    prompt = build_prompt("这是什么", clean_card_text(QQ_CARD_DUMP), "",
                          has_images=True, quote_hint=hint)
    if hint not in prompt or "这是什么" not in prompt:
        failed.append(f"card [封面提示]: {prompt!r}")
    if prompt.index(hint) > prompt.index("这是什么"):
        failed.append("card [封面提示]: 提示行应在自己的话之前")


def check_forward(failed):
    """合并转发（聊天记录）文本转储清理（v2.3.3）"""

    def expect(desc, got, must_contain=(), must_not_contain=(), exact=None):
        if exact is not None and got != exact:
            failed.append(f"forward [{desc}]: {got!r} != {exact!r}")
            return
        for needle in must_contain:
            if needle not in got:
                failed.append(f"forward [{desc}]: 缺少 {needle!r} → {got!r}")
        for needle in must_not_contain:
            if needle in got:
                failed.append(f"forward [{desc}]: 不应包含 {needle!r} → {got!r}")

    expected = (
        "QQ 合并转发消息（聊天记录，共 3 条）\n"
        "- 刺鸟/ty无限：https://www.bilibili.com/video/BV1cXae6mEny/\n"
        "- 刺鸟/ty无限：实战经验解析韭留美\n"
        "- 刺鸟/ty无限：win11不能"
    )

    # 1. 实测原文（引用形态 / 直接发送形态）→ 完全一致的「谁说了什么」清单
    expect("引用形态", clean_forward_text(QQ_FORWARD_DUMP_QUOTED), exact=expected)
    expect("直接发送形态", clean_forward_text(QQ_FORWARD_DUMP_DIRECT), exact=expected)
    # 结构性标记与容器标题都必须清掉
    for dump in (QQ_FORWARD_DUMP_QUOTED, QQ_FORWARD_DUMP_DIRECT):
        expect("去除结构标记", clean_forward_text(dump),
               must_not_contain=["=== 消息", "--- 第", "[消息内容]", "[发送者]",
                                 "[消息类型]", "[关联消息]", "[群聊的聊天记录]"])

    # 2. parse_forward_dump：条数与发送者
    parsed = parse_forward_dump(QQ_FORWARD_DUMP_QUOTED)
    if not parsed or parsed["total"] != 3:
        failed.append(f"forward [解析]: {parsed!r}")
    elif [it["sender"] for it in parsed["items"]] != ["刺鸟/ty无限"] * 3:
        failed.append(f"forward [发送者]: {parsed['items']!r}")

    # 3. 没有发送者时只列内容；多行内容合并为一行
    no_sender = clean_forward_text("=== 消息 1 ===\n[消息内容] 第一行\n第二行")
    expect("无发送者+多行内容", no_sender, exact="QQ 合并转发消息（聊天记录，共 1 条）\n- 第一行 第二行")

    # 4. 超过上限只列前 N 条并标注剩余条数
    big = "\n".join(
        f"=== 消息 {i} ===\n[消息内容] 内容{i}\n[发送者] 甲" for i in range(1, 41)
    )
    cleaned = clean_forward_text(big)
    expect("超长转发截断", cleaned,
           must_contain=["共 40 条", "以下为前 30 条", "内容1", "内容30", "另有 10 条未列出"],
           must_not_contain=["内容31"])

    # 5. 非合并转发文本原样返回
    for raw in ("今天星期几", "[图片]", "", "=== 消息 1 ===", "[群聊的聊天记录]"):
        if clean_forward_text(raw) != raw:
            failed.append(f"forward [非转发不改写]: {raw!r} → {clean_forward_text(raw)!r}")

    # 6. 统一入口 clean_dump_text：转发与卡片分别命中，普通文本不动
    if clean_dump_text(QQ_FORWARD_DUMP_QUOTED) != expected:
        failed.append("forward [统一入口]: 合并转发未命中")
    if clean_dump_text(QQ_CARD_DUMP) != clean_card_text(QQ_CARD_DUMP):
        failed.append("forward [统一入口]: 卡片消息未命中")
    if clean_dump_text("今天有什么大新闻") != "今天有什么大新闻":
        failed.append("forward [统一入口]: 普通文本被改写")

    # 7. 引用合并转发 + 自己的话 → 提问结构正确
    prompt = build_prompt(
        "分析一下这段聊天记录",
        clean_dump_text(QQ_FORWARD_DUMP_QUOTED),
        "",
        default_quote_prompt="请针对我引用的这条消息进行回答。",
    )
    expect("合并转发进 prompt", prompt,
           must_contain=["[引用消息]", "共 3 条", "刺鸟/ty无限：win11不能", "分析一下这段聊天记录"],
           must_not_contain=["=== 消息", "--- 第"])


def main():
    failed = []
    check_text_cases(failed)
    check_find_reply(failed)
    check_images(failed)
    check_sender(failed)
    check_prompt(failed)
    check_card(failed)
    check_forward(failed)
    if failed:
        for line in failed:
            print(f"FAIL: {line}")
        print(f"QUOTE PARSER: {len(failed)} CHECK(S) FAILED")
        sys.exit(1)
    print("ALL QUOTE PARSER CHECKS PASSED")


if __name__ == "__main__":
    main()
