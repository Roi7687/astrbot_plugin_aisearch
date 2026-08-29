# astrbot_plugin_aisearch 开发文档

> **插件名称**：AI搜索  
> **版本**：v2.2.1  
> **作者**：Roi  
> **许可证**：AGPL-3.0  
> **仓库地址**：https://github.com/Roi7687/astrbot_plugin_aisearch  

---

## 一、项目概述

这是一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件，通过自动化操控 DeepSeek Chat 网页端，实现**联网 AI 搜索**与**识图模式**功能。用户在聊天中发送 `/ais <关键词>`（或 @机器人 + 图片），插件在后台通过反检测无头浏览器（cloakbrowser）访问 DeepSeek Chat，获取 AI 结果后以 Markdown 格式返回给用户。

核心思路：**不调用 API，而是直接自动化操控浏览器**，利用 DeepSeek Chat 自带的联网搜索与识图能力。

**v2.0.0 变更**：取消单轮 / 多轮区分，统一为 `/ais`；会话持久化 + 空闲自动销毁 + 主动通知；新增识图模式（@机器人 + 图片）、会话查看 / 重置 / 切换功能。

**v2.1.0 变更**：会话模型由「双槽位」升级为「**多会话 + 本地 id**」——`/ais session` 与 `/ais list` 合并为 `/ais list`；每个会话由本地递增 id（1、2、3...）标识，`/ais list` 展示带 id 的会话列表，`/ais switch <id>` / `/ais list <id>` 按 id 切换；`/ais new` 不再销毁旧会话，旧会话保留在列表中可随时切回；已关闭的会话按 id 切换时自动重建；空闲计时器与主动通知改为按会话 id 索引。

**v2.1.1 变更**：修复 Linux 服务器（英文系统）上识图模式报「未找到「识图模式」入口」的问题——浏览器启动/上下文强制 `locale=zh-CN`（`--lang` + Playwright context locale），保证 DeepSeek 恒为中文 UI；识图入口查找升级为多策略（中/英文精确标签 → 模糊文本 → 展开「+ / 更多」菜单），失败时保存 `vision_debug.png` 截图与页面文本留档。

**v2.2.0 变更**：① **自动触发**——@机器人（或私聊）直接发文字/图片即可自动进入 AI 搜索对话，无需 `/ais` 指令；仅切换/新建会话时使用指令（`AUTO_TRIGGER` 可关）；② **固定启用深度思考 + 联网搜索**——每次发送前自动确保两个模式开关开启（幂等检测激活态，`ALWAYS_DEEP_THINK` / `ALWAYS_WEB_SEARCH` 可配）；③ **修复连续对话返回旧结果**——回答定位从「最后一回复块」改为「发送前回复块计数基线 + 定位新回复块」，避免上一轮回复或用户消息被误判为最新回答。

**v2.2.1 变更**：修复慢速网络下识图「文本已发送但图片未上传完」导致 AI 识别不到图片的问题——上传等待从「blob 缩略图出现」升级为**双路上传完成确认**（① 监听上传网络请求 POST/PUT 含 upload/file 全部响应；② DOM 上传中标志消失兜底），确认完成才发送提问；同时**生成完成确认**——文本稳定后还需页面无「生成中」标志（停止按钮/流式光标/思考中指示，排除回答正文与隐藏元素）才返回结果，避免深度思考/长回答中间停顿 >9s 时提前返回半截答案；总等待上限放宽至 300 秒。

---

## 二、项目结构

