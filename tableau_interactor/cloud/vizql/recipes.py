"""High-level chart recipes built on top of `api.py` primitives.

Each recipe is a parameterized chart builder. The function name describes the
visual outcome; arguments describe the data placement. Recipes auto-detect the
active sheet, clear it if requested, and leave the workbook in a renderable state.
"""

import time
from . import api, exec as ex


def _clear_active_sheet(page, sheet: str) -> None:
    """Remove all pills from columns, rows, filters, pages on the active sheet."""
    for shelf in ("columns", "rows", "filters", "pages"):
        while True:
            pills = api.pills_on_shelf(page, shelf)
            if not pills:
                break
            api.remove_pill(page, shelf, 0)
            time.sleep(0.15)


def _ensure_ready(page) -> str:
    """Inject dispatcher if needed; return active sheet name."""
    ex.inject_dispatcher(page)
    return api.active_sheet_name(page)


# ---- Recipes -------------------------------------------------------------


def bar_chart(page, dimension: str, measure: str,
              *, color: str | None = None,
              clear: bool = True, sheet: str | None = None) -> None:
    """Vertical bar chart: dimension on Columns, measure on Rows.

    color=None for a plain bar; color=<field> for color-segmented bars.
    Mark type forced to Bar.
    """
    sheet = sheet or _ensure_ready(page)
    if clear:
        _clear_active_sheet(page, sheet)
    api.drop_field(page, dimension, "columns", sheet)
    api.drop_field(page, measure, "rows", sheet)
    api.set_mark_type(page, "bar", sheet)
    if color:
        api.drop_field(page, color, "color", sheet)


def stacked_bar(page, dimension: str, measure: str, color: str,
                *, clear: bool = True, sheet: str | None = None) -> None:
    """Stacked bar - same as bar_chart with color, exposed as a named recipe."""
    bar_chart(page, dimension, measure, color=color, clear=clear, sheet=sheet)


def line_chart(page, date_field: str, measure: str,
               *, color: str | None = None,
               date_part: str = "year",
               clear: bool = True, sheet: str | None = None) -> None:
    """Time-series line chart: date on Columns, measure on Rows.

    Tableau auto-picks line when a continuous date is paired with a measure.
    """
    sheet = sheet or _ensure_ready(page)
    if clear:
        _clear_active_sheet(page, sheet)
    api.drop_field(page, date_field, "columns", sheet)
    api.drop_field(page, measure, "rows", sheet)
    # Set date part if not default
    if date_part:
        api.set_date_part(page, "columns", 0, date_part)
    if color:
        api.drop_field(page, color, "color", sheet)
    # Tableau auto-selects Line for continuous date + measure; force just in case
    api.set_mark_type(page, "line", sheet)


def pie_chart(page, dimension: str, measure: str,
              *, label: str | None = None,
              clear: bool = True, sheet: str | None = None) -> None:
    """Pie chart: dimension on Color, measure on Angle. Mark type = Pie.

    The dimension drives slice colors; measure drives slice sizes.
    """
    sheet = sheet or _ensure_ready(page)
    if clear:
        _clear_active_sheet(page, sheet)
    api.set_mark_type(page, "pie", sheet)
    api.drop_field(page, dimension, "color", sheet)
    api.drop_field(page, measure, "angle", sheet)
    if label:
        api.drop_field(page, label, "label", sheet)


def area_chart(page, date_field: str, measure: str,
               *, color: str | None = None,
               date_part: str = "month",
               clear: bool = True, sheet: str | None = None) -> None:
    """Area chart over time. Stacked when color is provided."""
    sheet = sheet or _ensure_ready(page)
    if clear:
        _clear_active_sheet(page, sheet)
    api.drop_field(page, date_field, "columns", sheet)
    api.drop_field(page, measure, "rows", sheet)
    if date_part:
        api.set_date_part(page, "columns", 0, date_part)
    api.set_mark_type(page, "area", sheet)
    if color:
        api.drop_field(page, color, "color", sheet)


def text_table(page, row_dim: str, col_dim: str, measure: str,
               *, clear: bool = True, sheet: str | None = None) -> None:
    """Crosstab / text table: two dimensions framing a measure shown as text."""
    sheet = sheet or _ensure_ready(page)
    if clear:
        _clear_active_sheet(page, sheet)
    api.drop_field(page, col_dim, "columns", sheet)
    api.drop_field(page, row_dim, "rows", sheet)
    api.set_mark_type(page, "text", sheet)
    api.drop_field(page, measure, "label", sheet)


def heatmap(page, x_dim: str, y_dim: str, measure: str,
            *, clear: bool = True, sheet: str | None = None) -> None:
    """Heatmap: discrete grid of squares colored by a measure."""
    sheet = sheet or _ensure_ready(page)
    if clear:
        _clear_active_sheet(page, sheet)
    api.drop_field(page, x_dim, "columns", sheet)
    api.drop_field(page, y_dim, "rows", sheet)
    api.set_mark_type(page, "square", sheet)
    api.drop_field(page, measure, "color", sheet)


def histogram(page, measure: str, *, bin_size: float | None = None,
              clear: bool = True, sheet: str | None = None) -> None:
    """Histogram: numeric bins on Columns, count of records on Rows."""
    sheet = sheet or _ensure_ready(page)
    if clear:
        _clear_active_sheet(page, sheet)
    # Create the bin field (creates `<measure> (bin)`)
    bin_field = api.create_bin(page, measure, bin_size=bin_size)
    api.drop_field(page, bin_field, "columns", sheet)
    api.drop_field(page, measure, "rows", sheet)
    api.change_aggregation(page, "rows", 0, "count")
    api.set_mark_type(page, "bar", sheet)


def scatter_plot(page, x_measure: str, y_measure: str,
                 *, color: str | None = None, size: str | None = None,
                 clear: bool = True, sheet: str | None = None) -> None:
    """Scatter: two measures on Columns and Rows; optional color/size encodings."""
    sheet = sheet or _ensure_ready(page)
    if clear:
        _clear_active_sheet(page, sheet)
    api.drop_field(page, x_measure, "columns", sheet)
    api.drop_field(page, y_measure, "rows", sheet)
    api.set_mark_type(page, "circle", sheet)
    if color:
        api.drop_field(page, color, "color", sheet)
    if size:
        api.drop_field(page, size, "size", sheet)


# Registry of recipes by name, for dynamic lookup of a builder by name.
RECIPES = {
    "bar_chart": bar_chart,
    "stacked_bar": stacked_bar,
    "line_chart": line_chart,
    "area_chart": area_chart,
    "pie_chart": pie_chart,
    "scatter_plot": scatter_plot,
    "text_table": text_table,
    "heatmap": heatmap,
    "histogram": histogram,
}
