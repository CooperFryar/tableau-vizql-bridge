# VizQL Protocol - what we know

Findings from live capture+replay against a Tableau Cloud pod. The
intended audience is anyone extending the `vizql/` package; the prose is short
on motivation and long on facts because the wire protocol is undocumented.

## Dispatcher

The in-page command dispatcher (Saltarelle class `WebCommandHandler`) is reachable at:

```
window.onerror._targets[0].$Y    ← instance; minified property names rotate
```

`dispatcher.js` finds it by **signature** (any object exposing
`executeSingleRemoteCommand`) rather than by literal path, so it survives
release-over-release minifier rotation. Stable backdoor anchor is
`window.onerror._targets` - Tableau registers the app there for crash reporting.

The dispatcher's public API:

```js
executeSingleRemoteCommand(commandObj, onSuccess, onError)
```

with `commandObj` shape:

```js
{
  commandNamespace: "tabdoc" | "tabsrv",
  commandName: "drop-on-shelf",
  commandParams: { /* all values stringified */ },
  telemetryId: "<any unique string>",
  noExceptionDialog: false,
  preserveRootResult: true,
}
```

## Param stringification

Every value in `commandParams` arrives at the server as a string. Primitives
serialize with `String()`, objects/arrays with `JSON.stringify()`. The
dispatcher in `dispatcher.js` handles this automatically - callers pass real
Python/JS values.

## Two key naming conventions exist for the same data

| Phase | Position object keys |
|---|---|
| `get-drag-pres-model` **response** | camelCase - `shelfType`, `shelfPosIndex`, `encodingTypePresModel` |
| `drop-on-shelf` **request** | kebab-case - `shelf-type`, `shelf-pos-index`, `encoding-type-pres-model` |

The universal-drop helper converts between them.

## Field encodings are not predictable from display name

Internal field IDs use the form:

```
[sqlproxy.<datasourceId>].[<role>:<DisplayName>:<key>]
```

- `<role>`: `none` (dimension), `sum`/`avg`/`count`/... (measure), `attr` (attribute)
- `<key>`: a per-field two-letter id (`nk`, `qk`, `ok`, …). **Not a type indicator.**
- Same field can map to different `<key>` depending on **target shelf**, e.g.
  `Quantity → columns-shelf` resolves to `[sum:Quantity:qk]` but
  `Quantity → pages-shelf` resolves to `[sum:Quantity:ok]`.

**Resolution path:** call `tabdoc/get-drag-pres-model` with the bare form
`[sqlproxy.<dsid>].[<DisplayName>]`; the response's
`drag.shelfDropModels[].fieldEncodings[0].fn` carries the canonical encoding
for that shelf.

## Drop-on-shelf full protocol

```
1. get-drag-pres-model        ← bare field name in; full presentation model out
2. (optional) drop-prepare    ← appears unnecessary for shelves so far
3. drop-on-shelf              ← actually mutates server state
```

The mutation commits **regardless of step 2 being skipped** - verified for
columns-shelf and rows-shelf with both dimensions and measures.

## UI refresh - solved

Calling `executeSingleRemoteCommand` mutates server state, fires the response
event, and the SPA's master listener (`$s` on the central command coordinator)
processes the response. But $s doesn't apply directly - it caches the response
in `coord.deferredServerResponseQueue[seq]` via `$9`. The actual apply happens
when something later calls **`coord.$B(null)`**, which:

1. Drains `deferredServerResponseQueue` in seq order
2. Calls `tL.update(presModel, ex)` on the workbook singleton
3. Fires `raiseModelsUpdated` (the global UI re-render trigger)
4. Clears the queue

Normally drag-handler completion calls `$B`. We can call it ourselves after
any SendCommand mutation - see `api.flush_ui(page)`. Applies in <500ms.

**Reaching the coordinator:**
```
coord = window.onerror._targets[0].$Y.$1$1._targets[0]
```
(the first subscriber to `WebCommandHandler.onRemoteCommandResponse`). Its
prototype keys include `executingCommands`, `waitingCommands`,
`deferredServerResponseQueue`, plus events `add_modelsUpdated`,
`add_worldUpdated`, `add_commandQueueComplete`.

