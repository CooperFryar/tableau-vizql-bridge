# tableau-vizql-bridge - API Reference

The public function surface for programmatic Tableau Cloud authoring. Every primitive accepts plain arguments and hides wire-protocol quirks internally - callers (other Python code, eventually a UI app) should never need to know about `paneSpec`, encoding-shelf indices, or calc-field reference formats.

**Three layers:**
1. **`vizql.exec`** - wire layer. `send_command(page, namespace, name, params)`. Use only for commands not yet wrapped.
2. **`vizql.api`** - typed primitives. Most code calls these. Auto-flush, auto-cleanup, typed errors.
3. **`vizql.recipes`** - composite chart builders. Built atop `api`. Each is a parameterized chart template.

---

## Quick start

```python
from tableau_interactor.cloud.vizql.connect import connect_to_workbook_page
from tableau_interactor.cloud.vizql import exec as ex, api, recipes

pw, page = connect_to_workbook_page()
try:
    ex.inject_dispatcher(page)              # one-time per session
    api.close_open_dialogs(page)            # start clean
    sheet = api.active_sheet_name(page)

    api.drop_field(page, "Ship Mode", "columns", sheet)
    api.drop_field(page, "Sales", "rows", sheet)
    api.drop_field(page, "Segment", "color", sheet)
    api.set_mark_type(page, "bar", sheet)
finally:
    pw.stop()
```

That's 4 wire calls; total ~2 seconds.

---

## Session lifecycle

### `connect.connect_to_workbook_page() -> (playwright, page)`
Connects to the running CDP browser (started via `python -m tableau_interactor.cloud.vizql.session`) and picks the most relevant workbook authoring page. Returns the Playwright instance and the page. **Caller must call `pw.stop()`** in a finally.

### `exec.inject_dispatcher(page) -> dict`
Injects `dispatcher.js` and returns the handshake `{ok, path, method, datasourceId}`. Idempotent - re-injecting refreshes the datasource id and dispatcher reference. Required once before any `send_command` call.

### `exec.info(page) -> dict`
Returns `{ready, path, method, datasourceId}` for the currently-injected dispatcher.

### `exec.send_command(page, namespace, name, params, *, timeout_ms=15000, opts=None) -> dict`
Send a wire command. Returns `{ok: bool, result: dict}` on success or `{ok: false, error/message: str}` on failure. Most commands' useful data is in `result` (already unwrapped from `vqlCmdResponse`).

### `exec.send_command_raw(...)` (same signature)
Use when the data you need is in `vqlCmdResponse.layoutStatus.presentationLayerNotification` (parameter `controllerId`, color-dialog `componentId`, etc.) - returns the full raw server response in `raw`.

---

## Primitives - `vizql.api`

All primitives auto-call `flush_ui` so UI updates apply immediately. Raise `RuntimeError` with a cleaned error message on failure.

### Drops & shelves

#### `drop_field(page, field_display_name, shelf, sheet, *, pos=0) -> str`
Drop a field on any shelf. Returns the resolved canonical `fn`.

- `shelf` accepts:
  - short names: `"columns"`, `"rows"`, `"filters"`, `"pages"`
  - full names: `"columns-shelf"`, `"rows-shelf"`, etc.
  - **marks card encodings:** `"color"`, `"size"`, `"label"`, `"detail"`, `"tooltip"`, `"angle"`, `"shape"`, `"image"`, `"text"`
- Handles plain measures, dimensions, AND calculated fields (schema-lookup via `find_field_fn`).
- Examples:
  ```python
  api.drop_field(page, "Sales", "rows", sheet)
  api.drop_field(page, "Segment", "color", sheet)
  api.drop_field(page, "Profit Ratio", "color", sheet)  # calc field - also works
  ```

#### `remove_pill(page, shelf, pos, sheet=None) -> str`
Remove the pill at position `pos` (0-based) on a shelf. Auto-resolves the field encoding to send the correct `drop-nowhere`. Returns the removed fn.

#### `move_pill(page, field_display_name, from_shelf, from_pos, to_shelf, to_pos=0, sheet=None) -> str`
Move a pill from one shelf to another. Clears the source (uses `shelfSelection`).

#### `copy_pill(page, field_display_name, from_shelf, from_pos, to_shelf, to_pos=0, sheet=None) -> str`
Same as `move_pill` but the source pill stays in place (no `shelfSelection`).

