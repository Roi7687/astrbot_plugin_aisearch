import asyncio
import logging
import os
from markdownify import markdownify as md
from cloakbrowser import launch_async
from .config import STATE_FILE, AuthError

logger = logging.getLogger("astrbot")

class DeepSeekSearchCore:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self.bearer_token = None

    async def _init_browser(self):
        """内部初始化机制：按需唤醒"""
        if not os.path.exists(STATE_FILE):
            raise AuthError("本地未找到登录凭证。")

        logger.info("🚀 [SearchCore] 正在启动无痕搜索内核...")
        self.browser = await launch_async(headless=True, humanize=True)
        self.context = await self.browser.new_context(storage_state=STATE_FILE)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(30000)

        def sniff_auth_token(request):
            auth_header = request.headers.get("authorization")
            if auth_header and auth_header.startswith("Bearer"):
                self.bearer_token = auth_header

        self.page.on("request", sniff_auth_token)

    async def _toggle_thinking(self, enable: bool):
        """尝试开启 DeepSeek 深度思考模式"""
        if not enable:
            return
        try:
            btn = self.page.locator('.ds-toggle-button').filter(has_text="深度思考")
            await btn.wait_for(state="visible", timeout=30000)
            await btn.click()
            await asyncio.sleep(1)
        except Exception:
            pass

    async def execute_search(self, query: str, thinking: bool = False) -> str:
        """执行搜索流"""
        if not self.browser:
            await self._init_browser()

        await self.page.goto("https://chat.deepseek.com/")
        await self.page.wait_for_load_state("networkidle")
        await asyncio.sleep(3)  # 等待 React 组件（深度思考按钮等）完全渲染

        # 0. 切换深度思考
        await self._toggle_thinking(thinking)

        # 1. 注入查询并发送
        input_box = self.page.locator("textarea[name='search'], textarea[placeholder*='发送消息']").first
        await input_box.wait_for(state="visible")
        await input_box.click()
        await asyncio.sleep(0.5)
        await input_box.press_sequentially(query, delay=30)
        await asyncio.sleep(0.5)
        await input_box.press("Enter")

        # 2. 监听文本稳定输出
        answer_locator = self.page.locator("div.ds-markdown.ds-assistant-message-main-content")
        await answer_locator.last.wait_for(state="visible", timeout=30000)

        previous_text = ""
        stable_count = 0
        while True:
            current_text = await answer_locator.last.inner_text()
            if current_text == previous_text and len(current_text) > 0:
                stable_count += 1
            else:
                stable_count = 0
                previous_text = current_text

            if stable_count >= 18:
                break
            await asyncio.sleep(0.5)

        try:
            await self.page.evaluate("() => document.querySelectorAll('.ds-markdown-cite').forEach(el => el.remove())")
        except Exception as e:
            logger.warning(f"⚠️ 清理 DOM 引用角标时发生轻微异常: {e}")

        raw_html = await answer_locator.last.inner_html()

        # 3. HTML → Markdown（ATX 标题风格）
        final_answer = md(raw_html, heading_style="ATX")

        # 4. 静默删除对话历史
        try:
            current_url = self.page.url
            if "/s/" in current_url:
                session_id = current_url.split("/s/")[-1]
                delete_api = "https://chat.deepseek.com/api/v0/chat_session/delete"
                headers = {"content-type": "application/json", "x-client-platform": "web"}
                if self.bearer_token:
                    headers["authorization"] = self.bearer_token

                await self.context.request.post(delete_api, data={"chat_session_id": session_id}, headers=headers)
                await self.page.goto("https://chat.deepseek.com/")
        except Exception as e:
            logger.warning(f"网络层删除历史记录失败: {e}")

        return final_answer

    async def close(self):
        if self.browser:
            await self.browser.close()
            self.browser = None