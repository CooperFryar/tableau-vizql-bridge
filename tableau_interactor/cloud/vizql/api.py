"""Stable public API for the VizQL bridge.

Higher-level primitives composed atop exec.send_command. This is what
Sheet/Workbook/recipes should use - not the scratch _test_/_diag_ scripts.
"""

import json
import re
import time
from typing import Any

from . import exec as ex


# ---- Naming-convention helpers --------------------------------------------

_CAMEL_RE = re.compile(r"([a-z])([A-Z])")


def to_kebab(obj: Any) -> Any:
    """Convert camelCase keys to kebab-case throughout a nested structure.

    get-drag-pres-model responses use camelCase keys; drop-on-shelf
    expects the same data with kebab-case keys.
    """
    if isinstance(obj, dict):
        return {_CAMEL_RE.sub(r"\1-\2", k).lower(): to_kebab(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_kebab(x) for x in obj]
    return obj


# ---- Field resolution -----------------------------------------------------


# ---- Schema resolver (real measures + calc fields) ------------------------


def get_schema_columns(page) -> list[dict]:
    """Fetch every column in the active datasource via `tabdoc/get-schema`.

    Each column has: `fieldCaption`, `fn`, `baseColumnName`, `isCalculated`,
    `fieldRole` (dimension/measure), and many more attributes. Use
    `find_field_fn` for the common name→fn lookup.
    """
    r = ex.send_command(page, "tabdoc", "get-schema", {})
    if not r.get("ok"):
        raise RuntimeError(f"get-schema failed: {r.get('message', '')[:300]}")
    ds_map = r["result"]["dataSchema"]["dataSources"]
    out = []
    for ds_key, ds_data in ds_map.items():
        # Real data sources: published extracts are keyed "sqlproxy.<id>",
        # files uploaded in web authoring are "federated.<id>". Only the
        # "Parameters" pseudo-source is excluded.
        if ds_key == "Parameters":
            continue
        out.extend(ds_data.get("columnList", []))
    return out


def find_field_fn(page, display_name: str) -> tuple[str, bool]:
    """Look up a field's BARE fn (suitable for get-drag-pres-model) by display name.

    Handles plain measures, dimensions, AND calculated fields. Calc fields in
    Tableau's schema are captioned as `AGG(<Name>)` and have an internal
    `Calculation_<id>` reference - neither matches the literal display name,
    so a plain `[sqlproxy.{ds}].[{name}]` construction fails for them.

    Returns (bareColumnName_or_constructed_fn, is_calculated).
    """
    # Resolve straight from the live schema - works for sqlproxy (published) and
    # federated (uploaded CSV) sources alike, does NOT depend on the dispatcher
    # having sniffed a datasourceId, and tolerates captions wrapped in a default
    # aggregation (``SUM(Nurses)`` → ``Nurses``).
    cols = get_schema_columns(page)
    agg_caption = f"AGG({display_name})"
    for c in cols:
        cap = c.get("fieldCaption", "")
        norm = re.sub(r"^[A-Za-z]+\((.*)\)$", r"\1", cap)
        if cap == display_name or cap == agg_caption or norm == display_name:
            base = c.get("baseColumnName")
            if base:
                return base, bool(c.get("isCalculated") in (True, "True"))
    # Fallback: construct from the field's datasource key (full prefix included).
    key, raw = _field_ref(page, display_name)
    return f"[{key}].[{raw}]", False


def resolve_drop_plan(page, field_display_name: str, sheet: str) -> dict:
    """Ask Tableau for the full drag presentation model for a field.

    Returns the parsed `drag` object containing per-shelf field encodings and
    drop positions. Raises RuntimeError on protocol failure.

    Now handles calc fields too - looks them up via `get-schema` to find the
    internal Calculation_<id> reference that Tableau actually expects.
    """
    bare, is_calc = find_field_fn(page, field_display_name)
    r = ex.send_command(page, "tabdoc", "get-drag-pres-model", {
        "worksheet": sheet,
        "isRightDrag": False,
        "paneSpec": 0,
        "dragSource": "drag-drop-schema",
        "fieldEncodings": [{"fn": bare}],
    })
    if not r.get("ok"):
        raise RuntimeError(f"get-drag-pres-model failed for {field_display_name!r} "
                           f"(fn={bare!r}, is_calc={is_calc}): "
                           f"{r.get('message', '')[:300]}")
    return r["result"]["drag"]


# ---- Atomic drop ----------------------------------------------------------


SHELF_TYPES = {
    "columns": "columns-shelf",
    "rows": "rows-shelf",
    "filters": "filter-shelf",
    "pages": "pages-shelf",
}

# Marks card encodings - all use shelfType="encoding-shelf" and are distinguished
# by encodingTypePresModel.encodingType. Pulled from the get-drag-pres-model
# response shape. Keyed by friendly name.
MARKS_ENCODINGS = {
    "color": "color-encoding",
    "size": "size-encoding",
    "shape": "shape-encoding",
    "label": "text-encoding",            # "Label" button maps to text-encoding
    "text": "text-encoding",
    "detail": "level-of-detail-encoding", # "Detail" button on marks card
    "tooltip": "tooltip-encoding",
    "angle": "wedge-size-encoding",       # for pie charts
    "image": "image-encoding",
    "edge": "edge-encoding",              # for path/network marks
    "sort": "sort-encoding",
    "level": "level-encoding",
    "custom": "custom-encoding",
}

# Friendly shelf name → DOM label for inspection
_SHELF_LABELS = {
    "columns-shelf": "Columns",
    "rows-shelf": "Rows",
    "filter-shelf": "Filters",
    "pages-shelf": "Pages",
}


def drop_field(page, field_display_name: str, shelf: str, sheet: str,
               *, pos: int = 0) -> str:
    """Drop a field on a shelf via the full VizQL protocol.

    `shelf` accepts:
      - shelf short names: "columns", "rows", "filters", "pages"
      - shelf full names:  "columns-shelf", "rows-shelf", "filter-shelf", "pages-shelf"
      - marks card encodings: "color", "size", "label", "detail", "tooltip",
                              "angle", "shape", "image", "text"
      - encoding-type strings: "color-encoding", "size-encoding", etc.

    Returns the resolved canonical fn used for the drop.
    """
    # Resolve shelf alias
    if shelf in MARKS_ENCODINGS:
        shelf_type = "encoding-shelf"
        encoding_type = MARKS_ENCODINGS[shelf]
    elif shelf.endswith("-encoding"):
        shelf_type = "encoding-shelf"
        encoding_type = shelf
    else:
        shelf_type = SHELF_TYPES.get(shelf, shelf)
        encoding_type = None

    plan = resolve_drop_plan(page, field_display_name, sheet)

    # For marks card encodings, find the entry by BOTH shelfType AND encodingType
    if encoding_type:
        target = None
        for m in plan["shelfDropModels"]:
            if m["shelfType"] != shelf_type:
                continue
            # Iterate this shelf-type's drop positions and pres models
            positions = m.get("shelfDropPositions", [])
            for posdef in positions:
                etype = posdef.get("encodingTypePresModel", {}).get("encodingType")
                if etype == encoding_type:
                    # Synthesize a tailored "target" out of this position + the matching fieldEncoding
                    target = {
                        "shelfType": shelf_type,
                        "fieldEncodings": m["fieldEncodings"],
                        "shelfDropPositions": [posdef],
                    }
                    break
            if target:
                break
        if target is None:
            avail = sorted({p.get("encodingTypePresModel", {}).get("encodingType")
                            for m in plan["shelfDropModels"]
                            if m["shelfType"] == "encoding-shelf"
                            for p in m.get("shelfDropPositions", [])})
            raise RuntimeError(f"encoding {encoding_type!r} not available for "
                               f"{field_display_name!r}; available: {sorted(a for a in avail if a)}")
    else:
        target = next((m for m in plan["shelfDropModels"] if m["shelfType"] == shelf_type), None)
        if target is None:
            avail = [m["shelfType"] for m in plan["shelfDropModels"]]
            raise RuntimeError(f"shelf {shelf_type!r} not in plan for {field_display_name!r}; "
                               f"available: {avail}")

    # Pick the matching fieldEncoding for this encoding-type if marks card,
    # otherwise just the first.
    if encoding_type:
        field_enc = next(
            (fe for fe in target["fieldEncodings"]
             if fe.get("encodingTypePresModel", {}).get("encodingType") == encoding_type),
            target["fieldEncodings"][0],
        )
    else:
        field_enc = target["fieldEncodings"][0]
    resolved_fn = field_enc["fn"]
    encoding_meta = field_enc["encodingTypePresModel"]

    drop_pos_camel = next(
        (p for p in target["shelfDropPositions"]
         if "shelfDropAction" not in p and p.get("shelfPosIndex") == pos),
        target["shelfDropPositions"][0],
    )

    # paneSpec on the top-level payload should mirror the drop position's paneSpec
    # for marks-card drops (otherwise zero).
    pane_spec = drop_pos_camel.get("paneSpec", 0)

    params = {
        "allowDuplicateFieldDropOnFilterShelf": False,
        "checkRelatability": True,
        "dragDescription": "",
        "dragSource": "drag-drop-schema",
        "dropTarget": "drag-drop-shelf",
        "fieldEncodings": [{
            "fn": resolved_fn,
            "encoding-type-pres-model": to_kebab(encoding_meta),
        }],
        "isCopy": False,
        "isDeadDrop": False,
        "isRightDrag": False,
        "paneSpec": pane_spec,
        "shelfDragSourcePosition": {"is-override": False},
        "shelfDropContext": "none",
        "shelfDropTargetPosition": to_kebab(drop_pos_camel),
        "worksheet": sheet,
    }
    r = ex.send_command(page, "tabdoc", "drop-on-shelf", params)
    if not r.get("ok"):
        raise RuntimeError(f"drop-on-shelf failed for {field_display_name!r} → {shelf_type}"
                           f"{'/' + encoding_type if encoding_type else ''}: "
                           f"{r.get('message', '')[:300]}")
    flush_ui(page)
    return resolved_fn


# ---- Move pill between shelves -------------------------------------------


def move_pill(page, field_display_name: str, from_shelf: str, from_pos: int,
              to_shelf: str, to_pos: int = 0, sheet: str | None = None) -> str:
    """Move a pill from one shelf to another. Uses drop-on-shelf with
    dragSource=drag-drop-shelf. Returns the resolved fn."""
    sheet = sheet or active_sheet_name(page)
    from_shelf_type = SHELF_TYPES.get(from_shelf, from_shelf)
    to_shelf_type = SHELF_TYPES.get(to_shelf, to_shelf)

    plan = resolve_drop_plan(page, field_display_name, sheet)
    target = next((m for m in plan["shelfDropModels"] if m["shelfType"] == to_shelf_type), None)
    if target is None:
        raise RuntimeError(f"target shelf {to_shelf_type!r} not in plan; "
                           f"available: {[m['shelfType'] for m in plan['shelfDropModels']]}")
    resolved_fn = target["fieldEncodings"][0]["fn"]
    encoding_meta = target["fieldEncodings"][0]["encodingTypePresModel"]
    drop_pos_camel = next(
        (p for p in target["shelfDropPositions"]
         if "shelfDropAction" not in p and p.get("shelfPosIndex") == to_pos),
        target["shelfDropPositions"][0],
    )

    params = {
        "allowDuplicateFieldDropOnFilterShelf": False,
        "checkRelatability": True,
        "dragDescription": "",
        "dragSource": "drag-drop-shelf",
        "dropTarget": "drag-drop-shelf",
        "fieldEncodings": [{
            "fn": resolved_fn,
            "encoding-type-pres-model": to_kebab(encoding_meta),
        }],
        "isCopy": False, "isDeadDrop": False, "isRightDrag": False,
        "paneSpec": drop_pos_camel.get("paneSpec", 0),
        "shelfDragSourcePosition": {
            "shelf-type": from_shelf_type,
            "shelf-pos-index": from_pos,
            "shelf-drop-action": "replace",
            "is-override": False,
        },
        "shelfDropContext": "none",
        "shelfDropTargetPosition": to_kebab(drop_pos_camel),
        "shelfSelection": [from_pos + 1],  # 1-based per drop-nowhere capture pattern
        "worksheet": sheet,
    }
    r = ex.send_command(page, "tabdoc", "drop-on-shelf", params)
    if not r.get("ok"):
        raise RuntimeError(f"move drop-on-shelf failed: {r.get('message', '')[:300]}")
    flush_ui(page)
    return resolved_fn


def copy_pill(page, field_display_name: str, from_shelf: str, from_pos: int,
              to_shelf: str, to_pos: int = 0, sheet: str | None = None) -> str:
    """Copy a pill from one shelf to another (source stays in place).

    Same protocol as `move_pill` but omits `shelfSelection` - the absence of
    that field is what tells the server not to clear the source. Returns the
    resolved fn placed on the destination.
    """
    sheet = sheet or active_sheet_name(page)
    from_shelf_type = SHELF_TYPES.get(from_shelf, from_shelf)
    to_shelf_type = SHELF_TYPES.get(to_shelf, to_shelf)

    plan = resolve_drop_plan(page, field_display_name, sheet)
    target = next((m for m in plan["shelfDropModels"] if m["shelfType"] == to_shelf_type), None)
    if target is None:
        raise RuntimeError(f"target shelf {to_shelf_type!r} not in plan; "
                           f"available: {[m['shelfType'] for m in plan['shelfDropModels']]}")
    resolved_fn = target["fieldEncodings"][0]["fn"]
    encoding_meta = target["fieldEncodings"][0]["encodingTypePresModel"]
    drop_pos_camel = next(
        (p for p in target["shelfDropPositions"]
         if "shelfDropAction" not in p and p.get("shelfPosIndex") == to_pos),
        target["shelfDropPositions"][0],
    )

    params = {
        "allowDuplicateFieldDropOnFilterShelf": False,
        "checkRelatability": True,
        "dragDescription": "",
        "dragSource": "drag-drop-shelf",
        "dropTarget": "drag-drop-shelf",
        "fieldEncodings": [{
            "fn": resolved_fn,
            "encoding-type-pres-model": to_kebab(encoding_meta),
        }],
        "isCopy": False, "isDeadDrop": False, "isRightDrag": False,
        "paneSpec": drop_pos_camel.get("paneSpec", 0),
        "shelfDragSourcePosition": {
            "shelf-type": from_shelf_type,
            "shelf-pos-index": from_pos,
            "shelf-drop-action": "replace",
            "is-override": False,
        },
        "shelfDropContext": "none",
        "shelfDropTargetPosition": to_kebab(drop_pos_camel),
        # No shelfSelection → server keeps source pill in place.
        "worksheet": sheet,
    }
    r = ex.send_command(page, "tabdoc", "drop-on-shelf", params)
    if not r.get("ok"):
        raise RuntimeError(f"copy drop-on-shelf failed: {r.get('message', '')[:300]}")
    flush_ui(page)
    return resolved_fn


# ---- Mark type ------------------------------------------------------------


MARK_TYPES = {
    # friendly → wire value (lowercase)
    "automatic": "auto", "auto": "auto",
    "bar": "bar", "line": "line", "area": "area",
    "circle": "circle", "square": "square", "shape": "shape",
    "text": "text", "map": "map", "pie": "pie",
    "gantt": "gantt", "gantt bar": "gantt",
    "polygon": "polygon", "density": "density",
}


def set_mark_type(page, mark_type: str, sheet: str | None = None) -> None:
    """Change the marks card mark type for the active (or specified) sheet."""
    sheet = sheet or active_sheet_name(page)
    wire = MARK_TYPES.get(mark_type.lower(), mark_type.lower())
    r = ex.send_command(page, "tabdoc", "set-primitive", {
        "worksheet": sheet,
        "paneSpec": 0,        # captured was 5; 0 also works as the SPA fills it in
        "primitiveType": wire,
    })
    if not r.get("ok"):
        raise RuntimeError(f"set-primitive failed for {wire!r}: {r.get('message', '')[:300]}")
    flush_ui(page)


# ---- Aggregation ----------------------------------------------------------


# Map friendly names to the wire values change-aggregation expects.
# Numerical aggregations AND date parts both flow through this command.
AGGREGATIONS = {
    # Numerical aggregations
    "sum": "sum",
    "avg": "average", "average": "average", "mean": "average",
    "median": "median",
    "count": "count",
    "countd": "countd", "count distinct": "countd",
    "min": "minimum", "minimum": "minimum",
    "max": "maximum", "maximum": "maximum",
    "stdev": "stdev", "std": "stdev",
    "stdevp": "stdevp",
    "var": "var", "variance": "var",
    "varp": "varp",
    "attr": "attribute", "attribute": "attribute",
    # Date parts (use change-aggregation with the date-part keyword)
    "year": "year",
    "quarter": "qtr", "qtr": "qtr",
    "month": "month",
    "week": "week",
    "weekday": "weekday",
    "day": "day",
    "hour": "hour",
    "minute": "minute",
    "second": "second",
    "year-quarter": "year-quarter",     # continuous combined
    "year-month": "year-month",
    # Continuous (chronological) date parts - preserve order across years
    "trunc-year":    "trunc-year",    "continuous-year":    "trunc-year",
    "trunc-quarter": "trunc-quarter", "continuous-quarter": "trunc-quarter",
    "trunc-month":   "trunc-month",   "continuous-month":   "trunc-month",
    "trunc-week":    "trunc-week",    "continuous-week":    "trunc-week",
    "trunc-day":     "trunc-day",     "continuous-day":     "trunc-day",
    "year-day": "year-day",
    "exact-date": "trunc-day",
}


def change_aggregation(page, shelf: str, pos: int, aggregation: str) -> None:
    """Change a measure pill's aggregation OR a date pill's date-part.

    `shelf` accepts the usual aliases (columns/rows/filters/pages), the full
    shelf-type strings, marks-card encoding names ("color", "size", "label",
    "detail", "tooltip", etc.), AND raw encoding-type strings ("size-encoding").

    For marks-card encodings, `pos` is interpreted as the encoding slot order
    on the active marks card (0=Color, 1=Size, 2=Label/Text, etc.) - pass 0
    when there's only one pill in that slot.
    """
    # Resolve shelf-type
    if shelf in MARKS_ENCODINGS or shelf.endswith("-encoding"):
        shelf_type = "encoding-shelf"
        is_marks = True
    else:
        shelf_type = SHELF_TYPES.get(shelf, shelf)
        is_marks = shelf_type == "encoding-shelf"

    wire = AGGREGATIONS.get(aggregation.lower(), aggregation.lower())
    params: dict = {
        "aggregation": wire,
        "shelfSelectionModel": {
            "shelf-type": shelf_type,
            "shelf-pos-indices": [pos],
        },
    }
    if is_marks:
        # Marks-card aggregation changes require paneSpec; columns/rows/filters do not.
        params["paneSpec"] = 0
    r = ex.send_command(page, "tabdoc", "change-aggregation", params)
    if not r.get("ok"):
        raise RuntimeError(f"change-aggregation failed for {wire!r} on {shelf}/{pos}: "
                           f"{r.get('message', '')[:300]}")
    flush_ui(page)


# Alias for readability when changing date parts
def set_date_part(page, shelf: str, pos: int, part: str) -> None:
    """Change a date pill's part (year/quarter/month/week/day/hour/etc.). Alias for change_aggregation."""
    change_aggregation(page, shelf, pos, part)


# ---- Field type (discrete vs continuous) ---------------------------------


# change-field-type values
FIELD_TYPES = {
    "discrete": "ordinal",
    "ordinal": "ordinal",
    "continuous": "interval",
    "interval": "interval",
    "dimension": "ordinal",  # informal alias
    "measure": "interval",   # informal alias
}


def _datasource_id(page) -> str:
    """Read the auto-detected datasource id from the dispatcher info."""
    info = ex.info(page)
    ds = info.get("datasourceId")
    if not ds:
        raise RuntimeError("datasource id unknown")
    return ds


def _ds_key(page) -> str:
    """Full datasource key INCLUDING its prefix, e.g. ``sqlproxy.<id>`` for a
    published extract or ``federated.<id>`` for a file uploaded in web
    authoring. Read straight from the live schema so it is correct regardless
    of how the data was connected (the dispatcher only auto-detects the bare id
    after it observes wire traffic, which a DOM-built workbook never produces).
    """
    r = ex.send_command(page, "tabdoc", "get-schema", {})
    if not r.get("ok"):
        raise RuntimeError(f"get-schema failed: {r.get('message', '')[:300]}")
    ds_map = r["result"]["dataSchema"]["dataSources"]
    keys = [k for k in ds_map if k != "Parameters"]
    if not keys:
        raise RuntimeError("no data datasource found in schema")
    keys.sort(key=lambda k: len(ds_map[k].get("columnList", [])), reverse=True)
    return keys[0]


def _field_ref(page, display_name: str) -> tuple[str, str]:
    """Resolve a field to (datasource_key, raw_column) from its schema
    ``baseColumnName`` - the source-aware way. Handles multiple data sources in
    one workbook (each field carries its own ``[<dskey>].[<rawcol>]``) and
    captions wrapped in a default aggregation (``SUM(Nurses)`` → ``Nurses``,
    ``ATTR(Status)`` → ``Status``). Falls back to the primary source + caption.
    """
    cols = get_schema_columns(page)
    agg_caption = f"AGG({display_name})"
    for c in cols:
        cap = c.get("fieldCaption", "")
        norm = re.sub(r"^[A-Za-z]+\((.*)\)$", r"\1", cap)
        if cap == display_name or cap == agg_caption or norm == display_name:
            base = c.get("baseColumnName")
            if base:
                toks = re.findall(r"\[([^\]]*)\]", base)
                if len(toks) >= 2:
                    return toks[0], toks[1]
    return _ds_key(page), display_name


def _raw_col(page, display_name: str) -> str:
    """The raw column token for a field (source-aware via _field_ref)."""
    return _field_ref(page, display_name)[1]


def _dim_fn(page, display_name: str, ds_key: str | None = None) -> str:
    """Discrete (dimension) field fn: ``[<dskey>].[none:<rawcol>:nk]`` - uses the
    field's OWN datasource so it works under multiple sources in one workbook."""
    key, raw = _field_ref(page, display_name)
    return f"[{ds_key or key}].[none:{raw}:nk]"


def _meas_fn(page, display_name: str, agg: str = "sum",
             ds_key: str | None = None) -> str:
    """Continuous (measure) field fn: ``[<dskey>].[<agg>:<rawcol>:qk]``."""
    key, raw = _field_ref(page, display_name)
    return f"[{ds_key or key}].[{agg}:{raw}:qk]"


# ---- Calculated fields ----------------------------------------------------


def _find_deletable_field(page, display_name: str) -> tuple[str, str] | None:
    """Locate any user-creatable field across the schema and return (fn_to_delete, kind).

    Searches all of:
      - main datasource `columnList` (calc fields, bins - sometimes with AGG(...) caption)
      - main datasource `fieldList`  (groups, sets, hierarchies)
      - Parameters `fieldList`       (parameters - internal name `Parameter N`)

    `kind` is one of "calc", "bin", "group", "set", "parameter", or "other".
    Returns None if no match.
    """
    r = ex.send_command(page, "tabdoc", "get-schema", {})
    if not r.get("ok"):
        raise RuntimeError(f"get-schema failed: {r.get('message', '')[:300]}")
    ds_map = r["result"]["dataSchema"]["dataSources"]
    agg_caption = f"AGG({display_name})"

    for ds_key, ds_data in ds_map.items():
        is_params = ds_key == "Parameters"
        # 1. columnList (calcs, bins)
        for c in ds_data.get("columnList", []):
            cap = c.get("fieldCaption", "")
            if cap not in (display_name, agg_caption):
                continue
            # baseColumnName is the bare reference Tableau expects for delete
            fn = c.get("baseColumnName") or c.get("fn")
            if not fn:
                continue
            if "(bin)" in cap:
                return fn, "bin"
            if c.get("isCalculated") in (True, "True"):
                return fn, "calc"
            return fn, "other"
        # 2. fieldList (groups, sets, parameters)
        for f in ds_data.get("fieldList", []):
            cap = f.get("fieldCaption", "")
            if cap != display_name:
                continue
            fn = f.get("fn")
            if not fn:
                continue
            if is_params:
                return fn, "parameter"
            if "(group)" in cap:
                return fn, "group"
            if cap.endswith(" Set"):
                return fn, "set"
            return fn, "other"
    return None


def delete_field(page, display_name: str) -> tuple[str, str]:
    """Delete any user-created field by display name - calc field, bin, group, set, OR parameter.

    Looks up the right internal reference via `tabdoc/get-schema` (calc fields use
    `Calculation_<id>`, parameters use `Parameter N`, others use display name).
    Sends `delete-calculation-fields-command` which is the universal delete wire.

    Returns (deleted_fn, kind). Raises ValueError if no matching field is found.
    """
    found = _find_deletable_field(page, display_name)
    if not found:
        raise ValueError(f"no deletable field found matching {display_name!r}")
    fn, kind = found
    r = ex.send_command(page, "tabdoc", "delete-calculation-fields-command", {
        "fieldVector": [fn],
        "isDeleteCalcConfirmed": False,
    })
    if not r.get("ok"):
        raise RuntimeError(f"delete-{kind} failed for {display_name!r}: "
                           f"{r.get('message', '')[:300]}")
    flush_ui(page)
    return fn, kind


# Backwards-compat alias - same behavior, narrower contract documented.
def delete_calc_field(page, display_name: str) -> str:
    """Deprecated alias for `delete_field`. Returns just the fn."""
    fn, _ = delete_field(page, display_name)
    return fn


def create_calc_field(page, name: str, formula: str) -> None:
    """Create a calculated field via the wire protocol - no dialog.

    Two-phase: `create-calc` opens the server-side calc context, then
    `apply-calculation` commits the name+formula. Mirrors what the
    Analysis -> Create Calculated Field dialog does.

    Reminder: prefer Groups / Sets / Bins / Parameters when they fit -
    calc fields are for genuine multi-field expressions. See
    feedback_grouping_mechanisms.md in memory.
    """
    ds = _datasource_id(page)
    r1 = ex.send_command(page, "tabdoc", "create-calc", {
        "datasource": f"sqlproxy.{ds}",
        "joinOnCalcInfo": {
            "for-join": False, "is-left": True,
            "clause-to-modify-index": -1, "table-alias": "", "join-expression": "",
        },
    })
    if not r1.get("ok"):
        raise RuntimeError(f"create-calc failed: {r1.get('message', '')[:300]}")
    r2 = ex.send_command(page, "tabdoc", "apply-calculation", {
        "updatedCalculationCaption": name,
        "updatedCalculationFormula": formula,
        "isFullStyling": True,
    })
    if not r2.get("ok"):
        raise RuntimeError(f"apply-calculation failed: {r2.get('message', '')[:300]}")
    # The SPA opens the calc editor pane when it sees create-calc; close it so
    # the workbook doesn't leave a stray dialog visible. (Captured in the SPA's
    # normal flow as the last command in calc creation.)
    ex.send_command(page, "tabdoc", "clear-calculation-model", {})
    flush_ui(page)
    close_open_dialogs(page)


def set_field_type(page, shelf: str, pos: int, field_type: str) -> None:
    """Toggle a pill between discrete (ordinal) and continuous (interval)."""
    shelf_type = SHELF_TYPES.get(shelf, shelf)
    wire = FIELD_TYPES.get(field_type.lower(), field_type.lower())
    r = ex.send_command(page, "tabdoc", "change-field-type", {
        "fieldType": wire,
        "shelfSelectionModel": {
            "shelf-type": shelf_type,
            "shelf-pos-indices": [pos],
        },
    })
    if not r.get("ok"):
        raise RuntimeError(f"change-field-type failed for {wire!r}: {r.get('message', '')[:300]}")
    flush_ui(page)


# ---- Dialog / UI state inspection ----------------------------------------


# Known blocking-dialog prefixes we've encountered in the wild. The scanner in
# `open_dialogs` finds *any* visible `*-Glass` overlay generically, so this is
# documentation more than a filter - but it pins down expected names for tests.
KNOWN_DIALOG_PREFIXES = {
    "detailedErrorDialog-Dialog":     "Tableau's 'Unexpected Server Error' modal - auto-popped when a wire command fails with noExceptionDialog=false. Always safe to dismiss.",
    "numericBinDialog-Dialog":        "Edit Bins dialog - opens when create-numeric-bin fires; no wire-level close exists.",
    "group-Dialog":                   "Group editor - opens during categorical-bin-* sequence.",
    "parameters-dialog-id-Dialog":    "Parameter editor - closed via parameter-close-dialog wire command, but X also works.",
    "sort-Dialog":                    "Sort dialog - opens via show-sort-dialog.",
    "tabConnectionDialog-Dialog":     "Connection / 'Connect to Data' dialog.",
    "filterDialog-Dialog":            "Filter Edit dialog (Top N tab, condition tab, etc.).",
}

# Dialogs that are ALWAYS unintended - flush_ui will auto-dismiss these.
ERROR_DIALOG_PREFIXES = {"detailedErrorDialog-Dialog"}


# JS that finds visible BLOCKING UI - i.e. things with a modal Glass overlay
# OR an open context menu. Skips legitimate display widgets like color legends
# and parameter control cards (those are visible on purpose).
_OPEN_DIALOGS_JS = r"""
() => {
  const isVisible = el => {
    if (!el.offsetParent && el !== document.body) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity) > 0;
  };
  const out = { dialogs: [], menus: [], popovers: [] };

  // 1. Modal dialogs - pair every Glass overlay with its dialog body.
  const glasses = [...document.querySelectorAll('[data-tb-test-id$="-Glass"], [data-tb-test-id$="-Glass-Root"]')]
    .filter(isVisible);
  for (const g of glasses) {
    const testId = g.getAttribute('data-tb-test-id') || '';
    const prefix = testId.replace(/-Glass(-Root)?$/, '');
    const body = document.querySelector(`[data-tb-test-id="${prefix}-Dialog-Content"], [data-tb-test-id="${prefix}-Content"], [data-tb-test-id="${prefix}"]`);
    const closeBtn = document.querySelector(`[data-tb-test-id="${prefix}-CloseButton"], [data-tb-test-id="${prefix}-Dialog-CloseButton"]`);
    out.dialogs.push({
      prefix,
      label: body?.getAttribute('aria-label') || prefix,
      text: (body?.innerText || '').slice(0, 80).replace(/\s+/g, ' '),
      closeSelector: closeBtn ? `[data-tb-test-id="${closeBtn.getAttribute('data-tb-test-id')}"]` : null,
    });
  }

  // 2. role=dialog without Glass (rare but possible)
  for (const el of document.querySelectorAll('[role="dialog"]')) {
    if (!isVisible(el)) continue;
    const testId = el.getAttribute('data-tb-test-id') || '';
    if (out.dialogs.some(d => testId.startsWith(d.prefix))) continue;
    out.dialogs.push({
      prefix: testId.replace(/-(Dialog-)?Content$/, ''),
      label: el.getAttribute('aria-label') || testId,
      text: el.innerText.slice(0, 80).replace(/\s+/g, ' '),
      closeSelector: null,
    });
  }

  // 3. Popovers - `tabUberPopup` is Tableau's universal floating popover (Color, Size,
  // Label, Detail, Tooltip, axis menus, etc). They are non-modal but still "transient
  // UI that should be closed" in automation context. Dismissed via Escape or canvas click.
  for (const el of document.querySelectorAll('.tabUberPopup')) {
    if (!isVisible(el)) continue;
    // Identify by descendant content - first heading/label/known-button text
    const heading = el.querySelector('[class*="header"], [class*="Header"], [class*="Title"]');
    const editBtn = el.querySelector('[data-tb-test-id$="-Button"]');
    out.popovers.push({
      kind: el.className.toString().includes("tabMarksCardUberPopup") ? "marks-card" : "generic",
      hint: heading?.innerText.trim().slice(0, 60) ||
            editBtn?.innerText.trim().slice(0, 60) ||
            el.innerText.split("\n", 1)[0].slice(0, 60),
      class: el.className.toString().slice(0, 80),
    });
  }

  // 4. Context menus
  for (const el of document.querySelectorAll('[role="menu"]')) {
    if (!isVisible(el)) continue;
    out.menus.push({
      label: el.getAttribute('aria-label'),
      itemCount: el.querySelectorAll('[role="menuitem"]').length,
    });
  }
  return out;
}
"""


def open_dialogs(page) -> dict:
    """Return any visible transient UI:
      - `dialogs`: modal dialogs (with glass overlay) - block all interaction
      - `popovers`: floating popovers (Marks card Color/Size/Label menus, axis
                    menus, etc.) - non-modal but should be dismissed in automation
      - `menus`: open context menus (right-click etc.)

    Does NOT include legitimate display widgets like color legends, parameter
    controls, or filter cards on the right side of the viz - those are
    persistent display elements, not transient UI.
    """
    return page.evaluate(_OPEN_DIALOGS_JS)


def close_error_dialogs(page) -> int:
    """Close ONLY error-style dialogs (the 'Unexpected Server Error' modal).

    Safe to call after every mutation - won't touch intentional dialogs like
    calc field editors or color pickers. Returns count closed.
    """
    state = open_dialogs(page)
    closed = 0
    for d in state.get("dialogs", []):
        if d.get("prefix") not in ERROR_DIALOG_PREFIXES:
            continue
        if d.get("closeSelector"):
            btn = page.locator(d["closeSelector"])
            if btn.count() > 0 and btn.first.is_visible():
                try:
                    btn.first.click(timeout=2000)
                    closed += 1
                    continue
                except Exception:
                    pass
        page.keyboard.press("Escape")
        closed += 1
    return closed


def close_open_dialogs(page, *, verbose: bool = False) -> int:
    """Close every blocking dialog + popover + context menu. Returns count closed.

    Strategy:
      1. Dialogs: try specific close button first (test-id), fall back to Escape.
      2. Popovers: Escape key (universal Tableau popover dismiss).
      3. Menus: Escape key.
      4. Re-scan up to 3 times in case closing one reveals another.
    """
    import time
    total_closed = 0
    for _round in range(3):
        state = open_dialogs(page)
        if not state.get("dialogs") and not state.get("popovers") and not state.get("menus"):
            break
        for d in state.get("dialogs", []):
            if verbose:
                print(f"  closing dialog: {d['label']}")
            closed = False
            if d.get("closeSelector"):
                btn = page.locator(d["closeSelector"])
                if btn.count() > 0 and btn.first.is_visible():
                    try:
                        btn.first.click(timeout=2000)
                        closed = True
                    except Exception:
                        pass
            if not closed:
                page.keyboard.press("Escape")
                time.sleep(0.2)
            total_closed += 1
        if state.get("popovers"):
            if verbose:
                for p in state["popovers"]:
                    print(f"  dismissing popover ({p['kind']}): {p['hint']}")
            # Tableau popovers (.tabUberPopup) listen for native outside-clicks via
            # a global document handler - synthetic JS MouseEvents don't reliably
            # trigger it. Use Playwright's real mouse to click an empty canvas
            # spot in the upper viz area (away from pills, shelves, and the
            # popover itself). If that area is occupied by the popover, the SPA
            # still treats clicks on the chart-rendering layer beneath as outside-the-popover.
            try:
                # Compute a safe dismiss point: middle-top of viewport, well clear
                # of left-side popovers (which usually anchor below the marks card on the left).
                vp = page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
                dx, dy = int(vp["w"] * 0.7), int(vp["h"] * 0.15)
                page.mouse.click(dx, dy)
            except Exception:
                pass
            time.sleep(0.2)
            # Escape for popovers that DO respect it
            page.keyboard.press("Escape")
            time.sleep(0.2)
            total_closed += len(state["popovers"])
        for _ in state.get("menus", []):
            page.keyboard.press("Escape")
            time.sleep(0.15)
            total_closed += 1
        time.sleep(0.4)
    return total_closed


# ---- Sheet operations -----------------------------------------------------


def new_sheet(page, *, insert_at_end: bool = True, switch_to: bool = True) -> str:
    """Create a new blank worksheet and (by default) switch to it.

    Returns the new sheet's name. Tableau auto-names it `Sheet N`.
    """
    r = ex.send_command(page, "tabdoc", "new-worksheet", {
        "insertAtEnd": insert_at_end,
        "shouldChangeUiMode": switch_to,
    })
    if not r.get("ok"):
        raise RuntimeError(f"new-worksheet failed: {r.get('message', '')[:300]}")
    flush_ui(page)
    return active_sheet_name(page)


# ---- Dashboards -----------------------------------------------------------


def new_dashboard(page) -> str:
    """Create a new (empty) dashboard. Returns the new dashboard's name (Tableau
    auto-names: 'Dashboard 1', 'Dashboard 2', ...).
    """
    before = set(list_sheets(page))
    r = ex.send_command(page, "tabdoc", "new-dashboard", {})
    if not r.get("ok"):
        raise RuntimeError(f"new-dashboard failed: {r.get('message','')[:300]}")
    flush_ui(page)
    after = set(list_sheets(page))
    new = list(after - before)
    return new[0] if new else "Dashboard ?"


def add_sheet_to_dashboard(page, worksheet: str, *, floating: bool = False) -> None:
    """Add a worksheet to the current dashboard.

    Equivalent to right-click sheet in dashboard pane → Add to Dashboard, or
    dragging a sheet from the Sheets list onto the canvas.

    Args:
      worksheet: name of the worksheet to add
      floating: True = floating zone (movable/resizable); False = tiled (default)
    """
    r = ex.send_command(page, "tabdoc", "add-sheet-to-dashboard", {
        "worksheet": worksheet,
        "addAsFloating": "true" if floating else "false",
    })
    if not r.get("ok"):
        raise RuntimeError(f"add-sheet-to-dashboard failed: {r.get('message','')[:300]}")
    flush_ui(page)


def remove_sheet_from_dashboard(page, worksheet: str, dashboard: str | None = None, *,
                                delete_orphans: bool = False) -> None:
    """Remove a worksheet zone from a dashboard (does NOT delete the worksheet)."""
    dashboard = dashboard or active_sheet_name(page)
    r = ex.send_command(page, "tabdoc", "remove-sheet-from-dashboard", {
        "dashboard": dashboard,
        "worksheet": worksheet,
        "deleteOrphans": "true" if delete_orphans else "false",
    })
    if not r.get("ok"):
        raise RuntimeError(f"remove-sheet-from-dashboard failed: {r.get('message','')[:300]}")
    flush_ui(page)


ZONE_TYPES = {
    "horizontal": "horizontal", "h-container": "horizontal", "hcontainer": "horizontal",
    "vertical":   "vertical",   "v-container": "vertical",   "vcontainer": "vertical",
    "text":       "text",
    "image":      "image",
    "web":        "web", "webpage": "web", "web-page": "web",
    "blank":      "blank",
    "navigation": "navigation", "nav": "navigation",
    "extension":  "extension",
    "download":   "download", "export": "download",
}


def add_dashboard_object(page, *, kind: str = "text",
                        x: int = 100, y: int = 100,
                        width: int = 200, height: int = 100,
                        floating: bool = True,
                        dashboard: str | None = None) -> None:
    """Add an object (text, image, web page, container, etc.) to a dashboard.

    Args:
      kind: "text" / "image" / "web" / "horizontal" / "vertical" / "blank" /
            "navigation" / "extension" / "download"
      x, y, width, height: position and size of the new zone (only meaningful
                           for floating zones; tiled zones are auto-positioned)
      floating: True (default) = floating zone; False = tiled
      dashboard: dashboard name (defaults to active sheet)
    """
    kind_wire = ZONE_TYPES.get(kind.lower())
    if not kind_wire:
        raise ValueError(f"kind must be one of {list(ZONE_TYPES)}, got {kind!r}")
    dashboard = dashboard or active_sheet_name(page)
    r = ex.send_command(page, "tabdoc", "drop-on-dashboard", {
        "dashboard": dashboard,
        "addAsFloating": "true" if floating else "false",
        "dropLocation": json.dumps({"x": int(x), "y": int(y), "w": int(width), "h": int(height)}),
        "zoneType": kind_wire,
        "isHorizontal": "false",
    })
    if not r.get("ok"):
        raise RuntimeError(f"drop-on-dashboard failed: {r.get('message','')[:300]}")
    flush_ui(page)


def add_text_object(page, text: str = "", **kwargs) -> None:
    """Add a text object to a dashboard. ``text`` arg is reserved - editing
    content needs a separate dialog interaction (deferred). For now, the object
    is created with empty/default content."""
    add_dashboard_object(page, kind="text", **kwargs)


def add_image_object(page, **kwargs) -> None:
    """Add an image placeholder to the dashboard (URL set via separate dialog)."""
    add_dashboard_object(page, kind="image", **kwargs)


def add_web_page_object(page, **kwargs) -> None:
    """Add a web-page embed placeholder."""
    add_dashboard_object(page, kind="web", **kwargs)


def add_blank_object(page, **kwargs) -> None:
    """Add a blank spacer object."""
    add_dashboard_object(page, kind="blank", **kwargs)


def add_horizontal_container(page, **kwargs) -> None:
    """Add a horizontal layout container to the dashboard."""
    add_dashboard_object(page, kind="horizontal", **kwargs)


def add_vertical_container(page, **kwargs) -> None:
    """Add a vertical layout container."""
    add_dashboard_object(page, kind="vertical", **kwargs)


def toggle_use_as_filter(page, worksheet: str, dashboard: str | None = None) -> None:
    """Toggle 'Use as Filter' on a worksheet zone in a dashboard.

    When enabled, clicking a mark in this worksheet filters all other sheets
    in the dashboard. This is the most common dashboard interactive behavior -
    a one-call shortcut for the most common filter action.

    Equivalent to clicking the funnel icon at the top-right of a worksheet zone.
    """
    dashboard = dashboard or active_sheet_name(page)
    r = ex.send_command(page, "tabdoc", "master-detail-filter", {
        "dashboard": dashboard,
        "worksheet": worksheet,
    })
    if not r.get("ok"):
        raise RuntimeError(f"master-detail-filter failed: {r.get('message','')[:300]}")
    flush_ui(page)


def open_actions_dialog(page) -> None:
    """Open the Dashboard → Actions dialog (Shift+Cmd+D). Use for manual
    interaction - full action-specification wire format isn't wrapped yet.
    See add_action()."""
    r = ex.send_command(page, "tabdoc", "show-action-list-dialog-for-dashboard", {"bool": "true"})
    if not r.get("ok"):
        raise RuntimeError(f"show-action-list-dialog-for-dashboard failed: {r.get('message','')[:300]}")
    flush_ui(page)


def discard_action_changes(page) -> None:
    """Close the Actions dialog without saving (matches the 'discard-action-change' wire)."""
    ex.send_command(page, "tabdoc", "discard-action-change", {})
    flush_ui(page)


ACTION_ACTIVATIONS = {
    "explicitly": "explicitly", "select": "explicitly", "click": "explicitly",
    "hover":      "hover",
    "menu":       "menu",
}

FILTER_ON_CLEAR = {
    "do-nothing":         "do-nothing",
    "show-all":           "show-all-values",
    "show-all-values":    "show-all-values",
    "exclude-all":        "exclude-all-values",
    "exclude-all-values": "exclude-all-values",
}


def _ensure_action_dialog_open(page) -> None:
    """Open the Actions dialog if it isn't already. Required before any
    add-new-*-action call (which expects the dialog's state to exist)."""
    ex.send_command(page, "tabdoc", "show-action-list-dialog-for-dashboard", {"bool": "true"})


def _commit_action_changes(page) -> None:
    """Commit pending action changes via the Actions dialog's OK wire path.
    Without this call, add-new-*-action changes are discarded when the
    dialog closes. Pair with _ensure_action_dialog_open at the start."""
    ex.send_command(page, "tabdoc", "commit-action-change", {})
    flush_ui(page)


def _action_common(caption: str, activation: str) -> dict:
    act = ACTION_ACTIVATIONS.get(activation.lower(), activation)
    return {
        "caption": caption,
        "runActionOnPresModel": {
            "runOnSingleSelectIsChecked": False,
            "activation": act,
            "runOnSingleSelectIsVisible": True,
        },
    }


def add_filter_action(page, caption: str = "Filter Action", *,
                     activation: str = "explicitly",
                     on_clear: str = "do-nothing") -> None:
    """Add a Filter action to the current dashboard.

    Args:
      caption: action name (shown in the Actions dialog list)
      activation: "explicitly" (click - default), "hover", or "menu"
      on_clear: behavior when selection cleared - "do-nothing" (default),
                "show-all-values", or "exclude-all-values"

    NOTE: Source and target sheets are NOT set here - Tableau defaults to all
    sheets on the dashboard. Edit the action in Tableau (Dashboard → Actions)
    to refine sources/targets/fields. The basic action is functional with defaults.
    """
    on_clear_wire = FILTER_ON_CLEAR.get(on_clear.lower(), on_clear)
    payload = {
        "updateActionCommonParamsPresModel": _action_common(caption, activation),
        "onClear": on_clear_wire,
    }
    _ensure_action_dialog_open(page)
    ex.send_command(page, "tabdoc", "create-new-filter-action-dialog", {})
    r = ex.send_command(page, "tabdoc", "add-new-filter-action", {
        "updateFilterActionParamsPresModel": json.dumps(payload),
    })
    if not r.get("ok"):
        raise RuntimeError(f"add-new-filter-action failed: {r.get('message','')[:300]}")
    _commit_action_changes(page)


def add_highlight_action(page, caption: str = "Highlight Action", *,
                        activation: str = "hover") -> None:
    """Add a Highlight action. Default activation is hover (typical for highlights)."""
    payload = {"updateActionCommonParamsPresModel": _action_common(caption, activation)}
    _ensure_action_dialog_open(page)
    ex.send_command(page, "tabdoc", "create-new-highlight-action-dialog", {})
    r = ex.send_command(page, "tabdoc", "add-new-highlight-action", {
        "updateHighlightActionParamsPresModel": json.dumps(payload),
    })
    if not r.get("ok"):
        raise RuntimeError(f"add-new-highlight-action failed: {r.get('message','')[:300]}")
    _commit_action_changes(page)


def add_url_action(page, caption: str = "URL Action", url: str = "https://example.com", *,
                  activation: str = "menu") -> None:
    """Add a Go-to-URL action. Default activation is menu (typical for URL actions)."""
    payload = {
        "updateActionCommonParamsPresModel": _action_common(caption, activation),
        "url": url,
    }
    _ensure_action_dialog_open(page)
    ex.send_command(page, "tabdoc", "create-new-url-action-dialog", {})
    r = ex.send_command(page, "tabdoc", "add-new-url-action", {
        "updateUrlActionParamsPresModel": json.dumps(payload),
    })
    if not r.get("ok"):
        raise RuntimeError(f"add-new-url-action failed: {r.get('message','')[:300]}")
    _commit_action_changes(page)


def add_go_to_sheet_action(page, caption: str = "Go to Sheet", target_sheet: str = "None",
                          *, activation: str = "on-select") -> None:
    """Add a Go-to-Sheet (navigation) action.

    Wire: `add-new-go-to-sheet-action` with `updateNavActionParamsPresModel`.
    Default target is "None" (no specific target - author refines in dialog).
    activation defaults to "on-select" (the Tableau Cloud default for nav).
    """
    payload = {
        "updateActionCommonParamsPresModel": _action_common(caption, activation),
        "targetSheet": target_sheet,
        "includedSheetValues": [True, True],
    }
    _ensure_action_dialog_open(page)
    ex.send_command(page, "tabdoc", "create-new-go-to-sheet-action-dialog", {})
    r = ex.send_command(page, "tabdoc", "add-new-go-to-sheet-action", {
        "updateNavActionParamsPresModel": json.dumps(payload),
    })
    if not r.get("ok"):
        raise RuntimeError(f"add-new-go-to-sheet-action failed: {r.get('message','')[:300]}")
    _commit_action_changes(page)


def add_parameter_action(page) -> None:
    """Add a Change Parameter action with default settings.

    Wire: `create-new-parameter-action-dialog` → `accept-parameter-action-dialog`.
    Unlike the other action types, parameter actions don't expose a
    `updateXActionParamsPresModel` payload at the add step - the dialog state
    holds the config and `accept` commits it with current defaults (typically
    the workbook's first parameter as target).

    To customize the parameter target / source field, open the Actions dialog
    in Tableau and edit the created action.
    """
    _ensure_action_dialog_open(page)
    ex.send_command(page, "tabdoc", "create-new-parameter-action-dialog", {})
    r = ex.send_command(page, "tabdoc", "accept-parameter-action-dialog", {})
    if not r.get("ok"):
        raise RuntimeError(f"accept-parameter-action-dialog failed: {r.get('message','')[:300]}")
    _commit_action_changes(page)


def toggle_dashboard_title(page, dashboard: str | None = None) -> None:
    """Toggle the dashboard title bar (Dashboard menu → Show Title)."""
    dashboard = dashboard or active_sheet_name(page)
    r = ex.send_command(page, "tabdoc", "show-dashboard-title", {"dashboard": dashboard})
    if not r.get("ok"):
        raise RuntimeError(f"show-dashboard-title failed: {r.get('message','')[:300]}")
    flush_ui(page)


def toggle_dashboard_grid(page, *, show: bool = True, dashboard: str | None = None) -> None:
    """Show or hide the dashboard layout grid (Dashboard menu → Show Grid)."""
    dashboard = dashboard or active_sheet_name(page)
    r = ex.send_command(page, "tabdoc", "dashboard-show-grid", {
        "dashboard": dashboard,
        "dashboardShowGrid": "true" if show else "false",
    })
    if not r.get("ok"):
        raise RuntimeError(f"dashboard-show-grid failed: {r.get('message','')[:300]}")
    flush_ui(page)


def toggle_device_preview(page, *, visible: bool = True, tablet: bool = False,
                         dashboard: str | None = None) -> None:
    """Show or hide the Device Preview panel for a dashboard.

    Args:
      visible: True to show, False to hide
      tablet: True = default to Tablet preview; False = Default Phone
      dashboard: dashboard name (defaults to active)
    """
    dashboard = dashboard or active_sheet_name(page)
    r = ex.send_command(page, "tabdoc", "set-is-device-preview-visible", {
        "dashboard": dashboard,
        "isVisible": "true" if visible else "false",
        "useTabletAsDefaultPreview": "true" if tablet else "false",
    })
    if not r.get("ok"):
        raise RuntimeError(f"set-is-device-preview-visible failed: {r.get('message','')[:300]}")
    flush_ui(page)


def clear_dashboard(page, dashboard: str | None = None, *, delete_orphans: bool = False) -> None:
    """Remove all zones from a dashboard (Dashboard menu → Clear Dashboard)."""
    dashboard = dashboard or active_sheet_name(page)
    r = ex.send_command(page, "tabdoc", "clear-sheet", {
        "sheet": dashboard,
        "deleteOrphans": "true" if delete_orphans else "false",
    })
    if not r.get("ok"):
        raise RuntimeError(f"clear-sheet failed: {r.get('message','')[:300]}")
    flush_ui(page)




def goto_sheet(page, sheet: str) -> None:
    """Switch the active sheet tab (equivalent to clicking the sheet tab)."""
    r = ex.send_command(page, "tabdoc", "goto-sheet", {"sheet": sheet})
    if not r.get("ok"):
        raise RuntimeError(f"goto-sheet failed: {r.get('message','')[:300]}")
    flush_ui(page)


def list_sheets(page) -> list[str]:
    """Return the names of all visible sheet tabs (excludes Data Source)."""
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('.tabAuthTabLabel'))"
        ".map(el => el.textContent.trim()).filter(t => t && t !== 'Data Source')"
    )


