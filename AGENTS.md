# astrbot_plugin_aisearch 开发文档

> **插件名称**：AI搜索  
> **版本**：v2.3.2  
> **作者**：Roi  
> **许可证**：AGPL-3.0  
> **仓库地址**：https://github.com/Roi7687/astrbot_plugin_aisearch  

---

## 一、项目概述

这是一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件，通过自动化操控 DeepSeek Chat 网页端，实现**联网 AI 搜索**与**图片理解**功能。用户在聊天中发送 `/ais <关键词>`（或 @机器人 + 图片），插件在后台通过反检测无头浏览器（cloakbrowser）访问 DeepSeek Chat，获取 AI 结果后以 Markdown 格式返回给用户。

核心思路：**不调用 API，而是直接自动化操控浏览器**，利用 DeepSeek Chat 自带的联网搜索与图片理解能力。

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

**v2.2.13 变更**：**恢复默认启动深度思考**——用户反馈普通/识图回答不理想（未启用思考）。核对 v1.3.0：其通过文本为「深度思考」的 `.ds-toggle-button` 点击开启；**2026-08 真实登录态浏览器实测**该元素在当前 UI 仍存在且可点击：点击后 class 追加 `ds-toggle-button--selected`、`aria-pressed` 变 true，再点会关闭（必须幂等）。新增 `_ensure_deep_think_on()`：每次提问前（`_send_query`，普通/识图共用）用 `_locate_by_text` 定位 → 检测激活态（selected/active class 或 aria=true）→ 未激活才点击 → 找不到仅首次警告后降级。**顺带修复 v2.2.4 引入的潜伏 bug**：`_locate_by_text` 把 `visible_timeout`（秒）直接当 playwright `wait_for` 的 timeout（毫秒）传入，2.0 秒被当成 2ms 导致等待必然超时（发送按钮兜底因 Enter 常成功而未暴露），改为 `int(visible_timeout * 1000)`。无任何参数/配置项——深度思考默认全开。

**v2.2.14 变更**：**修复深度思考下连续对话返回上一轮回复**——根因：发送后对「对话框最后一条回复」做 9 秒稳定检测，而深度思考/联网搜索下 AI 数十秒后才开始输出，旧回复一直是「最后一条」，稳定检测直接通过 → 返回旧回复。修复：**发送前**采样助手回复块基线（块数量 + 最后一块文本，仅在 assistant 专用选择器 `div.ds-markdown.ds-assistant-message-main-content` 上采样——通用 `.ds-markdown` 会匹配用户消息，实测会把用户消息当基线导致永远等不到变化），发送后轮询等待「回复块数量增加 或 最后一块文本变化」= 本轮新回复开始渲染（180s 上限），随后对最新回复做 9s 文本稳定检测。**真实浏览器双问验证**：快答（1+1，<2s 渲染）与慢答（相对论，深度思考数十秒）均正确拿到本轮新回复、不会返回上一轮内容。

**v2.3.0 变更**：**合并普通/识图模式，清除模式相关冗余代码**——DeepSeek 网页端改版，官方公告「快速、专家、识图模式已合并升级」（输入区提示：全新模型上线，同时具备日常对话、图片理解与复杂问题解决能力）；实测确认「识图模式」入口已从页面消失（模式栏仅剩「深度思考」「智能搜索」两个开关，原「联网搜索」更名为「智能搜索」），输入框原生支持图片上传（`input[type=file]` accept 含图片类型、multiple）。插件同步调整：① **合并处理链路**：`on_image_message` + `on_auto_message` 合并为单一 `on_message`，文本与图片走同一路径（图片在**当前会话**中上传提问，保持上下文连续，不再另开识图会话）；② **删除模式体系**：`MODE_NORMAL` / `MODE_VISION` / `MODES` / `MODE_LABELS` / `VISION_WELCOME_TEXT` / `VISION_DEBUG_SHOT` 等常量（`DEFAULT_VISION_PROMPT` 改名 `DEFAULT_IMAGE_PROMPT`）、`Conversation.mode` 字段、`current_mode` 属性、`ensure_mode()` / `_latest_of_mode()` 方法、`command_parser.MODE_ALIASES`（`/ais switch 识图/普通` 取消，只按本地 id 切换）；③ **删除识图入口相关代码**：`_switch_to_vision` / `_in_vision_state` / `_click_vision_entry` / `_expand_vision_menu` / `_dump_vision_failure` 及 `VISION_ENTRY_*` / `VISION_EXPAND_LABELS` 常量（约 137 行）与 `vision_debug.png` 留档机制；④ 会话列表不再显示模式列（`👉 1 ｜ 3 条 ｜ 最后活跃 08-30 17:14`），usage/help/README 文案同步。旧数据兼容：conversations.json 中残留的 `mode` 字段被 `Conversation.from_dict` 自动忽略，v2.0 双槽位旧格式仍可迁移。**真实浏览器端到端验证**：先发文本（「收到」）再在同一会话发图（红底白圆），`created=False` 复用会话且 AI 正确回答「主要是红色，图中还有一个白色圆形」。

