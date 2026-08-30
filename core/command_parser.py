"""/ais 指令参数解析器（纯函数，不依赖 astrbot，便于单元测试）。

/ais 用法：
  /ais <问题>                     在当前会话提问（无会话则自动创建普通会话）
  /ais new | reset | 重置         开启新的普通会话（旧会话保留，可按 id 切回）
  /ais list | 列表 | 状态 | session   查看全部会话（带本地 id）
  /ais list <id>                  按本地 id 直接切换（便捷写法）
  /ais switch <id> | <识图/普通>   切换会话（按本地 id 或模式）
  /ais switch                     无参数 = 显示会话列表
  /ais help | 帮助                查看帮助

兼容说明：`-t` / `-v` 旗标自 v2.2.5 / v2.2.10 起无实际效果
（深度思考跟随 DeepSeek 网页端设置；识图由发送图片自动触发），
解析时仍会剥离，避免把旗标文本当作问题发给 AI。
"""

SUBCOMMAND_ALIASES = {
    "new": "new", "reset": "new", "重置": "new", "新会话": "new", "重新开始": "new",
    # session/状态 已合并进 list（统一查看/切换入口）
    "list": "list", "列表": "list", "会话列表": "list", "sessions": "list",
    "session": "list", "状态": "list", "会话": "list", "info": "list", "查看": "list",
    "switch": "switch", "切换": "switch",
    "help": "help", "帮助": "help", "帮助文档": "help",
}

MODE_ALIASES = {
    "vision": "vision", "识图": "vision", "图片": "vision", "图像": "vision", "v": "vision",
    "normal": "normal", "普通": "normal", "文本": "normal", "对话": "normal", "n": "normal",
}


def _strip_compat_flags(tokens):
    """剥离开头的 -t / -v 兼容旗标（无实际效果，仅避免旗标文本被当问题发送），
    返回剩余 tokens。"""
    rest = []
    for tok in tokens:
        if tok in ("-t", "--think", "思考", "-v", "--vision", "识图模式"):
            continue
        rest.append(tok)
    return rest


def _parse_switch_target(tokens):
    """解析 switch/list 的目标参数：本地 id 或模式别名。

    返回 {"local_id": int} / {"mode": str} / {}（无有效目标）
    """
    if len(tokens) < 2:
        return {}
    cand = tokens[1].lower()
    if cand.isdigit():
        return {"local_id": int(cand)}
    if cand in MODE_ALIASES:
        return {"mode": MODE_ALIASES[cand]}
    return {}


def parse_ais_command(raw: str):
    """解析 /ais 后的参数。

    返回 (action, payload)：
      action: "usage" | "help" | "send" | "new" | "list" | "switch"
      payload: dict
        - send:    {"text": str}
        - switch:  {"local_id": int} | {"mode": str} | {}
        - 其余:    {}
    """
    q = (raw or "").strip()
    if not q:
        return "usage", {}

    tokens = q.split()
    rest_tokens = _strip_compat_flags(tokens)
    rest = " ".join(rest_tokens).strip()

    # 无正文且只有旗标
    if not rest:
        return "usage", {}

    # 子命令：只有第一个 token 是关键词才识别（避免误伤正常提问）
    first = rest_tokens[0].lower() if rest_tokens else ""
    if first in SUBCOMMAND_ALIASES:
        action = SUBCOMMAND_ALIASES[first]
        if action == "help":
            return "help", {}
        if action == "new":
            return "new", {}
        if action == "list":
            # /ais list [id | 识图/普通]：无参显示列表；带有效目标直接切换
            target = _parse_switch_target(rest_tokens)
            if target:
                return "switch", target
            return "list", {}
        if action == "switch":
            # /ais switch [id | 识图/普通]：无参或参数无效时由调用方显示列表
            return "switch", _parse_switch_target(rest_tokens)
        # 兜底
        return action, {}

    # 普通提问（-t/-v 旗标已剥离且无效果）
    return "send", {"text": rest}