```
astrbot_plugin_aisearch/
├── core/                      # 核心业务模块
│   ├── __init__.py
│   ├── config.py              # 全局配置：路径常量、会话/识图参数 & 自定义异常
│   ├── command_parser.py      # /ais 指令参数解析器（纯函数，可单测）
│   ├── session_core.py        # 统一持久化会话核心（多会话 + 本地 id 计数）
│   └── login_core.py          # 登录模块：微信扫码登录 DeepSeek
├── tests/                     # 单元测试（mock 掉浏览器依赖）
│   ├── test_command_parser.py      # 指令解析 33 用例
│   └── test_session_core_mock.py   # 会话核心 mock 测试（含旧数据迁移）
├── __init__.py                # 包初始化文件（空）
├── main.py                    # 插件主入口：命令注册、异步调度、主动通知
├── auth_state.json            # 持久化存储的登录凭证（cookies/localStorage）
├── conversations.json         # 会话元数据持久化（本地 id / 会话 ID / URL / 消息数）
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
| `ALWAYS_DEEP_THINK` / `ALWAYS_WEB_SEARCH` | 固定开启「深度思考」/「联网搜索」（默认 True） |
| `AUTO_TRIGGER` | 自动触发：@机器人/私聊直接提问无需 /ais（默认 True） |
| `DEFAULT_VISION_PROMPT` | 仅发图不带文字时的默认提问 |
| `VISION_WELCOME_TEXT` | 识图模式新会话欢迎语（用于验证切换成功） |
| `MODE_NORMAL` / `MODE_VISION` | 会话模式常量（normal / vision） |
| `AuthError` / `ConversationError` | 自定义异常 |

### 3.2 `core/command_parser.py` — 指令解析器（纯函数）

| 函数 | 说明 |
|------|------|
| `parse_ais_command(raw)` | 解析 `/ais` 后的参数，返回 (action, payload) |

**返回的 action**：`usage` / `help` / `send` / `new` / `list` / `switch`

**解析规则**：
- 开头的 `-t` / `-v` 旗标（可组合，如 `-v -t`）
- 首个 token 命中子命令关键词表（含中文别名：重置/状态/列表/切换/帮助）→ 子命令
  - `session` / `状态` / `会话` / `info` / `查看` 均为 `list` 的别名（已合并）
  - `list` / `switch` 后跟数字 → `{"local_id": int}`（按本地 id 切换）
  - `list` / `switch` 后跟模式别名（识图/普通/图片/文本...）→ `{"mode": "vision"|"normal"}`
  - `switch` 无参或参数无效 → `{"local_id": None, "mode": None}`（调用方显示列表）
- 其余情况 → send（`-v` 时 mode=vision，否则由调用方决定当前模式）
- 其余 token 原样拼接为提问文本

### 3.3 `core/session_core.py` — 统一会话核心（全异步，多会话 + 本地 id）

| 类 / 方法 | 说明 |
|-----------|------|
| `Conversation` | 会话元数据 dataclass（local_id / mode / session_id / url / message_count / created_at / last_active / destroyed） |
| `DeepSeekSessionCore` | 统一会话核心：`conversations: dict[local_id, Conversation]` + `next_id` 计数器 + `current_id` |
| `current_conversation` / `current_mode` | 当前会话 / 当前会话模式（只读属性） |
| `send_message(text, thinking, mode=None)` | 在当前会话（或指定模式会话）发送文本，返回 `(是否新建, 回答文本)` |
| `send_image_message(text, paths, thinking)` | 识图会话上传图片 + 发送提问，返回 `(是否新建, 回答文本)` |
| `ensure_mode(mode)` | 确保当前会话为指定模式：复用该模式最近活跃会话或新建，返回 `(conv, 是否新建)` |
| `switch_conversation(local_id)` | 按本地 id 切换；id 不存在报错，已销毁则同模式重建（id 保留），返回 `(conv, 是否重建)` |
| `new_conversation(mode=None)` | 创建新会话（分配新 id）并设为当前；**不销毁旧会话** |
| `destroy_conversation(local_id)` | 销毁会话（空闲超时调用），返回是否真有会话被销毁 |
| `conversation_summary(local_id)` / `list_summary()` | 单个 / 全部会话展示数据（按 id 升序） |
| `close()` | 关闭浏览器（插件卸载 / 登录刷新） |

**设计要点**：
- **多会话 + 本地 id**：本地维护会话列表（普通/识图混合），`next_id` 本地递增计数（1、2、3...），持久化于 conversations.json；重启后 id 连续不重复
- **串行化**：所有页面操作经 `asyncio.Lock` 互斥，避免并发命令竞态
- **惰性创建**：首次使用时才启动浏览器 / 开启新对话
- **模式复用**：`ensure_mode('vision')` 优先复用该模式最近活跃的未销毁会话，无则新建
- **已销毁会话重建**：按 id 切换到 `destroyed=True` 的会话时，同模式重建并保留原 id
- **识图模式切换**：新对话页点击「识图模式」入口（精确标签 → 模糊文本 → 展开「+ / 更多」菜单多策略查找，中/英文标签均支持），以欢迎语「使用识图模式开始对话」或激活态类名验证；查找失败时保存 vision_debug.png 截图留档
- **图片上传**：Playwright `set_input_files` 注入 `input[type=file]` → 等 blob 缩略图出现 → **双路上传完成确认**（`_wait_upload_finished`：① 监听上传网络请求 POST/PUT 含 upload/file 全部响应；② DOM 上传中标志消失兜底），确认完成才发送提问，避免慢速网络下「文本已发送但图片未传完」
- **固定启用模式开关**：`_ensure_toggle_on(label)` 幂等开启「深度思考」/「联网搜索」（检测 active/selected/aria-checked 激活态，已开启不重复点击，避免切换类 bug）
- **答案等待**：发送前记录助手回复块数量为基线 → 等待块数超过基线定位「新回复」→ 对新回复做文本稳定检测（连续 9 秒无变化）**且** `_is_generation_finished` 确认页面无生成中标志（停止按钮/流式光标/思考中指示，排除回答正文与隐藏元素）才算完成（总上限 300 秒）；避免旧回复/用户消息误判与长停顿提前返回；保留引用角标清理与 HTML→Markdown 转换
- **会话删除**：`POST /api/v0/chat_session/delete`（Bearer 令牌嗅探），新旧 URL 格式（`/a/chat/s/<uuid>` 与 `/s/<id>`）均兼容
- **元数据持久化**：每次变更写入 conversations.json（`{"next_id", "current_id", "conversations"}` 新格式；旧版 `{mode: {...}}` 双槽位格式自动迁移为 id 1=normal、2=vision）

### 3.4 `main.py` — 插件主入口

| 类 / 方法 | 说明 |
|-----------|------|
| `CloakSearchPlugin` | 插件主类 |
| `ai_search_command()` | `/ais` 统一指令（send/new/session/list/switch/help） |
| `on_image_message()` | `@filter.event_message_type(ALL)` 图片消息处理：@机器人 + 图片（或私聊图片）→ 识图会话 |
| `on_auto_message()` | `@filter.event_message_type(ALL)` 自动触发：@机器人/私聊文本（非指令、非图片）→ 直接 AI 搜索对话，无需 /ais |
| `_respond()` | 统一回复：记录通知来源 + 刷新空闲计时 + 「已开启新会话」前缀 + 键盘按钮；返回空串表示已发送 |
| `_arm_idle_timer()` / `_idle_waiter()` | 每会话独立空闲计时器，超时销毁 + 主动通知 |
| `_record_notice_source()` / `_notify()` | 记录消息来源；主动推送（QQ 官方走 bot API，其余走 `context.send_message`） |
| `_collect_images()` | 兼容新旧 AstrBot API 提取图片组件 |
| `_prepare_image_paths()` | 图片 → 本地路径（自动下载、PIL 压缩） |
| `_build_keyboard()` / `_try_send_with_keyboard()` | QQ 官方平台键盘按钮消息（其余平台自动回退纯文本） |
| `login_command()` | `/cloak登录`：后台线程扫码登录，成功后重置浏览器内核 |

**命令列表（v2.1.0）**：

| 命令 | 说明 |
|------|------|
| `/ais <问题>` | 当前会话提问（无会话自动创建） |
| `/ais -t <问题>` | 深度思考 |
| `/ais -v <问题>` | 切到识图会话提问 |
| `/ais new`（reset/重置） | 开启新会话（旧会话保留，可按 id 切回） |
| `/ais list`（列表/状态/session） | 查看全部会话（带本地 id，👉 为当前） |
| `/ais switch <id>`（切换） | 按本地 id 切换会话（已关闭的自动重建） |
| `/ais switch 识图/普通` | 按模式切换（复用该模式最近会话，无则新建） |
| `/ais list <id>` | 列表便捷切换（等同于 /ais switch <id>） |
| `/ais help`（帮助） | 帮助 |
| `/cloak登录` | 微信扫码登录 |

**主动通知策略**：
- 命令触发的状态变化（创建/切换）：在命令回复中带前缀说明（含会话 #id）
- 空闲超时销毁：向该会话最后使用的聊天窗口**主动推送**（QQ 官方平台用 `post_group_message / post_c2c_message`，其余平台用 `context.send_message(unified_msg_origin, MessageChain)`）
- 计时器与通知来源均按会话 **local_id** 索引（`_timer_tasks` / `_notice_sources` 的 key）

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

### 自动触发（@机器人/私聊文本，v2.2.0 新增）

```
用户 @机器人 发送文字（或私聊直接发送）
       │
       ▼
  on_auto_message()（EventMessageType.ALL）
       ├─ AUTO_TRIGGER 关闭 / 事件已停止 → 返回
       ├─ 空文本 / 以 / 开头（指令） → 返回
       ├─ 含图片 → 返回（交给 on_image_message 识图）
       ├─ 群聊未@未唤醒 → 返回（不拦截正常聊天）
       └─ 命中 → event.stop_event()（阻止默认 LLM 响应）
       ▼
  session.send_message(text) → (created, result)
       ▼
  _respond()：记录通知来源 + 刷新空闲计时 + 前缀 + 键盘按钮 → 回复
