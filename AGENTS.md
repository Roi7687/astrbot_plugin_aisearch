# astrbot_plugin_aisearch 开发文档

> **插件名称**：AI搜索  
> **版本**：v2.2.12  
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

**v2.2.2 变更**：**减少冗余通知/防刷屏**——① 取消会话空闲超时的主动推送通知（`_notify` / `_record_notice_source` / `_notice_sources` 全部移除，超时静默销毁，仅留日志）；② 自动触发/识图处理器新增**自我消息过滤**（`_is_self_message`：`get_sender_id() == get_self_id()` 时跳过），防止部分平台回传机器人自身消息导致自我回复循环刷屏。

**v2.2.3 变更**：**修复自动触发拦截指令**——AstrBot 的 waking_check 阶段会把指令前缀（默认 `/`）从 `message_str` 剥离（`/cloak登录` 到达插件时已是 `cloak登录`），导致 `on_auto_message` 的 `startswith("/")` 检查失效：指令文本被当作提问发送给 DeepSeek（报「未检测到登录凭证」），且 `stop_event()` 会阻断后续指令 handler（`StarRequestSubStage` 遇 `is_stopped` 即 break），`/cloak登录`、`/ais` 等指令全部失效。新增 `_is_command_message`：检查 `event.get_extra("activated_handlers")` 中是否存在带 Command 类 filter（CommandFilter / CommandGroupFilter，含其他插件与内置指令）的 handler，命中即让路。

**v2.2.4 变更**：**修复模式开关检查报错与卡顿**——cloakbrowser 的 humanize（isolated-world resolver）不支持 `.filter(has_text=...)` 链式 locator，且「联网搜索」按钮在当前 UI 中无法定位导致每次提问 `wait_for` 15 秒超时。重写 `_ensure_toggle_on`：改用**纯 CSS 定位 + `all_inner_texts` 过滤 + 尾部 `.nth()`**（`_locate_by_text`），兼容 humanize；找不到开关时 **2 秒内快速降级**（DeepSeek 网页端默认/记忆开启，不影响使用），警告日志仅首次输出一次（`_toggle_warned` 缓存）；同步修复 `_type_and_send` 发送按钮兜底中的同类 `.filter()` 用法。

**v2.2.5 变更**：**移除「固定启用深度思考/联网搜索」开关检查**——开关在 DeepSeek 当前网页 UI 中无法稳定定位（`.ds-toggle-button` 结构不匹配），检查始终落空且徒增日志噪音。删除 `_ensure_toggle_on` / `ALWAYS_DEEP_THINK` / `ALWAYS_WEB_SEARCH`，不再自动操作开关，深度思考与联网搜索跟随 DeepSeek 网页端设置（默认开启）；`-t` 旗标保留解析（兼容）但无实际效果，键盘按钮与帮助文案同步清理。

**v2.2.6 变更**：**修复「等待 AI 回答超时」**——慢速网络 + 深度思考 + 联网搜索下，DeepSeek 可能超过 60 秒才开始输出，原 60 秒「等待新回复块」超时过短；且若 Enter 发送失败（消息未发出）会干等超时。修复：① 等待新回复超时放宽至 **180 秒**；② 等待期间每 5 秒检查输入框是否清空，未清空则补点发送按钮，**连续 3 次仍失败提前报「消息发送失败」**；③ 超时前将页面文本片段写入日志留档，便于排查。

**v2.2.7 变更**：**回归简单等待逻辑（修复卡死）**——基线计数 / 生成标志检测 / 补发重试等复杂逻辑在部分环境下导致「读入信息后卡死」或误判。`_send_query` 回归最早版本：发送后等待对话框最后一条回复出现（30 秒，兜底 `.ds-markdown`），随后对最后一条做**文本稳定检测**（连续 9 秒无变化视为完成），保留 300 秒总上限防无限等待；删除 `_is_generation_finished`。**识图上传双路确认（v2.2.1）完整保留**。

**v2.2.8 变更**：**支持引用消息（Reply）中的图片**——QQ 引用（回复）一条含图片的消息时，图片位于 `Reply.chain`（被引用消息段列表）。`_collect_images` 增强：除当前消息图片外，同时收集所有 `Reply.chain` 中的 `Image` 组件（chain 为 None 时防御跳过），引用图片自动进入识图流程；`on_auto_message` 也会自动把「含引用图片的消息」让给识图处理。

