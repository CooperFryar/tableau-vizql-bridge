"""Screenshot the authoring tab (after pressing Escape to clear stray menus).

Usage: python -m tableau_interactor.cloud.vizql.shot [out_path]
"""
import sys
import time

from .connect import connect_to_workbook_page


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "screenshots/authoring_now.png"
    goto = sys.argv[2] if len(sys.argv) > 2 else None
    pw, page = connect_to_workbook_page()
    try:
        page.bring_to_front()
        page.keyboard.press("Escape")
        if goto:
            from . import api, exec as ex
            ex.inject_dispatcher(page)
            api.goto_sheet(page, goto)
            time.sleep(1.5)
        time.sleep(0.5)
        page.screenshot(path=out)
        print(f"url: {page.url[:70]}")
        print(f"saved: {out}")
        return 0
    finally:
        pw.stop()


if __name__ == "__main__":
    raise SystemExit(main())
