import asyncio
import logging
from cloakbrowser import launch_async
from .config import STATE_FILE, QR_FILE

logger = logging.getLogger("astrbot")

class CloakAuthenticator:
    def execute_login_flow(self, task_state):
        """在独立线程中运行，内部使用独立事件循环 + launch_async"""
        asyncio.run(self._async_login(task_state))

    async def _async_login(self, task_state):
        """异步登录全流程"""
        browser = None
        try:
            logger.info("🚀 [LoginCore] 正在启动独立登录实例...")
            browser = await launch_async(headless=True, humanize=True)
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto("https://chat.deepseek.com/sign_in")

            logger.info("🔍 正在定位二维码外层容器...")
            qr_container = page.locator("#wxLogin, .ds-sign-in-with-wechat-block").first
            await qr_container.wait_for(state="visible", timeout=15000)

            logger.info("⏳ 等待微信二维码图像网络加载与页面渲染...")
            await asyncio.sleep(3)

            await qr_container.screenshot(path=QR_FILE)
            logger.info("📸 [LoginCore] 二维码区域截图成功！")

            # 信号 1：二维码就绪
            task_state.qr_path = QR_FILE
            task_state.qr_ready.set()

            logger.info("⏳ [LoginCore] 等待主人扫码确认...")
            chat_box = page.locator("textarea[name='search'], textarea[placeholder*='发送消息']").first
            await chat_box.wait_for(state="visible", timeout=120000)

            logger.info("💾 [LoginCore] 扫码成功，正在持久化状态凭证...")
            await context.storage_state(path=STATE_FILE)

            # 信号 2：扫码成功，凭证已保存
            task_state.success = True

        except Exception as e:
            logger.error(f"❌ [LoginCore] 登录流程异常: {e}")
            task_state.error = e
        finally:
            if browser:
                await browser.close()
            # 最终信号：不管成功失败，通知主线程
            task_state.done.set()