**v2.2.9 变更**：**每次传图新建识图会话**——多张图堆在同一个 DeepSeek 识图会话中易导致回复异常。`send_image_message` 改为每次上传都 `_create_conversation(MODE_VISION)` + `_register`（不可调用 `new_conversation()`，其内部持锁会死锁），不再复用旧识图会话；旧会话保留在列表中可按 id 切回，空闲超时自动销毁。

**v2.2.10 变更**：**指令/列表体验清理**——① **`-v` 旗标移除语义**：带图消息（@机器人 + 图片 / 私聊发图）已自动新建识图会话，`/ais -v` 不再切换到识图模式；解析仍剥离 `-v`（避免旗标文本被当问题发送）但无效果，键盘按钮「🖼 识图对话」与 usage/help 文案同步移除；② **`/ais new` 总是新建普通会话**：不再继承上一轮会话的模式（上一轮为识图时新建识图会话没有意义）；③ **`/ais list` 改为紧凑 markdown 表格风格**：一行一个会话（`👉 1  普通对话 ｜ 3 条 ｜ 最后活跃 08-30 17:14`），去掉 # 号与多行提示；④ **已销毁（空闲超时关闭）的会话不再显示在列表中**——用户不会关心，仍可通过 `/ais switch <id>` 切换时自动重建。

**v2.2.11 变更**：**冗余逻辑清理**——① **`-t` 旗标彻底不再识别**（v2.2.5 起已无效果，`-t` 按普通文本处理）；② **删除 `thinking` 参数链**：`send_message` / `send_image_message` / `_send_query` 的 `thinking` 参数已无任何作用（深度思考跟随 DeepSeek 网页端设置），彻底移除；③ **删除 `on_auto_message` 的 `text.startswith("/")` 死代码**（waking_check 已剥离指令前缀，该检查永不命中，`_is_command_message` 已完整覆盖）；④ **`MODE_ALIASES` 移除 `"v"` / `"n"` 单字母别名**（`-v` 时代残留）；⑤ **`/ais list <id>` 便捷切换移除**（与 `/ais switch <id>` 重复，切换统一走 switch）；⑥ **销毁即删除记录**：会话空闲销毁时从 conversations.json 中一并移除该记录（用户无法再访问已销毁会话，且避免文件无限膨胀），加载时自动清理历史 destroyed 数据并立即落盘；销毁当前会话时重置 current_id；`switch_conversation` 删除「已销毁重建」分支（id 不存在直接报错）。

**v2.2.12 变更**：**修复 id 无限增长与空闲销毁误杀**——① **id 计数归 1**：`_register` 分配 id 前、`destroy_conversation` 全部销毁后、`_load_conversations` 加载空文件时，只要**没有任何存活会话**就把 `next_id` 归 1（新会话从 #1 重新开始）；② **页面失效不再残留 destroyed 记录**：`ensure_current_locked` 两处「页面已失效自动重建」路径由「标记 destroyed=True 保留」改为「删除记录后新建」，与 v2.2.11 语义一致；③ **空闲销毁以真实空闲时间为准**：`_idle_waiter` 计时器到点后二次校验 `last_active`——若 sleep 期间会话仍有活动（如一次提问耗时跨越计时终点），按最后活跃重新计时而非销毁；`destroy_conversation` 增加**锁内权威校验**（等待锁期间有新活动则放弃销毁），杜绝「对话中被销毁」。每次提问/切换/新建后 `_arm_idle_timer` 都会重置该会话计时器。

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
│   ├── test_command_parser.py      # 指令解析 35 用例
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
- 开头的 `-v` 兼容旗标（v2.2.10 起无实际效果，解析时剥离避免旗标文本被当问题发送）；`-t` v2.2.11 起彻底不识别（按普通文本处理）
- 首个 token 命中子命令关键词表（含中文别名：重置/状态/列表/切换/帮助）→ 子命令
  - `session` / `状态` / `会话` / `info` / `查看` 均为 `list` 的别名（已合并）
  - `switch` 后跟数字 → `{"local_id": int}`（按本地 id 切换）；`list` 不再接受目标参数（v2.2.11）
  - `switch` 后跟模式别名（识图/普通/图片/文本...）→ `{"mode": "vision"|"normal"}`
  - `switch` 无参或参数无效 → `{"local_id": None, "mode": None}`（调用方显示列表）
- 其余情况 → send，payload 为 `{"text": str}`（当前会话提问）
- 其余 token 原样拼接为提问文本

### 3.3 `core/session_core.py` — 统一会话核心（全异步，多会话 + 本地 id）

