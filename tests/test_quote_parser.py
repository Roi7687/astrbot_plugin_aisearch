"""引用消息解析单元测试（纯函数，无第三方依赖）

覆盖 v2.3.1 修复的核心场景：QQ 群「引用一条消息 + @机器人 + 自己的话」时，
被引用的内容（Reply.chain / Reply.message_str）必须进入提问文本，
否则 DeepSeek 只看到「回答一下这个问题」这类指代不明的话。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.quote_parser import (
    REPLY_LABEL,
    build_prompt,
    extract_quote_images,
    extract_quote_text,
    find_reply,
    quote_sender,
)

DEFAULT_IMAGE_PROMPT = "请详细描述这张图片的内容。"
DEFAULT_QUOTE_PROMPT = "请针对我引用的这条消息进行回答。"


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


def main():
    failed = []
    check_text_cases(failed)
    check_find_reply(failed)
    check_images(failed)
    check_sender(failed)
    check_prompt(failed)
    if failed:
        for line in failed:
            print(f"FAIL: {line}")
        print(f"QUOTE PARSER: {len(failed)} CHECK(S) FAILED")
        sys.exit(1)
    print("ALL QUOTE PARSER CHECKS PASSED")


if __name__ == "__main__":
    main()