### Pill manipulation

#### `change_aggregation(page, shelf, pos, aggregation)`
Change a measure's aggregation OR a date's date-part. Works on regular shelves AND marks-card encodings.
- Measure aggregations: `"sum"`, `"avg"`/`"average"`, `"median"`, `"count"`, `"countd"`, `"min"`, `"max"`, `"stdev"`, `"var"`, `"attr"`
- Date parts: `"year"`, `"quarter"`, `"month"`, `"week"`, `"weekday"`, `"day"`, `"hour"`, `"minute"`, `"second"`, `"year-quarter"`, `"year-month"`
- For marks-card encodings, `pos` is the slot order: **0=Color, 1=Size, 2=Label/Text** etc.

#### `set_date_part(page, shelf, pos, part)`
Alias for `change_aggregation` with a date-part value.

#### `set_field_type(page, shelf, pos, field_type)`
Toggle a pill between **discrete** (`"discrete"`/`"ordinal"`) and **continuous** (`"continuous"`/`"interval"`).

### Marks card

#### `set_mark_type(page, mark_type, sheet=None)`
Mark types: `"automatic"`/`"auto"`, `"bar"`, `"line"`, `"area"`, `"circle"`, `"square"`, `"shape"`, `"text"`, `"map"`, `"pie"`, `"gantt"`, `"polygon"`, `"density"`.

### Data modeling

#### `create_calc_field(page, name, formula)`
Two-phase wire (`create-calc` + `apply-calculation` + `clear-calculation-model`). Auto-closes the editor pane.

#### `create_set(page, field_display_name) -> str`
Returns the new set's display name (e.g. `"Segment Set"`).