**v2.3.1 变更**：**修复 QQ 群引用（回复）消息解析——被引用内容丢失导致 AI 答非所问**。现象（2026-10-11 01:17:41 线上日志）：群友「引用一条消息 + @机器人 + 回答一下这个问题」，DeepSeek 收到后回「请把你想让我回答的问题发出来」。根因：AstrBot 的消息链是 `[Reply(被引用内容), At(机器人), Plain(自己的话)]`，而 `event.get_message_str()` **只含「自己的话」**——被引用的文本/图片只存在于 `Reply` 组件（`chain` / `message_str`）中，插件此前只取 `message_str` 就发给 DeepSeek，「这个问题」指代不明。修复（新增 `core/quote_parser.py` 纯函数模块 + `main.py` 接线）：① **引用文本入 prompt**——`[引用消息] 被引用内容` 在前、用户自己的话在后（有 `sender_nickname` 时标注发送者；QQ 官方平台昵称为空则用 `[引用消息]`）；② **引用图片上传**——`Reply.chain` 中的 `Image` 单独收集并上传（此前混在 `_collect_images` 里，v2.3.1 起统一由 `_collect_quote` 提取，避免漏取/重复取）；③ **优先复用 AstrBot 官方解析工具**（`astrbot.core.utils.quoted_message.extract_quoted_message_{text,images}`，AstrBot ≥ 4.4）——可处理远程 `get_msg`、合并转发等跨平台场景，旧版本或调用失败时回退本地 `quote_parser`；为防群里无关引用消息触发远程拉取，`is_at` 判断前置到解析之前；④ **默认提示词补位**——只有引用、自己没打字时用 `DEFAULT_QUOTE_PROMPT`（新增，引用图片无文字仍用 `DEFAULT_IMAGE_PROMPT`）；⑤ **引用解析为空且自己没说话 → 放行**（不再向 DeepSeek 发空提问）；⑥ **可观测性**——每次提问输出 `📝 [AIS] 本轮提问（N 字…）`、解析到引用时输出 `🔗 [AIS] 解析到引用消息：…`（DEBUG）。**顺带修复**：⑦ `/ais` 指令路径同样带上引用文本/图片（此前只有自动触发路径）；⑧ **插件 `terminate()` 清理**——`Star.terminate` 是空实现，重载后旧实例的空闲计时器仍在运行并可能销毁**新实例正在对话**的会话（日志实测：旧实例 01:16:13 销毁的会话，新实例 01:17 仍在其内提问），浏览器进程也不退出；现于卸载/重载时取消全部计时器并 `session.close()`；⑨ **测试隔离修复**——`test_session_core_mock.py` 原先读写并 `os.remove` **线上** `conversations.json`（有数据时首个断言即失败、且会删掉线上会话元数据），改为指向临时目录。单元测试新增 `tests/test_quote_parser.py`（引用解析与 prompt 组装，纯函数）与 `tests/test_quote_flow.py`（真实 AstrBot 消息链驱动 `on_message`/`ai_search_command` 的 12 项端到端断言）。

**v2.3.2 变更**：**修复卡片消息（小程序 / 音乐 / 图文…）被整段 URL 带偏**。现象（2026-10-11 11:49 线上日志）：群友引用一条 B 站小程序卡片 + @机器人「这是什么」，DeepSeek 的回答整段都在解释 `preview` 那条 CDN 链接，完全没回答「这张卡片是什么」。根因：QQ 官方平台对卡片消息的 `content` 字段是**腾讯渲染好的多行文本转储**，引用它或直接发它都会原样进入消息文本，其中 `source_logo`（图标）与 `preview`（封面）是两条**超长带签名 CDN URL**（实测整段 402 字里约 330 字是这两条 URL）。修复（`core/quote_parser.py` 新增 `parse_card_dump()` / `clean_card_text()`）：① **只保留认识的字段**——`title`/`标题`→标题、`摘要`/`desc`/`summary`→摘要、`source`/`app`/`appname`→来源、`url`/`jumpUrl`/`link`→链接；`preview`/`source_logo`/`icon`/`cover` 等资源字段与 `appid`/`type`/签名串等未知字段**一律丢弃**（不认识的内容宁可走原文兜底，也不塞进 prompt 当噪音）；② **头部重构**——`[卡片消息] 小程序` → `QQ 小程序卡片（来源：哔哩哔哩）`（无子类型则 `QQ 卡片消息`；非「卡片」类别保留原 kind）；③ **标题与摘要互相包含时只留信息量更大的一条**，不重复占 token；④ **原文兜底**——不是卡片、或清理后没有任何有效字段（例如卡片内容只有封面 URL）时**原样返回**，绝不让信息变少；⑤ 清理同时作用于**引用消息**与**直接发送的卡片消息**（`main._shrink_card_text()` 三处调用：`on_message` 当前消息、`_collect_quote` 引用、`_ask` 指令），并输出 `🃏 [AIS] 识别到X卡片消息：402 字 → 清理后 53 字…` 日志。实测同一张卡片的提问由 **415 字压到 66 字**。**注意**：卡片封面（`preview`）目前只丢弃、不上传，如需让 AI 看图可后续加开关。单元测试新增 `check_card()`（实测原文、标题/摘要去重、链接字段保留、技术字段过滤、中文冒号/无子类型兼容、非卡片不改写、无有效字段回退）与端到端 ⑬⑭⑮（引用卡片 / 直接发卡片 / 普通多行文本回归）。

