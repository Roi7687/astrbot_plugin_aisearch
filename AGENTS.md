# astrbot_plugin_aisearch 开发文档

> **插件名称**：AI搜索  
> **版本**：v2.0.0  
> **作者**：Roi  
> **许可证**：AGPL-3.0  
> **仓库地址**：https://github.com/Roi7687/astrbot_plugin_aisearch  

---

## 一、项目概述

这是一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件，通过自动化操控 DeepSeek Chat 网页端，实现**联网 AI 搜索**与**识图模式**功能。用户在聊天中发送 `/ais <关键词>`（或 @机器人 + 图片），插件在后台通过反检测无头浏览器（cloakbrowser）访问 DeepSeek Chat，获取 AI 结果后以 Markdown 格式返回给用户。

核心思路：**不调用 API，而是直接自动化操控浏览器**，利用 DeepSeek Chat 自带的联网搜索与识图能力。

**v2.0.0 变更**：取消单轮 / 多轮区分，统一为 `/ais`；会话持久化 + 空闲自动销毁 + 主动通知；新增识图模式（@机器人 + 图片）、会话查看 / 重置 / 切换功能。

---

## 二、项目结构

```
astrbot_plugin_aisearch/
├── core/                      # 核心业务模块
│   ├── __init__.py
│   ├── config.py              # 全局配置：路径常量、会话/识图参数 & 自定义异常
│   ├── command_parser.py      # /ais 指令参数解析器（纯函数，可单测）
│   ├── session_core.py        # 统一持久化会话核心（普通/识图双槽位）
│   └── login_core.py          # 登录模块：微信扫码登录 DeepSeek
├── tests/                     # 单元测试（mock 掉浏览器依赖）
│   └── test_session_core_mock.py
├── __init__.py                # 包初始化文件（空）
├── main.py                    # 插件主入口：命令注册、异步调度、主动通知
├── auth_state.json            # 持久化存储的登录凭证（cookies/localStorage）
├── conversations.json         # 会话元数据持久化（会话 ID / URL / 消息数）
├── metadata.yaml              # AstrBot 插件元数据
├── requirements.txt           # Python 依赖清单
├── README.md                  # 项目说明
├── LICENSE                    # AGPL-3.0 许可证
└── AGENTS.md                  # 本文档 — 开发文档
```

---

## 三、模块详解

### 3.1 `core/config.py` — 全局配置

| 配置项 | 说明 |
|--------|------|
| `PLUGIN_DIR` / `STATE_FILE` / `QR_FILE` | 插件目录、凭证、二维码临时文件路径 |
| `CONVERSATIONS_FILE` | 会话元数据持久化路径（conversations.json） |
| `IDLE_TIMEOUT_SECONDS` | 会话空闲自动销毁时间（默认 300 秒） |
| `MAX_IMAGE_BYTES` | 单张图片大小上限（超过自动压缩） |
| `DEFAULT_VISION_PROMPT` | 仅发图不带文字时的默认提问 |
| `VISION_WELCOME_TEXT` | 识图模式新会话欢迎语（用于验证切换成功） |
| `MODE_NORMAL` / `MODE_VISION` | 会话模式常量（normal / vision） |
| `AuthError` / `ConversationError` | 自定义异常 |

### 3.2 `core/command_parser.py` — 指令解析器（纯函数）

| 函数 | 说明 |
|------|------|
| `parse_ais_command(raw)` | 解析 `/ais` 后的参数，返回 (action, payload) |

**返回的 action**：`usage` / `help` / `send` / `new` / `session` / `list` / `switch`

**解析规则**：
- 开头的 `-t` / `-v` 旗标（可组合，如 `-v -t`）
- 首个 token 命中子命令关键词表（含中文别名：重置/状态/列表/切换/帮助）→ 子命令
- 其余情况 → send（`-v` 时 mode=vision，否则由调用方决定当前模式）
- 其余 token 原样拼接为提问文本

### 3.3 `core/session_core.py` — 统一会话核心（全异步）

| 类 / 方法 | 说明 |
|-----------|------|
| `Conversation` | 会话元数据 dataclass（mode / session_id / url / message_count / created_at / last_active / destroyed） |
| `DeepSeekSessionCore` | 统一会话核心 |
| `send_message(mode, text, thinking)` | 在指定模式会话中发送文本（无会话自动创建） |
| `send_image_message(mode, text, paths, thinking)` | 上传图片 + 发送提问（识图会话） |
| `ensure_conversation(mode)` | 确保会话存在并切换到它 |
| `switch_conversation(mode)` | 切换会话（不存在则创建） |
| `reset_conversation(mode)` | 重置：服务器端删除旧会话 + 开启同模式新会话 |
| `destroy_conversation(mode)` | 销毁会话（空闲超时调用），返回是否真有会话被销毁 |
| `conversation_summary / list_summary` | 会话信息展示数据 |
| `close()` | 关闭浏览器（插件卸载 / 登录刷新） |

