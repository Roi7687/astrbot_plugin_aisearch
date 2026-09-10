"""/ais 指令参数解析器（纯函数，不依赖 astrbot，便于单元测试）。

/ais 用法：
  /ais <问题>                     在当前会话提问（无会话则自动创建）
  /ais new | reset | 重置         开启新会话（旧会话保留，可按 id 切回）
  /ais list | 列表 | 状态 | session   查看会话列表（带本地 id）
  /ais switch <id>                按本地 id 切换会话
  /ais switch                     无参数 = 显示会话列表
  /ais help | 帮助                查看帮助

兼容说明：`-v` 旗标自 v2.2.10 起已无意义（DeepSeek v2.3.0 起把普通/识图模式
合并为统一对话，图片由发送图片自动处理），解析时仍会剥离，避免把旗标文本
当作问题发给 AI；`-t` 旗标 v2.2.11 起彻底不识别（按普通文本处理）。
"""

SUBCOMMAND_ALIASES = {
    "new": "new", "reset": "new", "重置": "new", "新会话": "new", "重新开始": "new",
    # session/状态 已合并进 list（统一查看/切换入口）
    "list": "list", "列表": "list", "会话列表": "list", "sessions": "list",
    "session": "list", "状态": "list", "会话": "list", "info": "list", "查看": "list",
    "switch": "switch", "切换": "switch",
    "help": "help", "帮助": "help", "帮助文档": "help",
}


def _strip_compat_flags(tokens):
    """剥离兼容旗标 -v（无实际效果，仅避免旗标文本被当问题发送），
    返回剩余 tokens。-t 已彻底移除识别（v2.2.11），按普通文本处理。"""
    rest = []
    for tok in tokens:
        if tok in ("-v", "--vision", "识图模式"):
            continue
        rest.append(tok)
    return rest


def _parse_switch_target(tokens):
    """解析 switch 的目标参数（本地 id）。

    返回 {"local_id": int} / {}（无有效目标）
    """
    if len(tokens) < 2:
        return {}
    cand = tokens[1]
    if cand.isdigit():
        return {"local_id": int(cand)}
    return {}


def parse_ais_command(raw: str):
    """解析 /ais 后的参数。

    返回 (action, payload)：
      action: "usage" | "help" | "send" | "new" | "list" | "switch"
      payload: dict
        - send:    {"text": str}
        - switch:  {"local_id": int} | {}
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
            # /ais list：无参显示列表（切换统一走 /ais switch <id>）
            return "list", {}
        if action == "switch":
            # /ais switch [id]：无参或参数无效时由调用方显示列表
            return "switch", _parse_switch_target(rest_tokens)
        # 兜底
        return action, {}

    # 普通提问（-v 兼容剥离且无效果；-t 已不识别，原样进入文本）
    return "send", {"text": rest}