---

## 二、项目结构

```
astrbot_plugin_aisearch/
├── core/                      # 核心业务模块
│   ├── __init__.py
│   ├── config.py              # 全局配置：路径常量、会话参数 & 自定义异常
│   ├── command_parser.py      # /ais 指令参数解析器（纯函数，可单测）
│   ├── quote_parser.py        # 引用消息（Reply）解析与提问组装（纯函数，可单测）
│   ├── session_core.py        # 统一持久化会话核心（多会话 + 本地 id 计数）
│   └── login_core.py          # 登录模块：微信扫码登录 DeepSeek
├── tests/                     # 单元测试（mock 掉浏览器依赖）
│   ├── test_command_parser.py      # 指令解析 34 用例
│   ├── test_quote_parser.py        # 引用消息解析 / prompt 组装（纯函数）
│   ├── test_session_core_mock.py   # 会话核心 mock 测试（含旧数据迁移，隔离临时目录）
│   └── test_quote_flow.py          # 引用消息端到端（真实 AstrBot 消息链驱动处理器）
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
| `DEFAULT_IMAGE_PROMPT` | 仅图片无文字时的默认提问 |
| `DEFAULT_QUOTE_PROMPT` | 仅引用消息、自己没打字时的默认提问（v2.3.1） |
| `AuthError` / `ConversationError` | 自定义异常 |
| `ImagePrepareError` | `ConversationError` 子类：图片全部下载/解析失败（v2.3.1，处理器据此回「⚠️」而非「❌ 搜索失败」） |

### 3.2 `core/command_parser.py` — 指令解析器（纯函数）

| 函数 | 说明 |
|------|------|
| `parse_ais_command(raw)` | 解析 `/ais` 后的参数，返回 (action, payload) |

**返回的 action**：`usage` / `help` / `send` / `new` / `list` / `switch`

**解析规则**：
- 开头的 `-v` 兼容旗标（无实际效果，解析时剥离避免旗标文本被当问题发送）；`-t` v2.2.11 起彻底不识别（按普通文本处理）
- 首个 token 命中子命令关键词表（含中文别名：重置/状态/列表/切换/帮助）→ 子命令
  - `session` / `状态` / `会话` / `info` / `查看` 均为 `list` 的别名（已合并）
  - `switch` 后跟数字 → `{"local_id": int}`（按本地 id 切换）；`list` 不再接受目标参数（v2.2.11）
  - `switch` 无参或参数无效 → `{}`（调用方显示列表）
- 其余情况 → send，payload 为 `{"text": str}`（当前会话提问）
- 其余 token 原样拼接为提问文本

### 3.3 `core/quote_parser.py` — 引用消息解析（纯函数）

| 函数 | 说明 |
|------|------|
| `find_reply(components)` | 返回消息链中第一个 `Reply` 组件（无则 None） |
| `quote_sender(reply)` | 被引用消息的发送者昵称（QQ 官方平台为空字符串） |
| `extract_quote_text(reply)` | 被引用文本：`chain` 中的 `Plain`/`At` 文本优先，`Reply.message_str` 兜底；非文本段转 `[图片]`/`[表情]`/`[转发消息]` 等占位符 |
| `extract_quote_images(reply)` | 被引用 `chain` 中的 `Image` 组件（保持原顺序） |
| `parse_card_dump(text)` | 识别 QQ 卡片消息（小程序 / 音乐 / 图文…）的腾讯文本转储（v2.3.2），返回 `{"kind", "subtype", "fields"}`；只保留认识的字段（标题/摘要/来源/链接），资源 URL（`preview`/`source_logo`/`icon`…）与 `appid`/`type`/签名串一律丢弃；不是卡片或无有效字段返回 None |
| `clean_card_text(text)` | 把卡片转储压成简洁描述（v2.3.2）：`[卡片消息] 小程序` → `QQ 小程序卡片（来源：哔哩哔哩）`，标题与摘要互相包含时只留一条；**识别失败/无有效字段时原样返回**，绝不让信息变少（实测 402 字 → 53 字） |
| `build_prompt(own_text, quote_text, quote_sender_name, has_images, default_image_prompt, default_quote_prompt)` | 组装提问：`[引用消息（昵称）] 被引用内容` + 空行 + `用户自己的话`；自己没打字时按「有图 → 图片提示词 / 只有引用 → 引用提示词」补位，全空则返回 `""`（调用方据此放行） |

**设计要点**：组件识别按**类名**做鸭子类型判断（`type(comp).__name__`），因此本模块 **不 import astrbot**，可在无 AstrBot 运行时的环境直接单测；`chain` / `message` 两种历史字段都兼容。

### 3.4 `core/session_core.py` — 统一会话核心（全异步，多会话 + 本地 id）

| 类 / 方法 | 说明 |
|-----------|------|
| `Conversation` | 会话元数据 dataclass（local_id / session_id / url / message_count / created_at / last_active / destroyed；**mode 字段 v2.3.0 已移除**，旧数据中的 mode 自动忽略） |
| `DeepSeekSessionCore` | 统一会话核心：`conversations: dict[local_id, Conversation]` + `next_id` 计数器 + `current_id` |
| `current_conversation` | 当前会话（只读属性） |
| `send_message(text)` | 在当前会话发送文本（无会话自动创建），返回 `(是否新建, 回答文本)` |
| `send_image_message(text, paths)` | 在**当前会话**上传图片 + 发送提问（v2.3.0 起图文同会话），返回 `(是否新建, 回答文本)` |
| `switch_conversation(local_id)` | 按本地 id 切换；id 不存在（含已销毁被移除）报错，返回 `(conv, 是否重建)` |
| `new_conversation()` | 创建新会话（分配新 id）并设为当前；不销毁旧会话 |
| `destroy_conversation(local_id)` | 销毁空闲超时的会话：服务器端删除 + 记录移除（v2.2.11）；**锁内校验真实空闲**、全部销毁后 id 归 1（v2.2.12），返回是否真的有会话被销毁 |
| `conversation_summary(local_id)` / `list_summary()` | 单个 / 全部会话展示数据（按 id 升序） |
| `close()` | 关闭浏览器（插件卸载 / 登录刷新） |

**设计要点**：
- **多会话 + 本地 id**：本地维护会话列表，`next_id` 本地递增计数（1、2、3...），持久化于 conversations.json；重启后 id 连续不重复；**v2.2.12 起没有任何存活会话时 id 归 1**（`_register` 分配前 / 全部销毁后 / 加载空文件时），新会话从 #1 重新开始
- **串行化**：所有页面操作经 `asyncio.Lock` 互斥，避免并发命令竞态
- **惰性创建**：首次使用时才启动浏览器 / 开启新对话
- **销毁即删除记录（v2.2.11，v2.2.12 强化）**：空闲销毁时从 conversations.json 移除该记录（用户无法再访问已销毁会话）；销毁当前会话时重置 current_id；**锁内权威校验真实空闲**——等待锁期间会话有新活动则放弃销毁（v2.2.12，杜绝「对话中被销毁」）；页面失效路径同样删除记录而非标记 destroyed（v2.2.12）；加载时自动清理历史 destroyed 数据并立即落盘；`Conversation.destroyed` 字段仅作历史兼容
- **图片上传（图文同会话）**：Playwright `set_input_files` 注入 `input[type=file]`（新 UI accept 含图片类型且支持多选） → 等 blob 缩略图出现 → **双路上传完成确认**（`_wait_upload_finished`：① 监听上传网络请求 POST/PUT 含 upload/file 全部响应；② DOM 上传中标志消失兜底），确认完成才发送提问，避免慢速网络下「文本已发送但图片未传完」
- **深度思考（v2.2.13 起默认开启）**：每次提问前 `_ensure_deep_think_on()` **幂等**确保开启——`_locate_by_text` 定位文本为「深度思考」的 `.ds-toggle-button`，检测激活态（class 含 selected/active 或 aria-pressed/aria-checked=true），未激活才点击（2026-08 真实浏览器实测该元素有效；再次点击会关闭，故必须幂等）；找不到仅首次警告后降级不阻塞。无需任何参数/配置。联网搜索不自动操作（跟随 DeepSeek 网页端设置，默认开）
- **答案等待**（v2.2.14 基线方案）：**发送前**在 assistant 专用选择器（`div.ds-markdown.ds-assistant-message-main-content`，不匹配用户消息）采样回复块基线（数量 + 最后一块文本），发送后轮询等待「回复块数量增加 或 最后一块文本变化」= 本轮新回复开始渲染（180s 上限），随后做**文本稳定检测**（连续 9 秒无变化视为输出完成，总上限 300s）——修复深度思考/联网搜索下 AI 延迟输出时把上一轮回复当成本轮结果的问题；不做回复块计数/生成标志/补发重试等复杂判断，避免部分环境下卡死；保留引用角标清理与 HTML→Markdown 转换
- **会话删除**：`POST /api/v0/chat_session/delete`（Bearer 令牌嗅探），新旧 URL 格式（`/a/chat/s/<uuid>` 与 `/s/<id>`）均兼容
- **元数据持久化**：每次变更写入 conversations.json（`{"next_id", "current_id", "conversations"}`）；旧版 `{mode: {...}}` 双槽位格式仍可迁移（v2.3.0 起忽略其中的 mode 字段）

### 3.5 `main.py` — 插件主入口

| 类 / 方法 | 说明 |
|-----------|------|
| `CloakSearchPlugin` | 插件主类 |
| `ai_search_command()` | `/ais` 统一指令（send/new/session/list/switch/help） |
| `on_message()` | `@filter.event_message_type(ALL)` 统一消息处理（v2.3.0 合并原 `on_image_message` + `on_auto_message`；v2.3.1 支持引用消息）：@机器人/私聊的**文本 / 图片 / 引用消息** → 当前会话提问，无需 /ais。顺序：自身消息/指令让路 → 取当前消息文本与图片 → 空消息放行 → 群聊未 @ 放行（**在解析引用之前**，避免为群里无关引用发起远程拉取）→ 解析引用 → `AUTO_TRIGGER=False` 且无图则放行 → 组装 prompt（空则放行）→ `stop_event()` → 提问 |
| `_collect_quote()` | 解析引用（Reply）消息（v2.3.1）→ `(被引用文本, 被引用图片, 发送者昵称)`；文本/图片优先用 AstrBot 官方 `extract_quoted_message_text/images`（可远程 `get_msg`、解析合并转发），旧版本或调用失败回退 `quote_parser`；图片先取 `Reply.chain` 内嵌，没有再走远程兜底 |
| `_build_query_prompt()` | 调用 `quote_parser.build_prompt()` 组装提问：`[引用消息] 被引用内容` 在前、自己的话在后；只有引用没打字时补 `DEFAULT_QUOTE_PROMPT` |
| `_shrink_card_text()` | 卡片消息清理包装（v2.3.2）：`clean_card_text()` + 「402 字 → 53 字」日志；三处调用——`on_message` 当前消息、`_collect_quote` 引用消息、`_ask` 指令提问 |
| `ai_search_command()` 的 `_ask()` / `_send_query()` | 指令路径同样带上引用文本/图片（v2.3.1）；`_send_query()` 有图走 `send_image_message`、无图走 `send_message`，图片全部解析失败抛 `ImagePrepareError`（回「⚠️」而非「❌ 搜索失败」） |
| `terminate()` | 插件禁用/重载清理（v2.3.1）：取消全部空闲计时器 + `session.close()`，防止旧实例计时器销毁**新实例正在使用**的会话、以及旧浏览器进程残留 |
| `_respond()` | 统一回复：刷新空闲计时 + 「已开启新会话」前缀 + 键盘按钮；返回空串表示已发送 |
| `_arm_idle_timer()` / `_idle_waiter()` | 每会话独立空闲计时器：每次提问/切换/新建后重置；到点后**按真实空闲时间（last_active）二次校验**，有新活动则重新计时，超时**静默销毁**（不推送通知，避免刷屏） |
| `_is_self_message()` | 跳过机器人自己发出的消息（部分平台会回传自身消息，防止自我回复循环） |
| `_is_command_message()` | 跳过被 AstrBot 识别为指令的消息（waking_check 已剥离指令前缀，无法靠文本判断；检查 activated_handlers 中带 Command 类 filter 的 handler），防止自动触发拦截 /cloak登录、/ais 等指令 |
| `_collect_images()` | 兼容新旧 AstrBot API 提取**当前消息**的图片组件；v2.3.1 起引用图片改由 `_collect_quote` 统一提取（避免漏取/重复取） |
| `_prepare_image_paths()` | 图片 → 本地路径（自动下载、PIL 压缩）；支持 `Image` 组件与字符串引用（`base64://` / http(s) / 本地路径，v2.3.1） |
| `_resolve_image_ref()` / `_save_base64_image()` / `_sniff_image_ext()` | 字符串图片引用落盘（v2.3.1）：base64 解码后按文件头补 `.png/.jpg/.gif/.webp` 扩展名 |
| `_build_keyboard()` / `_try_send_with_keyboard()` | QQ 官方平台键盘按钮消息（其余平台自动回退纯文本） |
| `login_command()` | `/cloak登录`：后台线程扫码登录，成功后重置浏览器内核 |