def duplicate_sheet(page, sheet: str | None = None, *, as_crosstab: bool = False,
                    is_dashboard: bool = False) -> str:
    """Duplicate a sheet (right-click sheet tab → Duplicate). Returns the new sheet's name.

    Args:
      sheet: sheet to duplicate (defaults to active)
      as_crosstab: True = "Duplicate as Crosstab" (turns chart into text table)
      is_dashboard: True if duplicating a dashboard
    """
    sheet = sheet or active_sheet_name(page)
    before = set(list_sheets(page))
    cmd = "duplicate-sheets-as-crosstabs" if as_crosstab else "duplicate-sheets"
    r = ex.send_command(page, "tabdoc", cmd, {
        "sheetPms": json.dumps([{"sheet-name": sheet, "is-dashboard": is_dashboard}]),
    })
    if not r.get("ok"):
        raise RuntimeError(f"{cmd} failed: {r.get('message','')[:300]}")
    flush_ui(page)
    after = set(list_sheets(page))
    new_sheets = list(after - before)
    return new_sheets[0] if new_sheets else f"{sheet} (?)"


def delete_sheet(page, sheet: str, *, delete_orphans: bool = False) -> None:
    """Delete a sheet (right-click sheet tab → Delete). Cannot delete the last sheet.

    Args:
      sheet: sheet name to delete
      delete_orphans: if True, also delete dashboards that referenced this sheet
    """
    r = ex.send_command(page, "tabdoc", "delete-sheets", {
        "sheets": json.dumps([sheet]),
        "deleteOrphans": "true" if delete_orphans else "false",
    })
    if not r.get("ok"):
        raise RuntimeError(f"delete-sheets failed: {r.get('message','')[:300]}")
    flush_ui(page)


