"""在集群里用无头浏览器打开 dashboard，验证工具权限页到底渲染了什么。

用法（在 tool-runtime-service pod 内执行）：
    python /tmp/check_dashboard.py
输出：console 报错、字段值、状态行文字。
"""

import asyncio
import json

from playwright.async_api import async_playwright

URL = "http://dashboard-service.agent.svc.cluster.local:5700/"
PASSWORD = "123456"        # dashboard 默认密码；不对也不影响读取 API（会走 401 显示）


async def main() -> None:
    errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page()
        page.on("console", lambda msg: errors.append(f"[{msg.type}] {msg.text}") if msg.type in ("error", "warning") else None)
        page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))

        await page.goto(URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)

        report = {}
        report["url"] = page.url
        report["tpRefreshType"] = await page.evaluate("typeof tpRefresh")
        report["scripts"] = await page.evaluate(
            "Array.from(document.querySelectorAll('script[src]')).map(s => s.src).filter(s => s.includes('toolperm'))"
        )
        report["tabChildren"] = await page.evaluate(
            "document.getElementById('tab-toolperm')?.children.length ?? null"
        )

        # 点一下「工具权限」页签
        try:
            await page.click('nav button[data-tab="toolperm"]', timeout=5000)
            await page.wait_for_timeout(1500)
        except Exception as exc:  # noqa: BLE001
            report["clickError"] = str(exc)

        report["afterClick"] = await page.evaluate("""
            (() => {
              const v = id => { const e = document.getElementById(id); return e ? e.value : null; };
              const t = id => { const e = document.getElementById(id); return e ? e.textContent.trim().slice(0, 200) : null; };
              return {
                status: t('tp-status'),
                levelsHtmlLen: (document.getElementById('tp-levels') || {}).innerHTML?.length ?? null,
                toolsHtmlLen: (document.getElementById('tp-tool-list') || {}).innerHTML?.length ?? null,
                l1: v('tp-blacklist'), l2: v('tp-l2'), l3: v('tp-l3'), l4: v('tp-l4'),
                noteAll: (v('tp-note-all') || '').slice(0, 40),
              };
            })()
        """)

        # 直接调用一次，排除"没触发"的可能
        try:
            await page.evaluate("tpRefresh()")
            await page.wait_for_timeout(1200)
            report["afterManualRefresh"] = await page.evaluate("""
                (() => {
                  const v = id => { const e = document.getElementById(id); return e ? e.value : null; };
                  return { status: (document.getElementById('tp-status')||{}).textContent,
                           l2: v('tp-l2'), l3: v('tp-l3') };
                })()
            """)
        except Exception as exc:  # noqa: BLE001
            report["manualRefreshError"] = str(exc)

        report["consoleErrors"] = errors[-12:]
        await browser.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