**命令列表（v2.1.0，v2.2.11 更新）**：

| 命令 | 说明 |
|------|------|
| `/ais <问题>` | 当前会话提问（无会话自动创建） |
| `/ais new`（reset/重置） | 开启新的**普通会话**（旧会话保留，可按 id 切回） |
| `/ais list`（列表/状态/session） | 查看会话列表（带本地 id，👉 为当前；**已关闭的不显示**） |
| `/ais switch <id>`（切换） | 按本地 id 切换会话（已关闭的会话记录已删除，需新建） |
| `/ais help`（帮助） | 帮助 |
| `/cloak登录` | 微信扫码登录 |

> `-v` 旗标保留解析（兼容剥离）但无实际效果（图片由发送图片自动处理）；`-t` 旗标 v2.2.11 起彻底不识别——深度思考无需任何旗标，每次提问自动开启。

**防刷屏与指令保护策略**：
- 会话空闲超时**静默销毁**，不推送任何主动通知（此前每会话超时都会向聊天窗口推送一条）
- 消息处理器跳过机器人自己发出的消息（`sender_id == self_id`），防止自我回复循环
- 消息处理器跳过**指令消息**（`_is_command_message`：activated_handlers 含 Command 类 filter 即让路；waking_check 已剥离指令前缀，不能靠文本判断），避免拦截 /cloak登录、/ais 及其他插件/内置指令
- 回复只跟随用户提问产生（指令 / 自动触发 各一条），QQ 官方平台键盘按钮与回复合并为一条消息