def hide_sheet(page, sheet: str, *, hidden: bool = True) -> None:
    """Hide / unhide a sheet from the workbook tabs."""
    r = ex.send_command(page, "tabdoc", "set-sheets-hidden", {
        "sheets": json.dumps([sheet]),
        "isHidden": "true" if hidden else "false",
        "includeStories": "false",
    })
    if not r.get("ok"):
        raise RuntimeError(f"set-sheets-hidden failed: {r.get('message','')[:300]}")
    flush_ui(page)


ANNOTATION_KINDS = {
    "point": "point", "mark": "mark", "area": "area",
}


def set_value_color(page, field_display_name: str, value_color_map: dict[str, str], *,
                   role: str = "dimension") -> dict:
    """Assign specific colors to specific categorical values (right-click Color →
    Edit Colors → click value → click palette swatch).

    Args:
      field_display_name: the field on Color (e.g. "Region", "Year of Order Date")
      value_color_map: dict mapping display values to colors
                       e.g. {"Central": "#ff0000", "East": "rgb(0,255,0)"}
      role: "dimension" (default) - measures use a continuous palette

    Returns ``{"componentId": ..., "applied": {...}, "missing": [...]}``.

    Colors accept hex ("#ff0000"), rgb ("rgb(255,0,0)"), or rgba.
    """
    if role == "measure":
        raise NotImplementedError("set_value_color is dimension-only; measures use continuous palettes")

    ds = _datasource_id(page)
    # Date hierarchies like "Year of Order Date" need a different fn; for now
    # accept either a fully qualified fn or assume nominal-key dimension.
    if field_display_name.startswith("["):
        fn = field_display_name
    else:
        fn = f"[sqlproxy.{ds}].[none:{field_display_name}:nk]"

    r = ex.send_command_raw(page, "tabdoc", "get-web-categorical-color-dialog", {
        "fieldVector": [fn],
    })
    if not r.get("ok"):
        raise RuntimeError(f"get-web-categorical-color-dialog failed: {r.get('error','')[:300]}")
    component_id = _find_in_tree(r.get("raw"), "componentId")
    if component_id is None:
        raise RuntimeError("could not extract componentId from color dialog response")

    # Extract the legend items: each is {text, itemValues, objectId, color}.
    # `objectId` is the index to pass to set-selected-legend-items.
    raw_repr = repr(r.get("raw"))
    # Match `'text': '<label>'` + nearby `'objectId': N`
    pairs = re.findall(
        r"'text':\s*'([^']+)'[^}]*?'objectId':\s*(\d+)",
        raw_repr,
    )
    label_to_idx = {label: int(idx) for label, idx in pairs}
    domain = [label for label, _ in pairs]

    applied = {}
    missing = []

    for value, color in value_color_map.items():
        if value not in label_to_idx:
            missing.append(value)
            continue
        idx = label_to_idx[value]
        # Select the value
        r1 = ex.send_command(page, "tabdoc", "set-selected-legend-items", {
            "componentId": component_id,
            "itemIndices": json.dumps([idx]),
        })
        if not r1.get("ok"):
            raise RuntimeError(f"set-selected-legend-items failed: {r1.get('message','')[:300]}")
        # Convert color to rgb(...) if hex
        color_wire = _normalize_color(color)
        r2 = ex.send_command(page, "tabdoc", "set-categorical-legend-item-color", {
            "componentId": component_id,
            "color": color_wire,
        })
        if not r2.get("ok"):
            raise RuntimeError(f"set-categorical-legend-item-color failed: {r2.get('message','')[:300]}")
        applied[value] = color_wire

    # Release the dialog component (commits)
    ex.send_command(page, "tabdoc", "release-component", {"componentId": component_id})
    flush_ui(page)
    close_open_dialogs(page)
    return {"componentId": component_id, "applied": applied, "missing": missing, "domain": domain}