```

### 文本提问（/ais 或自动触发）

```
用户发送提问（/ais 问题 或 @机器人 问题）
       │
       ▼
  ai_search_command() / on_auto_message()
       │  send: mode = -v ? "vision" : None（保持当前会话）
       ▼
  DeepSeekSessionCore.send_message(text, thinking, mode) → (created, result)
       │
       ├─ ensure_current_locked(mode)：
       │    ├─ 当前会话匹配？ ──→ _goto_session(conv.url)
       │    ├─ mode 指定？ ──→ 复用该模式最近活跃会话 / 新建
       │    └─ 无当前会话？ ──→ _create_conversation(normal) + _register(分配新 id)
       │                          ├─ _open_new_chat()（新对话页）
       │                          └─ vision？ _switch_to_vision()（点击识图模式）
       │
       ├─ _ensure_toggle_on(深度思考/联网搜索)（固定开启，幂等）
       ├─ 输入 + Enter/按钮发送
       ├─ 等待新回复块（基线计数）→ 文本稳定检测（9s 无变化）→ HTML → Markdown
       └─ _mark_active(conv)（消息数+1，刷新空闲计时，回写真实 URL/id）
       │
       ▼
  _respond()（created 时前缀「🟢 已开启新的…（会话 #id）」；QQ 官方附带键盘按钮）
       │
       ▼
  _arm_idle_timer(local_id) → 300s 无活动 → destroy + 主动通知
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
  send_image_message(text, paths) → (created, result)
       ├─ ensure_current_locked(vision)：复用最近识图会话或新建（分配新 id）
       ├─ set_input_files 上传 → 等 blob 缩略图
       └─ 输入文字（无则默认提示词）→ 发送 → 等待答案
       ▼
  _respond() → 返回结果 + 键盘按钮
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