**设计要点**：
- **双槽位**：普通（normal）与识图（vision）各一个持久会话，互不干扰
- **串行化**：所有页面操作经 `asyncio.Lock` 互斥，避免并发命令竞态
- **惰性创建**：首次使用时才启动浏览器 / 开启新对话
- **识图模式切换**：新对话页点击「识图模式」入口（叶节点文本匹配），以欢迎语「使用识图模式开始对话」或激活态类名验证
- **图片上传**：Playwright `set_input_files` 注入 `input[type=file]`，以 `img[src^="blob:"]` 缩略图出现作为上传成功标志
- **答案等待**：文本稳定检测（连续 9 秒无变化），保留引用角标清理与 HTML→Markdown 转换
- **会话删除**：`POST /api/v0/chat_session/delete`（Bearer 令牌嗅探），新旧 URL 格式（`/a/chat/s/<uuid>` 与 `/s/<id>`）均兼容
- **元数据持久化**：每次变更写入 conversations.json，重启后可切回未销毁会话

### 3.4 `main.py` — 插件主入口

| 类 / 方法 | 说明 |
|-----------|------|
| `CloakSearchPlugin` | 插件主类 |
| `ai_search_command()` | `/ais` 统一指令（send/new/session/list/switch/help） |
| `on_image_message()` | `@filter.event_message_type(ALL)` 图片消息处理：@机器人 + 图片（或私聊图片）→ 识图会话 |
| `_arm_idle_timer()` / `_idle_waiter()` | 每会话独立空闲计时器，超时销毁 + 主动通知 |
| `_record_notice_source()` / `_notify()` | 记录消息来源；主动推送（QQ 官方走 bot API，其余走 `context.send_message`） |
| `_collect_images()` | 兼容新旧 AstrBot API 提取图片组件 |
| `_prepare_image_paths()` | 图片 → 本地路径（自动下载、PIL 压缩） |
| `_build_keyboard()` / `_try_send_with_keyboard()` | QQ 官方平台键盘按钮消息（其余平台自动回退纯文本） |
| `login_command()` | `/cloak登录`：后台线程扫码登录，成功后重置浏览器内核 |

**命令列表（v2.0.0）**：

| 命令 | 说明 |
|------|------|
| `/ais <问题>` | 当前会话提问（无会话自动创建） |
| `/ais -t <问题>` | 深度思考 |
| `/ais -v <问题>` | 切到识图会话提问 |
| `/ais new`（reset/重置） | 重置会话 |
| `/ais session`（状态） | 查看当前会话 |
| `/ais list`（列表） | 查看全部会话 |
| `/ais switch [识图/普通]` | 切换会话 |
| `/ais help`（帮助） | 帮助 |
| `/cloak登录` | 微信扫码登录 |

**主动通知策略**：
- 命令触发的状态变化（创建/重置/切换）：在命令回复中带前缀说明
- 空闲超时销毁：向该会话最后使用的聊天窗口**主动推送**（QQ 官方平台用 `post_group_message / post_c2c_message`，其余平台用 `context.send_message(unified_msg_origin, MessageChain)`）

### 3.5 `core/login_core.py` — 登录模块

与 v1.x 相同，二维码容器选择器改为**兜底链**（`#wxLogin` → `.ds-sign-in-with-wechat-block` → 二维码 img → 「微信扫码登录」文本向上找容器），降低网页改版导致的登录失效风险。

---

## 四、技术栈

| 技术 | 版本 | 用途 |
|------|------|------|
| Python | 3.x | 主语言（async/await） |
| AstrBot API | — | 插件框架（`Star`, `Context`, `AstrMessageEvent`, `filter.event_message_type`） |
| cloakbrowser | 0.3.30 | 反检测无头浏览器（`launch_async` Async API） |
| playwright | 1.60.0 | 底层浏览器自动化引擎 |
| markdownify | 0.14.1 | HTML → Markdown |
| httpx | 0.28.1 | 图片下载等 HTTP 客户端 |
| Pillow | ≥11.2.1（可选，AstrBot 核心自带） | 图片压缩（缺失时跳过压缩） |

---

## 五、数据流

### 文本提问（/ais）