def _normalize_color(color: str) -> str:
    """Normalize a color spec to Tableau's `rgb(r,g,b)` wire format.

    Accepts: '#rgb', '#rrggbb', '#rrggbbaa', 'rgb(r,g,b)', 'rgba(...)'.
    """
    s = color.strip()
    if s.startswith("rgb"):
        return s
    if s.startswith("#"):
        hex_part = s[1:]
        if len(hex_part) == 3:
            r = int(hex_part[0] * 2, 16); g = int(hex_part[1] * 2, 16); b = int(hex_part[2] * 2, 16)
            return f"rgb({r},{g},{b})"
        if len(hex_part) == 6:
            r = int(hex_part[0:2], 16); g = int(hex_part[2:4], 16); b = int(hex_part[4:6], 16)
            return f"rgb({r},{g},{b})"
        if len(hex_part) == 8:
            r = int(hex_part[0:2], 16); g = int(hex_part[2:4], 16); b = int(hex_part[4:6], 16); a = int(hex_part[6:8], 16) / 255
            return f"rgba({r},{g},{b},{a:.3f})"
    raise ValueError(f"unrecognized color spec: {color!r}")


def edit_tooltip(page, text: str = "", *, sheet: str | None = None) -> dict:
    """Set the tooltip text for the active worksheet's marks.

    Opens the Tooltip rich-text editor (Marks card → Tooltip button), types the
    given text via clipboard paste, and commits. To preserve existing
    formatting/field references, leave ``text`` empty and edit in Tableau.

    NOTE: Force-click on the Tooltip button is required (synthetic clicks
    don't open the editor). This primitive uses Playwright directly.
    """
    sheet = sheet or active_sheet_name(page)
    # 1. Open the editor via wire (launch-custom-tooltip-rich-text-editor)
    r = ex.send_command(page, "tabdoc", "launch-custom-tooltip-rich-text-editor", {
        "worksheet": sheet,
        "paneSpec": "0",
        "richTextEditorConfiguration": json.dumps({}),
    })
    if not r.get("ok"):
        raise RuntimeError(f"launch-custom-tooltip-rich-text-editor failed: {r.get('message','')[:300]}")
    flush_ui(page)

    # 2. Type the text via Playwright clipboard paste
    if text:
        time.sleep(0.4)
        page.evaluate(f"navigator.clipboard.writeText({json.dumps(text)})")
        # Click into the editor
        editor = page.locator('[contenteditable="true"]').first
        if editor.count():
            editor.click()
            page.keyboard.press("Meta+a")
            page.keyboard.press("Meta+v")
            time.sleep(0.2)

    # 3. Commit and close
    ex.send_command(page, "tabdoc", "close-rich-text-editor", {})
    flush_ui(page)
    close_open_dialogs(page)
    return {"sheet": sheet, "text": text}


def add_annotation(page, *, kind: str = "point", x: int = 200, y: int = 200,
                  text: str = "") -> dict:
    """Add an annotation to the viz (right-click viz → Annotate → Point/Mark/Area).

    Args:
      kind: "point" (default - at given coordinates), "mark" (anchored to selected
            mark - requires prior mark selection), "area" (anchored to a region)
      x, y: target coordinates (relative to viz origin) for Point annotations
      text: annotation text (plain). For rich-text formatting, edit interactively
            in Tableau.

    NOTE: text commit goes through a Rich Text Editor dialog. This wrapper
    creates the annotation and immediately closes the editor with the given text;
    formatting (bold, fonts, field inserts) needs interactive editing.
    """
    kind_wire = ANNOTATION_KINDS.get(kind.lower())
    if not kind_wire:
        raise ValueError(f"kind must be one of {list(ANNOTATION_KINDS)}")
    r = ex.send_command(page, "tabdoc", "create-annotation", {
        "annotateEnum": kind_wire,
        "selectionList": json.dumps([]),
        "targetPoint": json.dumps({"x": int(x), "y": int(y)}),
        "richTextEditorConfiguration": json.dumps({}),
    })
    if not r.get("ok"):
        raise RuntimeError(f"create-annotation failed: {r.get('message','')[:300]}")
    # Close the rich-text-editor dialog. Tableau accepts a text-content param;
    # if it doesn't take here, the annotation persists with placeholder text.
    ex.send_command(page, "tabdoc", "close-rich-text-editor", {
        "textContent": text,
    })
    flush_ui(page)
    close_open_dialogs(page)
    return {"kind": kind_wire, "x": x, "y": y, "text": text}


NUMBER_FORMAT_CODES = {
    "auto":       "system-locale",
    "number":     "system-number",
    "currency":   "system-currency",
    "percent":    "system-percent", "percentage": "system-percent",
    "scientific": "system-scientific",
}

UNIT_FORMATS = {
    "none":      "units-none",
    "thousands": "units-thousands", "k": "units-thousands",
    "millions":  "units-millions",  "m": "units-millions",
    "billions":  "units-billions",  "b": "units-billions",
}


def set_number_format(page, measure: str, *,
                     format: str = "currency",
                     aggregation: str = "sum",
                     decimal_places: int = 2,
                     units: str = "none",
                     show_separator: bool = True,
                     prefix: str = "",
                     suffix: str = "") -> None:
    """Set the number format of a measure pill (right-click pill → Format Number).

    Args:
      measure: e.g. "Sales", "Profit"
      format: "auto", "number", "currency", "percent", "scientific"
      aggregation: pill aggregation (must match the pill on the shelf)
      decimal_places: how many decimals
      units: "none" / "thousands" / "millions" / "billions"
      show_separator: thousand separators (comma)
      prefix, suffix: custom strings to wrap the value
    """
    code = NUMBER_FORMAT_CODES.get(format.lower())
    if not code:
        raise ValueError(f"format must be one of {list(NUMBER_FORMAT_CODES)}")
    units_wire = UNIT_FORMATS.get(units.lower())
    if not units_wire:
        raise ValueError(f"units must be one of {list(UNIT_FORMATS)}")

    ds = _datasource_id(page)
    agg = AGGREGATIONS.get(aggregation.lower(), aggregation.lower())
    fn = f"[sqlproxy.{ds}].[{agg}:{measure}:qk]"
    style_contexts = [
        {"style-element": "elementLabel", "style-scope": "fsNone",
         "field-vector": [fn], "element-instance-id": "", "field-type": "quantitative"},
        {"style-element": "elementCell", "style-scope": "fsNone",
         "field-vector": [fn], "element-instance-id": "", "field-type": "quantitative"},
    ]
    opts = {
        "lcid": 1033,
        "decimalMark": ".",
        "displayFormatPrefix": prefix,
        "displayFormatSuffix": suffix,
        "separatorCharacters": ",",
        "decimalPlaces": int(decimal_places),
        "showSeparator": bool(show_separator),
        "formatString": "",
        "unitsFormat": units_wire,
        "formatCode": code,
        "negativeFormat": "automatic",
    }
    r = ex.send_command(page, "tabdoc", "set-number-format-sheet-style", {
        "styleAttribute": "saTextFormat",
        "styleContexts": json.dumps(style_contexts),
        "numberFormattingOptions": json.dumps(opts),
    })
    if not r.get("ok"):
        raise RuntimeError(f"set-number-format-sheet-style failed: {r.get('message','')[:300]}")
    flush_ui(page)


AXIS_EXTENT_TYPES = {
    "auto":        "axis-extent-automatic", "automatic": "axis-extent-automatic",
    "uniform":     "axis-extent-uniform",
    "independent": "axis-extent-independent",
    "fixed":       "axis-extent-fixed",
}


def set_axis_title(page, measure: str, title: str, *, aggregation: str = "sum",
                  orientation: str = "vertical", duplicate_index: int = 0,
                  sheet: str | None = None) -> None:
    """Set the axis title for a measure (right-click axis → Edit Axis → Title)."""
    sheet = sheet or active_sheet_name(page)
    ds = _datasource_id(page)
    agg = AGGREGATIONS.get(aggregation.lower(), aggregation.lower())
    orient_wire = REF_LINE_ORIENTATIONS.get(orientation.lower(), orientation)
    fn = f"[sqlproxy.{ds}].[{agg}:{measure}:qk]"
    r = ex.send_command(page, "tabdoc", "set-axis-title", {
        "fn": fn,
        "axisOrientation": orient_wire,
        "duplicateIndex": str(duplicate_index),
        "visualIdPresModel": json.dumps({"worksheet": sheet}),
        "axisTitle": title,
    })
    if not r.get("ok"):
        raise RuntimeError(f"set-axis-title failed: {r.get('message','')[:300]}")
    flush_ui(page)
    close_open_dialogs(page)


def set_axis_extent_type(page, measure: str, *, extent_type: str = "auto",
                        aggregation: str = "sum",
                        orientation: str = "vertical",
                        duplicate_index: int = 0,
                        sheet: str | None = None) -> None:
    """Switch axis range mode (Edit Axis → Range radio buttons).

    extent_type: "auto" / "uniform" / "independent" / "fixed"
    """
    et = AXIS_EXTENT_TYPES.get(extent_type.lower())
    if not et:
        raise ValueError(f"extent_type must be one of {list(AXIS_EXTENT_TYPES)}")
    sheet = sheet or active_sheet_name(page)
    ds = _datasource_id(page)
    agg = AGGREGATIONS.get(aggregation.lower(), aggregation.lower())
    orient_wire = REF_LINE_ORIENTATIONS.get(orientation.lower(), orientation)
    fn = f"[sqlproxy.{ds}].[{agg}:{measure}:qk]"
    r = ex.send_command(page, "tabdoc", "set-both-axis-extents-type", {
        "fn": fn,
        "axisOrientation": orient_wire,
        "duplicateIndex": str(duplicate_index),
        "visualIdPresModel": json.dumps({"worksheet": sheet}),
        "axisExtentsType": et,
    })
    if not r.get("ok"):
        raise RuntimeError(f"set-both-axis-extents-type failed: {r.get('message','')[:300]}")
    flush_ui(page)
    close_open_dialogs(page)