| 类 / 方法 | 说明 |
|-----------|------|
| `Conversation` | 会话元数据 dataclass（local_id / mode / session_id / url / message_count / created_at / last_active / destroyed） |
| `DeepSeekSessionCore` | 统一会话核心：`conversations: dict[local_id, Conversation]` + `next_id` 计数器 + `current_id` |
| `current_conversation` / `current_mode` | 当前会话 / 当前会话模式（只读属性） |
| `send_message(text, mode=None)` | 在当前会话（或指定模式会话）发送文本，返回 `(是否新建, 回答文本)` |
| `send_image_message(text, paths)` | 识图会话上传图片 + 发送提问，返回 `(是否新建, 回答文本)` |
| `ensure_mode(mode)` | 确保当前会话为指定模式：复用该模式最近活跃会话或新建，返回 `(conv, 是否新建)` |
| `switch_conversation(local_id)` | 按本地 id 切换；id 不存在（含已销毁被移除）报错，返回 `(conv, 是否重建)` |
| `new_conversation(mode=MODE_NORMAL)` | 创建新会话（分配新 id）并设为当前；**v2.2.10 起总是新建普通会话，不继承当前模式**；不销毁旧会话 |
| `destroy_conversation(local_id)` | 销毁空闲超时的会话：服务器端删除 + 记录移除（v2.2.11）；**锁内校验真实空闲**、全部销毁后 id 归 1（v2.2.12），返回是否真的有会话被销毁 |
| `conversation_summary(local_id)` / `list_summary()` | 单个 / 全部会话展示数据（按 id 升序） |
| `close()` | 关闭浏览器（插件卸载 / 登录刷新） |

**设计要点**：
- **多会话 + 本地 id**：本地维护会话列表（普通/识图混合），`next_id` 本地递增计数（1、2、3...），持久化于 conversations.json；重启后 id 连续不重复；**v2.2.12 起没有任何存活会话时 id 归 1**（`_register` 分配前 / 全部销毁后 / 加载空文件时），新会话从 #1 重新开始
- **串行化**：所有页面操作经 `asyncio.Lock` 互斥，避免并发命令竞态
- **惰性创建**：首次使用时才启动浏览器 / 开启新对话
- **模式复用**：`ensure_mode('vision')` 优先复用该模式最近活跃的未销毁会话，无则新建；**但 `send_image_message` 例外（v2.2.9）：每次传图强制新建识图会话**（多图同会话易致回复异常）
- **销毁即删除记录（v2.2.11，v2.2.12 强化）**：空闲销毁时从 conversations.json 移除该记录（用户无法再访问已销毁会话）；销毁当前会话时重置 current_id；**锁内权威校验真实空闲**——等待锁期间会话有新活动则放弃销毁（v2.2.12，杜绝「对话中被销毁」）；页面失效路径同样删除记录而非标记 destroyed（v2.2.12）；加载时自动清理历史 destroyed 数据并立即落盘；`Conversation.destroyed` 字段仅作历史兼容
- **识图模式切换**：新对话页点击「识图模式」入口（精确标签 → 模糊文本 → 展开「+ / 更多」菜单多策略查找，中/英文标签均支持），以欢迎语「使用识图模式开始对话」或激活态类名验证；查找失败时保存 vision_debug.png 截图留档
- **图片上传**：Playwright `set_input_files` 注入 `input[type=file]` → 等 blob 缩略图出现 → **双路上传完成确认**（`_wait_upload_finished`：① 监听上传网络请求 POST/PUT 含 upload/file 全部响应；② DOM 上传中标志消失兜底），确认完成才发送提问，避免慢速网络下「文本已发送但图片未传完」
- **深度思考/联网搜索**：v2.2.5 起**不自动操作开关**（UI 无法稳定定位，检查徒增噪音），跟随 DeepSeek 网页端设置（默认开启）；`-t` 旗标 v2.2.11 起彻底不识别
- **答案等待**（v2.2.7 回归简单版）：发送后等待对话框最后一条回复出现（30 秒，兜底 `.ds-markdown`），随后对最后一条做**文本稳定检测**（连续 9 秒无变化视为完成，总上限 300 秒防无限等待）；不做回复块计数 / 生成标志 / 补发重试等复杂判断，避免部分环境下卡死；保留引用角标清理与 HTML→Markdown 转换
- **会话删除**：`POST /api/v0/chat_session/delete`（Bearer 令牌嗅探），新旧 URL 格式（`/a/chat/s/<uuid>` 与 `/s/<id>`）均兼容
- **元数据持久化**：每次变更写入 conversations.json（`{"next_id", "current_id", "conversations"}` 新格式；旧版 `{mode: {...}}` 双槽位格式自动迁移为 id 1=normal、2=vision）

