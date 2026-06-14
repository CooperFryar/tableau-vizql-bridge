"""Persistent Playwright session that stays open between commands.

Usage:
    python -m tableau_interactor.cloud.session start
    # Browser stays open, writes PID and CDP endpoint to session.json
    # Other scripts connect to the running browser via CDP
"""

import json
import os
import signal
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

SESSION_FILE = Path("tableau_cloud_state/session.json")
STATE_DIR = Path("tableau_cloud_state")


def _settle(page):
    """Wait for the page to settle without ever throwing.

    Tableau Cloud's SPA frequently never reaches ``networkidle`` (background
    polling keeps the network busy), so a bare ``wait_for_load_state("networkidle")``
    will time out after 30s and - because it runs before the keep-alive loop -
    kill the whole session, closing the browser. Fall back to ``domcontentloaded``
    and swallow timeouts so startup can never tear the session down.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:
            pass


def start_session():
    """Start a persistent browser session. Blocks until killed."""
    STATE_DIR.mkdir(exist_ok=True)

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=False,
        args=["--remote-debugging-port=9222"],
    )

    storage_state = STATE_DIR / "auth.json" if (STATE_DIR / "auth.json").exists() else None
    context = browser.new_context(
        storage_state=str(storage_state) if storage_state else None,
        no_viewport=True,  # let the page follow the actual window size
    )
    page = context.new_page()

    # Login
    url = os.getenv("TABLEAU_CLOUD_URL", "")
    email = os.getenv("TABLEAU_EMAIL", "")
    password = os.getenv("TABLEAU_PASSWORD", "")

    if url:
        page.goto(url)
        _settle(page)
        time.sleep(2)

        # Dismiss cookies
        for sel in ['button:has-text("Accept All")', 'button:has-text("Accept")', '#onetrust-accept-btn-handler']:
            btn = page.locator(sel)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                time.sleep(1)
                break

        if email and password:
            # Email step
            email_input = page.locator('input[type="email"], input[name="email"], #email')
            if email_input.count() > 0:
                email_input.first.fill(email)
                submit = page.locator('button[type="submit"], button:has-text("Sign In")')
                if submit.count() > 0:
                    submit.first.click()
                _settle(page)
                time.sleep(2)

            # Cookie again
            for sel in ['button:has-text("Accept All")', 'button:has-text("Accept")', '#onetrust-accept-btn-handler']:
                btn = page.locator(sel)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    time.sleep(1)
                    break

            # Password step
            pw_input = page.locator('input[type="password"], input[name="password"], #password')
            if pw_input.count() > 0:
                pw_input.first.fill(password)
                submit = page.locator('button[type="submit"], button:has-text("Sign In")')
                if submit.count() > 0:
                    submit.first.click()
                _settle(page)
                time.sleep(3)

    # Save auth
    context.storage_state(path=str(STATE_DIR / "auth.json"))

    # Save session info
    session_info = {"pid": os.getpid(), "cdp": "http://localhost:9222"}
    SESSION_FILE.write_text(json.dumps(session_info))

    print(f"Session started (PID {os.getpid()})")
    print(f"Current URL: {page.url}")
    print("Browser is open. Use connect() from other scripts.")
    print("Press Ctrl+C to stop.")

    # Keep alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        context.storage_state(path=str(STATE_DIR / "auth.json"))
        browser.close()
        pw.stop()
        SESSION_FILE.unlink(missing_ok=True)
        print("Session closed.")


if __name__ == "__main__":
    start_session()
