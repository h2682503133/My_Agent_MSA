"""在集群里用无头浏览器打开网页 chat，检查表情图片是否真的能显示。"""

import asyncio
import json

from playwright.async_api import async_playwright

BASE = "http://frontend-service.agent.svc.cluster.local"
USER_ID = "nh"


async def main() -> None:
    report = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        await page.goto(f"{BASE}/login.html", wait_until="domcontentloaded", timeout=30000)
        await page.fill("#user_id", USER_ID)
        await page.click("#login_btn")
        await page.wait_for_timeout(3000)

        await page.goto(f"{BASE}/chat.html", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2500)

        report["url"] = page.url
        report["imgs"] = await page.evaluate("""
            Array.from(document.querySelectorAll('img')).map(img => ({
              cls: img.className, src: img.getAttribute('src'),
              w: img.naturalWidth, h: img.naturalHeight,
              complete: img.complete,
            })).slice(0, 12)
        """)
        report["msgImgs"] = await page.evaluate("""
            Array.from(document.querySelectorAll('img.msg-img')).map(img => ({
              src: img.getAttribute('src'), w: img.naturalWidth,
            })).slice(0, 8)
        """)
        report["errors"] = errors[-6:]
        await browser.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