If `$B` is minified to a different name on a future Tableau release, find it
by signature: scan the coordinator prototype for a method whose source
contains `deferredServerResponseQueue` AND `raiseModelsUpdated`.

**Legacy fallback:** `api.refresh_ui_via_sheet_switch(page)` does a DOM
sheet-switch (~3s) for defensive use if the dispatcher path breaks.

## Useful read-only commands

| Command | Returns |
|---|---|
| `tabdoc/get-show-me` | Recommended chart types for current field selection |
| `tabdoc/get-drag-pres-model` | Drop plan for a field across all shelves |
| `tabsrv/ensure-layout-for-sheet` | Server-side layout for a sheet name |

## Command vocabulary captured

These are the wire commands the API uses, grouped by purpose. Each is callable via
`ex.send_command(page, namespace, name, params)` directly, or via the typed
wrappers in `api.py`.

### Shelves
| Command | Wraps | Notes |
|---|---|---|
| `tabdoc/get-drag-pres-model` | `resolve_drop_plan` | Returns the shelf-specific encoding for a bare field; field-encoding resolver |
| `tabdoc/drop-on-shelf` | `drop_field`, `move_pill`, `copy_pill` | Universal drop. Uses `shelf-type="encoding-shelf"` for marks card |
| `tabdoc/drop-nowhere` | `remove_pill` | Remove a pill from a shelf |
| `tabdoc/change-aggregation` | `change_aggregation`, `set_date_part` | Measure agg (sum/avg/…) AND date part. **Date parts have two flavors:** discrete (`year`, `month`, `quarter` - buckets values, e.g. all Januaries stack) and continuous/truncated (`trunc-year`, `trunc-month`, `trunc-quarter`, `trunc-week`, `trunc-day` - preserve chronological order across years). For a multi-year trend line use `trunc-month` (alias `continuous-month`) - gives one mark per actual month. Note `year-month` is NOT the continuous-month token; it yields raw exact-date marks. |
| `tabdoc/change-field-type` | `set_field_type` | Discrete (`ordinal`) vs continuous (`interval`) |
| `tabdoc/set-primitive` | `set_mark_type` | bar / line / area / pie / circle / square / shape / etc. |

### Data modeling
| Command | Wraps | Notes |
|---|---|---|
| `tabdoc/create-calc` + `apply-calculation` + `clear-calculation-model` | `create_calc_field` | Two-step open-edit-close. Close required to dismiss SPA's auto-opened editor |
| `tabdoc/create-set` | `create_set` | One command, fn only - Cloud creates "<Field> Set" with full domain |
| `tabdoc/categorical-bin-add` + `…-create-bin-with-items` + `…-rename-bin` + `…-clear-cache` | `create_group` | Multi-step. Item indices are 0-based positions in the field's domain |
| `tabdoc/create-numeric-bin` + `edit-numeric-bin` | `create_bin` | Optional second step sets `userBinSize` |

