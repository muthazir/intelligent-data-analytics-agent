"""
report.py
---------
PDF report generation.

Position in pipeline:
    POST /upload → POST /analyze/{file_id} → [GET /report/{file_id}]

Source of truth: the PDF is built ENTIRELY from the already-persisted
analysis result — AnalysisRun.result_json in SQL Server (latest run), or the
legacy uploads/{file_id}_result.json when no database copy exists (see
app/services/result_store.py). That is the exact same JSON
POST /analyze/{file_id} returns. This route never re-runs analytics,
anomaly detection, or narration; it only renders what already exists, so
the PDF and the API response can never disagree.

Text safety: every free-text value (filename, product/category names,
narration) is XML-escaped before it reaches a ReportLab Paragraph, so
characters such as & < > cannot be interpreted as markup. A Unicode TrueType
font (Arial or Segoe UI, both carry the ₹ sign) is embedded when available;
otherwise Helvetica is used and characters outside Latin-1 are replaced
with "?" — visibly, never silently dropped.

Dataset isolation: every read/write in this module is keyed by file_id
(a UUID); there is no shared/global state between requests.

Caching:
    - database-sourced result → uploads/{file_id}_run{run_id}_report_v{N}.pdf;
      a new /analyze run has a new run_id, so a stale PDF is never reused.
    - file-sourced (legacy) result → uploads/{file_id}_report_v{N}.pdf, reused only
      if it is newer than the result.json it was built from.
    N = REPORT_VERSION, bumped whenever the layout changes so PDFs cached by
    older code are rebuilt.

Endpoint:
    GET /report/{file_id}  → application/pdf

Error handling:
    404 — file_id has no analysis result yet (call /analyze first)
    422 — stored result exists but is malformed/unreadable (stale/corrupt)
    500 — PDF rendering itself failed unexpectedly
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.fonts import addMapping
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, ListFlowable, ListItem,
)
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.barcharts import VerticalBarChart

from app.services import result_store

router = APIRouter()
UPLOAD_DIR = "uploads"
REPORT_VERSION = 2
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

_FONT_DIRS = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
              "/usr/share/fonts/truetype/dejavu", "/Library/Fonts"]
_FONT_CANDIDATES = [            # (family name, regular file, bold file)
    ("IDAA-Arial",   "arial.ttf",      "arialbd.ttf"),
    ("IDAA-Segoe",   "segoeui.ttf",    "segoeuib.ttf"),
    ("IDAA-DejaVu",  "DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
]
_font_state: dict = {}


def _fonts() -> dict:
    """
    Register (once) the first available Unicode TTF family that contains the
    rupee sign. Returns {"regular", "bold", "glyphs" (set of code points) | None}.
    Falls back to the built-in Helvetica (Latin-1 only, glyphs None).
    """
    if _font_state:
        return _font_state
    for family, regular, bold in _FONT_CANDIDATES:
        for folder in _FONT_DIRS:
            reg_path, bold_path = os.path.join(folder, regular), os.path.join(folder, bold)
            if not (os.path.exists(reg_path) and os.path.exists(bold_path)):
                continue
            try:
                reg_font = TTFont(family, reg_path)
                if 0x20B9 not in reg_font.face.charToGlyph:
                    continue
                pdfmetrics.registerFont(reg_font)
                pdfmetrics.registerFont(TTFont(family + "-Bold", bold_path))
                addMapping(family, 0, 0, family)
                addMapping(family, 1, 0, family + "-Bold")
                addMapping(family, 0, 1, family)
                addMapping(family, 1, 1, family + "-Bold")
                _font_state.update(regular=family, bold=family + "-Bold",
                                   glyphs=set(reg_font.face.charToGlyph))
                return _font_state
            except Exception as exc:          # unreadable font file → try the next one
                _logger.warning("Could not register font %s: %s", reg_path, exc)
    _font_state.update(regular="Helvetica", bold="Helvetica-Bold", glyphs=None)
    return _font_state


_LATIN1_REPLACEMENTS = {
    "\u20b9": "Rs. ", "\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"', "\u2026": "...",
}


def _plain(text) -> str:
    """
    Text the current font can render (no markup escaping). Unsupported
    characters (e.g. emoji with a Unicode font, or anything outside Latin-1
    with Helvetica) become "?" rather than disappearing.
    """
    text = "" if text is None else str(text)
    glyphs = _fonts()["glyphs"]
    if glyphs is None:
        for bad, good in _LATIN1_REPLACEMENTS.items():
            text = text.replace(bad, good)
        return "".join(ch if ord(ch) < 256 else "?" for ch in text)
    return "".join(ch if (ord(ch) in glyphs or ch in "\n\t") else "?" for ch in text)


def _t(text) -> str:
    """Escaped text, safe to place inside a ReportLab Paragraph."""
    return escape(_plain(text), {'"': "&quot;", "'": "&#39;"})


# ---------------------------------------------------------------------------
# Value formatting (schema-aware)
# ---------------------------------------------------------------------------

_KPI_SPEC = {
    "ecommerce": [
        ("total_revenue", "Total revenue", "money"), ("total_orders", "Total orders", "count"),
        ("avg_order_value", "Average order value", "money"), ("unique_customers", "Unique customers", "count"),
        ("cancellation_rate_pct", "Cancellation rate", "pct"), ("return_rate_pct", "Return rate", "pct"),
    ],
    "cafe": [
        ("total_revenue", "Total revenue", "money"), ("busiest_day", "Busiest day", "text"),
    ],
    "gym": [
        ("total_members", "Members", "count"), ("total_revenue", "Total monthly fees", "money"),
        ("avg_age", "Average age", "number"), ("avg_attendance_days", "Average attendance (days)", "number"),
    ],
}

_CHART_SPEC = {   # (metrics key, title, value kind, keep chronological order)
    "ecommerce": [("monthly_revenue", "Monthly revenue", "money", True),
                  ("revenue_by_category", "Revenue by category", "money", False),
                  ("top_products", "Top products by revenue", "money", False)],
    "cafe":      [("revenue_by_category", "Revenue by category", "money", False),
                  ("top_items", "Top items by revenue", "money", False)],
    "gym":       [("membership_split", "Members by membership type", "count", False),
                  ("payment_status_split", "Members by payment status", "count", False)],
}


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fmt(value, kind: str = "auto") -> str:
    if value is None or value == "":
        return "N/A"
    if kind == "money" and _is_number(value):
        return f"₹{value:,.2f}"
    if kind == "pct" and _is_number(value):
        return f"{value:g}%"
    if kind == "count" and _is_number(value):
        return f"{int(value):,}"
    if _is_number(value):
        return f"{value:,.2f}" if isinstance(value, float) and not value.is_integer() else f"{value:,.0f}"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _humanize(key: str) -> str:
    """'avg_order_value' -> 'Avg Order Value'"""
    return key.replace("_", " ").replace("pct", "%").strip().title()


def _kpi_rows(schema: str, metrics: dict) -> list[tuple[str, str]]:
    spec = _KPI_SPEC.get(schema)
    if spec:
        return [(label, _fmt(metrics.get(key), kind)) for key, label, kind in spec]
    return [(_humanize(k), _fmt(v)) for k, v in metrics.items()
            if v is None or isinstance(v, (int, float, str, bool))]


def _charts(schema: str, metrics: dict) -> list[tuple[str, dict, str, bool]]:
    spec = _CHART_SPEC.get(schema)
    if spec is None:     # unknown schema: any dict-of-numbers breakdown
        spec = [(k, _humanize(k), "auto", False) for k, v in metrics.items() if isinstance(v, dict)]
    out = []
    for key, title, kind, chrono in spec:
        data = metrics.get(key)
        if isinstance(data, dict) and data and all(_is_number(v) for v in data.values()):
            out.append((title, data, kind, chrono))
    return out[:2]


# ---------------------------------------------------------------------------
# Story (testable) + rendering
# ---------------------------------------------------------------------------

def _styles() -> dict:
    f = _fonts()
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("IDAATitle", parent=base["Title"], fontName=f["bold"], fontSize=19, spaceAfter=4),
        "h2":    ParagraphStyle("IDAAH2", parent=base["Heading2"], fontName=f["bold"], spaceBefore=14, spaceAfter=6),
        "body":  ParagraphStyle("IDAABody", parent=base["BodyText"], fontName=f["regular"], fontSize=10, leading=14),
        "cell":  ParagraphStyle("IDAACell", parent=base["BodyText"], fontName=f["regular"], fontSize=9, leading=11),
        "head":  ParagraphStyle("IDAAHead", parent=base["BodyText"], fontName=f["bold"], fontSize=9,
                                leading=11, textColor=colors.white),
        "meta":  ParagraphStyle("IDAAMeta", parent=base["Normal"], fontName=f["regular"],
                                textColor=colors.grey, fontSize=8.5, leading=11),
    }


def _table(rows: list[list[str]], widths: list[float], st: dict, header: bool = True) -> Table:
    data = [[Paragraph(_t(c), st["head"] if header and i == 0 else st["cell"]) for c in row]
            for i, row in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d9d9d9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f2f2f")))
    t.setStyle(TableStyle(style))
    return t


def _bar_chart(data: dict, chronological: bool, max_bars: int = 8) -> Drawing:
    items = sorted(data.items()) if chronological else sorted(data.items(), key=lambda kv: kv[1], reverse=True)
    items = items[-max_bars:] if chronological else items[:max_bars]
    f = _fonts()
    drawing = Drawing(440, 210)
    chart = VerticalBarChart()
    chart.x, chart.y, chart.width, chart.height = 50, 45, 370, 150
    chart.data = [[float(v) for _, v in items]]
    chart.categoryAxis.categoryNames = [_plain(k)[:16] for k, _ in items]
    chart.categoryAxis.labels.angle = 30
    chart.categoryAxis.labels.dy = -12
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.fontName = f["regular"]
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.labels.fontName = f["regular"]
    chart.valueAxis.valueMin = 0
    chart.bars[0].fillColor = colors.HexColor("#f5c400")
    drawing.add(chart)
    return drawing


def build_story(file_id: str, result: dict, meta: dict) -> list:
    """All report flowables for one persisted analysis result (no I/O)."""
    st = _styles()
    story: list = []
    analytics = result.get("analytics") or {}
    metrics = analytics.get("metrics") or {}
    schema = result.get("schema_type") or analytics.get("schema_type") or "unknown"
    filename = result.get("filename") or meta.get("filename") or "(filename unavailable)"
    data_source = result.get("data_source", "file")
    persistence = result.get("db_persistence") or {}

    story.append(Paragraph("IDAA — Intelligent Data Analytics Report", st["title"]))
    story.append(Paragraph(
        f"Dataset: <b>{_t(filename)}</b> &nbsp;|&nbsp; Schema: <b>{_t(schema)}</b> "
        f"&nbsp;|&nbsp; file_id: {_t(file_id)}", st["meta"]))
    story.append(Paragraph(
        f"Analysis computed at: {_t(analytics.get('computed_at') or 'unknown')} &nbsp;|&nbsp; "
        f"Report generated: {_t(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}", st["meta"]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Data &amp; persistence", st["h2"]))
    story.append(_table([
        ["Item", "Value"],
        ["Data analysed from", "SQL Server" if data_source == "sql_server" else "Cleaned CSV file"],
        ["Analysis run saved to SQL Server", {"saved": "Yes", "skipped": "No (file-only mode)",
                                              "failed": "No (save failed)"}.get(persistence.get("status"), "N/A")],
        ["AnalysisRun id", _fmt(persistence.get("run_id"), "count")],
        ["Anomalies / insights stored", f"{_fmt(persistence.get('anomalies_persisted'), 'count')} / "
                                        f"{_fmt(persistence.get('insights_persisted'), 'count')}"],
    ], [7.5 * cm, 9.5 * cm], st))

    story.append(Paragraph("Key metrics", st["h2"]))
    kpis = _kpi_rows(schema, metrics)
    if kpis:
        story.append(_table([["Metric", "Value"], *[[k, v] for k, v in kpis]], [7.5 * cm, 9.5 * cm], st))
    else:
        story.append(Paragraph("No metrics were produced for this dataset.", st["body"]))

    for title, data, _kind, chrono in _charts(schema, metrics):
        story.append(Paragraph(_t(title), st["h2"]))
        story.append(_bar_chart(data, chrono))

    story.append(Paragraph("Anomalies (Z-score &gt; 2.5)", st["h2"]))
    anomalies = result.get("anomalies") or []
    if not anomalies:
        story.append(Paragraph("No anomalies were detected in this dataset.", st["body"]))
    else:
        rows = [["Column", "Row", "Value", "Z-score", "Type"]]
        for a in anomalies[:15]:
            rows.append([a.get("column", ""), _fmt(a.get("row_index"), "count"), _fmt(a.get("value")),
                         _fmt(a.get("z_score")), a.get("type", "")])
        story.append(_table(rows, [4.5 * cm, 2 * cm, 4 * cm, 2.5 * cm, 4 * cm], st))
        if len(anomalies) > 15:
            story.append(Paragraph(f"… and {len(anomalies) - 15} more.", st["meta"]))

    story.append(Paragraph("Insights", st["h2"]))
    source = result.get("narration_source")
    source_text = {"gemini": "Narrated by Google Gemini from the metrics above (numbers verified against the analysis).",
                   "fallback": "Generated deterministically from the metrics above (Gemini not used)."}
    story.append(Paragraph(_t(source_text.get(source, "Narration source not recorded.")), st["meta"]))
    narration = [line for line in (result.get("narration") or []) if str(line).strip()]
    if narration:
        story.append(ListFlowable([ListItem(Paragraph(_t(line), st["body"])) for line in narration],
                                  bulletType="bullet", bulletFontName=_fonts()["regular"]))
    else:
        story.append(Paragraph("No narration was generated for this run.", st["body"]))
    return story


def _build_pdf(file_id: str, result: dict, meta: dict, out_path: str) -> None:
    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
        title="IDAA Analysis Report", author="IDAA",
    )
    doc.build(build_story(file_id, result, meta))


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------

def _validate_file_id(file_id: str) -> None:
    try:
        uuid.UUID(file_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid file_id")


def _pdf_path(file_id: str, run_id: int | None) -> str:
    if run_id is not None:
        return os.path.join(UPLOAD_DIR, f"{file_id}_run{run_id}_report_v{REPORT_VERSION}.pdf")
    return os.path.join(UPLOAD_DIR, f"{file_id}_report_v{REPORT_VERSION}.pdf")


def _load_meta(file_id: str) -> dict:
    """Best-effort load of upload metadata (filename). Never raises."""
    path = os.path.join(UPLOAD_DIR, f"{file_id}_meta.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# GET /report/{file_id}
# ---------------------------------------------------------------------------

@router.get("/report/{file_id}")
def get_report(file_id: str):
    """
    Generate (or reuse a fresh cached copy of) the PDF report for a dataset
    that has already been analyzed.

    Steps:
        1. Validate file_id
        2. Load the canonical result (SQL Server result_json, else the
           legacy local result file). 404 if missing, 422 if malformed.
        3. Reuse a cached PDF for the same run (see module docstring);
           otherwise (re)build it.
        4. Return the PDF as application/pdf.
    """
    _validate_file_id(file_id)

    result, source = result_store.load_result(file_id)   # 404 / 422 handled inside
    meta   = _load_meta(file_id)                         # best-effort, never raises

    run_id   = (result.get("db_persistence") or {}).get("run_id") if source == result_store.SOURCE_DATABASE else None
    pdf_path = _pdf_path(file_id, run_id)

    needs_build = True
    if os.path.exists(pdf_path):
        if run_id is not None:
            needs_build = False      # one PDF per run — the run's result never changes
        else:
            try:
                needs_build = os.path.getmtime(pdf_path) < os.path.getmtime(result_store.result_file_path(file_id))
            except OSError:
                needs_build = True

    if needs_build:
        try:
            _build_pdf(file_id, result, meta, pdf_path)
        except Exception as exc:
            _logger.exception("PDF generation failed for file_id=%s", file_id)
            # Don't leave a half-written/corrupt PDF behind for next request
            if os.path.exists(pdf_path):
                try:
                    os.remove(pdf_path)
                except OSError:
                    pass
            raise HTTPException(status_code=500, detail=f"PDF generation failed: {type(exc).__name__}")

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=f"idaa_report_{file_id[:8]}.pdf",
    )