### 3.6 `core/login_core.py` — 登录模块

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

### 自动触发（@机器人/私聊的文本或图片，v2.2.0 新增；v2.3.0 图文合并）

```
用户 @机器人 发送文字/图片（或私聊直接发送；也可**引用一条消息**，v2.3.1）
       │
       ▼
  on_message()（EventMessageType.ALL）
       ├─ 事件已停止 / 机器人自身消息 → 返回
       ├─ 指令消息（activated_handlers 含 Command 类 filter）→ 返回，交给指令处理器
       ├─ 取当前消息文本（_clean_image_text）+ 当前消息图片
       ├─ 无文本、无图片、无引用 → 返回（放行给默认 LLM）
       ├─ 群聊未@未唤醒 → 返回（不拦截正常聊天；放在解析引用之前）
       ├─ _collect_quote()：Reply → 被引用文本 / 图片 / 昵称
       ├─ 无图片且 AUTO_TRIGGER=False → 返回（图片不受该开关限制）
       ├─ build_prompt()：`[引用消息] 被引用内容` + 空行 + 自己的话；空则返回
       └─ 命中 → event.stop_event()（阻止默认 LLM 响应）
       ▼
  有图片 → send_image_message(prompt, paths)（当前会话上传图片 + 提问）
  仅文本 → send_message(prompt)
       ▼
  _respond()：刷新空闲计时 + 前缀 + 键盘按钮 → 回复
```