### 3.4 `main.py` — 插件主入口

| 类 / 方法 | 说明 |
|-----------|------|
| `CloakSearchPlugin` | 插件主类 |
| `ai_search_command()` | `/ais` 统一指令（send/new/session/list/switch/help） |
| `on_image_message()` | `@filter.event_message_type(ALL)` 图片消息处理：@机器人 + 图片（或私聊图片）→ 识图会话 |
| `on_auto_message()` | `@filter.event_message_type(ALL)` 自动触发：@机器人/私聊文本（非指令、非图片）→ 直接 AI 搜索对话，无需 /ais |
| `_respond()` | 统一回复：刷新空闲计时 + 「已开启新会话」前缀 + 键盘按钮；返回空串表示已发送 |
| `_arm_idle_timer()` / `_idle_waiter()` | 每会话独立空闲计时器：每次提问/切换/新建后重置；到点后**按真实空闲时间（last_active）二次校验**，有新活动则重新计时，超时**静默销毁**（不推送通知，避免刷屏） |
| `_is_self_message()` | 跳过机器人自己发出的消息（部分平台会回传自身消息，防止自我回复循环） |
| `_is_command_message()` | 跳过被 AstrBot 识别为指令的消息（waking_check 已剥离指令前缀，无法靠文本判断；检查 activated_handlers 中带 Command 类 filter 的 handler），防止自动触发拦截 /cloak登录、/ais 等指令 |
| `_collect_images()` | 兼容新旧 AstrBot API 提取图片组件；v2.2.8 起同时收集引用消息（Reply.chain）中的图片 |
| `_prepare_image_paths()` | 图片 → 本地路径（自动下载、PIL 压缩） |
| `_build_keyboard()` / `_try_send_with_keyboard()` | QQ 官方平台键盘按钮消息（其余平台自动回退纯文本） |
| `login_command()` | `/cloak登录`：后台线程扫码登录，成功后重置浏览器内核 |

**命令列表（v2.1.0，v2.2.11 更新）**：

| 命令 | 说明 |
|------|------|
| `/ais <问题>` | 当前会话提问（无会话自动创建） |
| `/ais new`（reset/重置） | 开启新的**普通会话**（旧会话保留，可按 id 切回） |
| `/ais list`（列表/状态/session） | 查看会话列表（带本地 id，👉 为当前；**已关闭的不显示**） |
| `/ais switch <id>`（切换） | 按本地 id 切换会话（已关闭的会话记录已删除，需新建） |
| `/ais switch 识图/普通` | 按模式切换（复用该模式最近会话，无则新建） |
| `/ais help`（帮助） | 帮助 |
| `/cloak登录` | 微信扫码登录 |

> `-v` 旗标自 v2.2.10 起保留解析（兼容剥离）但无实际效果（识图由发送图片自动触发，v2.2.9 起每次传图新建识图会话）；`-t` 旗标 v2.2.11 起彻底不识别（深度思考跟随 DeepSeek 网页端设置）。

**防刷屏与指令保护策略**：
- 会话空闲超时**静默销毁**，不推送任何主动通知（此前每会话超时都会向聊天窗口推送一条）
- 自动触发 / 识图处理器跳过机器人自己发出的消息（`sender_id == self_id`），防止自我回复循环
- 自动触发 / 识图处理器跳过**指令消息**（`_is_command_message`：activated_handlers 含 Command 类 filter 即让路；waking_check 已剥离指令前缀，不能靠文本判断），避免拦截 /cloak登录、/ais 及其他插件/内置指令
- 回复只跟随用户提问产生（指令 / 自动触发 / 识图各一条），QQ 官方平台键盘按钮与回复合并为一条消息

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
       ├─ AUTO_TRIGGER 关闭 / 事件已停止 / 机器人自身消息 → 返回
       ├─ 空文本 → 返回
       ├─ 指令消息（activated_handlers 含 Command 类 filter）→ 返回，交给指令处理器
       ├─ 含图片 → 返回（交给 on_image_message 识图）
       ├─ 群聊未@未唤醒 → 返回（不拦截正常聊天）
       └─ 命中 → event.stop_event()（阻止默认 LLM 响应）
       ▼
  session.send_message(text) → (created, result)
       ▼
  _respond()：刷新空闲计时 + 前缀 + 键盘按钮 → 回复