def set_axis_range(page, measure: str, *,
                  min: float | None = None,
                  max: float | None = None,
                  aggregation: str = "sum",
                  orientation: str = "vertical",
                  duplicate_index: int = 0,
                  sheet: str | None = None) -> None:
    """Set the fixed min and/or max of an axis. Auto-switches to fixed mode.

    Args:
      measure: source measure (e.g. "Profit", "Sales")
      min: lower bound (None = leave as-is)
      max: upper bound (None = leave as-is)
      aggregation: pill aggregation
      orientation: "vertical" (Y, default) or "horizontal" (X)
      duplicate_index: 0 unless dual axis
      sheet: defaults to active

    Example:
      api.set_axis_range(page, "Profit", min=0, max=300000)
    """
    if min is None and max is None:
        raise ValueError("pass at least one of min= or max=")
    sheet = sheet or active_sheet_name(page)
    ds = _datasource_id(page)
    agg = AGGREGATIONS.get(aggregation.lower(), aggregation.lower())
    orient_wire = REF_LINE_ORIENTATIONS.get(orientation.lower(), orientation)
    fn = f"[sqlproxy.{ds}].[{agg}:{measure}:qk]"
    vis_id = json.dumps({"worksheet": sheet})

    # Switch to fixed mode (required before set-axis-range-* takes effect)
    ex.send_command(page, "tabdoc", "set-both-axis-extents-type", {
        "fn": fn, "axisOrientation": orient_wire, "duplicateIndex": str(duplicate_index),
        "visualIdPresModel": vis_id, "axisExtentsType": "axis-extent-fixed",
    })
    if min is not None:
        r = ex.send_command(page, "tabdoc", "set-axis-range-start", {
            "fn": fn, "axisOrientation": orient_wire, "duplicateIndex": str(duplicate_index),
            "visualIdPresModel": vis_id, "minValue": str(int(min) if float(min).is_integer() else min),
        })
        if not r.get("ok"):
            raise RuntimeError(f"set-axis-range-start failed: {r.get('message','')[:300]}")
    if max is not None:
        r = ex.send_command(page, "tabdoc", "set-axis-range-end", {
            "fn": fn, "axisOrientation": orient_wire, "duplicateIndex": str(duplicate_index),
            "visualIdPresModel": vis_id, "maxValue": str(int(max) if float(max).is_integer() else max),
        })
        if not r.get("ok"):
            raise RuntimeError(f"set-axis-range-end failed: {r.get('message','')[:300]}")
    flush_ui(page)
    # Server opens the Edit Axis dialog as a side effect - close it.
    close_open_dialogs(page)


def reset_axis_range(page, measure: str, *, aggregation: str = "sum",
                    orientation: str = "vertical", duplicate_index: int = 0) -> None:
    """Clear a manually-set axis range - right-click axis → Clear Axis Range."""
    ds = _datasource_id(page)
    agg = AGGREGATIONS.get(aggregation.lower(), aggregation.lower())
    orient_wire = REF_LINE_ORIENTATIONS.get(orientation.lower(), orientation)
    fn = f"[sqlproxy.{ds}].[{agg}:{measure}:qk]"
    r = ex.send_command(page, "tabdoc", "reset-axis-range", {
        "fn": fn,
        "axisOrientation": orient_wire,
        "duplicateIndex": str(duplicate_index),
    })
    if not r.get("ok"):
        raise RuntimeError(f"reset-axis-range failed: {r.get('message','')[:300]}")
    flush_ui(page)
    close_open_dialogs(page)


def toggle_dual_axis(page, *, shelf: str = "rows", pos: int = 1, sheet: str | None = None) -> dict:
    """Toggle dual-axis on the measure pill at ``pos`` on the given shelf.

    Equivalent to right-clicking the rightmost measure → Dual Axis. Calling
    again removes the dual axis. The pill at ``pos`` is moved to its own
    secondary axis on the perpendicular dimension.

    Args:
      shelf: "rows" (default) or "columns" - where the measures live
      pos: index of the measure to put on the secondary axis (usually 1 for
           the second measure)
      sheet: defaults to active
    """
    sheet = sheet or active_sheet_name(page)
    shelf_wire = SORT_SHELVES.get(shelf.lower(), shelf)
    r = ex.send_command(page, "tabdoc", "dual-axis", {
        "shelfSelectionModel": json.dumps({"shelf-type": shelf_wire, "shelf-pos-indices": [pos]}),
        "worksheet": sheet,
    })
    if not r.get("ok"):
        raise RuntimeError(f"dual-axis failed: {r.get('message','')[:300]}")
    flush_ui(page)
    return {"shelf": shelf_wire, "pos": pos, "sheet": sheet}


def toggle_column_totals(page) -> None:
    """Toggle 'Show Column Grand Totals' (Analysis menu → Totals)."""
    r = ex.send_command(page, "tabdoc", "show-col-totals", {})
    if not r.get("ok"):
        raise RuntimeError(f"show-col-totals failed: {r.get('message','')[:300]}")
    flush_ui(page)


def toggle_row_totals(page) -> None:
    """Toggle 'Show Row Grand Totals' (Analysis menu → Totals)."""
    r = ex.send_command(page, "tabdoc", "show-row-totals", {})
    if not r.get("ok"):
        raise RuntimeError(f"show-row-totals failed: {r.get('message','')[:300]}")
    flush_ui(page)


def toggle_subtotals(page, *, add: bool = True) -> None:
    """Add or remove subtotals (Analysis menu → Totals → Add/Remove All Subtotals)."""
    cmd = "add-subtotals" if add else "remove-subtotals"
    r = ex.send_command(page, "tabdoc", cmd, {})
    if not r.get("ok"):
        raise RuntimeError(f"{cmd} failed: {r.get('message','')[:300]}")
    flush_ui(page)


def swap_rows_and_columns(page, sheet: str | None = None) -> None:
    """Swap the Rows and Columns shelves (toolbar Swap Rows/Columns button)."""
    sheet = sheet or active_sheet_name(page)
    r = ex.send_command(page, "tabdoc", "swap-rows-and-columns", {
        "visualIdPresModel": json.dumps({"worksheet": sheet}),
    })
    if not r.get("ok"):
        raise RuntimeError(f"swap-rows-and-columns failed: {r.get('message','')[:300]}")
    flush_ui(page)


def rename_sheet(page, new_name: str, *, old_name: str | None = None) -> None:
    """Rename a worksheet. If old_name not given, the active sheet is renamed."""
    sheet = old_name or active_sheet_name(page)
    r = ex.send_command(page, "tabdoc", "rename-sheet", {
        "sheet": sheet,
        "newSheet": new_name,
    })
    if not r.get("ok"):
        raise RuntimeError(f"rename-sheet failed: {r.get('message', '')[:300]}")
    flush_ui(page)


# ---- Color palette --------------------------------------------------------


# Friendly palette names → Tableau wire palette IDs (categorical palettes).
COLOR_PALETTES = {
    "tableau 10": "tableau_10_0",
    "tableau 20": "tableau_20_0",
    "color blind": "color_blind_10_0",
    "seattle grays": "seattle_grays_10_0",
    "traffic light": "traffic_light_9_0",
    "superfishel stone": "superfishel_stone_10_0",
    "miller stone": "miller_stone_11_0",
    "nuriel stone": "nuriel_stone_10_0",
    "classic 10": "tableau_classic_10",
    "classic 20": "tableau_classic_20",
    "summer": "summer_8_0",
    "winter": "winter_10_0",
    "green orange teal": "green_orange_teal_12_0",
    "blue red brown": "blue_red_brown_12_0",
    "purple pink gray": "purple_pink_gray_12_0",
}


def set_color_palette(page, palette: str, field_display_name: str,
                      *, role: str = "dimension") -> None:
    """Change the categorical color palette for a specific field.

    Args:
      palette: Friendly name (e.g. "Color Blind") or raw wire id.
      field_display_name: The field currently encoded on Color (required to
                          identify which legend's palette to swap).
      role: "dimension" or "measure" - controls the field encoding key (nk/qk).
    """
    wire_palette = COLOR_PALETTES.get(palette.lower(), palette)
    ds = _datasource_id(page)
    if role == "measure":
        fn = f"[sqlproxy.{ds}].[sum:{field_display_name}:qk]"
    else:
        # Bins/groups need the 'ok' key; default to nk for plain dimensions.
        fn = f"[sqlproxy.{ds}].[none:{field_display_name}:nk]"

    # 1. Open color dialog server-side - returns componentId.
    r1 = ex.send_command_raw(page, "tabdoc", "get-web-categorical-color-dialog", {
        "fieldVector": [fn],
    })
    if not r1.get("ok"):
        raise RuntimeError(f"get-web-categorical-color-dialog failed: {r1.get('error', '')[:200]}")
    import re
    m = re.search(r"['\"]componentId['\"]:\s*(\d+)", repr(r1.get("raw")))
    if not m:
        raise RuntimeError("could not extract componentId from color dialog response")
    component_id = int(m.group(1))

    # 2. Apply palette
    r2 = ex.send_command(page, "tabdoc", "assign-categorical-color-palette", {
        "componentId": component_id,
        "colorPaletteId": wire_palette,
        "applyColors": True,
    })
    if not r2.get("ok"):
        raise RuntimeError(f"assign-categorical-color-palette failed: {r2.get('message', '')[:300]}")

    # 3. Release the dialog component
    ex.send_command(page, "tabdoc", "release-component", {"componentId": component_id})
    flush_ui(page)
    close_open_dialogs(page)


# ---- Filters --------------------------------------------------------------


# ---- Parameters -----------------------------------------------------------


# Friendly name → wire dataType for parameter-edit-data-type
PARAM_TYPES = {
    "int": "integer", "integer": "integer",
    "float": "real", "real": "real", "double": "real",
    "string": "string", "str": "string", "text": "string",
    "bool": "bool", "boolean": "bool",
    "date": "date",
    "datetime": "datetime",
    "spatial": "spatial",
}


def create_parameter(page, name: str, *,
                     data_type: str = "integer",
                     current_value: str | int | float | bool = "",
                     allowable: str = "all") -> None:
    """Create a workbook parameter - fully wire-driven, no dialog.

    Sequence: create-new-parameter → parameter-edit-data-type →
    parameter-edit-name → parameter-edit-value → parameter-close-dialog.

    Args:
      name: Parameter name (e.g. "Top N")
      data_type: One of {int, float, string, bool, date, datetime, spatial}
                 (also accepts Tableau wire names: integer/real/string/bool/...).
      current_value: Initial value. Will be String()'d for the wire.
      allowable: "all" (default), "list", or "range" - only "all" supported here
                 for now. List/range require additional commands per type.
    """
    wire_dt = PARAM_TYPES.get(data_type.lower(), data_type.lower())

    # 1. Create - use RAW so we get controllerId from presentationLayerNotification.
    r1 = ex.send_command_raw(page, "tabdoc", "create-new-parameter", {
        "fn": f"[Parameters].[{name}]",  # name ignored by server; we rename below
    })
    if not r1.get("ok"):
        raise RuntimeError(f"create-new-parameter failed: {r1.get('error', '')[:300]}")

    # 2. Extract controllerId from the raw response.
    import re
    blob = repr(r1.get("raw"))
    m = re.search(r"['\"]controllerId['\"]:\s*(\d+)", blob)
    if not m:
        raise RuntimeError("could not parse controllerId from create-new-parameter raw response")
    controller_id = int(m.group(1))

    # 3. Set data type
    r2 = ex.send_command(page, "tabdoc", "parameter-edit-data-type", {
        "controllerId": controller_id,
        "dataType": wire_dt,
    })
    if not r2.get("ok"):
        raise RuntimeError(f"parameter-edit-data-type failed: {r2.get('message', '')[:300]}")

    # 4. Rename
    r3 = ex.send_command(page, "tabdoc", "parameter-edit-name", {
        "controllerId": controller_id,
        "actualParameterDisplayName": name,
    })
    if not r3.get("ok"):
        raise RuntimeError(f"parameter-edit-name failed: {r3.get('message', '')[:300]}")

    # 5. Set current value (only if provided non-empty)
    if current_value != "":
        r4 = ex.send_command(page, "tabdoc", "parameter-edit-value", {
            "controllerId": controller_id,
            "valueString": str(current_value),
        })
        if not r4.get("ok"):
            raise RuntimeError(f"parameter-edit-value failed: {r4.get('message', '')[:300]}")

    # 6. Commit and close. The wire command commits values server-side but
    # doesn't dismiss the SPA's dialog UI - needs an explicit DOM close.
    r5 = ex.send_command(page, "tabdoc", "parameter-close-dialog", {
        "controllerId": controller_id,
        "commitEdits": True,
    })
    if not r5.get("ok"):
        raise RuntimeError(f"parameter-close-dialog failed: {r5.get('message', '')[:300]}")
    flush_ui(page)
    close_open_dialogs(page)


def resolve_parameter_fn(page, caption: str) -> str:
    """Resolve a parameter's display caption to its internal
    ``[Parameters].[Parameter N]`` fn (which is what wires that reference
    parameters in formulas - e.g. Top N's ``limitCountExpression`` - actually
    need). The caption form works for some convenience commands but not for
    embedded formula expressions.

    Falls back to ``[Parameters].[{caption}]`` if the caption is already the
    internal name (e.g. ``Parameter 3``) or can't be found in the schema.
    """
    if caption.startswith("Parameter "):
        return f"[Parameters].[{caption}]"
    r = ex.send_command_raw(page, "tabdoc", "get-schema", {})
    s = repr(r.get("raw", ""))
    # Find blocks where a [Parameters].[X] is paired with this caption.
    import re
    pattern = (
        r"\[Parameters\]\.\[(Parameter \d+)\][^{}]{0,400}?"
        r"['\"]fieldCaption['\"]:\s*['\"]" + re.escape(caption) + r"['\"]"
    )
    m = re.search(pattern, s)
    if m:
        return f"[Parameters].[{m.group(1)}]"
    return f"[Parameters].[{caption}]"


def show_parameter_control(page, parameter_name: str) -> None:
    """Show the parameter control widget on the viz.

    Equivalent to right-click parameter → "Show Parameter". Wire command:
    `show-parameter-controls`.
    """
    r = ex.send_command(page, "tabdoc", "show-parameter-controls", {
        "fieldVector": [f"[Parameters].[{parameter_name}]"],
    })
    if not r.get("ok"):
        raise RuntimeError(f"show-parameter-controls failed: {r.get('message', '')[:300]}")
    flush_ui(page)


def set_parameter_value(page, parameter_name: str, value: str | int | float | bool) -> None:
    """Update a parameter's current value.

    Uses tab.ParameterServerCommands.setParameterValue internally - but we go
    via wire by calling the underlying command directly.
    """
    ds = _datasource_id(page)
    # Parameter references use [Parameters].[Name] shape, not the sqlproxy form
    r = ex.send_command(page, "tabdoc", "set-parameter-value", {
        "fn": f"[Parameters].[{parameter_name}]",
        "valueString": str(value),
    })
    if not r.get("ok"):
        raise RuntimeError(f"set-parameter-value failed: {r.get('message', '')[:300]}")
    flush_ui(page)


def show_filter_card(page, field_display_name: str, *, role: str = "dimension") -> None:
    """Show the filter control widget on the viz (the interactive filter card).

    Equivalent to right-click the filter pill → "Show Filter". Wire command:
    `show-quickfilter-doc`. The field must already be on the Filters shelf.
    """
    ds = _datasource_id(page)
    if role == "measure":
        fn = f"[sqlproxy.{ds}].[sum:{field_display_name}:qk]"
    else:
        fn = f"[sqlproxy.{ds}].[none:{field_display_name}:nk]"
    r = ex.send_command(page, "tabdoc", "show-quickfilter-doc", {
        "globalFieldName": fn,
        "membershipTarget": "filter",
    })
    if not r.get("ok"):
        raise RuntimeError(f"show-quickfilter-doc failed: {r.get('message', '')[:300]}")
    flush_ui(page)


def add_filter(page, field_display_name: str, *,
               role: str = "dimension",
               aggregation: str | None = None) -> None:
    """Add a default "show all" filter on a field - bypasses the picker dialog.

    Uses `create-default-quick-filter` (the same command the SPA enqueues after
    a successful drop-on-shelf when shelfType=filter-shelf). Default behavior
    is "include all values" - caller can later constrain via update_filter.

    Args:
      field_display_name: e.g. "Segment", "Sales"
      role: "dimension" or "measure" - determines the encoding role
      aggregation: optional for measures; e.g. "sum" → uses [sum:Sales:qk].
                   Default for measures is "sum"; ignored for dimensions.
    """
    if role == "measure":
        agg = AGGREGATIONS.get((aggregation or "sum").lower(), "sum")
        fn = _meas_fn(page, field_display_name, agg)
    else:
        fn = _dim_fn(page, field_display_name)
    r = ex.send_command(page, "tabdoc", "create-default-quick-filter", {
        "fn": fn,
        "membershipTarget": "filter",
    })
    if not r.get("ok"):
        raise RuntimeError(f"create-default-quick-filter failed: {r.get('message', '')[:300]}")
    flush_ui(page)