### Filters & parameters
| Command | Wraps | Notes |
|---|---|---|
| `tabdoc/create-default-quick-filter` | `add_filter` | Bypasses the dialog; default "all values" filter |
| `tabdoc/show-quickfilter-doc` | `show_filter_card` | Display the filter widget on the viz |
| `tabdoc/hide-quickfilter-doc` | (n/a) | Hides an already-visible quickfilter widget. Pair with `show-quickfilter-doc` if attempting a re-bind (not a reliable refresh - see below) |
| `tabdoc/launch-filter-dialog` | (n/a) | What drag-to-Filters actually fires; we skip and use create-default-quick-filter |
| `tabdoc/edit-filter-dialog` + `categorical-filter-init-with-domain` + `get-categorical-filter-domain-page` + `categorical-filter-select-relational-members-deferred` + `categorical-filter-deselect-relational-members-deferred` + `close-categorical-filter-dialog` | `edit_filter_values` | Multi-step. **Server returns its own `categoricalFilterCacheInfo` inside `edit-filter-dialog`'s response - extract and reuse, do NOT generate client-side UUIDs** (server returns `categoricalFilterCacheNotFoundPresModel` otherwise). The "reset to all" select-deferred lets us treat the call idempotently. Close payload is tiny (`{"filterName":""}`) - real state lives in the categorical cache. Fires `doc:filter-changed-event` on close (drives viz update). Quick-filter widget on canvas may stay stale on first wire-driven change; see `edit_filter_values` docstring. |
| `tabdoc/edit-filter-dialog` + `close-quantitative-filter-dialog` | `configure_range_filter` | Measure range filter. Server returns `quantitativeFilter` (with `filterStoreId`) and `quantitativeFilterDialogRange` in `edit-filter-dialog`'s response - extract verbatim. Range payload: `{isMinOpen, isMaxOpen, minValue, maxValue, included}`. `isMinOpen=True` = unbounded lower (At Most mode); `isMaxOpen=True` = unbounded upper (At Least mode). Even on the unbounded side a numeric value must still be sent (server ignores it). `included: "include-range"` or `"exclude-range"`. **Side effect:** close-quantitative-filter-dialog hides the quick-filter widget; primitive auto-shows it via `show-quickfilter-doc`. |
| (close-categorical-filter-dialog with `categoricalFilterLimitUpdate`) | `configure_top_n_filter` | Top/Bottom N. `filterLimitType: "by-field"` + `aggregation` + `columnName` + `limitCountExpression` + `sortEnd: "top"\|"bottom"`. Set to `"none"` to clear. Top N is orthogonal to member-include - they layer. |
| (close-categorical-filter-dialog with `categoricalFilterPatternUpdate`) | `configure_wildcard_filter` | Text pattern match. `filterPatternType`: `"contains"\|"starts-with"\|"ends-with"\|"exactly"`. `isPatternExclusive: True` = keep non-matches. Empty pattern + `useAllWhenPatternEmpty: True` = clear. |
| (close-categorical-filter-dialog with `categoricalFilterConditionUpdate`) | `configure_condition_filter` | Aggregated-measure condition. `filterConditionType: "by-field"\|"none"`. **Wire op names are abbreviated and unintuitive**: `op-greater` (NOT `op-gthan`/`op-greater-than`), `op-gequal`, `op-less`, `op-lequal`, `op-equals`, `op-not-equals`. `dataValue` encoding from `validate-data-format`: `"r:6:0:<int>"` for reals (no trailing `.0`). |
| `tabdoc/validate-data-format` | (n/a) | Returns `dataValueCompact` (the exact wire encoding) for any user-typed numeric input. Useful to discover the right `dataValue` format for a new condition op. |
| `tabdoc/launch-filter-dialog` + `edit-filter-dialog` (with `forceRelativeDate: true`) + `close-quantitative-filter-dialog` (with `relativeDateFilter` blob) | `configure_date_filter` | Relative-date filter on date dimensions. Field uses `:qk` suffix in filter ops, `:ok` in the drop wrapper. `dateRangeType`: `lastn`/`last`/`curr`/`nextn`/`next`/`yeartodate`/`todate`/`null`. `datePeriodType`: year/quarter/month/week/day/hour/minute/second. The close payload includes a `simpleCommandModel` wrapping a `tabdoc:drop-on-shelf` (filter is created on the shelf as part of the same call). filterStoreId comes from edit-filter-dialog response. |
| `tabdoc/set-filter-shared` | `set_filter_scope` | "Apply to Worksheets" scope. `filterMode`: `"local"` (one sheet) or `"global"` (all sheets using this datasource). |
| `tabdoc/set-filter-context` | `set_filter_context` | Add to / Remove from Context. `fieldVector: ["..."]` + `state: "true"\|"false"`. |
| `tabdoc/build-shelf-item-context-menu` | (discovery) | Right-click pill menu. Returns commandItems with embedded wire commands - invaluable for discovering exact wire syntax of menu actions (`Add to Context`, `Apply to Worksheets`, etc.). Params: `shelfItemId`, `shelfType`, `paneSpec`, `isMobile`. |
| `tabdoc/quick-sort` | `quick_sort` | Toolbar Sort Asc/Desc. Params: `sortOrder` (`asc`/`desc`/`none`), `visualIdPresModel`. Infers dim+measure from chart. |
| `tabdoc/sort-dialog-sort` | `sort_field` | Full Sort dialog commit (no need to open the dialog). Params: `globalFieldName`, `worksheet`, `visualIdPresModel`, `sortOrder`, `sortBy` (`field`/`datasource`/`alphabetic`/`manual`), `sortMeasureName`, `aggregation`, `keepFieldFilters`, `sortRangeList`, `setDefault`, `sortPartitioning` (`nested`/`global`), `shelfType`. |
| `tabdoc/add-reference-line` + `close-ref-line-editor` | `add_analytics_object` / `add_reference_line` / `add_trend_line` | All analytics-pane objects (reference line, trend line, bands, box plot) flow through this single wire command with different `analyticsObjectType` values: `custom-reference-line`, `trend-line`, `reference-band`, `distribution-band`, `box-plot`, `average-line`, `median-with-quartiles`, `constant-line`. Params: `analyticsObjectType`, `axisOrientation` (`o-vert`/`o-horiz`), `duplicateIndex`, `fieldVector`, `fn`, `referenceLineScopeType` (`per-pane`/`per-cell`/`entire-table`). Close-editor commits with defaults. |
| `tabdoc/duplicate-sheets`, `delete-sheets`, `set-sheets-hidden`, `swap-rows-and-columns` | `duplicate_sheet` / `delete_sheet` / `hide_sheet` / `swap_rows_and_columns` | Sheet-tab right-click ops. Plural forms: `sheets=["..."]`, `sheetPms=[{"sheet-name":"...","is-dashboard":false}]`. |
| `tabdoc/show-col-totals`, `show-row-totals`, `add-subtotals`, `remove-subtotals` | `toggle_*_totals` / `toggle_subtotals` | Toggles - empty body. |
| `tabdoc/dual-axis` | `toggle_dual_axis` | Params: `shelfSelectionModel={"shelf-type":"rows-shelf","shelf-pos-indices":[1]}`, `worksheet`. Same command toggles on/off. |
| `tabdoc/set-axis-title`, `set-both-axis-extents-type`, `reset-axis-range` | `set_axis_title` / `set_axis_extent_type` / `reset_axis_range` | Axis editing. Common params: `fn`, `axisOrientation`, `duplicateIndex`, `visualIdPresModel`. Setting fixed min/max needs a different (uncaptured) command. |
| `tabdoc/set-number-format-sheet-style` | `set_number_format` | Number format. Wire shape includes `styleAttribute: "saTextFormat"`, `styleContexts: [...]` (element+scope+field), `numberFormattingOptions: {formatCode, decimalPlaces, unitsFormat, ...}`. `formatCode` values: `system-currency`, `system-percent`, `system-number`, `system-scientific`, `system-locale`. |
| `tabdoc/create-annotation` + `close-rich-text-editor` | `add_annotation` | Params: `annotateEnum` (`point`/`mark`/`area`), `targetPoint: {x,y}`, `selectionList`. Rich-text content goes through close-rich-text-editor `textContent`. |

