import os

# 获取插件根目录的绝对路径（core/ 的上级目录）
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(PLUGIN_DIR, "auth_state.json")
QR_FILE = os.path.join(PLUGIN_DIR, "qrcode.png")
CONVERSATIONS_FILE = os.path.join(PLUGIN_DIR, "conversations.json")
VISION_DEBUG_SHOT = os.path.join(PLUGIN_DIR, "vision_debug.png")  # 识图入口查找失败时的调试截图

# ── 浏览器语言/时区（强制中文 UI，避免 Linux 等英文系统下 DeepSeek 返回英文界面导致文本匹配失败）──
BROWSER_LOCALE = "zh-CN"
BROWSER_TIMEZONE = "Asia/Shanghai"

# ── 会话行为配置 ──
IDLE_TIMEOUT_SECONDS = 300  # 会话无活动自动销毁（秒），修改后需重启插件生效
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 单张图片上限（上传前压缩，超过则警告跳过）

# ── 固定启用模式（每次发送前自动确保开启，无需 -t 旗标）──
ALWAYS_DEEP_THINK = True  # 固定开启「深度思考」
ALWAYS_WEB_SEARCH = True  # 固定开启「联网搜索」

# ── 自动触发（无需 /ais 指令）──
AUTO_TRIGGER = True  # @机器人（或私聊）发送文本 → 自动进入 AI 搜索对话；仅切换会话时需用指令

# ── DeepSeek 网页版常量 ──
DEEPSEEK_BASE = "https://chat.deepseek.com"
VISION_WELCOME_TEXT = "使用识图模式开始对话"  # 识图模式新会话欢迎语
DEFAULT_VISION_PROMPT = "请详细描述这张图片的内容。"  # 仅图片无文字时的默认提问

# ── 会话模式 ──
MODE_NORMAL = "normal"
MODE_VISION = "vision"
MODES = (MODE_NORMAL, MODE_VISION)
MODE_LABELS = {
    MODE_NORMAL: "普通对话",
    MODE_VISION: "识图对话",
}


class AuthError(Exception):
    """自定义异常：当未找到登录凭证或凭证失效时抛出"""


class ConversationError(Exception):
    """自定义异常：会话操作失败（页面异常、风控、识图模式不可用等）"""