#### `create_group(page, field_display_name, groups: dict[str, list[int]]) -> str`
Multi-step categorical-bin protocol. `groups` is `{group_name: [domain_item_indices]}` (0-based positions in the field's alphabetical domain).
Returns the new group field's name (e.g. `"Ship Mode (group)"`).

#### `create_bin(page, field_display_name, bin_size=None) -> str`
Numeric bin on a measure. Optional `bin_size` overrides Tableau's default. Returns `"<Field> (bin)"`.

#### `delete_field(page, display_name) -> (str, str)`
**Universal delete** - works for calc fields, bins, groups, sets, AND parameters. Looks up the right internal reference via `get-schema` (calc fields use `Calculation_<id>`, parameters use `Parameter N`, etc.) and sends `delete-calculation-fields-command`. Returns `(deleted_fn, kind)` where `kind` is `"calc"`/`"bin"`/`"group"`/`"set"`/`"parameter"`.

```python
api.delete_field(page, "Profit Margin")    # calc field
api.delete_field(page, "Sales (bin)")      # bin
api.delete_field(page, "Region Set")       # set
api.delete_field(page, "Sub-Category (group)")  # group
api.delete_field(page, "Top N")            # parameter
```

`delete_calc_field` is kept as a deprecated alias returning just the fn.

### Filters

#### `add_filter(page, field_display_name, *, role="dimension", aggregation=None)`
Adds a default "include-all" filter on the Filters shelf (no dialog). For measures, pass `role="measure"` and optionally an aggregation (default `"sum"`).

#### `show_filter_card(page, field_display_name, *, role="dimension")`
Equivalent to right-click filter pill → "Show Filter". Renders the filter control widget on the right side of the viz. The field must already be on the Filters shelf.

#### `edit_filter_values(page, field_display_name, *, include=None, exclude=None, role="dimension", sheet=None) -> dict`
Constrain a categorical filter to specific values. Pass **exactly one** of `include=[...]` (keep only these) or `exclude=[...]` (keep all but these). Returns `{"kept": [...], "excluded": [...]}`. Idempotent - re-selects everything before deselecting the unwanted indices, so safe to call repeatedly.

```python
api.edit_filter_values(page, "Region", exclude=["Central", "South"])  # keep East+West
api.edit_filter_values(page, "Region", include=["West"])                # keep only West
api.edit_filter_values(page, "Region", include=["Central","East","South","West"])  # reset
```

Dimensions only - measures use a continuous range filter (separate primitive, not yet implemented). Raises `ValueError` if any value isn't in the field's domain.

**Quick-filter card caveat:** when a visible quick-filter widget already exists, the first wire-driven change may not refresh the widget UI (the viz updates correctly). A second wire-driven change usually kicks the widget into a subscribed state. Hide+show via `hide-quickfilter-doc` / `show-quickfilter-doc` does not reliably force a refresh.

#### `configure_range_filter(page, field_display_name, *, min=None, max=None, aggregation="sum", include=True, show_card=True) -> dict`
Constrain a measure (quantitative) filter to a numeric range. Field must already be on the Filters shelf as a measure.

```python
api.add_filter(page, "Sales", role="measure", aggregation="sum")  # once
api.configure_range_filter(page, "Sales", min=400000)              # At Least 400k
api.configure_range_filter(page, "Sales", max=200000)              # At Most 200k
api.configure_range_filter(page, "Sales", min=100000, max=500000)  # Range
api.configure_range_filter(page, "Sales", min=100000, max=500000, include=False)  # Exclude range
```

Returns `{"min", "max", "kind": "range"|"at-least"|"at-most", "included"}`. Raises `ValueError` if both `min` and `max` are None. Uses the same dialog-cacheInfo pattern as `edit_filter_values` - extracts `quantitativeFilter` and `filterStoreId` from `edit-filter-dialog`'s response.

**Side effect:** `close-quantitative-filter-dialog` hides the visible quick-filter widget. `show_card=True` (default) re-shows it. Pass `show_card=False` to suppress.

#### `configure_top_n_filter(page, field_display_name, *, n, by=None, aggregation="sum", end="top") -> dict`
Set or clear a Top N / Bottom N limit on a categorical filter. Top N is orthogonal to member-inclusion - you can combine an include-set with a Top N rank.

```python
api.configure_top_n_filter(page, "Region", n=2, by="Sales")             # Top 2 by SUM(Sales)
api.configure_top_n_filter(page, "Region", n=3, by="Profit", aggregation="avg")  # Top 3 by AVG(Profit)
api.configure_top_n_filter(page, "Region", n=1, by="Sales", end="bottom")        # Bottom 1
api.configure_top_n_filter(page, "Region", n=None)                      # CLEAR the limit
```

Rides on the same `close-categorical-filter-dialog` command as `edit_filter_values`, populating `categoricalFilterLimitUpdate` with `filterLimitType: "by-field"` (or `"none"` to clear).

#### `configure_wildcard_filter(page, field_display_name, *, pattern, match="contains", exclude=False) -> dict`
Apply a wildcard text-pattern filter on a categorical dimension. Pass `pattern=None` to clear.

```python
api.configure_wildcard_filter(page, "Sub-Category", pattern="Phones")             # contains
api.configure_wildcard_filter(page, "Region", pattern="E", match="starts-with")   # starts-with
api.configure_wildcard_filter(page, "Region", pattern="South", match="exactly", exclude=True)
api.configure_wildcard_filter(page, "Region", pattern=None)                       # clear
```

`match` accepts `"contains"` / `"starts-with"` / `"ends-with"` / `"exactly"`. Populates the `categoricalFilterPatternUpdate` block.

#### `configure_condition_filter(page, field_display_name, *, by, op=">=", value=0, aggregation="sum") -> dict`
Apply a condition filter - restrict a dimension's domain to values whose aggregated measure satisfies a comparison. Pass `by=None` to clear.

```python
api.configure_condition_filter(page, "Region", by="Sales", op=">=", value=600000)  # SUM(Sales) >= 600k
api.configure_condition_filter(page, "Region", by="Profit", op="<", value=0, aggregation="avg")
api.configure_condition_filter(page, "Region", by=None)                            # clear
```

`op` shorthand: `>`, `>=`, `<`, `<=`, `=`/`==`, `!=`/`<>` (mapped to wire names `op-greater`, `op-gequal`, `op-less`, `op-lequal`, `op-equals`, `op-not-equals`). Populates `categoricalFilterConditionUpdate`. Wildcard, Top N, and Condition are orthogonal - set them in any combination on the same filter.

#### `configure_date_filter(page, field_display_name, *, period="year", n=1, range_type="lastn", include_nulls=False, anchor_date=None) -> dict`
Apply a relative-date filter to a date dimension. Creates the filter if not on the shelf; replaces if present.

```python
api.configure_date_filter(page, "Order Date", period="year", n=2, range_type="lastn")    # last 2 years
api.configure_date_filter(page, "Order Date", period="quarter", range_type="curr")        # current quarter
api.configure_date_filter(page, "Order Date", range_type="yeartodate")                    # YTD
api.configure_date_filter(page, "Order Date", period="month", n=6, range_type="lastn")
```

`period`: year/quarter/month/week/day/hour/minute/second. `range_type`: lastn/last/curr/nextn/next/yeartodate/todate/null. Wire path goes through `close-quantitative-filter-dialog` with a `relativeDateFilter` blob (relative date is treated as quantitative; uses :qk suffix for fn).

#### `set_filter_scope(page, field_display_name, scope, *, role="dimension", aggregation=None, is_date=False) -> dict`
Set the "Apply to Worksheets" scope for a filter.

```python
api.set_filter_scope(page, "Region", "data-source")   # all sheets using this datasource
api.set_filter_scope(page, "Region", "worksheet")      # back to local
```

`scope`: `"worksheet"` / `"local"` (default) or `"data-source"` / `"global"`. (Selected-worksheets dialog flow not yet wrapped.)

#### `set_filter_context(page, field_display_name, *, in_context=True, role="dimension", aggregation=None, is_date=False) -> dict`
Promote a filter to / demote from the Context. Context filters compute first - useful when other filters depend on them (e.g. Top N within a context).

```python
api.set_filter_context(page, "Region", in_context=True)    # right-click → Add to Context
api.set_filter_context(page, "Region", in_context=False)   # remove from context
```

### Sort

#### `quick_sort(page, direction="desc", sheet=None) -> dict`
Toolbar-equivalent sort - sort the chart's primary dimension by its measure. `direction`: `"asc"` / `"desc"` / `"none"` (or `"ascending"`/`"descending"`/`"clear"`).

```python
api.quick_sort(page, "desc")   # Sort Descending toolbar button
api.quick_sort(page, "none")   # clear sort
```

#### `sort_field(page, field_display_name, *, direction="desc", by=None, aggregation="sum", scope="nested", shelf="columns", sheet=None) -> dict`
Full Sort dialog equivalent - sort a specific dimension pill by a specific measure.

```python
api.sort_field(page, "Category", by="Profit", direction="asc")          # Sort Category by AVG(Profit)? No - SUM by default
api.sort_field(page, "Category", by="Profit", aggregation="avg", direction="desc")
api.sort_field(page, "Region", direction="none", by="data-source")      # clear sort
```

`scope`: `"nested"` (default - sort within partitions) or `"global"`. `shelf`: `"columns"` or `"rows"` depending on where the dim is.

### Analytics (reference & trend lines)

#### `add_analytics_object(page, *, kind="reference-line", measure="Sales", aggregation="sum", scope="per-pane", orientation="vertical", sheet=None) -> dict`
Add a reference line, trend line, band, distribution band, or box plot to a chart axis. All ride on the `add-reference-line` wire command with different `analyticsObjectType` values.

`kind`: `"reference-line"` / `"trend-line"` / `"reference-band"` / `"distribution-band"` / `"box-plot"` / `"average-line"` / `"median-line"` / `"constant-line"`.

`scope`: `"per-pane"` / `"per-cell"` / `"entire-table"`. `orientation`: `"vertical"` (Y-axis) or `"horizontal"` (X-axis).

```python
api.add_analytics_object(page, kind="reference-band", measure="Sales", scope="entire-table")
api.add_analytics_object(page, kind="distribution-band", measure="Profit")
```

#### `add_reference_line(page, measure="Sales", **kwargs) -> dict`
Sugar for `add_analytics_object(kind="reference-line")`.

#### `add_trend_line(page, measure="Sales", **kwargs) -> dict`
Sugar for `add_analytics_object(kind="trend-line")`. **Only renders on continuous numeric/date axes** (wire succeeds on discrete charts but nothing visible appears).

Detailed configuration (trend model linear/log/poly, CI level, line/band formatting) requires dialog interactions not yet wrapped.

### Dashboards

#### `new_dashboard(page) -> str`
Create a new (empty) dashboard. Returns the auto-assigned name.

#### `add_sheet_to_dashboard(page, worksheet, *, floating=False) -> None`
Add a worksheet to the current dashboard. `floating=True` for a movable floating zone.

#### `remove_sheet_from_dashboard(page, worksheet, dashboard=None, *, delete_orphans=False) -> None`
Remove a worksheet zone (does NOT delete the worksheet itself).

#### `goto_sheet(page, sheet) -> None`
Switch active tab. Works for both worksheets and dashboards.

#### `add_dashboard_object(page, *, kind, x, y, width, height, floating=True, dashboard=None) -> None`
Add a layout object. `kind`: `text` / `image` / `web` / `blank` / `horizontal` / `vertical` / `navigation` / `extension` / `download`. Convenience wrappers: `add_text_object`, `add_image_object`, `add_web_page_object`, `add_blank_object`, `add_horizontal_container`, `add_vertical_container`. *Note: object content (text body, image URL) is set via separate dialogs not yet wrapped.*

#### `toggle_use_as_filter(page, worksheet, dashboard=None) -> None`
Toggle "Use as Filter" on a worksheet zone (funnel icon). When enabled, clicking a mark in this worksheet filters all other sheets in the dashboard. **The 80% case for dashboard interactivity.**

#### `toggle_dashboard_title(page, dashboard=None) -> None`
Show/hide the dashboard title bar.

#### `toggle_dashboard_grid(page, *, show=True, dashboard=None) -> None`
Show or hide the dashboard layout grid (Dashboard menu → Show Grid).

#### `toggle_device_preview(page, *, visible=True, tablet=False, dashboard=None) -> None`
Show/hide the Device Preview panel. `tablet=True` defaults to Tablet; otherwise Phone.

#### `clear_dashboard(page, dashboard=None, *, delete_orphans=False) -> None`
Remove all zones from a dashboard.

#### `open_actions_dialog(page) -> None` / `discard_action_changes(page) -> None`
Open / dismiss the Actions dialog.

#### Action primitives (Dashboard → Actions)
All ride on the same pattern: open Actions dialog → open per-type sub-dialog → add → commit-action-change.

```python
api.add_filter_action(page, caption="Region Filter", activation="explicitly", on_clear="do-nothing")
api.add_highlight_action(page, caption="Hover Highlight", activation="hover")
api.add_url_action(page, caption="Open Docs", url="https://tableau.com", activation="menu")
# Partial (sub-dialog wire harder):
# api.add_go_to_sheet_action(page, caption=..., target_sheet=...)
# api.add_parameter_action(page, caption=..., parameter=...)
```

`activation`: `"explicitly"` (click - default for filter/parameter), `"hover"` (default for highlight), `"menu"` (default for URL).
`on_clear` (filter only): `"do-nothing"` / `"show-all-values"` / `"exclude-all-values"`.

Source/target sheets default to "All sheets on the dashboard". Refine via the Actions dialog UI for now (per-sheet selection wire not yet wrapped).

```python
api.add_go_to_sheet_action(page, caption="Drill", target_sheet="Sheet 1", activation="on-select")
api.add_parameter_action(page)   # default config - refine target/source via dialog
```

### Sheet operations

#### `list_sheets(page) -> list[str]`
Return all visible sheet tab names (excludes Data Source).

#### `duplicate_sheet(page, sheet=None, *, as_crosstab=False, is_dashboard=False) -> str`
Duplicate a sheet (right-click tab → Duplicate). Returns the new sheet's name. `as_crosstab=True` is "Duplicate as Crosstab".

#### `delete_sheet(page, sheet, *, delete_orphans=False) -> None`
Delete a sheet (cannot delete the last one). `delete_orphans=True` also removes dashboards that referenced it.

#### `hide_sheet(page, sheet, *, hidden=True) -> None`
Hide / unhide a sheet from the tab bar.

#### `swap_rows_and_columns(page, sheet=None) -> None`
Swap the Rows and Columns shelves (toolbar Swap button).

### Totals & subtotals

```python
api.toggle_column_totals(page)   # Analysis → Totals → Show Column Grand Totals
api.toggle_row_totals(page)      # Analysis → Totals → Show Row Grand Totals
api.toggle_subtotals(page, add=True)   # Add All Subtotals (or False to remove)
```

### Dual axis

#### `toggle_dual_axis(page, *, shelf="rows", pos=1, sheet=None) -> dict`
Toggle dual-axis on the measure pill at `pos` on the given shelf (calling again removes it). Equivalent to right-clicking the rightmost measure → Dual Axis.

### Axis editing

#### `set_axis_title(page, measure, title, *, aggregation="sum", orientation="vertical", duplicate_index=0, sheet=None) -> None`
Set the axis title for a measure.

```python
api.set_axis_title(page, "Profit", "Profit ($)")
api.set_axis_title(page, "Sales", "Revenue", orientation="horizontal")
```

#### `set_axis_extent_type(page, measure, *, extent_type="auto", ...) -> None`
Switch axis range mode: `"auto"` / `"uniform"` / `"independent"` / `"fixed"`.

#### `set_axis_range(page, measure, *, min=None, max=None, aggregation="sum", orientation="vertical", duplicate_index=0, sheet=None) -> None`
Set the fixed numeric min and/or max of an axis. Auto-switches to fixed mode.

```python
api.set_axis_range(page, "Profit", min=0, max=300000)
api.set_axis_range(page, "Sales", max=900000)   # min stays auto-derived
```

#### `reset_axis_range(page, measure, *, aggregation="sum", orientation="vertical") -> None`
Clear a manually-set axis range (right-click axis → Clear Axis Range).

### Number formatting

#### `set_number_format(page, measure, *, format="currency", aggregation="sum", decimal_places=2, units="none", show_separator=True, prefix="", suffix="") -> None`
Set the number format for a measure pill.

```python
api.set_number_format(page, "Sales", format="currency", decimal_places=0, units="thousands")  # $1,432K
api.set_number_format(page, "Profit", format="percent", decimal_places=1)                      # 12.3%
api.set_number_format(page, "Profit", format="auto")                                            # reset
```

`format`: `"auto"` / `"number"` / `"currency"` / `"percent"` / `"scientific"`. `units`: `"none"` / `"thousands"` / `"millions"` / `"billions"`.

### Annotations

#### `add_annotation(page, *, kind="point", x=200, y=200, text="") -> dict`
Add a Point, Mark, or Area annotation to the viz. Coordinates are relative to the viz origin. Rich-text formatting needs interactive editing in Tableau.

#### `edit_tooltip(page, text="", *, sheet=None) -> dict`
Set the tooltip text for the active worksheet (Marks card → Tooltip → Edit → OK). Opens the rich-text editor, types via clipboard paste, then commits via `close-rich-text-editor`.

#### `set_value_color(page, field_display_name, value_color_map, *, role="dimension") -> dict`
Assign specific colors to specific categorical values (right-click Color → Edit Colors → click value → click swatch). Wire chain: `get-web-categorical-color-dialog` → `set-selected-legend-items` → `set-categorical-legend-item-color` → `release-component`.

```python
api.set_value_color(page, "Region", {"Central": "#ff0000", "East": "#00ff00"})
# For date-part fields, pass a fully-qualified fn:
api.set_value_color(page, "[sqlproxy.X].[yr:Order Date:ok]", {"2022": "#ff0000"})
```

Returns `{"componentId", "applied" (value→rgb), "missing" (values not in domain), "domain" (all available values)}`.

### Verification helpers

#### `viz_status(page) -> dict`
Read the viz status bar - cheap programmatic check of what's rendered. Returns:
```python
{
  "marks": 2,
  "rows": 1,
  "columns": 2,
  "aggregations": [{"name": "SUM(Sales)", "value_raw": "1,431,642", "value": 1431642.0}],
  "raw": {...},
}
```

Use for assertions in recipes/tests instead of screenshots when you only need counts and totals.

#### `screenshot(page, path="tableau_cloud_state/snapshot.png", *, full_page=False) -> str`
Write a Playwright screenshot of the current viz. Use for visual checkpoints when shape/color/layout matters; for numeric assertions prefer `viz_status`.

### Parameters

#### `create_parameter(page, name, *, data_type="integer", current_value="", allowable="all")`
Five-step wire flow. `data_type` accepts `"int"`/`"integer"`, `"float"`/`"real"`, `"string"`, `"bool"`, `"date"`, `"datetime"`, `"spatial"`. Note: switching data type from default Integer to String may fail (known sharp edge - see TODO.md).

#### `set_parameter_value(page, parameter_name, value)`
Update a parameter's current value at runtime.

#### `show_parameter_control(page, parameter_name)`
Render the parameter control widget on the viz.

### Sheets

#### `new_sheet(page, *, insert_at_end=True, switch_to=True) -> str`
Creates a new blank worksheet, switches to it. Returns the auto-generated name.

#### `rename_sheet(page, new_name, *, old_name=None)`
Rename the active (or specified) sheet.

#### `active_sheet_name(page) -> str`
Returns the currently-selected sheet's display name.

#### `pills_on_shelf(page, shelf) -> list[str]`
Returns visible pill texts on a shelf. NOTE: `'filters'` returns empty due to a known DOM-selector mismatch (cosmetic only).

#### `visible_pills(page) -> list[str]`
All visible pills across all shelves AND the marks card. Useful for assertions.

### Color

#### `set_color_palette(page, palette, field_display_name, *, role="dimension")`
Multi-step (`get-web-categorical-color-dialog` + `assign-categorical-color-palette` + `release-component`).
- `palette` accepts friendly names (`"Color Blind"`, `"Tableau 10"`, `"Tableau 20"`, `"Traffic Light"`, etc.) or raw wire ids.
- **Known limitation**: does not yet handle bin/group fields (those use `:ok` suffix; current code assumes plain `:nk`).

### UI state

#### `flush_ui(page) -> dict`
Drain the SPA's deferred response queue AND auto-dismiss any "Unexpected Server Error" dialog. Called automatically by every primitive - only call directly if you went around the wrappers with `ex.send_command`.

#### `open_dialogs(page) -> dict`
Inspect all currently-open transient UI: `{dialogs, popovers, menus}`. Skips legitimate display widgets (color legends, parameter controls, filter cards).

#### `close_open_dialogs(page, *, verbose=False) -> int`
Dismiss every dialog, popover, and context menu. Up to 3 rounds (closing one may reveal another). Returns count closed.

#### `close_error_dialogs(page) -> int`
Subset of `close_open_dialogs` that only targets `detailedErrorDialog` - safe to call after any mutation.

### Schema introspection

#### `get_schema_columns(page) -> list[dict]`
The full datasource column list (151 columns for Superstore) including calc fields. Each column has `fieldCaption`, `fn`, `baseColumnName`, `isCalculated`, `fieldRole`, `dataType`, default aggregation, hidden flag, etc. Use this for AI-driven field analysis.

#### `find_field_fn(page, display_name) -> (str, bool)`
Returns `(bare_fn, is_calculated)` for any field - handles the calc-field name→Calculation_<id> mapping.

#### `resolve_drop_plan(page, field_display_name, sheet) -> dict`
The full `drag` model from `get-drag-pres-model` - per-shelf field encodings and drop positions. Used internally by `drop_field`; useful for advanced custom drops.

---

## Recipes - `vizql.recipes`

High-level chart builders. Each takes the page + field names, clears the sheet (by default), and produces a finished chart.

| Recipe | Signature |
|---|---|
| `bar_chart(page, dimension, measure, *, color=None, clear=True, sheet=None)` | Vertical bar; optional color-segmented |
| `stacked_bar(page, dimension, measure, color, *, clear=True, sheet=None)` | Named alias for `bar_chart(..., color=...)` |
| `line_chart(page, date_field, measure, *, color=None, date_part="year", clear=True, sheet=None)` | Time-series line |
| `area_chart(page, date_field, measure, *, color=None, date_part="month", clear=True, sheet=None)` | Stacked area when color given |
| `pie_chart(page, dimension, measure, *, label=None, clear=True, sheet=None)` | Mark=pie, dim=Color, measure=Angle |
| `scatter_plot(page, x_measure, y_measure, *, color=None, size=None, clear=True, sheet=None)` | Mark=circle |
| `text_table(page, row_dim, col_dim, measure, *, clear=True, sheet=None)` | Crosstab with measure as text label |
| `heatmap(page, x_dim, y_dim, measure, *, clear=True, sheet=None)` | Mark=square, measure on Color |
| `histogram(page, measure, *, bin_size=None, clear=True, sheet=None)` | Auto-creates bin field; bar of counts |

`recipes.RECIPES` is a name→callable registry for dynamic lookup of a recipe by name.

---

## How to extend the API

When you encounter a Tableau action that isn't yet wrapped, follow this pattern:

### 1. Capture the wire command

```bash
# Arm the watcher
.venv/bin/python -m tableau_interactor.cloud.vizql.watch arm

# Perform the action in the browser (manually or via Playwright)

# Read the captured commands + payloads
.venv/bin/python -m tableau_interactor.cloud.vizql.watch report
```

The report shows every `/commands/` request with its payload + response. Identify the **mutation command** (skip the noise: `get-show-me`, `build-*-context-menu`, `get-analytics-assistant-feature-availability`, `get-drag-pres-model`).

### 2. Extract the payload

Look at `tableau_cloud_state/watch_report.json` for the full multipart body. Parse the fields you need from the captured payload.

### 3. Wrap as a primitive in `api.py`

Follow the existing pattern:

```python
def my_new_primitive(page, arg1: str, arg2: int) -> None:
    """One-line description. Where this fits in the user flow.

    Args:
      arg1: ...
      arg2: ...
    """
    # 1. Resolve any field references via find_field_fn if applicable
    # 2. Build the params dict mirroring the captured payload
    params = {
        "key1": arg1,
        "key2": arg2,
    }
    # 3. Send via the wire
    r = ex.send_command(page, "tabdoc", "the-wire-command-name", params)
    if not r.get("ok"):
        raise RuntimeError(f"the-wire-command-name failed: {r.get('message', '')[:300]}")
    # 4. Always flush so the UI updates
    flush_ui(page)
```

### 4. Use `send_command_raw` if you need response data

Things like dialog `controllerId` or `componentId` live in `vqlCmdResponse.layoutStatus.presentationLayerNotification` - `send_command` strips these. Use `send_command_raw` and parse the response with a regex.

### 5. Add it to a recipe (if it's a building block)

If the primitive is a step toward a common chart pattern, compose it into `recipes.py`. Recipes assume the lower layers - they don't call `ex.send_command` directly.

### Diagnostic rule: "0 drop targets ⇒ wrong fn"

If `get-drag-pres-model` returns `shelfDropModels: []` for a field you know is draggable manually, the `fn` reference is wrong. Arm the watcher, drag the field by hand, and read the actual request body to find the right reference. This is how the calc-field `Calculation_<id>` puzzle was cracked.

### Common wire-protocol quirks to bake into primitives

- **`noExceptionDialog: true`** is the dispatcher.js default - don't override unless you want server errors to pop user-visible dialogs.
- **Marks-card encoding changes need `paneSpec: 0`** in their params; columns/rows changes don't.
- **Marks-card encoding slot order**: 0=Color, 1=Size, 2=Label/Text, 3+=Detail/Tooltip/Shape.
- **Bin/group field encodings use `:ok`** suffix instead of `:nk`. (Not yet handled everywhere.)
- **Calc fields use `Calculation_<id>` references** - call `find_field_fn` to resolve.
- **Always call `flush_ui(page)`** at the end of any mutation primitive.
- **Every `create_*` primitive that may open a SPA dialog ends with `close_open_dialogs(page)`** - the wire-level close commands (`clear-calculation-model`, `parameter-close-dialog`, `release-component`) commit server-side state but often don't dismiss the SPA's dialog UI. The standard tail is: `wire-close → flush_ui → close_open_dialogs`. This applies to `create_calc_field`, `create_parameter`, `create_bin`, `create_group`, `set_color_palette` - and any new primitive that opens a dialog.

---

## File map

| Path | Role |
|---|---|
| `cloud/vizql/dispatcher.js` | Injected JS - finds dispatcher, exposes `window.__tab.sendCommand` + `sendCommandRaw` |
| `cloud/vizql/exec.py` | Python wire layer - `send_command`, `send_command_raw`, `inject_dispatcher`, `info` |
| `cloud/vizql/api.py` | Typed primitives - the public surface |
| `cloud/vizql/recipes.py` | High-level chart builders |
| `cloud/vizql/connect.py` | Picks the right workbook page from the CDP session |
| `cloud/vizql/hunt.py` | Dispatcher discovery + method-wrap instrumentation |
| `cloud/vizql/watch.py` | Long-running capture harness - `arm` / `report` subcommands |
| `cloud/vizql/PROTOCOL.md` | Wire-protocol reference: every command we use, naming conventions, quirks |
| `cloud/vizql/capture_drop.py` | One-shot capture harness for a single drag |
| `cloud/vizql/shot.py` | Screenshot helper |
| `cloud/session.py` | Starts the persistent CDP browser session |

---

## See also

- `cloud/vizql/PROTOCOL.md` - wire-protocol reference for every command and its quirks
- `TODO.md` - open issues and roadmap toward the smart-UI vision
- `TABLEAU_CLOUD_PLAYBOOK.md` - DOM selectors for the legacy Playwright-driven helpers (mostly superseded by api.py, kept for actions still requiring DOM)
