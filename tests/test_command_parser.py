"""/ais 指令解析器单元测试（纯函数，无第三方依赖）"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.command_parser import parse_ais_command

# (raw, expected_action, expected_payload)
CASES = [
    # 空输入 → usage
    ("", "usage", {}),
    ("   ", "usage", {}),
    ("-t", "usage", {}),
    ("-v -t", "usage", {}),

    # 普通提问
    ("你好", "send", {"text": "你好"}),
    ("今天有什么大新闻", "send", {"text": "今天有什么大新闻"}),
    # -t / -v 兼容旗标：剥离但无效果（v2.2.5 / v2.2.10 起）
    ("-t 解释量子纠缠", "send", {"text": "解释量子纠缠"}),
    ("-v 这张图", "send", {"text": "这张图"}),
    ("-v -t 看图", "send", {"text": "看图"}),
    ("--vision 描述图片", "send", {"text": "描述图片"}),
    # 未知旗标原样保留（当作普通问题的一部分）
    ("-x 自定义", "send", {"text": "-x 自定义"}),

    # new 子命令（含别名）
    ("new", "new", {}),
    ("reset", "new", {}),
    ("重置", "new", {}),
    ("新会话", "new", {}),

    # list：session/状态 已合并进 list
    ("list", "list", {}),
    ("列表", "list", {}),
    ("session", "list", {}),
    ("状态", "list", {}),
    ("sessions", "list", {}),
    ("会话列表", "list", {}),

    # list 带 id / 模式 → 直接切换
    ("list 2", "switch", {"local_id": 2}),
    ("列表 3", "switch", {"local_id": 3}),
    ("session 1", "switch", {"local_id": 1}),
    ("list 识图", "switch", {"mode": "vision"}),

    # switch：按本地 id / 模式；无参或参数无效 → 显示列表
    ("switch", "switch", {}),
    ("切换", "switch", {}),
    ("switch 2", "switch", {"local_id": 2}),
    ("切换 1", "switch", {"local_id": 1}),
    ("switch 识图", "switch", {"mode": "vision"}),
    ("switch 普通", "switch", {"mode": "normal"}),
    ("switch abc", "switch", {}),
    ("switch 10", "switch", {"local_id": 10}),

    # help
    ("help", "help", {}),
    ("帮助", "help", {}),
]

def main():
    failed = 0
    for raw, exp_action, exp_payload in CASES:
        action, payload = parse_ais_command(raw)
        ok = action == exp_action and payload == exp_payload
        if not ok:
            failed += 1
            print(f"FAIL: {raw!r} -> ({action!r}, {payload!r}), expected ({exp_action!r}, {exp_payload!r})")
    if failed:
        print(f"COMMAND PARSER: {failed}/{len(CASES)} FAILED")
        sys.exit(1)
    print(f"ALL {len(CASES)} PARSER TESTS PASSED")

if __name__ == "__main__":
    main()