### 文本提问（/ais 或自动触发）

```
用户发送提问（/ais 问题 或 @机器人 问题）
       │
       ▼
  ai_search_command() / on_message()
       │  send: 直接在当前会话提问（-v 兼容剥离无效果；-t 已不识别）
       ▼
  DeepSeekSessionCore.send_message(text) → (created, result)
       │
       ├─ ensure_current_locked()：
       │    ├─ 有当前会话？ ──→ _goto_session(conv.url)（失效则删记录后新建）
       │    └─ 无当前会话？ ──→ _create_conversation() + _register(分配新 id)
       │                          └─ _open_new_chat()（新对话页）
       │
       ├─ _send_query()：确保「深度思考」开启（幂等，v2.2.13）→ 输入发送
       ├─ 发送前基线 → 等新回复块出现/文本变化（v2.2.14）→ 稳定 9s → HTML → Markdown
       └─ _mark_active(conv)（消息数+1，刷新空闲计时，回写真实 URL/id）
       │
       ▼
  _respond()（created 时前缀「🟢 已开启新的…（会话 #id）」；QQ 官方附带键盘按钮）
       │
       ▼
  _arm_idle_timer(local_id) → 300s 无活动 → destroy（静默，不推送通知）
```

### 图片提问（@机器人 + 图片，v2.2.8 起支持引用消息中的图片；v2.3.0 起图文同会话）

