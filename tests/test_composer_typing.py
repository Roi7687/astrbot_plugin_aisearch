"""输入框写入方式回归测试（需要 cloakbrowser，起一个无头浏览器，约 10 秒）。

**为什么需要这个测试（v2.3.3）**

线上现象（2026-10-11）：引用一条合并转发消息提问后，DeepSeek 180 秒没有任何
新回复，插件报「等待 AI 回答超时」；再往前一次（11:49）引用卡片消息时，模型
的回答全在解释卡片封面那条 CDN 链接。

根因不在解析，而在**发送**：`session_core._type_and_send` 用
`input_box.press_sequentially(text)` 逐字输入，而**逐字输入会把文本里的 `\\n`
当成回车键按下**——在聊天输入框里回车就是「发送」，于是整段多行提问被拆成
多条消息碎片：

    提问 "第一行\\n第二行\\n第三行"  →  实际发出 3 条独立消息

本测试用一个本地 HTML（textarea + input 事件同步模型 + Enter 发送）复现该行为，
并验证修复方案 `page.keyboard.insert_text()` 能整体写入、只提交一次。

没有 cloakbrowser 时自动跳过（返回 0），不影响其他纯函数测试。
"""

import asyncio
import os
import sys
import tempfile
import traceback

HTML = """<!doctype html><meta charset="utf-8">
<body>
<textarea id="box" style="width:400px;height:200px"></textarea>
<script>
  window.submits = [];
  window.model = '';
  window.inputEvents = 0;
  const box = document.getElementById('box');
  box.addEventListener('input', () => { window.model = box.value; window.inputEvents++; });
  box.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!window.model) return;          // 状态为空 → 发送无效（模拟按钮禁用）
      window.submits.push(box.value);
      box.value = ''; window.model = '';
    }
  });
</script>
</body>"""

PROMPT = "[引用消息] 第一条\n第二条\n第三条"


async def run_checks(failed):
    try:
        from cloakbrowser import launch_async
    except Exception as e:  # pragma: no cover - 无浏览器依赖的环境
        print(f"SKIP: 未安装 cloakbrowser（{e}），跳过输入框回归测试")
        return False

    path = os.path.join(tempfile.mkdtemp(prefix="ais_composer_"), "composer.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(HTML)

    browser = await launch_async(headless=True, humanize=True, locale="zh-CN")
    try:
        page = await browser.new_page()
        await page.goto("file:///" + path.replace("\\", "/"))
        box = page.locator("#box")

        # ① 旧做法（逐字输入）：多行提问会被拆成多条消息 —— 证明 bug 存在，
        #    也保证将来若有人改回逐字输入，这个测试会立刻失败报警。
        await box.click()
        await box.press_sequentially(PROMPT, delay=20)
        submits = await page.evaluate("() => window.submits")
        if len(submits) != 2 or submits != ["[引用消息] 第一条", "第二条"]:
            failed.append(
                f"逐字输入的行为与预期不符（可能 Playwright/humanize 变更）: {submits!r}"
            )

        # ② 新做法（一次性插入）：整体写入，Enter 只提交一次，且内容完整
        await page.reload()
        box = page.locator("#box")
        await box.click()
        await page.keyboard.insert_text(PROMPT)
        model = await page.evaluate("() => window.model")
        if model != PROMPT:
            failed.append(f"insert_text 未同步到框架状态（input 事件）: {model!r}")
        await box.press("Enter")
        submits = await page.evaluate("() => window.submits")
        if submits != [PROMPT]:
            failed.append(f"insert_text + Enter 应整体提交一次，实际 {submits!r}")

        # ③ 新做法在单行提问上同样正常
        await page.reload()
        box = page.locator("#box")
        await box.click()
        await page.keyboard.insert_text("今天有什么大新闻")
        await box.press("Enter")
        submits = await page.evaluate("() => window.submits")
        if submits != ["今天有什么大新闻"]:
            failed.append(f"单行 insert_text + Enter 异常: {submits!r}")
    finally:
        await browser.close()
    return True


def main():
    failed = []
    ran = asyncio.run(run_checks(failed))
    if not ran:
        return 0
    if failed:
        for line in failed:
            print(f"FAIL: {line}")
        print(f"COMPOSER TYPING: {len(failed)} CHECK(S) FAILED")
        return 1
    print("ALL COMPOSER TYPING CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
