---
name: tableau
description: "Conversational Tableau Cloud authoring. Invoke when the user types /tableau or asks to build/modify a Tableau Cloud worksheet or dashboard (a chart, filter, calc field, parameter, dashboard, etc.). Drives Tableau Cloud's internal VizQL command API via the tableau-vizql-bridge library - no official API."
---

# /tableau - build Tableau Cloud vizzes by talking to Claude

This skill lets you author Tableau Cloud worksheets and dashboards by describing
what you want. Claude turns the request into VizQL **wire commands** (via the
`tableau_interactor.cloud.vizql` library) and runs them against your live,
signed-in Tableau Cloud session - no clicking, no official API.

## When invoked, run this flow

### 1. Make sure a session is live
Everything attaches to a persistent, logged-in browser over CDP. Check it's up:

```bash
NODE_OPTIONS="--no-deprecation" .venv/bin/python -c "from tableau_interactor.cloud.vizql.connect import connect_to_workbook_page as c; pw,p=c(); print('OK', p.url); pw.stop()"
```

- If it connects → continue.
- If it fails → tell the user to start one (and wait for them):
  ```
  NODE_OPTIONS="--no-deprecation" .venv/bin/python -m tableau_interactor.cloud.session
  ```
  Then they sign in (it can reuse saved auth) and leave the window open.

### 2. Ask what they want to do
Ask the user to pick one - keep it short:

1. **Continue the workbook that's already open** - Claude attaches to the current
   authoring view and reports the active sheet + current viz state, then takes
   the next instruction.
2. **Start a new workbook** - the wire stack drives an *open* workbook; it does
   not create one. Ask the user to click **New Workbook** in the Tableau Cloud
   window and connect their data source (e.g. Superstore), confirm a blank
   worksheet is showing, then Claude takes over.
3. **Something else** - e.g. inspect/verify a sheet, clean up stray fields,
   export a screenshot, or capture a not-yet-supported wire command. Handle per
   the request using the primitives below.

### 3. Build what they ask for
For each request ("a bar chart of Sales by Region", "add a top-10 filter", "make
a dashboard that cross-filters the bar chart"):

1. Look up the needed primitives in **`API_REFERENCE.md`** (full signatures) -
   the source of truth for what's callable. Prefer `recipes.py` builders for
   whole charts; drop to `api.py` primitives for individual edits.
2. **Write a named temp script** `tableau_interactor/cloud/vizql/_<task>.py` and
   run it with `.venv/bin/python -m ...`. Do not inline Python in bash. The
   standard shape:

   ```python
   import os
   os.environ["NODE_OPTIONS"] = "--no-deprecation"
   from tableau_interactor.cloud.vizql.connect import connect_to_workbook_page
   from tableau_interactor.cloud.vizql import exec as ex, api, recipes

   pw, page = connect_to_workbook_page()
   try:
       ex.inject_dispatcher(page)        # idempotent
       api.close_open_dialogs(page)      # start clean
       sheet = api.active_sheet_name(page)
       recipes.bar_chart(page, "Region", "Sales", color="Segment")
       print(api.viz_status(page))       # verify, don't guess
   finally:
       pw.stop()
   ```
3. **Verify with `api.viz_status(page)`** (marks/rows/columns/aggregations) and
   report what was built. Use `api.screenshot(page, path)` only when the user
   wants to see it.
4. Then ask for the next instruction, or stop.

## Rules
- **`api.*` primitives auto-flush the UI.** If you call a raw `ex.send_command`,
  call `api.flush_ui(page)` yourself afterward.
- **Reference fields by display name** - the resolver handles calc fields, bins,
  groups, sets, parameters, and uploaded-CSV columns. Never hand-build an `fn`.
- **Always `pw.stop()` in a `finally`** - the caller owns the playwright instance.
- **Check `PROTOCOL.md § Known limitations`** before relying on an edge feature.
- If an operation has no primitive yet, capture it: arm `watch.py`, do the action
  by hand in the browser, read the request body, then wrap it as a new `api.py`
  primitive (and add it to `API_REFERENCE.md` + `PROTOCOL.md`). The diagnostic
  rule "0 drop targets ⇒ wrong fn" is the escape hatch when a field won't resolve.

## Reference docs
- `README.md` - overview and setup.
- `API_REFERENCE.md` - full primitive list with signatures.
- `tableau_interactor/cloud/vizql/PROTOCOL.md` - wire facts, command vocabulary,
  known limitations.