### Dashboards
| Command | Wraps | Notes |
|---|---|---|
| `tabdoc/new-dashboard` | `new_dashboard` | No params. Auto-names "Dashboard N". |
| `tabdoc/add-sheet-to-dashboard` | `add_sheet_to_dashboard` | `worksheet`, `addAsFloating`. |
| `tabdoc/remove-sheet-from-dashboard` | `remove_sheet_from_dashboard` | `dashboard`, `worksheet`, `deleteOrphans`. |
| `tabdoc/goto-sheet` | `goto_sheet` | `sheet` - works for both worksheets and dashboards. |
| `tabdoc/drop-on-dashboard` | `add_dashboard_object` (and sugar wrappers) | Single wire for all object types via `zoneType`: `text` / `image` / `web` / `blank` / `horizontal` / `vertical` / `navigation` / `extension` / `download`. Required params: `dashboard`, `addAsFloating`, `dropLocation: {x,y,w,h}`, `zoneType`, `isHorizontal`. |
| `tabdoc/master-detail-filter` | `toggle_use_as_filter` | The funnel icon on a worksheet zone - the 80% case for dashboard interactivity. Just `dashboard` + `worksheet`. |
| `tabdoc/show-dashboard-title` | `toggle_dashboard_title` | Toggle. Param: `dashboard`. |
| `tabdoc/clear-sheet` | `clear_dashboard` | Strip all zones. Params: `sheet`, `deleteOrphans`. |
| `tabdoc/show-action-list-dialog-for-dashboard` | `open_actions_dialog` | Open the Actions dialog. Param: `bool: "true"`. |
| `tabdoc/discard-action-change`, `commit-action-change` | `discard_action_changes` / used internally | Discard or commit pending action changes. **Critical for action wires** - without `commit-action-change`, all `add-new-*-action` calls are rolled back when the dialog closes. |
| `tabdoc/create-new-{filter,highlight,url,navigation,parameter}-action-dialog` + `add-new-{type}-action` | `add_filter_action`, `add_highlight_action`, `add_url_action` (filter/highlight/url work; navigation/parameter need more capture) | Each action type has paired sub-dialog + commit wires. Full sequence: `show-action-list-dialog-for-dashboard` → `create-new-{type}-action-dialog` → `add-new-{type}-action {updateXActionParamsPresModel: {...}}` → `commit-action-change`. Payload schema: `updateActionCommonParamsPresModel: {caption, runActionOnPresModel: {activation: explicitly/hover/menu}}` + type-specific fields (filter has `onClear`, url has `url`, etc.). |
| `tabdoc/master-detail-filter` (also seen as `tabdoc/master-detail-filter`) | `toggle_use_as_filter` | The funnel icon shortcut - auto-creates a filter action with sensible defaults. Use this when you just want "click marks here to filter the rest of the dashboard". |
| **Popup-popup gotcha:** The Add Action dropdown in the Actions dialog won't open via normal Playwright clicks; use `force=True`. Discovered 2026-05-19. |
| `tabdoc/add-new-go-to-sheet-action` | `add_go_to_sheet_action` | Wire is `add-new-go-to-sheet-action` (NOT `add-new-navigation-action`). Param: `updateNavActionParamsPresModel: {updateActionCommonParamsPresModel, targetSheet: "None"\|"<sheet>", includedSheetValues: [true,true]}`. Activation: `on-select`. |
| `tabdoc/create-new-parameter-action-dialog` + `accept-parameter-action-dialog` | `add_parameter_action` | Parameter actions use a different commit pattern - no `add-new-X-action`; the dialog state is committed via `accept-parameter-action-dialog`. To customize the target parameter/source field, edit the action via the Actions dialog UI. |
| `tabdoc/dashboard-show-grid` | `toggle_dashboard_grid` | Note: the menu data string says `dashboard-show-grid-web-wrapper` but the actual wire is `dashboard-show-grid`. Params: `dashboard`, `dashboardShowGrid: "true"\|"false"`. |
| `tabdoc/set-is-device-preview-visible` | `toggle_device_preview` | Params: `dashboard`, `isVisible`, `useTabletAsDefaultPreview`. |
| `tabdoc/launch-custom-tooltip-rich-text-editor` + `close-rich-text-editor` | `edit_tooltip` | **Required:** `paneSpec: "0"`. Open editor → type via clipboard paste → close commits. |
| `tabdoc/set-selected-legend-items` + `set-categorical-legend-item-color` + `release-component` | `set_value_color` | Per-value categorical color assignment. Open dialog via `get-web-categorical-color-dialog` (returns `componentId` + legend items with `text`/`objectId`/`color`). Then for each value: select via `set-selected-legend-items {componentId, itemIndices: [objectId]}` → assign via `set-categorical-legend-item-color {componentId, color: "rgb(r,g,b)"}`. Hex colors must be converted to rgb(). |
| `tabdoc/set-axis-range-start` / `set-axis-range-end` | `set_axis_range` | Fixed-range min/max. Must call `set-both-axis-extents-type {axisExtentsType: "axis-extent-fixed"}` first. **Side effect:** the Edit Axis dialog opens server-side; primitive auto-closes it via `close_open_dialogs`. |
| **Axis dialog side-effect:** `set-axis-title`, `set-both-axis-extents-type`, `set-axis-range-*`, `reset-axis-range` all open the Edit Axis dialog server-side as a side effect - close it via `close_open_dialogs` after. Cooper observation 2026-05-19. |
| `tabdoc/create-new-parameter` + `parameter-edit-data-type` + `parameter-edit-name` + `parameter-edit-value` + `parameter-close-dialog` | `create_parameter` | Multi-step. `controllerId` comes from raw response (use `ex.send_command_raw`) |
| `tabdoc/show-parameter-controls` | `show_parameter_control` | Display parameter widget |
| `tabdoc/set-parameter-value` | `set_parameter_value` | Update parameter value at runtime |