```
群聊 @机器人 + 图片（或私聊图片；也可引用一条含图片的消息）
       │
       ▼
  on_message()（EventMessageType.ALL）
       ├─ _collect_images()：当前消息图片
       └─ _collect_quote() ：引用 chain 中的图片（无内嵌时用官方工具远程兜底）
       ▼
  _prepare_image_paths()（convert_to_file_path / 引用落盘 / 下载 / PIL 压缩）
       ▼
  send_image_message(prompt, paths) → (created, result)
       ├─ ensure_current_locked()（复用当前会话；无则新建）
       ├─ set_input_files 上传 → blob 缩略图 + 双路上传完成确认
       └─ 输入文字（自己的话；没有则 DEFAULT_IMAGE_PROMPT / DEFAULT_QUOTE_PROMPT）→ 发送 → 等待答案
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

### ✅ v2.2.13 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 深度思考默认开启 | ✅ | 每次提问前 `_ensure_deep_think_on()` 幂等开启（`.ds-toggle-button` + 文本「深度思考」+ selected/aria-pressed 激活态检测，真实浏览器实测有效；v1.3.0 元素复用），普通/识图一致，无需任何参数 |
| `_locate_by_text` 潜伏 bug 修复 | ✅ | `wait_for(timeout)` 单位毫秒：`visible_timeout`（秒）改乘 1000，修复 v2.2.4 起 2.0s 被当 2ms 导致等待必然超时的问题（连带修复发送按钮兜底） |
| 单元测试 | ✅ | 指令解析 35 用例 + 会话核心 mock 测试全部通过；真实浏览器闭环验证 ensure→激活、幂等不误关 |

### ✅ v2.2.14 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 修复连续对话返回旧回复 | ✅ | 发送前在 assistant 专用选择器采样基线（块数量 + 最后文本），发送后轮询等「块数增加/文本变化」（180s）再进入 9s 稳定检测；修复深度思考延迟输出时把上一轮回复当成本轮结果 |
| 快答/慢答双场景验证 | ✅ | 真实浏览器：1+1 快答（<2s 渲染）与相对论慢答（深度思考数十秒）连续两问均返回各自新回复，不会错拿上一轮内容 |
| 单元测试 | ✅ | 指令解析 35 用例 + 会话核心 mock 测试全部通过 |

### ✅ v2.3.0 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 普通/识图模式合并 | ✅ | DeepSeek 官方合并「快速/专家/识图模式」后，插件统一为单一会话类型：文本与图片都在当前会话处理，图片不再另开识图会话 |
| 消息处理器合并 | ✅ | `on_image_message` + `on_auto_message` → 单一 `on_message`（图片走 `send_image_message`，文本走 `send_message`） |
| 模式体系删除 | ✅ | 删除 MODE_* / MODES / MODE_LABELS / VISION_* 常量、`Conversation.mode`、`current_mode`、`ensure_mode()`、`_latest_of_mode()`、`MODE_ALIASES`（约 200 行） |
| 识图入口代码删除 | ✅ | 删除 `_switch_to_vision` / `_in_vision_state` / `_click_vision_entry` / `_expand_vision_menu` / `_dump_vision_failure` 及入口标签常量、调试截图机制（约 137 行） |
| 文案/列表同步 | ✅ | 会话列表去掉模式列；usage/help/README 全部改写为「图文同会话」表述 |
| 单元测试 | ✅ | 指令解析 34 用例 + 会话核心 mock 测试（新增「mode 字段已移除」「旧数据 mode 被忽略」用例）全部通过；真实浏览器图文同会话端到端验证通过 |

### ✅ v2.3.1 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 引用文本进入提问 | ✅ | 新增 `core/quote_parser.py`：从 `Reply.chain` / `Reply.message_str` 提取被引用文本，组装为 `[引用消息] 被引用内容` + 自己的话（引用在前）；修复「引用+提问」时 DeepSeek 只收到「回答一下这个问题」的问题 |
| 引用图片进入上传链路 | ✅ | 引用消息 chain 中的 `Image` 由 `_collect_quote()` 统一提取并上传；`_prepare_image_paths()` 同时支持 `Image` 组件与字符串引用（`base64://` / http(s) / 本地路径） |
| 复用 AstrBot 官方引用解析 | ✅ | 优先 `astrbot.core.utils.quoted_message.extract_quoted_message_{text,images}`（跨平台：远程 `get_msg`、合并转发），旧版本/失败回退本地纯函数；`is_at` 判断前置，避免群里无关引用触发远程拉取 |
| 默认提示词与放行 | ✅ | 新增 `DEFAULT_QUOTE_PROMPT`（仅引用无文字时用）；引用解析为空且自己没说话 → 放行，不再向 DeepSeek 发空提问 |
| `/ais` 指令路径同步 | ✅ | 指令提问同样带上引用文本/图片（此前只有自动触发路径支持） |
| 插件 `terminate()` 清理 | ✅ | 卸载/重载时取消全部空闲计时器 + 关闭浏览器；修复「旧实例计时器销毁新实例正在用的会话」与浏览器进程残留 |
| 可观测性 | ✅ | 提问日志 `📝 [AIS] 本轮提问（N 字，图片 M 张）…`；解析到引用时 `🔗 [AIS] 解析到引用消息：文本=… 图片=… 发送者=…`（DEBUG 级） |
| 测试隔离修复 | ✅ | `test_session_core_mock.py` 改为读写临时目录（原先会读线上 `conversations.json` 导致首个断言失败、并在结尾删除线上文件） |
| 单元测试 | ✅ | 新增 `tests/test_quote_parser.py`（纯函数）+ `tests/test_quote_flow.py`（真实 AstrBot 消息链驱动 12 项断言：引用文本/仅引用/引用图片/无引用回归/未@放行/自身消息/空引用/指令路径/AUTO_TRIGGER/长文本），四个测试脚本全部通过 |