```
用户发送 /ais 问题
       │
       ▼
  ai_search_command() → parse_ais_command()
       │  send: mode = -v ? vision : current_mode
       ▼
  DeepSeekSessionCore.send_message(mode, text, thinking)
       │
       ├─ 无会话/已销毁？ ──→ _create_conversation(mode)
       │                        ├─ _open_new_chat()（新对话页）
       │                        └─ vision？ _switch_to_vision()（点击识图模式）
       │
       ├─ 已有会话？ ──→ _goto_session(conv.url)
       │
       ├─ _toggle_thinking() + 输入 + Enter/按钮发送
       ├─ 文本稳定检测（9s 无变化）→ HTML → Markdown
       └─ _mark_active()（消息数+1，刷新空闲计时）
       │
       ▼
  返回结果（QQ 官方附带键盘按钮）
       │
       ▼
  _arm_idle_timer(mode) → 300s 无活动 → destroy + 主动通知
```

### 识图（@机器人 + 图片）

```
群聊 @机器人 + 图片（或私聊图片）
       │
       ▼
  on_image_message()（EventMessageType.ALL）
       ├─ 无图片 / 未@ → 直接返回，不拦截
       ├─ 命中 → event.stop_event()（阻止默认 LLM 响应）
       ▼
  _prepare_image_paths()（convert_to_file_path / 下载 / PIL 压缩）
       ▼
  send_image_message(vision, text, paths)
       ├─ 创建/切换到识图会话
       ├─ set_input_files 上传 → 等 blob 缩略图
       └─ 输入文字（无则默认提示词）→ 发送 → 等待答案
       ▼
  返回结果 + 键盘按钮
```

---

## 六、当前开发进度

### ✅ v2.0.0 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 统一 /ais 指令 | ✅ | 取消 /ais 与 /mais 区分，子命令模式（new/session/list/switch/help） |
| 持久化会话 + 空闲自动销毁 | ✅ | 每模式独立计时器，超时服务器端删会话 + 主动通知 |
| 会话状态查看 | ✅ | /ais session、/ais list |
| 会话重置 | ✅ | /ais new：关闭旧会话开启新会话 |
| 会话切换 | ✅ | /ais switch 识图/普通；/ais -v 快捷切换 |
| 识图模式 | ✅ | @机器人+图片 / 私聊图片 → 识图会话；set_input_files 上传 + blob 缩略图校验 |
| 图片自动压缩 | ✅ | Pillow 可选依赖，1024px / JPEG q70 |
| 主动通知 | ✅ | 超时销毁主动推送（QQ 官方 bot API + 通用 context.send_message 双通道） |
| 会话元数据持久化 | ✅ | conversations.json，重启可切回 |
| 图片消息不拦截普通消息 | ✅ | 无图片或未@时直接放行 |
| 会话 ID 兼容新旧 URL | ✅ | /a/chat/s/<uuid> 与 /s/<id> |
| 单元测试 | ✅ | 指令解析 23 用例 + 会话核心 mock 测试（tests/） |

### ⚠️ 已知问题 / 待改进

- **UI 依赖**：识图模式入口、上传输入框等选择器基于 2026-06 上线的 DeepSeek 网页版 UI，改版后需更新 `session_core.py`
- **QQ 官方平台主动消息**：官方 API 主动推送依赖 `msg_id=None`，个别平台策略可能限流
- **未实机验证**：识图全流程（登录态下）需在真实环境验证，选择器如有出入按日志调整
- 多图片消息的图片顺序与文字绑定关系待实测确认

### 🔮 后续开发建议

- 支持「专家模式」等其他一级对话模式
- 会话列表持久化到侧边栏（DeepSeek 网页端会话列表）的同步清理
- 多图分次发送时的进度提示
- 将选择器抽离为可配置项，降低网页改版维护成本

---

## 七、安装与使用

1. 首次使用前，发送 `/cloak登录` 微信扫码登录
2. **提问**：`/ais [-t] <问题>`；**识图**：`/ais -v <问题>` 或群聊 @机器人 + 图片
3. **会话管理**：`/ais session` 查看状态；`/ais new` 重置；`/ais switch` 切换
4. 示例：
   - `/ais 今天有什么大新闻`
   - `/ais -t 解释量子纠缠的原理`（深度思考）
   - 群里 @机器人 发一张截图 → 自动识图分析
   - `/ais switch 识图` → `/ais 这张图里有什么公式`
5. 空闲 300 秒无活动，会话自动关闭并收到通知

---

*文档更新日期：2026-08-29（v2.0.0）*
