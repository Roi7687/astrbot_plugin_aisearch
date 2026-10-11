"""引用消息端到端流程测试（真实 AstrBot 消息链 + 插件处理器）

用真实的 AstrBot 组件（Reply / At / Plain / Image）与 AstrMessageEvent 复现
QQ 官方平台在群里「引用一条消息 + @机器人 + 自己的话」时的消息链，
直接调用插件的 on_message / ai_search_command，断言**最终发给 DeepSeek 的
提问文本与图片**（用假会话内核记录调用）。

对应线上故障（日志 2026-10-11 01:17:41）：
    消息链 = [Reply(今天星期几), At(机器人), Plain(回答一下这个问题)]
    message_str = 回答一下这个问题
    → 修复前 DeepSeek 只收到「回答一下这个问题」，回「请把你想让我回答的问题发出来」

注：需要 AstrBot 运行时代码。测试按插件目录向上三级定位 AstrBot 根目录
（instances/<id>/core），找不到时跳过并返回成功（纯函数测试仍可独立运行）。
"""

import asyncio, os, sys, types, traceback, tempfile, base64, logging

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
PLUGINS_DIR = os.path.dirname(PLUGIN_DIR)
ASTROBOT_ROOT = os.path.abspath(os.path.join(PLUGIN_DIR, "..", "..", ".."))

sys.path.insert(0, PLUGIN_DIR)
sys.path.insert(0, PLUGINS_DIR)
if os.path.isdir(os.path.join(ASTROBOT_ROOT, "astrbot")):
    sys.path.insert(0, ASTROBOT_ROOT)

# 隔离临时目录：AstrBot 导入时会按 cwd 创建 data/（cmd_config.json、t2i_templates…），
# 因此**必须在导入 astrbot 之前**切换 cwd，避免在插件目录里生成 data/；
# 会话元数据同样只读写这个临时目录（绝不触碰线上 conversations.json）。
TMP_DIR = tempfile.mkdtemp(prefix="ais_quote_flow_")
os.chdir(TMP_DIR)

# 浏览器 / 渲染依赖打桩（本测试不触及浏览器）
sys.modules.setdefault("cloakbrowser", types.SimpleNamespace(launch_async=lambda **kw: None))
sys.modules.setdefault("markdownify", types.SimpleNamespace(markdownify=lambda html, **kw: "[md]"))

try:
    from astrbot.core.message.components import At, Image, Plain, Reply
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
except Exception as e:  # pragma: no cover - 无 AstrBot 运行时的纯函数测试环境
    print(f"SKIP: 未找到 AstrBot 运行时（{e}），跳过端到端引用消息测试")
    sys.exit(0)

import core.session_core as session_core

session_core.CONVERSATIONS_FILE = os.path.join(TMP_DIR, "conversations.json")

import astrbot_plugin_aisearch.main as plugin_main
from core.config import DEFAULT_IMAGE_PROMPT, DEFAULT_QUOTE_PROMPT

BOT_ID = "F9F5BCD9CCABB46B5B5B6B5E671C1BCF"
USER_ID = "3A9027AED45B5058E2F58D938516F36F"
QUOTE_ID = "QUOTED-MSG-ID"

# 1x1 透明 PNG（用于图片链路，内容不重要）
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
PNG_PATH = os.path.join(TMP_DIR, "quoted.png")
with open(PNG_PATH, "wb") as f:
    f.write(PNG_BYTES)

# QQ 卡片消息（小程序）的文本转储——实测原文（2026-10-11 11:49 线上日志）
QQ_CARD_DUMP = """[卡片消息] 小程序
摘要: [QQ小程序]《42中队》官方预告片「宣誓」，游戏现已开放愿望单
source: 哔哩哔哩
source_logo: https://open.gtimg.cn/open/app_icon/00/95/17/76/100951776_100_m.png?t=1791448425
title: 《42中队》官方预告片「宣誓」，游戏现已开放愿望单
preview: https://qq.ugcimg.cn/v1/vd9mc16v4u4fdiu18nu79url5qnvdhq6h0qurfl2em9inbs1l72j5g0j39i6b38s8s20ob4ukjcuolmnmlfjulu9n752u026n7q8h357l59utm035s9v8q8lkuh0l3dlgl2c8ips491q7mcv2jk7hcokrg/e9vf2tsqr6j29hl2kodb3vahlc"""


