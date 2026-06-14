"""Connect to the running Playwright session and pick the workbook authoring page."""

import os

from playwright.sync_api import sync_playwright, Page


def connect_to_workbook_page() -> tuple[object, Page]:
    """Connect to the running CDP browser and return (playwright, page).

    Prefers a page whose URL looks like an authoring view (`/authoring/` or `/v/`),
    falls back to a `$newWorkbook$_` tab, then to whichever page is frontmost.
    Caller is responsible for stopping the playwright instance.
    """
    cdp_url = os.getenv("TABLEAU_CDP_URL", "http://localhost:9222")
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0]
    pages = context.pages
    if not pages:
        raise RuntimeError("No pages open in the CDP session")

    def score(p: Page) -> int:
        url = p.url or ""
        if "/authoring/" in url:
            return 3
        if "/v/" in url and "/sessions/" not in url:
            return 2
        if "newWorkbook" in url:
            return 2
        if "tableau" in url:
            return 1
        return 0

    page = max(pages, key=score)
    page.bring_to_front()
    return pw, page
