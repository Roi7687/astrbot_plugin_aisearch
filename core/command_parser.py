"""/ais 指令参数解析器（纯函数，不依赖 astrbot，便于单元测试）。

/ais 用法：
  /ais <问题>                 在当前会话提问（无会话则自动创建普通会话）
  /ais -t <问题>              开启深度思考
  /ais -v <问题>              切到识图会话并提问（无识图会话则自动创建）
  /ais -v -t <问题>           识图会话 + 深度思考
  /ais new | reset | 重置     关闭当前会话并开启新会话（同模式）
  /ais session | 状态         查看当前会话信息
  /ais list | 列表            查看全部会话
  /ais switch [模式] | 切换    切换当前会话（可指定 识图/普通，缺省列出可选模式）
  /ais help | 帮助            查看帮助
"""

SUBCOMMAND_ALIASES = {
    "new": "new", "reset": "new", "重置": "new", "新会话": "new", "重新开始": "new",
    "session": "session", "状态": "session", "会话": "session", "info": "session",
    "list": "list", "列表": "list", "会话列表": "list", "sessions": "list",
    "switch": "switch", "切换": "switch",
    "help": "help", "帮助": "help", "帮助文档": "help",
}

MODE_ALIASES = {
    "vision": "vision", "识图": "vision", "图片": "vision", "图像": "vision", "v": "vision",
    "normal": "normal", "普通": "normal", "文本": "normal", "对话": "normal", "n": "normal",
}

# 独立子命令时不允许带模式参数的命令
SWITCHABLE_MODES = ("vision", "normal")


def _strip_flags(tokens):
    """提取开头的 -t / -v 旗标，返回 (thinking, vision, 剩余tokens)"""
    thinking = False
    vision = False
    rest = []
    for tok in tokens:
        if tok in ("-t", "--think", "思考"):
            thinking = True
        elif tok in ("-v", "--vision", "识图模式"):
            vision = True
        elif tok.startswith("-"):
            # 未知旗标：直接拼回文本（当作普通问题的一部分）
            rest.append(tok)
        else:
            rest.append(tok)
    return thinking, vision, rest


def parse_ais_command(raw: str):
    """解析 /ais 后的参数。

    返回 (action, payload)：
      action: "usage" | "help" | "send" | "new" | "session" | "list" | "switch"
      payload: dict
        - send:    {"mode": "normal"|"vision"|None, "thinking": bool, "text": str}
        - switch:  {"mode_arg": str|None}
        - 其余:    {}
    """
    q = (raw or "").strip()
    if not q:
        return "usage", {}

    tokens = q.split()
    thinking, vision, rest_tokens = _strip_flags(tokens)
    rest = " ".join(rest_tokens).strip()

    # 无正文且只有旗标
    if not rest:
        if thinking or vision:
            return "usage", {}
        return "usage", {}

    # 子命令：只有第一个 token 是关键词才识别（避免误伤正常提问）
    first = rest_tokens[0].lower() if rest_tokens else ""
    if first in SUBCOMMAND_ALIASES:
        action = SUBCOMMAND_ALIASES[first]
        if action == "help":
            return "help", {}
        if action == "new":
            return "new", {}
        if action == "session":
            return "session", {}
        if action == "list":
            return "list", {}
        if action == "switch":
            # switch [模式]
            mode_arg = None
            if len(rest_tokens) > 1:
                cand = rest_tokens[1].lower()
                if cand in MODE_ALIASES:
                    mode_arg = MODE_ALIASES[cand]
            return "switch", {"mode_arg": mode_arg}
        # 兜底
        return action, {}

    # 普通提问（含旗标）
    mode = "vision" if vision else None
    return "send", {"mode": mode, "thinking": thinking, "text": rest}
