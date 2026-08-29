import asyncio
import logging
from cloakbrowser import launch_async
from .config import BROWSER_LOCALE, BROWSER_TIMEZONE, STATE_FILE, QR_FILE

logger = logging.getLogger("astrbot")

# 微信二维码容器选择器兜底链（网页 UI 更新时按顺序尝试）
QR_SELECTORS = [
    "#wxLogin",
    ".ds-sign-in-with-wechat-block",
    "[class*='qrcode'] img, [class*='qr-code'] img",
]


class CloakAuthenticator:
    def execute_login_flow(self, task_state):
        """在独立线程中运行，内部使用独立事件循环 + launch_async"""
        asyncio.run(self._async_login(task_state))

    async def _async_login(self, task_state):
        """异步登录全流程"""
        browser = None
        try:
            logger.info("🚀 [LoginCore] 正在启动独立登录实例...")
            # 与正式会话内核保持一致：强制中文 UI，避免英文系统下登录到英文界面
            browser = await launch_async(
                headless=True,
                humanize=True,
                locale=BROWSER_LOCALE,
                timezone=BROWSER_TIMEZONE,
            )
            context = await browser.new_context(
                locale=BROWSER_LOCALE,
                timezone_id=BROWSER_TIMEZONE,
            )
            page = await context.new_page()

            await page.goto("https://chat.deepseek.com/sign_in")

            logger.info("🔍 正在定位二维码外层容器...")
            qr_container = None
            last_error = None
            for selector in QR_SELECTORS:
                try:
                    locator = page.locator(selector).first
                    await locator.wait_for(state="visible", timeout=8000)
                    qr_container = locator
                    logger.info(f"✅ [LoginCore] 二维码容器定位成功: {selector}")
                    break
                except Exception as e:
                    last_error = e
                    continue
            if qr_container is None:
                # 兜底：按文本「微信扫码登录」向上找容器
                try:
                    text_loc = page.locator("text=微信扫码登录").last
                    await text_loc.wait_for(state="visible", timeout=5000)
                    qr_container = text_loc.locator("xpath=ancestor::div[contains(@class, 'block') or contains(@class, 'login') or contains(@class, 'sign')][1]")
                    await qr_container.wait_for(state="visible", timeout=5000)
                    logger.info("✅ [LoginCore] 二维码容器定位成功（文本兜底）")
                except Exception as e2:
                    last_error = e2
            if qr_container is None:
                raise RuntimeError(f"二维码容器定位失败，最后错误: {last_error}")

            logger.info("⏳ 等待微信二维码图像网络加载与页面渲染...")
            await asyncio.sleep(3)

            await qr_container.screenshot(path=QR_FILE)
            logger.info("📸 [LoginCore] 二维码区域截图成功！")

            # 信号 1：二维码就绪
            task_state.qr_path = QR_FILE
            task_state.qr_ready.set()

            logger.info("⏳ [LoginCore] 等待主人扫码确认...")
            chat_box = page.locator("textarea[name='search'], textarea[placeholder*='发送消息'], textarea").first
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
