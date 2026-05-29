# astrbot_plugin_aisearch 开发文档

> **插件名称**：AI搜索  
> **版本**：v1.3.0  
> **作者**：Roi  
> **许可证**：AGPL-3.0  
> **仓库地址**：https://github.com/Roi7687/astrbot_plugin_aisearch  

---

## 一、项目概述

这是一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件，通过自动化操控 DeepSeek Chat 网页端，实现**联网 AI 搜索**功能。用户在聊天中发送 `/ais <关键词>`，插件会在后台通过无头浏览器访问 DeepSeek Chat（开启了搜索模式），获取 AI 结果后以 Markdown 格式返回给用户。

核心思路：**不调用 API，而是直接自动化操控浏览器**，利用 DeepSeek Chat 自带的联网搜索能力获取实时信息。

---

## 二、项目结构

```
astrbot_plugin_aisearch/
├── core/                      # 核心业务模块
│   ├── __init__.py
│   ├── config.py              # 全局配置：路径常量 & 自定义异常
│   ├── search_core.py         # 单轮搜索：浏览器自动化执行搜索
│   ├── multi_search_core.py   # 多轮对话：持久化浏览器会话
│   └── login_core.py          # 登录模块：微信扫码登录 DeepSeek
├── __init__.py                # 包初始化文件（空）
├── main.py                    # 插件主入口：命令注册、异步调度
├── auth_state.json            # 持久化存储的登录凭证（cookies/localStorage）
├── metadata.yaml              # AstrBot 插件元数据
├── requirements.txt           # Python 依赖清单
├── README.md                  # 项目说明
├── LICENSE                    # AGPL-3.0 许可证
└── DEV.md                     # 本文档 — 开发文档
```

---

## 三、模块详解

### 3.1 `core/config.py` — 全局配置

| 配置项 | 说明 |
|--------|------|
| `PLUGIN_DIR` | 插件所在目录的绝对路径 |
| `STATE_FILE` | 登录凭证存储路径 (`auth_state.json`) |
| `QR_FILE` | 二维码截图临时存储路径 (`qrcode.png`) |
| `AuthError` | 自定义异常，未找到凭证或凭证失效时抛出 |

### 3.2 `main.py` — 插件主入口

继承 AstrBot 的 `Star` 基类，是整个插件的调度中心。

**核心组件：**

| 类 / 方法 | 说明 |
|-----------|------|
| `LoginTaskState` | 登录线程与 async 之间的信号桥 |
| `CloakSearchPlugin` | 插件主类 |
| `__init__()` | 初始化搜索引擎实例、多轮搜索引擎实例、超时计时器 |
| `ai_search_command()` | 命令 `/ais`（别名 `搜索`）— 直接 `await` 搜索 |
| `ai_multi_search_command()` | 命令 `/mais`（别名 `多轮搜索`）— 管理持久会话 |
| `login_command()` | 命令 `/cloak登录` — 后台线程执行扫码登录 |
| `_reset_session_expiry()` | 重置多轮会话空闲超时计时器（`asyncio.ensure_future`） |
| `_session_expiry_waiter()` | 异步等待超时后自动关闭会话 |
| `_close_multi_session()` | 关闭多轮对话会话并重置状态 |
| `_build_keyboard()` | 构建 QQ 键盘按钮数据（`action.type=2` 指令按钮） |
| `_parse_thinking_flag()` | 解析 `-t` 参数，返回 `(thinking, clean_query)` 元组 |
| `_try_send_with_keyboard()` | 在 QQ 官方平台发送带键盘按钮的消息（非 QQ 平台自动回退） |

**命令列表：**

| 命令 | 别名 | 用法 | 说明 |
|------|------|------|------|
| `/ais` | `搜索` | `/ais [-t] <搜索词>` | 执行 AI 联网搜索（单轮），`-t` 启用深度思考 |
| `/mais` | `多轮搜索` | `/mais [-t] <搜索词>` | 多轮对话搜索，`-t` 启用深度思考 |
| `/cloak登录` | — | `/cloak登录` | 微信扫码登录 DeepSeek |

**架构设计要点：**
- 搜索命令（`/ais`、`/mais`）采用**原生 `async/await`**，直接调用 Playwright Async API，无线程开销
- 多轮会话超时使用 `asyncio.ensure_future` 实现异步计时器
- 登录命令（`/cloak登录`）因需等待用户扫码（2+ 分钟），使用后台线程 + 独立事件循环（`asyncio.run`）避免阻塞主事件循环
- `AuthError` 被精准捕获，优雅引导用户重新登录
- **QQ 键盘按钮**：搜索结果自动附带指令按钮（`action.type=2`），点击后自动在输入框插入对应命令，方便连续搜索/对话；仅 QQ 官方平台生效，其他平台自动回退为纯文本

### 3.3 `core/login_core.py` — 登录模块

