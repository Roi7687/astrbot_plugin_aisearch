import os

# 获取插件根目录的绝对路径（core/ 的上级目录）
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(PLUGIN_DIR, "auth_state.json")
QR_FILE = os.path.join(PLUGIN_DIR, "qrcode.png")
CONVERSATIONS_FILE = os.path.join(PLUGIN_DIR, "conversations.json")

# ── 浏览器语言/时区（强制中文 UI，避免 Linux 等英文系统下 DeepSeek 返回英文界面导致文本匹配失败）──
BROWSER_LOCALE = "zh-CN"
BROWSER_TIMEZONE = "Asia/Shanghai"

# ── 会话行为配置 ──
IDLE_TIMEOUT_SECONDS = 300  # 会话无活动自动销毁（秒），修改后需重启插件生效
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 单张图片上限（上传前压缩，超过则警告跳过）

# ── 自动触发（无需 /ais 指令）──
AUTO_TRIGGER = True  # @机器人（或私聊）发送文本 → 自动进入 AI 搜索对话；仅切换会话时需用指令

# ── DeepSeek 网页版常量 ──
DEEPSEEK_BASE = "https://chat.deepseek.com"
DEFAULT_IMAGE_PROMPT = "请详细描述这张图片的内容。"  # 仅图片无文字时的默认提问
DEFAULT_QUOTE_PROMPT = "请针对我引用的这条消息进行回答。"  # 仅引用消息无文字时的默认提问
CARD_COVER_HINT = "（随附卡片封面图，请侧重描述封面画面，并结合卡片摘要作答）"  # 卡片带封面时的提示行（v2.3.3）

# 注：v2.3.0 起 DeepSeek 已把「快速 / 专家 / 识图模式」合并为统一对话
# （官方公告：「快速、专家、识图模式已合并升级」），图片理解内置于同一会话，
# 因此插件不再有「普通模式 / 识图模式」之分，相关常量与切换入口全部移除。


class AuthError(Exception):
    """自定义异常：当未找到登录凭证或凭证失效时抛出"""


class ConversationError(Exception):
    """自定义异常：会话操作失败（页面异常、风控、发送失败等）"""


class ImagePrepareError(ConversationError):
    """自定义异常：图片下载/解析失败（当前消息图片或引用图片都无法提交）"""
