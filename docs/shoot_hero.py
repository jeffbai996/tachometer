"""Render docs/hero.png from a running tachometer.

    python -m tachometer --port 8710 &
    python docs/shoot_hero.py

Kept in the repo rather than done by hand so the hero can be regenerated when
the panel changes, instead of drifting until it shows a version nobody ships.

Requires playwright (`pip install playwright && playwright install chromium`).
"""
from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8710/"
OUT = sys.argv[2] if len(sys.argv) > 2 else "docs/hero.png"


async def main() -> None:
    async with async_playwright() as p:
        # --no-sandbox: without it chromium starts but every navigation to a
        # local HTTP server hangs until timeout on some Linux setups.
        browser = await p.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page(
            viewport={"width": 1200, "height": 900}, device_scale_factor=2)
        # NOT networkidle: the panel polls forever, so the network never goes
        # idle and the wait would hang until it times out.
        await page.goto(URL, wait_until="load", timeout=30000)
        # Let the pollers land and the curve fill before shooting — an empty
        # chart is exactly what the hero must not show.
        await page.wait_for_timeout(9000)
        try:
            await page.click('.rk-metric-btn[data-metric="tok"]', timeout=5000)
            await page.wait_for_timeout(1200)
        except Exception:
            pass
        # Turn the vendor split on: it is the thing that says most about what
        # the panel does in a single frame.
        try:
            needs_click = await page.evaluate(
                "() => {const b = document.getElementById('rk-mixbtn');"
                " return !!b && !b.hidden && !b.classList.contains('is-on');}")
            if needs_click:
                await page.click("#rk-mixbtn", timeout=5000)
                await page.wait_for_timeout(1200)
        except Exception:
            pass
        await page.locator("#tach").screenshot(path=OUT, timeout=30000)
        await browser.close()
    print("wrote", OUT)


asyncio.run(main())