| 类 / 方法 | 说明 |
|-----------|------|
| `CloakAuthenticator` | 登录认证器 |
| `execute_login_flow()` | 同步入口，在独立线程中运行 `asyncio.run()` |
| `_async_login()` | 异步登录全流程（`launch_async` + 独立事件循环） |

**登录流程：**

1. `execute_login_flow()` 在独立线程中调用 `asyncio.run(_async_login())`
2. 使用 `launch_async` 启动无头浏览器（`headless=True, humanize=True`）
3. 导航至 `https://chat.deepseek.com/sign_in`
4. 定位微信二维码容器 `#wxLogin` / `.ds-sign-in-with-wechat-block`
5. 等待 3 秒渲染后截图保存为 `qrcode.png`
6. 通过 `task_state.qr_ready` 信号通知主程序二维码已就绪
7. 等待用户扫码（监听聊天输入框出现，最长 120 秒）
8. 扫码成功后调用 `context.storage_state()` 持久化凭证到 `auth_state.json`
9. 通过 `task_state.done` 信号通知主程序完成

### 3.4 `core/search_core.py` — 搜索核心

| 类 / 方法 | 说明 |
|-----------|------|
| `DeepSeekSearchCore` | 搜索引擎核心类（全异步） |
| `_init_browser()` | 按需初始化浏览器实例（懒加载，`launch_async`） |
| `_toggle_thinking()` | `async` — 切换 DeepSeek 深度思考模式（点击 toggle 按钮） |
| `execute_search(query, thinking)` | `async` — 执行搜索流程，`thinking=True` 启用深度思考 |
| `close()` | `async` — 关闭浏览器实例 |

**搜索流程：**

1. **懒加载初始化**：检查 `auth_state.json` 存在 → 启动无头浏览器 → 加载凭证 → 挂载流量嗅探器（捕获 `Bearer` Token）
2. **导航**：访问 `https://chat.deepseek.com/`
3. **深度思考切换**：若指定 `-t`，点击页面「深度思考」toggle 按钮（`aria-pressed` 状态检测）
4. **注入查询**：定位输入框 → 逐字输入（`delay=30ms`）→ 按 Enter 发送
4. **等待结果**：监听 `.ds-markdown.ds-assistant-message-main-content` 元素，采用**文本稳定检测**策略（连续 9 秒无变化视为输出完成）
5. **清理 DOM**：移除所有 `.ds-markdown-cite` 引用角标
6. **HTML → Markdown**：使用 `markdownify` 将 HTML 转换为 Markdown（ATX 标题风格）
7. **静默删除对话历史**：通过 DeepSeek API `chat_session/delete` 删除当前会话，保持页面干净
8. **返回结果**

### 3.5 `core/multi_search_core.py` — 多轮对话核心

| 类 / 方法 | 说明 |
|-----------|------|
| `MultiSearchCore` | 多轮对话搜索引擎（全异步） |
| `start_session()` | `async` — 启动持久化浏览器会话（首次 `/mais` 触发） |
| `_init_browser()` | `async` — 按需初始化浏览器实例（`launch_async`） |
| `_toggle_thinking()` | `async` — 切换 DeepSeek 深度思考模式 |
| `send_message(query, thinking)` | `async` — 在当前会话中发送消息并获取回复，`thinking=True` 启用深度思考 |
| `close()` | `async` — 关闭浏览器实例并标记会话结束 |

**与单轮搜索的关键区别：**

| 特性 | 单轮 `/ais` | 多轮 `/mais` |
|------|------------|-------------|
| 浏览器生命周期 | 每次搜索新建，用后销毁 | 会话期间持续存活 |
| 对话历史 | 搜索后自动删除 | 保留，支持上下文连续 |
| 页面导航 | 每次重新加载首页 | 首次加载后停留在对话页 |
| 空闲超时 | 无（用完即关） | 300 秒无活动自动关闭 |

---

## 四、技术栈