class FakeSession:
    """记录发给 DeepSeek 的提问（不启动浏览器）"""

    def __init__(self):
        self.calls = []
        self.current_conversation = None

    async def send_message(self, text):
        self.calls.append({"kind": "text", "text": text, "paths": []})
        return False, "FAKE-ANSWER"

    async def send_image_message(self, text, paths):
        self.calls.append({"kind": "image", "text": text, "paths": list(paths)})
        return False, "FAKE-ANSWER"


def build_event(own_text="", quote_text=None, quote_images=None,
                own_images=None, at_bot=True, sender_id=USER_ID,
                auto_trigger=None):
    """按 QQ 官方平台适配器的结构组装消息链与事件。

    quote_text 为 None 表示没有引用消息；否则生成
    Reply(chain=[...], message_str=quote_text) 放在链首（与适配器一致）。
    """
    chain = []
    if quote_text is not None or quote_images:
        quoted_chain = []
        if quote_text:
            quoted_chain.append(Plain(quote_text))
        for img in quote_images or []:
            quoted_chain.append(img)
        chain.append(Reply(id=QUOTE_ID, chain=quoted_chain, message_str=quote_text or ""))
    if at_bot:
        chain.append(At(qq=BOT_ID, name=""))
    for img in own_images or []:
        chain.append(img)
    if own_text:
        chain.append(Plain(own_text))

    abm = AstrBotMessage()
    abm.type = MessageType.GROUP_MESSAGE
    abm.self_id = BOT_ID
    abm.session_id = "group-session"
    abm.message_id = "msg-1"
    abm.group = Group(group_id="group-openid")
    abm.sender = MessageMember(sender_id, "群友")
    abm.message = chain
    abm.message_str = own_text  # 适配器只把「自己的话」写进 message_str
    abm.raw_message = None

    event = AstrMessageEvent(
        message_str=own_text,
        message_obj=abm,
        platform_meta=PlatformMetadata(
            name="qq_official", id="default", description="test"
        ),
        session_id="group-session",
    )
    event.is_at_or_wake_command = at_bot
    return event


async def collect(agen):
    return [item async for item in agen]


def make_plugin():
    plugin = plugin_main.CloakSearchPlugin(None)
    plugin.session = FakeSession()
    return plugin


def check(failed, desc, cond, detail=""):
    if not cond:
        failed.append(f"{desc}{(' → ' + detail) if detail else ''}")