def _find_in_tree(obj: Any, key: str) -> Any:
    """Depth-first search for ``key`` in a nested dict/list tree. Returns the
    first matching value, or None. Used to dig out server-assigned cache
    identifiers buried inside vqlCmdResponse / presentationLayerNotification."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_in_tree(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_in_tree(item, key)
            if found is not None:
                return found
    return None


def edit_filter_values(page, field_display_name: str, *,
                       include: list[str] | None = None,
                       exclude: list[str] | None = None,
                       role: str = "dimension",
                       sheet: str | None = None) -> dict:
    """Constrain a categorical filter to specific values.

    The field must already be on the Filters shelf (call ``add_filter`` first).
    Pass exactly one of ``include=`` (keep only these) or ``exclude=`` (keep
    everything but these).

    Wire flow mirrors the SPA's Edit Filter dialog:
      edit-filter-dialog (raw - extract server-assigned cacheInfo) →
      categorical-filter-init-with-domain → get-categorical-filter-domain-page →
      categorical-filter-select-* (reset) →
      categorical-filter-deselect-* (per excluded member) →
      close-categorical-filter-dialog.

    Returns ``{"kept": [...], "excluded": [...]}``.

    NOTE: changes filter shelf state; an existing quick-filter card on the
    canvas does NOT auto-resync. Call ``show_filter_card`` after to refresh.
    """
    if (include is None) == (exclude is None):
        raise ValueError("pass exactly one of include= or exclude=")

    sheet = sheet or active_sheet_name(page)

    if role == "measure":
        # Measures use a continuous range filter, not categorical - different command path.
        raise NotImplementedError("edit_filter_values is categorical only; use configure_range_filter for measures")
    fn = _dim_fn(page, field_display_name)

    # 1. Open the dialog server-side and harvest the cacheInfo it generates.
    r = ex.send_command_raw(page, "tabdoc", "edit-filter-dialog", {"globalFieldName": fn})
    if not r.get("ok"):
        raise RuntimeError(f"edit-filter-dialog failed: {r.get('error','')[:300]}")
    cache_info = _find_in_tree(r.get("raw"), "categoricalFilterCacheInfo")
    if not cache_info:
        raise RuntimeError(f"edit-filter-dialog returned no categoricalFilterCacheInfo for {field_display_name!r}")
    page_cache = cache_info["relationalPageCacheId"]
    cache_info_s = json.dumps(cache_info)
    page_cache_s = json.dumps(page_cache)

    # 2. Initialize cache with our generated UUIDs.
    r = ex.send_command(page, "tabdoc", "categorical-filter-init-with-domain", {
        "categoricalFilterCacheInfo": cache_info_s,
    })
    if not r.get("ok"):
        raise RuntimeError(f"categorical-filter-init-with-domain failed: {r.get('message','')[:300]}")

    # 3. Fetch the domain page to learn the label→index mapping.
    r = ex.send_command_raw(page, "tabdoc", "get-categorical-filter-domain-page", {
        "rowIndex": "0",
        "categoricalFilterCacheInfo": cache_info_s,
        "relationalPageCacheId": page_cache_s,
    })
    if not r.get("ok"):
        raise RuntimeError(f"get-categorical-filter-domain-page failed: {r.get('error','')[:300]}")
    domain_page = _find_in_tree(r.get("raw"), "categoricalFilterMemberDomainPage")
    if not domain_page:
        # controllerMissing or similar - surface what the server returned.
        cmd_return = _find_in_tree(r.get("raw"), "commandReturn") or {}
        raise RuntimeError(f"get-categorical-filter-domain-page returned {cmd_return!r}")
    domain = [m.get("label") for m in (domain_page.get("domainMembers") or [])]
    if not domain:
        raise RuntimeError(f"empty domain for {field_display_name!r}")
    label_to_index = {label: i for i, label in enumerate(domain)}

    # 4. Compute target set.
    if include is not None:
        wanted = set(include)
        missing = [v for v in include if v not in label_to_index]
        if missing:
            raise ValueError(f"include values not in domain {domain}: {missing}")
        to_keep = [label_to_index[v] for v in include]
        to_deselect = [label_to_index[label] for label in domain if label not in wanted]
    else:
        missing = [v for v in exclude if v not in label_to_index]
        if missing:
            raise ValueError(f"exclude values not in domain {domain}: {missing}")
        excluded = set(exclude)
        to_deselect = [label_to_index[v] for v in exclude]
        to_keep = [label_to_index[label] for label in domain if label not in excluded]

    # 5. Reset to "all selected" via select-deferred on every index.
    if domain:
        all_indices = list(range(len(domain)))
        r = ex.send_command(page, "tabdoc", "categorical-filter-select-relational-members-deferred", {
            "categoricalFilterCacheInfo": cache_info_s,
            "filterIndices": json.dumps(all_indices),
            "relationalPageCacheId": page_cache_s,
        })
        if not r.get("ok"):
            raise RuntimeError(f"select-relational-members failed: {r.get('message','')[:300]}")

    # 6. Deselect the unwanted indices.
    if to_deselect:
        r = ex.send_command(page, "tabdoc", "categorical-filter-deselect-relational-members-deferred", {
            "categoricalFilterCacheInfo": cache_info_s,
            "filterIndices": json.dumps(to_deselect),
            "relationalPageCacheId": page_cache_s,
        })
        if not r.get("ok"):
            raise RuntimeError(f"deselect-relational-members failed: {r.get('message','')[:300]}")

    # 7. Commit. Update payload is empty because the actual selection state lives
    #    in the categorical-filter cache.
    r = ex.send_command(page, "tabdoc", "close-categorical-filter-dialog", {
        "categoricalFilterCacheInfo": cache_info_s,
        "categoricalFilterUpdate": json.dumps({"filterName": ""}),
    })
    if not r.get("ok"):
        raise RuntimeError(f"close-categorical-filter-dialog failed: {r.get('message','')[:300]}")

    flush_ui(page)
    close_open_dialogs(page)
    return {
        "kept": [label for label in domain if label_to_index[label] in to_keep],
        "excluded": [label for label in domain if label_to_index[label] in to_deselect],
    }


def configure_top_n_filter(page, field_display_name: str, *,
                           n: int | None,
                           by: str | None = None,
                           aggregation: str = "sum",
                           end: str = "top",
                           count_parameter: str | None = None) -> dict:
    """Set or clear a Top N (or Bottom N) limit on a categorical filter.

    The field must already be on the Filters shelf (call ``add_filter`` first).
    Rides on the same dialog protocol as ``edit_filter_values`` - it's a single
    ``close-categorical-filter-dialog`` with a populated
    ``categoricalFilterLimitUpdate`` block.

    Args:
      field_display_name: dimension to limit (e.g. "Region", "Sub-Category")
      n: how many to keep - pass ``None`` to clear an existing Top N limit.
         Ignored when ``count_parameter`` is set.
      by: measure to rank by (required when n or count_parameter is set)
      aggregation: sum/avg/min/max/count/median/stdev/variance (default "sum")
      end: "top" or "bottom"
      count_parameter: name of a parameter to drive the count (e.g. "Top N
         Customers"). When set, the Top N limit becomes parameter-driven so
         end users can change N from the parameter control. ``n`` is ignored.

    Returns ``{"n": n, "by": by, "aggregation": aggregation, "end": end,
              "count_parameter": count_parameter}``.
    """
    if end not in ("top", "bottom"):
        raise ValueError(f"end must be 'top' or 'bottom', got {end!r}")
    has_limit = count_parameter is not None or n is not None
    if n is not None and n < 1:
        raise ValueError(f"n must be >= 1 or None, got {n}")
    if has_limit and not by:
        raise ValueError("by= is required when n or count_parameter is set")

    agg = AGGREGATIONS.get(aggregation.lower(), aggregation.lower())
    cache_info = _open_categorical_dialog(page, field_display_name)
    update = _default_categorical_update()
    if count_parameter is not None:
        # Tableau stores parameters by internal name (Parameter N); the user
        # gives us a caption. Resolve so the formula references the right one.
        count_expr = resolve_parameter_fn(page, count_parameter)
        summary_n = f"<{count_parameter}>"
    elif n is not None:
        count_expr = str(n)
        summary_n = str(n)
    else:
        count_expr = "10"
        summary_n = None
    update["categoricalFilterLimitUpdate"] = {
        "aggregation": agg,
        "columnName": f"[{by}]" if by else f"[{field_display_name}]",
        "filterLimitType": "by-field" if has_limit else "none",
        "formula": "",
        "limitCountExpression": count_expr,
        "percentileParam": 0,
        "sortEnd": end,
    }
    update["categoricalRelationalUpdatedState"]["limitSummary"] = (
        f"{end.title()} {summary_n} by {by}" if has_limit else "None"
    )

    r = ex.send_command(page, "tabdoc", "close-categorical-filter-dialog", {
        "categoricalFilterCacheInfo": json.dumps(cache_info),
        "categoricalFilterUpdate": json.dumps(update),
    })
    if not r.get("ok"):
        raise RuntimeError(f"close-categorical-filter-dialog failed: {r.get('message','')[:300]}")
    flush_ui(page)
    close_open_dialogs(page)
    return {"n": n, "by": by, "aggregation": aggregation, "end": end,
            "count_parameter": count_parameter}


WILDCARD_TYPES = {
    "contains": "contains",
    "starts-with": "starts-with", "startswith": "starts-with", "starts_with": "starts-with",
    "ends-with": "ends-with", "endswith": "ends-with", "ends_with": "ends-with",
    "exactly": "exactly",
}

CONDITION_OPS = {
    ">":  "op-greater", "gt":  "op-greater",
    ">=": "op-gequal",  "gte": "op-gequal",
    "<":  "op-less",    "lt":  "op-less",
    "<=": "op-lequal",  "lte": "op-lequal",
    "=":  "op-equals",  "==":  "op-equals", "eq": "op-equals",
    "!=": "op-not-equals", "<>": "op-not-equals", "ne": "op-not-equals",
}


def _open_categorical_dialog(page, field_display_name: str) -> dict:
    """Internal: open the categorical filter dialog and return its cacheInfo.
    Used by configure_top_n_filter / configure_wildcard_filter /
    configure_condition_filter."""
    ds = _datasource_id(page)
    fn = f"[sqlproxy.{ds}].[none:{field_display_name}:nk]"
    r = ex.send_command_raw(page, "tabdoc", "edit-filter-dialog", {"globalFieldName": fn})
    if not r.get("ok"):
        raise RuntimeError(f"edit-filter-dialog failed: {r.get('error','')[:300]}")
    cache_info = _find_in_tree(r.get("raw"), "categoricalFilterCacheInfo")
    if not cache_info:
        raise RuntimeError(f"edit-filter-dialog returned no categoricalFilterCacheInfo for {field_display_name!r}")
    r = ex.send_command(page, "tabdoc", "categorical-filter-init-with-domain", {
        "categoricalFilterCacheInfo": json.dumps(cache_info),
    })
    if not r.get("ok"):
        raise RuntimeError(f"categorical-filter-init-with-domain failed: {r.get('message','')[:300]}")
    return cache_info


def _default_categorical_update() -> dict:
    """Baseline payload - all four tabs set to neutral defaults. Caller mutates
    the one tab they're configuring before sending close-categorical-filter-dialog."""
    return {
        "filterName": "",
        "categoricalFilterConditionUpdate": {
            "aggregation": "count",
            "filterConditionType": "none",
            "fn": "",
            "percentileParam": 0,
            "expressionOp": "op-equals",
            "dataValue": "i:0",
            "condition": "",
        },
        "categoricalFilterLimitUpdate": {
            "aggregation": "count",
            "columnName": "",
            "filterLimitType": "none",
            "formula": "",
            "limitCountExpression": "10",
            "percentileParam": 0,
            "sortEnd": "top",
        },
        "categoricalFilterPatternUpdate": {
            "filterPatternType": "contains",
            "isPatternExclusive": False,
            "patternFilterString": "",
            "useAllWhenPatternEmpty": True,
        },
        "categoricalRelationalUpdatedState": {
            "filterSelectionTracking": "dont-track-selection-state",
            "updatedTuples": [],
            "filterDomainType": "cascading",
            "isSelectionExclusive": False,
            "filtersPresetType": "none",
            "filtersRangeType": "selected",
            "useAllWhenManualEmpty": True,
            "selectionSummary": "All",
            "conditionSummary": "None",
            "limitSummary": "None",
            "patternSummary": "All",
        },
    }


def configure_wildcard_filter(page, field_display_name: str, *,
                              pattern: str | None,
                              match: str = "contains",
                              exclude: bool = False) -> dict:
    """Apply (or clear) a wildcard text-pattern filter on a categorical field.

    Args:
      field_display_name: dimension to filter (must be on Filters shelf)
      pattern: text to match. Pass ``None`` or ``""`` to clear an existing
               wildcard filter.
      match: one of {"contains", "starts-with", "ends-with", "exactly"}
      exclude: False = keep matches (default), True = keep non-matches

    Examples:
      configure_wildcard_filter(page, "Sub-Category", pattern="Phones")
      configure_wildcard_filter(page, "Region", pattern="E", match="starts-with")
      configure_wildcard_filter(page, "Region", pattern=None)   # clear

    Wire: same close-categorical-filter-dialog as edit_filter_values; populates
    the ``categoricalFilterPatternUpdate`` block.
    """
    pattern_kind = WILDCARD_TYPES.get(match.lower().replace("_", "-"))
    if not pattern_kind:
        raise ValueError(f"match must be one of {list(WILDCARD_TYPES)}, got {match!r}")

    cache_info = _open_categorical_dialog(page, field_display_name)
    update = _default_categorical_update()
    update["categoricalFilterPatternUpdate"] = {
        "filterPatternType": pattern_kind,
        "isPatternExclusive": bool(exclude),
        "patternFilterString": pattern or "",
        "useAllWhenPatternEmpty": True,
    }
    update["categoricalRelationalUpdatedState"]["patternSummary"] = (
        f"{'Not ' if exclude else ''}{pattern_kind} '{pattern}'" if pattern else "All"
    )

    r = ex.send_command(page, "tabdoc", "close-categorical-filter-dialog", {
        "categoricalFilterCacheInfo": json.dumps(cache_info),
        "categoricalFilterUpdate": json.dumps(update),
    })
    if not r.get("ok"):
        raise RuntimeError(f"close-categorical-filter-dialog failed: {r.get('message','')[:300]}")
    flush_ui(page)
    close_open_dialogs(page)
    return {"pattern": pattern, "match": pattern_kind, "exclude": exclude}


def configure_condition_filter(page, field_display_name: str, *,
                               by: str | None,
                               op: str = ">=",
                               value: float | int = 0,
                               aggregation: str = "sum") -> dict:
    """Apply (or clear) a condition filter on a categorical field - restrict to
    values whose aggregated measure satisfies a comparison.

    Args:
      field_display_name: dimension to filter (must be on Filters shelf)
      by: measure name (e.g. "Sales") or ``None`` to clear an existing condition
      op: one of {">", ">=", "<", "<=", "=", "!=", or wire names like
          "op-greater-than"}
      value: numeric threshold
      aggregation: sum/avg/min/max/count/median/stdev/variance (default "sum")

    Example:
      configure_condition_filter(page, "Sub-Category", by="Sales", op=">=", value=200000)
      configure_condition_filter(page, "Region", by=None)   # clear

    Wire: same close-categorical-filter-dialog as edit_filter_values; populates
    the ``categoricalFilterConditionUpdate`` block.
    """
    ds = _datasource_id(page)
    agg = AGGREGATIONS.get(aggregation.lower(), aggregation.lower())
    wire_op = CONDITION_OPS.get(op.lower(), op)
    if by is None:
        condition_type = "none"
    else:
        condition_type = "by-field"

    cache_info = _open_categorical_dialog(page, field_display_name)
    update = _default_categorical_update()
    update["categoricalFilterConditionUpdate"] = {
        "aggregation": agg,
        "filterConditionType": condition_type,
        "fn": f"[sqlproxy.{ds}].[{by}]" if by else "",
        "percentileParam": 0,
        "expressionOp": wire_op,
        # Wire encoding: "r:6:0:<integer-value>" for reals (from validate-data-format format).
        # Strip trailing ".0" - Tableau expects "r:6:0:400000" not "r:6:0:400000.0".
        "dataValue": f"r:6:0:{int(value) if float(value).is_integer() else value}" if by else "i:0",
        "condition": "",
    }
    update["categoricalRelationalUpdatedState"]["conditionSummary"] = (
        f"{agg.upper()}({by}) {op} {value}" if by else "None"
    )

    r = ex.send_command(page, "tabdoc", "close-categorical-filter-dialog", {
        "categoricalFilterCacheInfo": json.dumps(cache_info),
        "categoricalFilterUpdate": json.dumps(update),
    })
    if not r.get("ok"):
        raise RuntimeError(f"close-categorical-filter-dialog failed: {r.get('message','')[:300]}")
    flush_ui(page)
    close_open_dialogs(page)
    return {"by": by, "op": op, "value": value, "aggregation": aggregation}


REF_LINE_SCOPES = {
    "per-pane": "per-pane", "pane": "per-pane",
    "per-cell": "per-cell", "cell": "per-cell",
    "entire-table": "entire-table", "table": "entire-table",
}

REF_LINE_ORIENTATIONS = {
    "vertical": "o-vert", "v": "o-vert", "y": "o-vert", "rows": "o-vert",
    "horizontal": "o-horiz", "h": "o-horiz", "x": "o-horiz", "columns": "o-horiz",
}


ANALYTICS_KINDS = {
    "reference-line": "custom-reference-line", "ref-line": "custom-reference-line", "line": "custom-reference-line",
    "trend-line":    "trend-line", "trend": "trend-line",
    "reference-band": "reference-band", "band": "reference-band",
    "distribution-band": "distribution-band", "distribution": "distribution-band",
    "box-plot": "box-plot",
    "average-line": "average-line", "avg-line": "average-line",
    "median-line": "median-with-quartiles",
    "constant-line": "constant-line",
}


def add_analytics_object(page, *, kind: str = "reference-line",
                         measure: str = "Sales",
                         aggregation: str = "sum",
                         scope: str = "per-pane",
                         orientation: str = "vertical",
                         sheet: str | None = None) -> dict:
    """Add an analytics-pane object to a chart axis (reference line, trend line,
    reference band, distribution band, box plot, etc.).

    Drives the wire path that right-click-axis → Add Reference Line fires.
    Trend lines and bands all flow through the same `add-reference-line` wire
    command with different `analyticsObjectType` values.

    Args:
      kind: one of "reference-line", "trend-line", "reference-band",
            "distribution-band", "box-plot", "average-line", "median-line",
            "constant-line"
      measure: source measure (e.g. "Sales", "Profit")
      aggregation: how the measure is aggregated (matches chart encoding)
      scope: "per-pane" (default), "per-cell", or "entire-table"
      orientation: "vertical" (Y-axis, default) or "horizontal" (X-axis)
      sheet: defaults to active sheet

    NOTE: trend lines only render meaningfully on continuous numeric/date axes;
    on discrete dimensions the wire succeeds but nothing visible appears.
    Detailed configuration (trend model type, CI level, band fill) requires
    additional dialog interactions not yet wrapped.
    """
    obj_type = ANALYTICS_KINDS.get(kind.lower())
    if not obj_type:
        raise ValueError(f"kind must be one of {list(ANALYTICS_KINDS)}, got {kind!r}")
    scope_wire = REF_LINE_SCOPES.get(scope.lower())
    if not scope_wire:
        raise ValueError(f"scope must be one of {list(REF_LINE_SCOPES)}")
    orient_wire = REF_LINE_ORIENTATIONS.get(orientation.lower())
    if not orient_wire:
        raise ValueError(f"orientation must be one of {list(REF_LINE_ORIENTATIONS)}")

    sheet = sheet or active_sheet_name(page)
    ds = _datasource_id(page)
    agg = AGGREGATIONS.get(aggregation.lower(), aggregation.lower())
    fn = f"[sqlproxy.{ds}].[{agg}:{measure}:qk]"

    r = ex.send_command(page, "tabdoc", "add-reference-line", {
        "analyticsObjectType": obj_type,
        "axisOrientation": orient_wire,
        "duplicateIndex": "0",
        "fieldVector": json.dumps([fn]),
        "fn": fn,
        "referenceLineScopeType": scope_wire,
    })
    if not r.get("ok"):
        raise RuntimeError(f"add-reference-line failed: {r.get('message','')[:300]}")

    # Commit the editor with defaults (some analytics types open one, some don't).
    ex.send_command(page, "tabdoc", "close-ref-line-editor", {
        "visualIdPresModel": json.dumps({"worksheet": sheet}),
    })
    flush_ui(page)
    close_open_dialogs(page)
    return {"kind": obj_type, "measure": measure, "aggregation": agg,
            "scope": scope_wire, "orientation": orient_wire}


def add_reference_line(page, measure: str = "Sales", **kwargs) -> dict:
    """Add a reference line - sugar for ``add_analytics_object(kind="reference-line")``."""
    return add_analytics_object(page, kind="reference-line", measure=measure, **kwargs)


def add_trend_line(page, measure: str = "Sales", **kwargs) -> dict:
    """Add a trend line - sugar for ``add_analytics_object(kind="trend-line")``.
    Only renders on continuous numeric/date axes."""
    return add_analytics_object(page, kind="trend-line", measure=measure, **kwargs)


SORT_DIRECTIONS = {
    "asc": "asc", "ascending": "asc", "up": "asc",
    "desc": "desc", "descending": "desc", "down": "desc",
    "none": "none", "clear": "none", "off": "none",
}

SORT_BY_KINDS = {
    "field": "field",
    "data-source": "datasource", "datasource": "datasource", "natural": "datasource",
    "alphabetic": "alphabetic", "alpha": "alphabetic",
    "manual": "manual",
}

SORT_SHELVES = {
    "columns": "columns-shelf", "columns-shelf": "columns-shelf",
    "rows": "rows-shelf", "rows-shelf": "rows-shelf",
}


def sort_field(page, field_display_name: str, *,
               direction: str = "desc",
               by: str | None = None,
               aggregation: str = "sum",
               scope: str = "nested",
               shelf: str = "columns",
               sheet: str | None = None) -> dict:
    """Sort a dimension pill by a specific measure (right-click → Sort... → OK).

    More powerful than ``quick_sort`` because you specify exactly which measure
    and how to aggregate it. Drives the wire path the Sort dialog uses
    (``sort-dialog-sort``) without actually opening the dialog UI.

    Args:
      field_display_name: dimension pill to sort (e.g. "Category", "Region")
      direction: "asc" / "desc" / "none" (none = clear sort, reverts to data source)
      by: measure name (e.g. "Sales", "Profit"). If None, defaults to the
          chart's measure encoding. Set to "data-source" to clear.
      aggregation: how to aggregate the by-measure (sum/avg/min/max/count/...)
      scope: "nested" (sort within partitions, default) or "global" (sort across)
      shelf: which shelf the dimension is on - "columns" (default) or "rows"
      sheet: defaults to active sheet
    """
    d = SORT_DIRECTIONS.get(direction.lower())
    if not d:
        raise ValueError(f"direction must be one of {list(SORT_DIRECTIONS)}, got {direction!r}")
    shelf_wire = SORT_SHELVES.get(shelf.lower(), shelf)
    sheet = sheet or active_sheet_name(page)
    ds_key = _ds_key(page)
    field_fn = _dim_fn(page, field_display_name, ds_key)

    if by == "data-source" or by == "datasource":
        sort_by = "datasource"
        measure_name = ""
    elif by is None:
        sort_by = "field"
        # default to first measure on opposite shelf - simplest is to leave empty and let server infer
        measure_name = ""
    else:
        sort_by = "field"
        measure_name = f"[{ds_key}].[{_raw_col(page, by)}]"

    agg = AGGREGATIONS.get(aggregation.lower(), aggregation.lower())

    params = {
        "globalFieldName": field_fn,
        "worksheet": sheet,
        "visualIdPresModel": json.dumps({"worksheet": sheet}),
        "sortOrder": d,
        "sortBy": sort_by,
        "sortMeasureName": measure_name,
        "aggregation": agg,
        "keepFieldFilters": "true",
        "sortRangeList": "[]",
        "setDefault": "false",
        "sortPartitioning": "nested" if scope == "nested" else "global",
        "shelfType": shelf_wire,
    }
    r = ex.send_command(page, "tabdoc", "sort-dialog-sort", params)
    if not r.get("ok"):
        raise RuntimeError(f"sort-dialog-sort failed: {r.get('message','')[:300]}")
    flush_ui(page)
    return {"field": field_display_name, "direction": d, "by": by, "scope": scope}


def quick_sort(page, direction: str = "desc", sheet: str | None = None) -> dict:
    """Toolbar-equivalent sort: sort the active dimension by its measure.

    Equivalent to clicking the Sort Ascending / Sort Descending toolbar
    buttons. Tableau infers which dimension and which measure from the active
    viz (typically: the discrete dimension on Columns or Rows, sorted by the
    measure encoded on the perpendicular shelf).

    Args:
      direction: "asc" / "desc" / "none" (or "ascending"/"descending"/"clear")
      sheet: defaults to current sheet
    """
    d = SORT_DIRECTIONS.get(direction.lower())
    if not d:
        raise ValueError(f"direction must be one of {list(SORT_DIRECTIONS)}, got {direction!r}")
    sheet = sheet or active_sheet_name(page)
    r = ex.send_command(page, "tabdoc", "quick-sort", {
        "sortOrder": d,
        "visualIdPresModel": json.dumps({"worksheet": sheet}),
    })
    if not r.get("ok"):
        raise RuntimeError(f"quick-sort failed: {r.get('message','')[:300]}")
    flush_ui(page)
    return {"direction": d, "sheet": sheet}


def set_filter_context(page, field_display_name: str, *, in_context: bool = True,
                       role: str = "dimension", aggregation: str | None = None,
                       is_date: bool = False) -> dict:
    """Add or remove a filter from the Context (right-click filter pill → Add to / Remove from Context).

    Context filters compute BEFORE other filters - useful for performance on
    Top N, percent-of-total, and other dependent filters.

    Args:
      field_display_name: filter field (must be on Filters shelf)
      in_context: True (default) = promote to context; False = demote
      role: "dimension" (default) or "measure"
      aggregation: for measures (default "sum")
      is_date: True for relative-date / date-range filters (:qk suffix)
    """
    ds = _datasource_id(page)
    if is_date:
        fn = f"[sqlproxy.{ds}].[none:{field_display_name}:qk]"
    elif role == "measure":
        agg = AGGREGATIONS.get((aggregation or "sum").lower(), "sum")
        fn = f"[sqlproxy.{ds}].[{agg}:{field_display_name}:qk]"
    else:
        fn = f"[sqlproxy.{ds}].[none:{field_display_name}:nk]"

    r = ex.send_command(page, "tabdoc", "set-filter-context", {
        "fieldVector": [fn],
        "state": "true" if in_context else "false",
    })
    if not r.get("ok"):
        raise RuntimeError(f"set-filter-context failed: {r.get('message','')[:300]}")
    flush_ui(page)
    return {"field": field_display_name, "in_context": in_context}


FILTER_SCOPES = {
    "worksheet":      "local",   # only this worksheet (default)
    "local":          "local",
    "data-source":    "global",  # all worksheets using this data source
    "datasource":     "global",
    "global":         "global",
    "all":            "global",
}


def set_filter_scope(page, field_display_name: str, scope: str = "worksheet", *,
                     role: str = "dimension", aggregation: str | None = None,
                     is_date: bool = False) -> dict:
    """Set the "Apply to Worksheets" scope for a filter on the Filters shelf.

    Args:
      field_display_name: filter field
      scope: "worksheet" (default - only this sheet) or "data-source" / "global"
             (all worksheets using this data source)
      role: "dimension" (default) or "measure"
      aggregation: for measures (default "sum")
      is_date: True if the filter is a relative-date or date-range filter
               (uses :qk suffix instead of :nk/:ok)

    NOTE: "Selected Worksheets..." (the third option) needs a dialog flow that
    isn't wrapped yet - for now use "worksheet" or "data-source" granularity.
    """
    mode = FILTER_SCOPES.get(scope.lower())
    if not mode:
        raise ValueError(f"scope must be one of {list(FILTER_SCOPES)}, got {scope!r}")

    ds = _datasource_id(page)
    if is_date:
        fn = f"[sqlproxy.{ds}].[none:{field_display_name}:qk]"
    elif role == "measure":
        agg = AGGREGATIONS.get((aggregation or "sum").lower(), "sum")
        fn = f"[sqlproxy.{ds}].[{agg}:{field_display_name}:qk]"
    else:
        fn = f"[sqlproxy.{ds}].[none:{field_display_name}:nk]"

    r = ex.send_command(page, "tabdoc", "set-filter-shared", {
        "filterMode": mode,
        "fn": fn,
        "membershipTarget": "filter",
    })
    if not r.get("ok"):
        raise RuntimeError(f"set-filter-shared failed: {r.get('message','')[:300]}")
    flush_ui(page)
    return {"field": field_display_name, "scope": scope, "wire_mode": mode}


DATE_PERIODS = {"year","quarter","month","week","day","hour","minute","second"}
DATE_RANGE_TYPES = {
    "last":  "last",        # last complete period
    "lastn": "lastn",       # last N periods
    "curr":  "curr",        # current period
    "current": "curr",
    "next":  "next",        # next complete period
    "nextn": "nextn",       # next N periods
    "yeartodate": "yeartodate",
    "todate": "todate",
    "null":  "null",
}


def configure_date_filter(page, field_display_name: str, *,
                          period: str = "year",
                          n: int = 1,
                          range_type: str = "lastn",
                          include_nulls: bool = False,
                          anchor_date: str | None = None) -> dict:
    """Apply a relative-date filter to a date dimension.

    Creates the filter if not present; replaces an existing date filter if any.
    Wire flow is anchored on `close-quantitative-filter-dialog` with a
    `relativeDateFilter` blob (relative date is treated as quantitative).

    Args:
      field_display_name: date dimension to filter (e.g. "Order Date")
      period: "year" / "quarter" / "month" / "week" / "day" / "hour" / "minute" / "second"
      n: how many periods (used when range_type is "lastn" or "nextn")
      range_type: one of "last", "lastn", "curr"/"current", "next", "nextn",
                  "yeartodate", "todate", "null"
      include_nulls: also include rows where the date is null
      anchor_date: defaults to today; pass "M/D/YYYY" to anchor elsewhere

    Examples:
      configure_date_filter(page, "Order Date", period="year", n=2, range_type="lastn")
      configure_date_filter(page, "Order Date", period="quarter", range_type="curr")
      configure_date_filter(page, "Order Date", range_type="yeartodate")
    """
    if period not in DATE_PERIODS:
        raise ValueError(f"period must be one of {DATE_PERIODS}, got {period!r}")
    range_wire = DATE_RANGE_TYPES.get(range_type.lower())
    if not range_wire:
        raise ValueError(f"range_type must be one of {list(DATE_RANGE_TYPES)}, got {range_type!r}")

    ds = _datasource_id(page)
    fn_ok = f"[sqlproxy.{ds}].[none:{field_display_name}:ok]"  # ordinal-key (drop)
    fn_qk = f"[sqlproxy.{ds}].[none:{field_display_name}:qk]"  # quantitative-key (filter)

    # The drop-on-shelf wrapper - same when creating or replacing
    drop_simple = (
        f'tabdoc:drop-on-shelf drag-description="" drag-source="drag-drop-schema" '
        f'drop-target="drag-drop-shelf" '
        f'field-encodings=[{{"fn": "{fn_ok}","encoding-type-pres-model":'
        f'{{"encoding-type": "invalid-encoding","custom-encoding-type-id": ""}}}}] '
        f'is-copy="false" is-dead-drop="false" is-right-drag="false" '
        f'shelf-drag-source-position={{"is-override": false}} shelf-drop-context="none" '
        f'shelf-drop-target-position={{"shelf-type": "filter-shelf","shelf-pos-index": 0,'
        f'"encoding-type-pres-model":{{"encoding-type": "invalid-encoding",'
        f'"custom-encoding-type-id": ""}},"is-override": false}}'
    )

    # 1. Open the relative-date editor - also performs the drop. Returns a
    #    filterStoreId we'll need for the close.
    r = ex.send_command_raw(page, "tabdoc", "edit-filter-dialog", {
        "simpleCommandModel": json.dumps({"simple-command": drop_simple}),
        "globalFieldName": fn_qk,
        "forceRelativeDate": "true",
    })
    if not r.get("ok"):
        raise RuntimeError(f"edit-filter-dialog (relative-date) failed: {r.get('error','')[:300]}")
    store_id = _find_in_tree(r.get("raw"), "filterStoreId")
    if store_id is None:
        # Walk for an existing relativeDateFilter blob (replace path)
        existing = _find_in_tree(r.get("raw"), "relativeDateFilter")
        if existing and "filterStoreId" in existing:
            store_id = existing["filterStoreId"]
    if store_id is None:
        raise RuntimeError("could not extract filterStoreId from edit-filter-dialog response")

    # 2. Build the relativeDateFilter payload.
    from datetime import datetime
    anchor = anchor_date or datetime.now().strftime("%-m/%-d/%Y")
    rdf = {
        "anchorValue": "u:null",
        "anchorDate": anchor,
        "areNullsIncluded": bool(include_nulls),
        "enableAnchor": False,
        "fn": fn_qk,
        "filterStoreId": store_id,
        "quantitativeFilterKind": "visual",
        "isDateTimeField": False,
        "isForceDirty": True,
        "isFilterPresent": False,
        "dateTimePeriods": [
            {"datePeriodType": "year", "caption": "Years", "isDateTimeAnchor": False},
            {"datePeriodType": "quarter", "caption": "Quarters", "isDateTimeAnchor": False},
            {"datePeriodType": "month", "caption": "Months", "isDateTimeAnchor": False},
            {"datePeriodType": "week", "caption": "Weeks", "isDateTimeAnchor": False},
            {"datePeriodType": "day", "caption": "Days", "isDateTimeAnchor": True},
        ],
        "datePeriodType": period,
        "rangeDefaultN": 3,
        "rangeN": int(n),
        "dateRangeType": range_wire,
        "worksheet": active_sheet_name(page),
    }

    # 3. Commit.
    r = ex.send_command(page, "tabdoc", "close-quantitative-filter-dialog", {
        "relativeDateFilter": json.dumps(rdf),
        "simpleCommandModel": json.dumps({"simpleCommand": drop_simple}),
        "storeId": str(store_id),
    })
    if not r.get("ok"):
        raise RuntimeError(f"close-quantitative-filter-dialog (relative-date) failed: {r.get('message','')[:300]}")
    flush_ui(page)
    close_open_dialogs(page)
    return {"period": period, "n": n, "range_type": range_wire, "include_nulls": include_nulls, "store_id": store_id}


def configure_range_filter(page, field_display_name: str, *,
                           min: float | int | None = None,
                           max: float | int | None = None,
                           aggregation: str = "sum",
                           include: bool = True,
                           show_card: bool = True) -> dict:
    """Constrain a measure (quantitative) filter to a numeric range.

    The field must already be on the Filters shelf as a measure (call
    ``add_filter(page, field, role='measure', aggregation=...)`` first).

    Args:
      field_display_name: e.g. "Sales", "Profit"
      min: inclusive lower bound. None = unbounded (yields "At Most" if max set).
      max: inclusive upper bound. None = unbounded (yields "At Least" if min set).
      aggregation: must match the aggregation on the filter pill (default "sum").
      include: True = include-range (default), False = exclude-range.

    Filter type derived from bounds:
      - min set, max set:   Range of Values
      - min set, max None:  At Least
      - min None, max set:  At Most
      - both None:          raises ValueError

    Wire flow: edit-filter-dialog (raw - extract quantitativeFilter +
    quantitativeFilterDialogRange) → close-quantitative-filter-dialog with
    updated range.

    Returns ``{"min": ..., "max": ..., "kind": "at-least"|"at-most"|"range"}``.
    """
    if min is None and max is None:
        raise ValueError("pass at least one of min= or max=")

    ds = _datasource_id(page)
    agg = AGGREGATIONS.get(aggregation.lower(), aggregation.lower())
    fn = f"[sqlproxy.{ds}].[{agg}:{field_display_name}:qk]"

    # 1. Open the dialog and harvest server-assigned quantitativeFilter + storeId.
    r = ex.send_command_raw(page, "tabdoc", "edit-filter-dialog", {"globalFieldName": fn})
    if not r.get("ok"):
        raise RuntimeError(f"edit-filter-dialog failed: {r.get('error','')[:300]}")
    quant_filter = _find_in_tree(r.get("raw"), "quantitativeFilter")
    current_range = _find_in_tree(r.get("raw"), "quantitativeFilterDialogRange")
    if not quant_filter or not current_range:
        raise RuntimeError(f"edit-filter-dialog returned no quantitativeFilter for {field_display_name!r}")
    store_id = quant_filter.get("filterStoreId")
    if store_id is None:
        raise RuntimeError("filterStoreId missing from edit-filter-dialog response")

    # 2. Build updated range. Tableau represents an unbounded side as isMinOpen/isMaxOpen=True.
    #    We must still send a numeric value for the open side (server uses it but ignores).
    new_min = float(min) if min is not None else float(current_range.get("minValue", 0))
    new_max = float(max) if max is not None else float(current_range.get("maxValue", 0))
    range_update = {
        "isMinOpen": min is None,
        "isMaxOpen": max is None,
        "minValue": f"{new_min}." if "." not in str(new_min) else str(new_min),
        "maxValue": f"{new_max}." if "." not in str(new_max) else str(new_max),
        "included": "include-range" if include else "exclude-range",
    }

    # 3. Commit.
    r = ex.send_command(page, "tabdoc", "close-quantitative-filter-dialog", {
        "quantitativeFilter": json.dumps(quant_filter),
        "quantitativeFilterDialogRange": json.dumps(range_update),
        "storeId": str(store_id),
    })
    if not r.get("ok"):
        raise RuntimeError(f"close-quantitative-filter-dialog failed: {r.get('message','')[:300]}")
    flush_ui(page)
    close_open_dialogs(page)

    # close-quantitative-filter-dialog hides the quick-filter widget as a side
    # effect (unlike the categorical close). Re-show by default.
    if show_card:
        try:
            ex.send_command(page, "tabdoc", "show-quickfilter-doc", {
                "globalFieldName": fn, "membershipTarget": "filter",
            })
            flush_ui(page)
        except Exception:
            pass

    if min is not None and max is not None:
        kind = "range"
    elif min is not None:
        kind = "at-least"
    else:
        kind = "at-most"
    return {"min": min, "max": max, "kind": kind, "included": "include" if include else "exclude"}


# ---- Sets -----------------------------------------------------------------


def create_set(page, field_display_name: str) -> str:
    """Create a Set from a field - all members included by default.

    Tableau Cloud's Create Set has no dialog: right-click → Create → Set creates
    `<Field> Set` instantly with the full domain. Returns the new set's field name.
    """
    ds = _datasource_id(page)
    fn = f"[sqlproxy.{ds}].[{field_display_name}]"
    r = ex.send_command(page, "tabdoc", "create-set", {"fn": fn})
    if not r.get("ok"):
        raise RuntimeError(f"create-set failed: {r.get('message', '')[:300]}")
    flush_ui(page)
    return f"{field_display_name} Set"


# ---- Groups (categorical bins) -------------------------------------------


def create_group(page, field_display_name: str,
                 groups: dict[str, list[int]]) -> str:
    """Create a Group field from selected domain item indices.

    Tableau uses the `categorical-bin-*` command family internally. The protocol:
      1. `categorical-bin-add` - opens grouping context (creates `<Field> (group)`)
      2. For each group: `categorical-bin-create-bin-with-items` + `categorical-bin-rename-bin`
      3. `categorical-bin-clear-cache` - cleanup

    Args:
      field_display_name: e.g. "Ship Mode" - the field to group on
      groups: dict of {group_name: [domain_item_indices_0_based]} - see PROTOCOL.md
              for how to read domain order. Example: {"Fast": [0, 1], "Slow": [2, 3]}

    Returns the new group field's display name (e.g. "Ship Mode (group)").
    """
    ds = _datasource_id(page)
    bare_fn = f"[sqlproxy.{ds}].[{field_display_name}]"
    group_fn = f"[sqlproxy.{ds}].[{field_display_name} (group)]"

    r0 = ex.send_command(page, "tabdoc", "categorical-bin-add", {"fn": bare_fn})
    if not r0.get("ok"):
        raise RuntimeError(f"categorical-bin-add failed: {r0.get('message', '')[:300]}")

    for bin_idx, (group_name, item_indices) in enumerate(groups.items(), start=1):
        r1 = ex.send_command(page, "tabdoc", "categorical-bin-create-bin-with-items", {
            "fn": group_fn,
            "itemIndices": item_indices,
        })
        if not r1.get("ok"):
            raise RuntimeError(f"create-bin-with-items failed for {group_name!r}: "
                               f"{r1.get('message', '')[:300]}")
        r2 = ex.send_command(page, "tabdoc", "categorical-bin-rename-bin", {
            "fn": group_fn,
            "targetBinId": bin_idx,
            "valueString": group_name,
        })
        if not r2.get("ok"):
            raise RuntimeError(f"rename-bin failed for {group_name!r}: "
                               f"{r2.get('message', '')[:300]}")

    ex.send_command(page, "tabdoc", "categorical-bin-clear-cache", {})
    flush_ui(page)
    # Group editor may stay open after the wire steps - close any stragglers.
    close_open_dialogs(page)
    return f"{field_display_name} (group)"


# ---- Numeric bins --------------------------------------------------------


def create_bin(page, field_display_name: str, bin_size: float | int | None = None) -> str:
    """Create a numeric bin field from a measure.

    `create-numeric-bin` makes `<Field> (bin)` with Tableau's default size;
    optional `edit-numeric-bin` sets a custom userBinSize.
    """
    ds = _datasource_id(page)
    bare_fn = f"[sqlproxy.{ds}].[{field_display_name}]"
    bin_fn = f"[sqlproxy.{ds}].[{field_display_name} (bin)]"

    r1 = ex.send_command(page, "tabdoc", "create-numeric-bin", {"columnName": bare_fn})
    if not r1.get("ok"):
        raise RuntimeError(f"create-numeric-bin failed: {r1.get('message', '')[:300]}")

    if bin_size is not None:
        r2 = ex.send_command(page, "tabdoc", "edit-numeric-bin", {
            "columnName": bin_fn,
            "userBinSize": bin_size,
        })
        if not r2.get("ok"):
            raise RuntimeError(f"edit-numeric-bin failed: {r2.get('message', '')[:300]}")

    flush_ui(page)
    # Edit Bins dialog has no wire-level close - auto-dismiss it. Idempotent
    # no-op if no dialog is open.
    close_open_dialogs(page)
    return f"{field_display_name} (bin)"


# ---- UI refresh -----------------------------------------------------------


# The SPA caches command responses in coord.deferredServerResponseQueue.
# Normally drag-handler completion triggers coord.$B(null) to drain & apply.
# We can do the same - flushes ALL pending responses (ours plus anything queued).
_FLUSH_JS = r"""
() => {
  try {
    const d = window.onerror._targets[0].$Y;
    const coord = d.$1$1._targets[0];
    const before = Object.keys(coord.deferredServerResponseQueue || {}).length;
    coord.$B(null);
    const after = Object.keys(coord.deferredServerResponseQueue || {}).length;
    return { ok: true, drained: before - after, before, after };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}
"""


def flush_ui(page) -> dict:
    """Drain the SPA's deferredServerResponseQueue and apply pending updates to the UI.
    Also auto-dismisses any "Unexpected Server Error" dialogs (`detailedErrorDialog`)
    that Tableau may have popped - these are always unintended.

    Returns {ok, drained, before, after, errorDialogsClosed}.
    """
    r = page.evaluate(_FLUSH_JS)
    try:
        r["errorDialogsClosed"] = close_error_dialogs(page)
    except Exception:
        r["errorDialogsClosed"] = -1
    return r


# Deprecated - kept for fallback if $B is unreachable on a future Tableau release.
def refresh_ui_via_sheet_switch(page) -> None:
    """Legacy DOM-based UI refresh (clicks another sheet and back, ~3s).

    Prefer flush_ui(). Kept as a defensive fallback if the dispatcher path breaks
    on a Tableau release. See PROTOCOL.md.
    """
    sheet = page.evaluate(
        "() => document.querySelector('.tabAuthSheetTabSelected, .tabAuthTabChecked')?.innerText"
    )
    if not sheet:
        raise RuntimeError("no active sheet detected")
    other = "Sheet 1" if sheet != "Sheet 1" else "Sheet 2"
    try:
        page.locator(f'.tabAuthTab >> text="{other}"').first.click()
        time.sleep(1.5)
        page.locator(f'.tabAuthTab >> text="{sheet}"').first.click()
        time.sleep(1.5)
    except Exception:
        pass


# ---- Active-sheet helpers -------------------------------------------------


def viz_status(page) -> dict:
    """Read the viz status bar (bottom of canvas) - a cheap way to verify the
    rendered viz state without screenshots.

    Returns:
      {
        "marks": int | None,        # number of marks rendered
        "rows": int | None,
        "columns": int | None,
        "aggregations": [           # whatever measures appear in the status
          {"name": "SUM(Sales)", "value_raw": "1,431,642", "value": 1431642.0},
          ...
        ],
        "raw": {...},               # raw text per status segment for debugging
      }
    """
    info = page.evaluate(r"""
() => {
  const bar = document.querySelector('.tabAuthTabStatusBar');
  if (!bar) return null;
  const out = {};
  for (const c of bar.children) {
    const t = (c.textContent || '').trim();
    if (c.classList.contains('numberOfMarks')) out.marks = t;
    else if (c.classList.contains('rowsByColumns')) out.rows_cols = t;
    else if (c.classList.contains('lastComputedCalculation')) out.calc = t;
    else if (t) out[c.className || 'other'] = t;
  }
  return out;
}
""")
    if not info:
        return {"marks": None, "rows": None, "columns": None, "aggregations": [], "raw": None}

    def _to_int(s: str | None) -> int | None:
        if not s: return None
        m = re.search(r"(\d[\d,]*)", s)
        return int(m.group(1).replace(",", "")) if m else None

    marks = _to_int(info.get("marks"))
    rc = info.get("rows_cols") or ""
    rm = re.match(r"(\d+)\s+rows?\s+by\s+(\d+)\s+columns?", rc)
    rows = int(rm.group(1)) if rm else None
    cols = int(rm.group(2)) if rm else None

    aggs = []
    calc = info.get("calc") or ""
    # Could be "SUM(Sales): 1,431,642" or "SUM(Sales): 1,431,642  AVG(Profit): 28.1"
    for m in re.finditer(r"([A-Za-z]+\([^)]+\))\s*:\s*([-\d,\.]+(?:[eE][-+]?\d+)?)", calc):
        raw = m.group(2)
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            val = None
        aggs.append({"name": m.group(1), "value_raw": raw, "value": val})

    return {
        "marks": marks,
        "rows": rows,
        "columns": cols,
        "aggregations": aggs,
        "raw": info,
    }


def screenshot(page, path: str = "tableau_cloud_state/snapshot.png", *, full_page: bool = False) -> str:
    """Write a Playwright screenshot of the current viz to ``path``. Returns the
    path. Useful for visual verification when the status bar isn't enough
    (chart shape, color encoding, etc.)."""
    page.screenshot(path=path, full_page=full_page)
    return path


def active_sheet_name(page) -> str:
    return page.evaluate(
        "() => document.querySelector('.tabAuthSheetTabSelected, .tabAuthTabChecked')?.innerText"
    )


def visible_pills(page) -> list[str]:
    return page.evaluate(
        "() => [...document.querySelectorAll('.tabAuthShelves .tabAuthPillLabel')]"
        ".map(el => el.innerText.trim())"
    )


_PILLS_PER_SHELF_JS = r"""
(label) => {
  const cardLabel = [...document.querySelectorAll('.tabAuthCardLabel')]
    .find(el => el.innerText.trim() === label);
  if (!cardLabel) return [];
  const card = cardLabel.closest('.tabAuthCard');
  return [...card.querySelectorAll('.tabAuthPillLabel')].map(p => p.innerText.trim());
}
"""


def pills_on_shelf(page, shelf: str) -> list[str]:
    """List the pills currently visible on a given shelf, in order.

    shelf accepts short or full name ("columns" / "columns-shelf").
    """
    shelf_type = SHELF_TYPES.get(shelf, shelf)
    label = _SHELF_LABELS.get(shelf_type)
    if not label:
        raise ValueError(f"unknown shelf {shelf!r}")
    return page.evaluate(_PILLS_PER_SHELF_JS, label)


# ---- Pill removal ---------------------------------------------------------


def remove_pill(page, shelf: str, pos: int, sheet: str | None = None) -> str:
    """Remove the pill at position `pos` (0-based) on a shelf.

    Reads the field encoding from the SPA's current model via get-drag-pres-model
    on the existing pill. Returns the encoded fn that was removed.
    """
    shelf_type = SHELF_TYPES.get(shelf, shelf)
    sheet = sheet or active_sheet_name(page)

    pills = pills_on_shelf(page, shelf_type)
    if pos >= len(pills):
        raise IndexError(f"shelf {shelf_type} has {len(pills)} pills; pos {pos} out of range")
    pill_text = pills[pos]
    # Strip aggregation wrapper "SUM(Sales)" → "Sales" for resolution
    import re
    m = re.match(r"^[A-Z]+\(([^)]+)\)$", pill_text)
    field_name = m.group(1) if m else pill_text

    plan = resolve_drop_plan(page, field_name, sheet)
    # Use the encoding for the shelf the pill is on (canonical for its current placement)
    target = next((m for m in plan["shelfDropModels"] if m["shelfType"] == shelf_type), None)
    if target is None:
        # Fall back to any encoding
        target = plan["shelfDropModels"][0]
    fn = target["fieldEncodings"][0]["fn"]

    params = {
        "dragSource": "drag-drop-shelf",
        "dropTarget": "drag-drop-none",
        "dragDescription": shelf_type,
        "isCopy": False,
        "isDeadDrop": False,
        "isRightDrag": False,
        "shelfDropContext": "none",
        "shelfDragSourcePosition": {
            "shelfType": shelf_type,
            "shelfPosIndex": pos,
            "shelfDropAction": "replace",
        },
        "shelfDropTargetPosition": {},
        "checkRelatability": True,
        "paneSpec": 0,
        "fieldEncodings": [{"fn": fn}],
        "shelfSelection": [pos + 1],  # 1-based per captured wire payload
    }
    r = ex.send_command(page, "tabdoc", "drop-nowhere", params)
    if not r.get("ok"):
        raise RuntimeError(f"drop-nowhere failed: {r.get('message', '')[:300]}")
    flush_ui(page)
    return fn