### Sheet operations
| Command | Wraps | Notes |
|---|---|---|
| `tabdoc/rename-sheet` | `rename_sheet` | sheet=old, newSheet=new |
| `tabsrv/ensure-layout-for-sheet` | (called by Tableau on switch) | Triggers server-side layout; does NOT trigger UI redraw on its own |
| `tabdoc/get-web-categorical-color-dialog` + `assign-categorical-color-palette` + `release-component` | `set_color_palette` | Multi-step. Needs `fieldVector` with correctly-encoded field reference |

### Utility / raw access
- `ex.send_command(page, ns, name, params)` - returns unwrapped `result`. Most commands.
- `ex.send_command_raw(page, ns, name, params)` - returns full server response including `presentationLayerNotification`. Use when the data you need (controllerId, componentId) is stripped by the standard unwrap.

## File map

| File | Role |
|---|---|
| `dispatcher.js` | Injected into page; finds dispatcher, exposes `window.__tab.sendCommand` and `sendCommandRaw` |
| `exec.py` | Python side - `inject_dispatcher`, `send_command`, `send_command_raw`, `info` |
| `api.py` | Typed primitives - all the public-API functions documented above |
| `recipes.py` | High-level chart recipes built on `api.py` |
| `hunt.py` | Dispatcher discovery + method-wrap instrumentation for capture |
| `watch.py` | Comprehensive long-running watcher - captures req+resp bodies, dispatcher calls, event fires |
| `capture_drop.py` | One-shot harness: arm hunter, drive a drag, report |
| `connect.py` | Picks the right workbook authoring page from the CDP session |
| `shot.py` | Screenshot helper |