### ✅ v2.1.0 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 多会话 + 本地 id | ✅ | 会话列表按本地递增 id（1、2、3...）索引，conversations.json 持久化 next_id，重启 id 连续 |
| 合并 session/list | ✅ | /ais session（状态/会话/info）全部合并为 /ais list，统一展示带 id 的会话列表（👉 为当前） |
| 按 id 切换 | ✅ | /ais switch <id> 或 /ais list <id>；id 不存在友好报错 |
| 已关闭会话重建 | ✅ | 按 id 切换已销毁会话时同模式重建并保留原 id（/ais list 中标注「⏸ 已关闭」） |
| /ais new 保留旧会话 | ✅ | 新建会话不再销毁旧会话，旧会话可随时按 id 切回 |
| 按模式切换 | ✅ | /ais switch 识图/普通：复用该模式最近活跃会话，无则新建 |
| 修复 list 报错 | ✅ | 补全缺失的 _format_session_list 展示逻辑（此前引用未定义方法导致报错） |
| 旧数据迁移 | ✅ | 旧版 {mode: {...}} 双槽位 conversations.json 自动迁移为 id 1=normal、2=vision |
| 单元测试 | ✅ | 指令解析 33 用例（含合并/切换语法）+ 会话核心 mock 测试（含迁移用例） |