```

### 文本提问（/ais 或自动触发）

```
用户发送提问（/ais 问题 或 @机器人 问题）
       │
       ▼
  ai_search_command() / on_auto_message()
       │  send: 直接在当前会话提问（-v 兼容剥离无效果；-t 已不识别）
       ▼
  DeepSeekSessionCore.send_message(text) → (created, result)
       │
       ├─ ensure_current_locked(None)：
       │    ├─ 当前会话匹配？ ──→ _goto_session(conv.url)
       │    └─ 无当前会话？ ──→ _create_conversation(normal) + _register(分配新 id)
       │                          ├─ _open_new_chat()（新对话页）
       │                          └─ vision？ _switch_to_vision()（点击识图模式）
       │
       ├─ 输入 + Enter/按钮发送
       ├─ 等待最后一条回复出现 → 文本稳定检测（9s 无变化）→ HTML → Markdown
       └─ _mark_active(conv)（消息数+1，刷新空闲计时，回写真实 URL/id）
       │
       ▼
  _respond()（created 时前缀「🟢 已开启新的…（会话 #id）」；QQ 官方附带键盘按钮）
       │
       ▼
  _arm_idle_timer(local_id) → 300s 无活动 → destroy（静默，不推送通知）
```

### 识图（@机器人 + 图片，v2.2.8 起支持引用消息中的图片；v2.2.9 起每图一个新会话）

```
群聊 @机器人 + 图片（或私聊图片；也可引用一条含图片的消息）
       │
       ▼
  on_image_message()（EventMessageType.ALL）
       ├─ 无图片 / 未@ → 直接返回，不拦截（_collect_images 同时检查当前消息与 Reply.chain）
       ├─ 命中 → event.stop_event()（阻止默认 LLM 响应）
       ▼
  _prepare_image_paths()（convert_to_file_path / 下载 / PIL 压缩）
       ▼
  send_image_message(text, paths) → (created, result)
       ├─ _create_conversation(MODE_VISION) + _register（每次传图强制新建识图会话）
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

### ✅ v2.2.2 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 取消超时主动通知 | ✅ | 会话空闲超时静默销毁（仅日志），`_notify` / `_record_notice_source` / `_notice_sources` 全部移除 |
| 自我消息过滤 | ✅ | `_is_self_message`（sender_id == self_id）应用于自动触发与识图处理器，防自我回复循环刷屏 |

### ✅ v2.2.3 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 修复自动触发拦截指令 | ✅ | `_is_command_message`：检查 activated_handlers 中带 Command 类 filter 的 handler（含其他插件/内置指令），命中即让路；修复 /cloak登录、/ais 等指令因指令前缀被 waking_check 剥离而被误当提问 + stop_event 阻断的问题 |

### ✅ v2.2.4 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 模式开关检查兼容 humanize | ✅ | `_locate_by_text`（纯 CSS + all_inner_texts + 尾部 .nth()）替代 `.filter(has_text=...)` 链式 locator，修复 isolated-world resolver 报错 |
| 找不到开关快速降级 | ✅ | 2 秒内跳过（不再 15 秒超时拖慢提问），警告仅首次输出（`_toggle_warned` 缓存） |
| 发送按钮兜底同步修复 | ✅ | `_type_and_send` 中同类 `.filter()` 用法改为 `_locate_by_text` |

### ✅ v2.2.5 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 移除开关检查 | ✅ | 删除 `_ensure_toggle_on` / `ALWAYS_DEEP_THINK` / `ALWAYS_WEB_SEARCH` / `_toggle_warned`，不再自动操作深度思考/联网搜索开关，消除日志噪音 |
| 文案清理 | ✅ | 键盘按钮移除「深度对话」，/ais help 与 usage、README 同步更新 |

### ✅ v2.2.6 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 等待新回复超时放宽 | ✅ | 60 秒 → 180 秒（慢速网络 + 深度思考 + 联网搜索足够） |
| 发送失败自愈 | ✅ | 等待期间每 5 秒检查输入框未清空则补点发送按钮，连续 3 次失败提前报「消息发送失败」 |
| 超时留档 | ✅ | 等待超时前将页面文本片段写入日志，便于排查 |

### ✅ v2.2.7 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 回归简单等待逻辑 | ✅ | `_send_query` 回归「等待最后一条回复出现 + 文本稳定检测（9s）」，删除基线计数 / 生成标志检测 / 补发重试，修复部分环境下卡死 |
| 识图上传保留 | ✅ | v2.2.1 的上传双路确认（`_wait_upload_finished`）完整保留，确保图片上传完成后再发送 |