async def main():
    failed = []

    # ① 本次线上故障场景：引用文本 + 自己的话
    plugin = make_plugin()
    await collect(plugin.on_message(
        build_event("回答一下这个问题", quote_text="今天星期几")
    ))
    check(failed, "① 引用消息应触发提问", len(plugin.session.calls) == 1,
          f"calls={plugin.session.calls}")
    if plugin.session.calls:
        text = plugin.session.calls[0]["text"]
        check(failed, "① 提问应包含被引用文本「今天星期几」", "今天星期几" in text, text)
        check(failed, "① 提问应包含自己的话「回答一下这个问题」",
              "回答一下这个问题" in text, text)
        check(failed, "① 提问应以引用标记开头", text.startswith("[引用消息"), text)
        check(failed, "① 被引用内容应在自己的话之前",
              text.index("今天星期几") < text.index("回答一下这个问题"), text)

    # ② 只引用、自己没说话 → 引用文本 + 默认引用提示词
    plugin = make_plugin()
    await collect(plugin.on_message(build_event("", quote_text="今天星期几")))
    check(failed, "② 仅引用也应提问", len(plugin.session.calls) == 1)
    if plugin.session.calls:
        text = plugin.session.calls[0]["text"]
        check(failed, "② 应包含引用文本", "今天星期几" in text, text)
        check(failed, "② 应补默认引用提示词", DEFAULT_QUOTE_PROMPT in text, text)

    # ③ 引用一条含图片的消息（自己带文字）→ 走图片链路，图片来自 Reply.chain
    plugin = make_plugin()
    await collect(plugin.on_message(build_event(
        "这图里是什么",
        quote_text=None,
        quote_images=[Image.fromFileSystem(PNG_PATH)],
    )))
    check(failed, "③ 引用图片应走 send_image_message",
          len(plugin.session.calls) == 1 and plugin.session.calls[0]["kind"] == "image",
          f"calls={plugin.session.calls}")
    if plugin.session.calls:
        call = plugin.session.calls[0]
        check(failed, "③ 应上传 1 张引用图片", len(call["paths"]) == 1, str(call["paths"]))
        check(failed, "③ 上传的应是引用图片本体",
              bool(call["paths"]) and os.path.samefile(call["paths"][0], PNG_PATH),
              str(call["paths"]))
        check(failed, "③ 提问应包含自己的话", "这图里是什么" in call["text"], call["text"])

    # ④ 引用含图片 + 自己没说话 → 默认图片提示词（不是默认引用提示词）
    plugin = make_plugin()
    await collect(plugin.on_message(build_event(
        "", quote_text=None, quote_images=[Image.fromFileSystem(PNG_PATH)]
    )))
    if plugin.session.calls:
        text = plugin.session.calls[0]["text"]
        check(failed, "④ 引用图片无文字应用默认图片提示词",
              DEFAULT_IMAGE_PROMPT in text and DEFAULT_QUOTE_PROMPT not in text, text)

    # ⑤ 回归：直接发图片（无引用）仍然工作
    plugin = make_plugin()
    await collect(plugin.on_message(build_event(
        "描述一下", own_images=[Image.fromFileSystem(PNG_PATH)]
    )))
    check(failed, "⑤ 直接发图片仍走图片链路",
          len(plugin.session.calls) == 1
          and plugin.session.calls[0]["kind"] == "image"
          and len(plugin.session.calls[0]["paths"]) == 1,
          f"calls={plugin.session.calls}")

    # ⑥ 回归：普通文本不带引用标记
    plugin = make_plugin()
    await collect(plugin.on_message(build_event("今天有什么大新闻")))
    check(failed, "⑥ 普通文本应原样提问",
          len(plugin.session.calls) == 1
          and plugin.session.calls[0]["text"] == "今天有什么大新闻",
          f"calls={plugin.session.calls}")

    # ⑦ 回归：群里未 @ 机器人的消息不拦截（不触发提问）
    plugin = make_plugin()
    await collect(plugin.on_message(
        build_event("随便聊聊", quote_text="某条消息", at_bot=False)
    ))
    check(failed, "⑦ 未 @ 机器人不应触发", plugin.session.calls == [],
          f"calls={plugin.session.calls}")

    # ⑧ 回归：机器人自己的消息不处理（防自我回复循环）
    plugin = make_plugin()
    await collect(plugin.on_message(
        build_event("我自己说的话", quote_text="引用", sender_id=BOT_ID)
    ))
    check(failed, "⑧ 机器人自身消息应跳过", plugin.session.calls == [],
          f"calls={plugin.session.calls}")

    # ⑨ 引用内容为空（chain 与 message_str 都空）+ 自己没说话 → 不发空提问
    plugin = make_plugin()
    await collect(plugin.on_message(build_event("", quote_text="", at_bot=True)))
    check(failed, "⑨ 空引用不应发空提问", plugin.session.calls == [],
          f"calls={plugin.session.calls}")

    # ⑩ /ais 指令路径同样带上引用文本
    plugin = make_plugin()
    await collect(plugin.ai_search_command(
        build_event("", quote_text="今天星期几"), "回答一下这个问题"
    ))
    check(failed, "⑩ /ais 指令应提问", len(plugin.session.calls) == 1,
          f"calls={plugin.session.calls}")
    if plugin.session.calls:
        text = plugin.session.calls[0]["text"]
        check(failed, "⑩ /ais 提问应包含引用文本", "今天星期几" in text, text)
        check(failed, "⑩ /ais 提问应包含问题本身", "回答一下这个问题" in text, text)

    # ⑪ AUTO_TRIGGER 关闭：纯引用文本不自动触发，引用图片仍处理（图片始终处理）
    original_auto = plugin_main.AUTO_TRIGGER
    plugin_main.AUTO_TRIGGER = False
    try:
        plugin = make_plugin()
        await collect(plugin.on_message(build_event("", quote_text="今天星期几")))
        check(failed, "⑪ AUTO_TRIGGER=False 时纯引用文本不应触发",
              plugin.session.calls == [], f"calls={plugin.session.calls}")

        plugin = make_plugin()
        await collect(plugin.on_message(build_event(
            "", quote_text=None, quote_images=[Image.fromFileSystem(PNG_PATH)]
        )))
        check(failed, "⑪ AUTO_TRIGGER=False 时引用图片仍应处理",
              len(plugin.session.calls) == 1
              and plugin.session.calls[0]["kind"] == "image",
              f"calls={plugin.session.calls}")
    finally:
        plugin_main.AUTO_TRIGGER = original_auto

    # ⑫ 引用文本过长不截断（逐字进入 prompt）
    long_text = "长" * 500
    plugin = make_plugin()
    await collect(plugin.on_message(build_event("总结一下", quote_text=long_text)))
    if plugin.session.calls:
        check(failed, "⑫ 长引用应完整进入 prompt",
              long_text in plugin.session.calls[0]["text"])

    # ⑬ 引用**卡片消息**（v2.3.2）：腾讯文本转储被清理成卡片描述，资源 URL 不进 prompt
    plugin = make_plugin()
    await collect(plugin.on_message(build_event("这是什么", quote_text=QQ_CARD_DUMP)))
    check(failed, "⑬ 引用卡片应触发提问", len(plugin.session.calls) == 1,
          f"calls={plugin.session.calls}")
    if plugin.session.calls:
        text = plugin.session.calls[0]["text"]
        check(failed, "⑬ 卡片应被解析出类型与来源",
              "QQ 小程序卡片" in text and "来源：哔哩哔哩" in text, text)
        check(failed, "⑬ 卡片标题应保留", "《42中队》官方预告片「宣誓」" in text, text)
        check(failed, "⑬ 图标/封面 URL 不应进入 prompt",
              "qq.ugcimg.cn" not in text and "open.gtimg.cn" not in text
              and "source_logo" not in text, text)
        check(failed, "⑬ 提问应包含自己的话", "这是什么" in text, text)
        check(failed, "⑬ 卡片清理后应显著变短", len(text) < 150, f"{len(text)} 字")

    # ⑭ 直接发送卡片消息（未引用）同样被清理
    plugin = make_plugin()
    await collect(plugin.on_message(build_event(QQ_CARD_DUMP)))
    check(failed, "⑭ 直接发送卡片应提问", len(plugin.session.calls) == 1,
          f"calls={plugin.session.calls}")
    if plugin.session.calls:
        text = plugin.session.calls[0]["text"]
        check(failed, "⑭ 直接发送的卡片也应被清理",
              "QQ 小程序卡片" in text and "qq.ugcimg.cn" not in text, text)

    # ⑮ 非卡片文本不被改写（回归）
    plugin = make_plugin()
    await collect(plugin.on_message(build_event("时间: 明天\n地点: 公司")))
    if plugin.session.calls:
        check(failed, "⑮ 普通多行文本不应被改写",
              plugin.session.calls[0]["text"] == "时间: 明天\n地点: 公司",
              plugin.session.calls[0]["text"])

    if failed:
        for line in failed:
            print(f"FAIL: {line}")
        print(f"QUOTE FLOW: {len(failed)} CHECK(S) FAILED")
        return 1
    print("ALL QUOTE FLOW CHECKS PASSED")
    return 0


if __name__ == "__main__":
    logging.disable(logging.INFO)  # 静音 AstrBot 初始化日志，只保留断言结果
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
