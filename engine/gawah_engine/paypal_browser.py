"""Headless buyer approval (route 2). Needs `playwright install chromium` and a sandbox PERSONAL
buyer account. Only used when card processing is not enabled for the merchant app."""

from __future__ import annotations

from pathlib import Path

RETURN_URL = "http://localhost:9/gawah-return"  # never served; we only watch the redirect


def approve_in_browser(approve_url: str, email: str, password: str, shot_dir: Path | None = None):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(approve_url, wait_until="domcontentloaded")
            page.locator("#email").fill(email)
            if page.locator("#btnNext").count():
                page.locator("#btnNext").click()
            page.locator("#password").wait_for(timeout=20000)
            page.locator("#password").fill(password)
            page.locator("#btnLogin").click()
            btn = page.locator("#payment-submit-btn, button:has-text('Complete Purchase'), "
                               "button:has-text('Pay Now'), button:has-text('Continue')").first
            btn.wait_for(timeout=30000)
            with page.expect_request(lambda r: r.url.startswith(RETURN_URL), timeout=45000):
                btn.click()
        except Exception:
            if shot_dir:
                shot_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot_dir / "approve_failure.png"))
            raise
        finally:
            browser.close()