| 技术 | 版本 | 用途 |
|------|------|------|
| Python | 3.x | 主语言（async/await） |
| AstrBot API | — | 插件框架（`Star`, `Context`, `AstrMessageEvent`） |
| [cloakbrowser](https://pypi.org/project/cloakbrowser/) | 0.3.30 | 反检测无头浏览器，使用 `launch_async` Async API |
| [playwright](https://pypi.org/project/playwright/) | 1.60.0 | 底层浏览器自动化引擎（Async API） |
| [markdownify](https://pypi.org/project/markdownify/) | 0.14.1 | HTML → Markdown 转换 |
| [httpx](https://pypi.org/project/httpx/) | 0.28.1 | HTTP 客户端（依赖） |

---

## 五、数据流

### 单轮搜索（/ais）

```
用户发送 /ais 查询词
       │
       ▼
  ai_search_command() (async)
       │
       ▼ await
  DeepSeekSearchCore.execute_search()
       │
       ├─ await launch_async()   ← 首次懒加载
       ├─ await page.goto()
       ├─ await input_box.press_sequentially()
       ├─ await 等待文本稳定（9 秒无变化）
       ├─ await HTML → Markdown
       ├─ await 删除对话历史
       │
       ▼
  返回结果给用户
```

### 多轮对话（/mais）

```
用户发送 /mais 查询词
       │
       ▼
  ai_multi_search_command() (async)
       │
       ├─ 首次？ ──→ await start_session()
       │             启动持久浏览器（launch_async）
       │
       ▼ await
  MultiSearchCore.send_message()
       │
       ├─ await press_sequentially()  ← 同一对话页
       ├─ await 等待文本稳定
       ├─ await HTML → Markdown
       │
       ▼
  返回结果（支持上下文连续追问）
       │
       ▼
  _reset_session_expiry()
  asyncio.ensure_future(_session_expiry_waiter())
       │
       ··· 300 秒无活动 ···
       │
       ▼
  await _close_multi_session() → 自动关闭浏览器
```

### 登录（/cloak登录）

```
用户发送 /cloak登录
       │
       ▼
  login_command() (async generator)
       │
       ├─ 启动后台线程 ──→ execute_login_flow()
       │                    │
       │                    ▼ asyncio.run()
       │               _async_login() (launch_async)
       │                    │
       │                    ├─ 截图二维码 → task_state.qr_ready.set()
       │                    │
       │  await asyncio.sleep(1) 轮询 ◀─┘
       │                    │
       ├─ yield 二维码图片   │
       ├─ yield 提示扫码     │
       │                    ├─ 等待扫码（120 秒）
       │  await asyncio.sleep(2) 轮询 ◀─┘
       │                    │
       ├─ yield 扫码结果     task_state.done.set()
       │
       ▼
  清理临时文件
```

---

## 六、当前开发进度

### ✅ 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 插件框架搭建 | ✅ 完成 | 继承 `Star`，注册命令，符合 AstrBot 插件规范 |
| 命令 `/ais`（单轮搜索） | ✅ 完成 | 支持中英文关键词，别名 `搜索`，原生 async/await |
| 命令 `/mais`（多轮对话） | ✅ 完成 | 别名 `多轮搜索`，持久浏览器会话，支持上下文连续 |
| 命令 `/cloak登录` | ✅ 完成 | 微信扫码登录 DeepSeek Chat |
| 微信扫码登录流程 | ✅ 完成 | 二维码截图 → 发送到聊天 → 等待扫码 → 凭证持久化 |
| 无头浏览器搜索 | ✅ 完成 | 使用 `cloakbrowser.launch_async` Async API，反检测 |
| 搜索结果 Markdown 化 | ✅ 完成 | 通过 `markdownify` 将 HTML 转为标准 Markdown |
| 对话历史自动清理 | ✅ 完成 | 单轮搜索完成后通过 API 静默删除 DeepSeek 会话 |
| DOM 引用角标清理 | ✅ 完成 | 移除 `.ds-markdown-cite` 元素 |
| 网络层 Bearer Token 嗅探 | ✅ 完成 | 通过 `page.on("request")` 拦截并保存认证令牌 |
| 原生异步架构 | ✅ 完成 | 搜索路径全 async/await，登录使用线程 + 独立事件循环 |
| 多轮会话空闲超时 | ✅ 完成 | 300 秒无活动自动关闭，`asyncio.ensure_future` 计时器 |
| 凭证失效优雅提示 | ✅ 完成 | 捕获 `AuthError`，引导用户重新登录 |
| QQ 键盘指令按钮 | ✅ 完成 | 搜索结果附带按钮（`action.type=2`），点击自动插入命令，仅 QQ 官方平台 |
| 深度思考模式 | ✅ 完成 | `-t` 参数启用 DeepSeek 深度思考，通过 toggle 按钮自动化切换 |
| 许可证 | ✅ 完成 | AGPL-3.0 |

### ⚠️ 已知问题 / 待改进

*暂无* 

### 🔮 后续开发建议

*暂无*

---

## 七、安装与使用

### 使用方法

1. 首次使用前，发送 `/cloak登录` 进行微信扫码登录
2. **单轮搜索**：`/ais [-t] <搜索词>`，每次独立搜索；`-t` 启用深度思考
3. **多轮对话**：`/mais [-t] <搜索词>`，浏览器保持存活，支持连续追问；5 分钟无活动后自动结束
4. 示例：
   - `/ais 今天有什么大新闻`
   - `/ais -t 解释量子纠缠的原理`（深度思考模式）
   - `/mais 帮我对比 Python 和 Go 的优缺点` → 继续追问 `/mais 那 Rust 呢`
   - QQ 群聊中搜索结果会附带按钮，点击即可快速发起下一轮搜索/对话

---

*文档更新日期：2026-05-29*
