import os

# 获取插件根目录的绝对路径（core/ 的上级目录）
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(PLUGIN_DIR, "auth_state.json")
QR_FILE = os.path.join(PLUGIN_DIR, "qrcode.png")

class AuthError(Exception):
    """自定义异常：当未找到登录凭证或凭证失效时抛出"""
    pass