### ✅ v2.2.8 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 引用消息图片识别 | ✅ | `_collect_images` 同时收集当前消息与 Reply.chain 中的图片（chain=None 防御），引用含图消息自动走识图 |
| 自动触发联动 | ✅ | `on_auto_message` 检测到引用图片后自动让给识图处理 |

### ✅ v2.2.9 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 每次传图新建识图会话 | ✅ | `send_image_message` 强制 `_create_conversation(MODE_VISION)` + `_register`，不再复用旧识图会话，避免多图同会话导致回复异常 |

### ✅ v2.2.10 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| `-v` 旗标移除语义 | ✅ | 带图自动新建识图会话后 `/ais -v` 无意义；解析保留兼容剥离但无效果，键盘按钮「识图对话」与 usage/help 文案同步移除（`-t` 同 v2.2.5 无效果） |
| `/ais new` 总是普通会话 | ✅ | `new_conversation` 缺省 `MODE_NORMAL`，不再继承上一轮会话模式（识图由发图触发，新建识图会话无意义） |
| 列表紧凑化 | ✅ | `/ais list` 一行一个会话：`👉 1  普通对话 ｜ 3 条 ｜ 最后活跃 08-30 17:14`，去掉 # 号与多行提示 |
| 列表过滤已销毁 | ✅ | 空闲超时关闭的会话不再显示在 `/ais list`（`_format_session_list` 过滤 `destroyed`），仍可按 id 切换时自动重建 |
| 单元测试 | ✅ | 指令解析 35 用例 + 会话核心 mock 测试全部通过 |

### ✅ v2.2.11 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| `-t` 旗标彻底移除 | ✅ | v2.2.5 起已无效果，v2.2.11 起不再识别（`-t` 按普通文本处理）；`-v` 保留兼容剥离 |
| 删除 `thinking` 参数链 | ✅ | `send_message` / `send_image_message` / `_send_query` 移除 `thinking` 参数（深度思考跟随 DeepSeek 网页端设置） |
| 删除 `startswith("/")` 死代码 | ✅ | `on_auto_message` 移除永不命中的 `/` 前缀检查（waking_check 已剥离前缀，`_is_command_message` 已覆盖） |
| 移除单字母模式别名 | ✅ | `MODE_ALIASES` 删除 `"v"` / `"n"`（`-v` 时代残留，`switch 识图/普通` 已够用） |
| 移除 `list <id>` 便捷切换 | ✅ | 切换统一走 `/ais switch <id>`，`/ais list` 仅显示列表 |
| 销毁即删除记录 | ✅ | 空闲销毁时从 conversations.json 移除记录（含 `current_id` 重置）；加载时自动清理历史 destroyed 数据并立即落盘；`switch_conversation` 不再有「已销毁重建」分支 |
| 单元测试 | ✅ | 指令解析 35 用例 + 会话核心 mock 测试（含销毁删除/旧格式清理用例）全部通过 |

### ✅ v2.2.12 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| id 计数归 1 | ✅ | 没有任何存活会话时 `next_id` 归 1（`_register` 分配前 / `destroy_conversation` 全部销毁后 / `_load_conversations` 加载空文件时），新会话从 #1 重新开始，不再无限增长 |
| 页面失效不留残留 | ✅ | `ensure_current_locked` 页面失效路径改为删除记录后新建（不再标记 destroyed=True 留残留记录） |
| 空闲销毁防误杀 | ✅ | `_idle_waiter` 到点后按 `last_active` 二次校验（有新活动则重新计时）；`destroy_conversation` 锁内权威校验真实空闲，杜绝「对话中被销毁」 |
| 单元测试 | ✅ | 指令解析 35 用例 + 会话核心 mock 测试（新增全部销毁归 1 / 空文件加载归 1 用例）全部通过 |

### ⚠️ 已知问题 / 待改进

- **UI 依赖**：识图模式入口、上传输入框等选择器基于 2026-06 上线的 DeepSeek 网页版 UI，改版后需更新 `session_core.py`
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
   - @机器人 今天有什么大新闻（自动触发，深度思考/联网搜索跟随 DeepSeek 网页端设置）
   - 群里 @机器人 发一张截图 → 自动识图分析
   - `/ais list` → `/ais switch 2` → @机器人 这张图里有什么公式
6. 空闲 300 秒无活动，会话自动关闭（静默，不推送通知）；已关闭的会话按 id 切换时自动重建

---

*文档更新日期：2026-08-30（v2.2.12）*