### ✅ v2.1.1 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 修复 Linux 识图入口找不到 | ✅ | 浏览器强制 `locale=zh-CN`（launch `--lang` + context locale），DeepSeek 恒为中文 UI，不再受服务器系统语言影响 |
| 识图入口多策略查找 | ✅ | 中/英文精确标签 → 模糊文本（带描述的入口）→ 展开「+ / 更多」菜单；点击优先最深叶节点 |
| 失败现场留档 | ✅ | 三次查找失败后保存 `vision_debug.png` 截图 + 页面文本片段到插件目录，日志含完整提示 |
| 新对话页就绪等待 | ✅ | `_open_new_chat` 等待 textarea 可见，慢速服务器下不再因页面未渲染完而误报 |
| 单元测试 | ✅ | 33 用例 + 会话核心 mock 测试全部通过（测试运行时会临时隔离真实 conversations.json） |

### ✅ v2.2.0 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 自动触发 | ✅ | @机器人/私聊文本自动进入 AI 搜索对话（on_auto_message，AUTO_TRIGGER 可关）；跳过指令与图片消息；未@群聊消息不拦截 |
| 固定启用深度思考+联网搜索 | ✅ | `_ensure_toggle_on` 幂等开启（检测激活态），ALWAYS_DEEP_THINK / ALWAYS_WEB_SEARCH 可配 |
| 修复连续对话返回旧结果 | ✅ | 回答定位改为「发送前回复块计数基线 + 定位新回复块」，普通/识图模式共用修复 |
| 统一回复逻辑 | ✅ | `_respond()` 收敛三处重复的回复代码（指令/自动触发/识图） |
| 帮助文案 | ✅ | /ais help 与 usage 同步更新自动触发说明 |

### ✅ v2.2.1 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 上传完成双路确认 | ✅ | 缩略图出现后监听上传网络请求（POST/PUT 含 upload/file）全部响应 + DOM 上传中标志消失兜底，确认后才发送提问（`_wait_upload_finished` / `_wait_upload_network_done` / `_wait_no_upload_marker`） |
| 生成完成确认 | ✅ | 文本稳定后还需 `_is_generation_finished`（无停止按钮/流式光标/思考中指示，排除回答正文与隐藏元素），深度思考/长回答停顿不再提前返回半截答案 |
| 慢速网络适配 | ✅ | 上传等待 90s + 回答等待总上限 300s |

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
2. **提问**：@机器人 直接发消息（或私聊直接发）即可自动提问，无需指令；`/ais <问题>` 亦可
3. **识图**：@机器人 + 图片（或私聊发图）自动进入识图会话
4. **会话管理**（仅切换时需要指令）：`/ais list` 查看全部会话（带本地 id）；`/ais switch <id>` 按 id 切换；`/ais new` 开启新会话（旧会话保留）
5. 示例：
   - @机器人 今天有什么大新闻（自动触发，深度思考+联网搜索固定开启）
   - 群里 @机器人 发一张截图 → 自动识图分析
   - `/ais list` → `/ais switch 2` → @机器人 这张图里有什么公式
6. 空闲 300 秒无活动，会话自动关闭并收到通知；已关闭的会话按 id 切换时自动重建

---

*文档更新日期：2026-08-29（v2.2.1）*