### ✅ v2.3.2 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 卡片消息解析 | ✅ | 新增 `parse_card_dump()` / `clean_card_text()`：把腾讯渲染的卡片文本转储解析成「卡片类型 + 来源 + 标题 + 摘要」，丢弃图标 / 封面等超长签名 URL（实测 402 字 → 53 字，提问 415 字 → 66 字） |
| 只保留认识的字段 | ✅ | 未知字段（`appid` / `type` / 签名串）与资源字段（`preview` / `source_logo` / `icon` / `cover`…）一律丢弃；不认识的宁可走原文兜底，也不塞进 prompt 当噪音 |
| 原文兜底 | ✅ | 不是卡片、或清理后无有效字段（例如卡片只有封面 URL）时**原样返回**，绝不让信息变少 |
| 三处生效 | ✅ | `_shrink_card_text()` 同时用于当前消息、引用消息与 `/ais` 指令提问；识别到卡片时输出 `🃏 [AIS] 识别到X卡片消息：N 字 → M 字…` 日志 |
| 单元测试 | ✅ | `check_card()`（实测原文 / 标题摘要去重 / 链接字段保留 / 技术字段过滤 / 中文冒号 / 无子类型 / 非卡片不改写 / 无有效字段回退）+ 端到端 ⑬⑭⑮（引用卡片 / 直接发卡片 / 普通多行文本回归），四个脚本全部通过 |

### ⚠️ 已知问题 / 待改进

- **UI 依赖**：深度思考开关、图片上传输入框等选择器基于 2026-08 上线的 DeepSeek 网页版 UI（模式合并版），改版后需更新 `session_core.py`
- **图片链路已实测**：真实登录态下「文本 + 图片同会话」端到端验证通过（上传红色圆图 → AI 正确回答颜色与形状）
- 多图片消息的图片顺序与文字绑定关系待实测确认
- **引用消息链路**：已由真实 AstrBot 消息链断言覆盖（`tests/test_quote_flow.py`），QQ 群内实测待确认——重测时看日志 `📝 [AIS] 本轮提问…` 是否含被引用文本、`🔗 [AIS] 解析到引用消息…` 是否出现
- **卡片封面未使用**：卡片的 `preview`（封面图）目前只丢弃、不上传，若需要 AI 看图需另加开关（QQ CDN 可能有防盗链）

### 🔮 后续开发建议

- 会话列表持久化到侧边栏（DeepSeek 网页端会话列表）的同步清理
- 多图分次发送时的进度提示
- 将选择器抽离为可配置项，降低网页改版维护成本

---

## 七、安装与使用

1. 首次使用前，发送 `/cloak登录` 微信扫码登录
2. **提问**：@机器人 直接发消息（或私聊直接发）即可自动提问，无需指令；`/ais <问题>` 亦可
3. **图片**：@机器人 + 图片（或私聊发图）自动在当前会话中理解图片并回答
4. **会话管理**（仅切换时需要指令）：`/ais list` 查看全部会话（带本地 id）；`/ais switch <id>` 按 id 切换；`/ais new` 开启新会话（旧会话保留）
5. 示例：
   - @机器人 今天有什么大新闻（自动触发，深度思考自动开启，联网搜索跟随网页端设置）
   - 群里 @机器人 发一张截图 → 自动理解图片并回答（与文字同一会话，上下文连续）
   - `/ais list` → `/ais switch 2` → @机器人 这张图里有什么公式
6. 空闲 300 秒无活动，会话自动关闭（静默，不推送通知）；已关闭的会话按 id 切换时自动重建
7. DeepSeek 已把「快速 / 专家 / 识图模式」合并为统一对话，插件同步取消模式区分：文本与图片都在当前会话中处理

---

*文档更新日期：2026-10-11（v2.3.2）*
