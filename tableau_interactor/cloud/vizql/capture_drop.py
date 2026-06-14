"""One-shot capture: arm the hunter, drive a single drop via Playwright, report.

Non-interactive - safe to invoke when nobody's watching the browser. Writes the full
report to tableau_cloud_state/dispatcher_hunt.json and prints a summary.

    .venv/bin/python -m tableau_interactor.cloud.vizql.capture_drop
"""

import json
import sys
import time

from .connect import connect_to_workbook_page
from .hunt import arm, report


def _drag(page, source_locator, target_locator, *, steps: int = 12) -> None:
    """Tableau Cloud's custom DnD needs gradual mouse movement - Playwright's
    drag_to() does not work."""
    src = source_locator.bounding_box()
    dst = target_locator.bounding_box()
    if not src or not dst:
        raise RuntimeError("Could not get bounding box for drag source or target")

    sx = src["x"] + src["width"] / 2
    sy = src["y"] + src["height"] / 2
    tx = dst["x"] + dst["width"] / 2
    ty = dst["y"] + dst["height"] / 2

    page.mouse.move(sx, sy)
    time.sleep(0.2)
    page.mouse.down()
    time.sleep(0.3)
    for i in range(steps + 1):
        t = i / steps
        page.mouse.move(sx + (tx - sx) * t, sy + (ty - sy) * t)
        time.sleep(0.04)
    time.sleep(0.3)
    page.mouse.up()


def main() -> int:
    pw, page = connect_to_workbook_page()
    try:
        print(f"Page: {page.url}")
        if "/newWorkbook/" not in page.url and "/authoring/" not in page.url and "/v/" not in page.url:
            print("ERROR: not on an authoring page. Open a workbook sheet first.", file=sys.stderr)
            return 2

        # Make sure the Show Me panel isn't covering targets
        try:
            show_me = page.locator('[data-tb-test-id="showme-ToolbarButton"]')
            if show_me.count() and "tabActive" in (show_me.first.get_attribute("class") or ""):
                page.locator('text="Show Me"').click()
                time.sleep(0.5)
        except Exception:
            pass

        print("Arming hunter…")
        armed = arm(page)
        print(f"  candidates from initial walk: {armed.get('candidateCount', 0)}")
        for c in (armed.get("candidatePreview") or [])[:5]:
            print(f"    {c['path']}  methods={c['methods']}")

        # Drag a fresh field - uses current data pane selector .tab-schema-field-pill.
        import sys as _sys
        field_name = None
        shelf_name = "Columns"
        for candidate in ["Discount", "Profit", "Quantity", "Segment", "Region", "City"]:
            loc = page.locator(f'.tab-schema-field-pill:has-text("{candidate}")').first
            if loc.count() > 0 and loc.is_visible():
                field_name = candidate
                break
        if field_name is None:
            print("Could not find a fresh field in the data pane", file=_sys.stderr)
            return 3

        field = page.locator(f'.tab-schema-field-pill:has-text("{field_name}")').first
        target = page.locator(f'text="{shelf_name}"').first
        print(f"Driving a drag of '{field_name}' onto {shelf_name}…")
        _drag(page, field, target)
        time.sleep(2.0)  # allow trailing refresh commands

        result = report(page)
        out = "tableau_cloud_state/dispatcher_hunt.json"
        with open(out, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\nFull report → {out}")

        print(f"\n=== {result['commandCount']} command(s) captured ===")
        for c in result["commands"]:
            print(f"  [{c['ts']:>5}ms] {c['method']} /{c['namespace']}/{c['name']}  ({c['via']})")

        print(f"\n=== {result['candidateCount']} dispatcher candidate(s) ===")
        for cand in result["candidates"][:20]:
            print(f"  {cand['path']}  methods={cand['methods']}  ctor={cand.get('ctor')}")

        print(f"\n=== {result.get('callCount', 0)} dispatcher call(s) intercepted ===")
        for call in result.get("calls", [])[:20]:
            print(f"  [{call['ts']:>5}ms] {call['path']}.{call['method']}({call['argc']} args)")
            for i, a in enumerate(call["args"]):
                preview = json.dumps(a, default=str)
                if len(preview) > 240:
                    preview = preview[:240] + "…"
                print(f"      arg{i}: {preview}")

        return 0
    finally:
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
