---
name: start
description: "Build or modify Tableau Cloud worksheets and dashboards by talking to Claude. Use when the user types /tableau:start or asks to create/edit a Tableau Cloud chart, filter, calculated field, parameter, or dashboard. Drives Tableau Cloud's internal VizQL command API via a bundled Python library, and sets up its own environment on first run. No coding required of the user."
---

# Tableau Cloud authoring

This skill lets a non-developer build Tableau Cloud vizzes by describing what they
want. Claude turns the request into VizQL wire commands (via the bundled
`tableau_interactor` library) and runs them against the user's live, signed-in
Tableau Cloud session. No official Tableau API.

Two stable locations, both provided by Claude Code at runtime:
- `${CLAUDE_PLUGIN_ROOT}` - where this plugin's bundled code lives (read-only).
- `${CLAUDE_PLUGIN_DATA}` - a persistent writable folder that survives updates.
  The venv, the saved Tableau login, and temp build scripts live here.

Throughout, use the plugin's own Python: `${CLAUDE_PLUGIN_DATA}/.venv/bin/python`.

## Step 1: First-run setup (skip if `${CLAUDE_PLUGIN_DATA}/.venv` already exists)

Check first: if `${CLAUDE_PLUGIN_DATA}/.venv/bin/python` exists, setup is done, go
to Step 2. Otherwise run setup, explaining to the user that the first run installs
a private environment and may take a couple of minutes:

1. Find a Python 3.11+ interpreter and create the venv in one step (the system
   `python3` is often too old, e.g. 3.9 on macOS, so probe several names):
   ```bash
   PYBIN=""
   for c in python3.13 python3.12 python3.11 python3 python; do
     command -v "$c" >/dev/null 2>&1 || continue
     "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null && { PYBIN="$c"; break; }
   done
   if [ -z "$PYBIN" ]; then echo "NO_PYTHON_311"; else "$PYBIN" -m venv "${CLAUDE_PLUGIN_DATA}/.venv"; echo "USED $PYBIN"; fi
   ```
   If it prints `NO_PYTHON_311`, tell the user to install Python 3.11+ from
   python.org, then stop.
2. Install the bundled library (and Playwright) into it:
   ```bash
   "${CLAUDE_PLUGIN_DATA}/.venv/bin/pip" install -q "${CLAUDE_PLUGIN_ROOT}"
   ```
3. Download the browser Playwright drives:
   ```bash
   NODE_OPTIONS="--no-deprecation" "${CLAUDE_PLUGIN_DATA}/.venv/bin/python" -m playwright install chromium
   ```
4. Ask the user for their Tableau Cloud sign-in URL (the pod URL, e.g.
   `https://10ay.online.tableau.com`). Write it to `${CLAUDE_PLUGIN_DATA}/.env`:
   ```bash
   printf 'TABLEAU_CLOUD_URL=%s\n' "<the url they gave>" > "${CLAUDE_PLUGIN_DATA}/.env"
   ```
   Do not ask for a password. Login happens in the browser (Step 2), and the
   session is remembered after that.

## Step 2: Make sure a signed-in session is running

The session is a persistent browser the build scripts attach to over CDP.

1. Check if one is already up:
   ```bash
   NODE_OPTIONS="--no-deprecation" "${CLAUDE_PLUGIN_DATA}/.venv/bin/python" -c "from tableau_interactor.cloud.vizql.connect import connect_to_workbook_page as c; pw,p=c(); print('SESSION_OK', p.url); pw.stop()"
   ```
   If it prints `SESSION_OK`, skip to Step 3.
2. If it fails, start one in the background (run from the data dir so the saved
   login persists there):
   ```bash
   cd "${CLAUDE_PLUGIN_DATA}" && NODE_OPTIONS="--no-deprecation" "${CLAUDE_PLUGIN_DATA}/.venv/bin/python" -m tableau_interactor.cloud.session
   ```
   Launch it with `run_in_background`. A Chromium window opens.
3. Tell the user: **log in to Tableau Cloud in the window that opened, then open or
   create a workbook so you are in an authoring view, and tell me when you're
   ready.** The login is saved to `${CLAUDE_PLUGIN_DATA}/tableau_cloud_state/` and
   reused next time, so this is usually a one-time step.

## Step 3: Ask what they want to do

Keep it short. Offer:
1. **Continue the workbook that's open** - attach, report the active sheet and
   current viz state, then take instructions.
2. **Start a new workbook** - the wire stack drives an open workbook, it does not
   create one. Ask the user to click New Workbook in the Tableau window and connect
   their data (for example Superstore), confirm a blank sheet is showing, then
   continue.
3. **Something else** - inspect/verify a sheet, clean up stray fields, take a
   screenshot, etc.

## Step 4: Build what they ask for

For each request ("a bar chart of Sales by Region", "add a top 10 filter", "make a
dashboard that cross-filters it"):

1. Find the needed calls in `${CLAUDE_PLUGIN_ROOT}/API_REFERENCE.md` (full
   signatures, the source of truth). Prefer `recipes.py` builders for whole charts;
   use `api.py` primitives for individual edits.
2. Write a temp script to `${CLAUDE_PLUGIN_DATA}/_<task>.py` and run it with the
   venv Python. The library is pip-installed into the venv, so plain imports work.
   Standard shape:
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
       print(api.viz_status(page))       # verify, do not guess
   finally:
       pw.stop()
   ```
   Run it:
   ```bash
   "${CLAUDE_PLUGIN_DATA}/.venv/bin/python" "${CLAUDE_PLUGIN_DATA}/_<task>.py"
   ```
3. Confirm the result with `api.viz_status(page)` and tell the user what was built.
   Use `api.screenshot(page, path)` only when they want to see it.
4. Ask for the next instruction, or stop.

## Rules
- `api.*` primitives auto-flush the UI. After a raw `ex.send_command`, call
  `api.flush_ui(page)` yourself.
- Reference fields by display name; the resolver handles calc fields, bins, groups,
  sets, parameters, and uploaded-CSV columns. Never hand-build an `fn`.
- Always `pw.stop()` in a `finally`.
- Check `${CLAUDE_PLUGIN_ROOT}/tableau_interactor/cloud/vizql/PROTOCOL.md`
  (Known limitations) before relying on an edge feature.
- If an operation has no primitive yet, capture it: arm `watch.py`, do the action
  by hand in the browser, read the request body, then wrap it as a new primitive.

## Reference docs (in `${CLAUDE_PLUGIN_ROOT}`)
- `README.md` - overview.
- `API_REFERENCE.md` - full primitive list.
- `tableau_interactor/cloud/vizql/PROTOCOL.md` - wire facts and known limitations.