## Additional protocol facts (2026-05-19)

### Marks-card encoding aggregation
`change-aggregation` on a marks-card encoding **requires `paneSpec: 0`** in its params - columns/rows/filters changes work without it. Also: `shelf-pos-indices` for encoding-shelf identifies the *slot order* on the marks card:

| Index | Slot |
|---|---|
| 0 | Color |
| 1 | Size |
| 2 | Label / Text |
| 3+ | Detail, Tooltip, Shape, etc. (order may vary by mark type) |

### Bin / Group field encoding key
Numeric bins (`<Field> (bin)`) and groups (`<Field> (group)`) reference themselves with the **`:ok` suffix** instead of `:nk`. Example: `[sqlproxy.{ds}].[none:Profit (bin):ok]`. This breaks helpers that hardcode `:nk` for dimensions (e.g. `set_color_palette`'s field-vector construction - known issue, not yet fixed).

### `noExceptionDialog: true` is the right default
Every `executeSingleRemoteCommand` call accepts a `noExceptionDialog` flag. Setting it to **`true`** routes server errors through our promise without Tableau popping its "Unexpected Server Error" modal. `dispatcher.js` defaults to `true` so failures are clean.

### Calc-field reference format
Calc fields (incl. Tableau's built-in `Profit Ratio`) are referenced by `Calculation_<long-id>`, NOT by display name. Schema lookup via `tabdoc/get-schema` returns each column's `baseColumnName` (the bare form for `get-drag-pres-model`) and `fn` (the resolved form with `usr:` prefix and encoding key). `api.find_field_fn` handles this transparently - callers always use the display name.

### `delete-calculation-fields-command` is a universal delete
Discovered 2026-05-19: this single wire command deletes calc fields, numeric bins, groups, sets, AND parameters - only the `fn` reference differs:

| Kind | fn shape | Where to look it up |
|---|---|---|
| Calc field | `[sqlproxy.{ds}].[Calculation_<long-id>]` | `dataSources.<sqlproxy>.columnList[].baseColumnName` |
| Bin | `[sqlproxy.{ds}].[<Field> (bin)]` | same - but `baseColumnName` uses literal name |
| Group | `[sqlproxy.{ds}].[<Field> (group)]` | `dataSources.<sqlproxy>.fieldList[].fn` |
| Set | `[sqlproxy.{ds}].[<Field> Set]` | `dataSources.<sqlproxy>.fieldList[].fn` |
| Parameter | `[Parameters].[Parameter N]` (internal name, NOT display caption) | `dataSources.Parameters.fieldList[].fn` |

`api.delete_field(page, display_name) -> (fn, kind)` handles all five.

### Parameter caption vs internal name
Parameters' display captions (`"Top N"`, `"Min Sales"`) are stored separately from their internal `name` (`"Parameter 3"`, `"Parameter 4"`). The internal name is what shows up in `fn`. Always go through `get-schema` to map caption → fn for parameter operations.

### Diagnostic rule: "0 drop targets ⇒ wrong fn"
When `get-drag-pres-model` returns `shelfDropModels: []` for a field that you know is drag-able in the UI, **the `fn` reference is wrong**. The fix:
1. Arm `watch.py`
2. Manually drag the field in the browser
3. Read the request body - the SPA knows the right fn and reveals it

This pattern cracked the calc-field puzzle and is the universal escape hatch when the resolver returns empty.

### `send_command_raw` for dialogs
Some commands' useful return data lives in `vqlCmdResponse.layoutStatus.presentationLayerNotification[…]`, which `getCommandReturnValue` strips out in the standard unwrap. Examples: parameter `controllerId`, color-dialog `componentId`. Use `ex.send_command_raw` for these to get the full server response.

### UI cleanup taxonomy
Three layers of transient UI need distinct handling:

| Type | Detection | Dismissal |
|---|---|---|
| Modal dialogs (`*-Glass`) | overlay test-id pattern | specific close button → Escape fallback |
| Popovers (`.tabUberPopup`) | class match | real Playwright mouse click at safe viewport spot - synthetic JS MouseEvents don't trigger Tableau's global outside-click handler |
| Context menus (`role="menu"`) | ARIA role | Escape |

All three are scanned by `api.open_dialogs(page)` and dismissed by `api.close_open_dialogs(page)`.

### `flush_ui` is the universal post-mutation step
After every wire mutation, call `flush_ui(page)` to:
1. Drain `coord.deferredServerResponseQueue` via `coord.$B(null)` → applies UI updates
2. Auto-dismiss `detailedErrorDialog` if it popped

Most `api.py` primitives call it internally; if you ever go around them with raw `ex.send_command`, remember to flush yourself.

## Known limitations

- **Playwright drag misses for some sources**: data pane → Filters shelf and Analytics pane → viz don't fire commands via Playwright's mouse events. Workaround: use the wire commands directly (the `api.py` wrappers do this; `add_filter` bypasses `launch-filter-dialog` entirely).
- **Field encoding for bins/groups**: bin and group fields use `:ok` not `:nk` suffix in their reference. `set_color_palette` currently assumes plain dimension encoding; `(bin)` / `(group)` fields need a custom fn.
- **Parameter data-type transitions**: `parameter-edit-data-type` may LogicException when switching between unrelated types in some orders. Workaround: create a fresh parameter with the right type from the start.
- **Group name collisions**: re-creating a group with a name that already exists fails. No collision detection yet.
- **`pills_on_shelf('filters')`**: returns empty because the filter shelf's DOM uses different pill classes than columns/rows. Cosmetic; doesn't affect functionality.
