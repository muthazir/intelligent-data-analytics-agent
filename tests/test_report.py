"""Stage 3 — PDF report robustness: escaping, Unicode, missing values, schemas."""

from __future__ import annotations

import os

import pytest
from reportlab.platypus import Paragraph, Table

import app.routes.report as R
from helpers import analyze_ok, cafe_df, ecommerce_df, gym_df, upload_ok

NASTY = 'Q1 sales & returns <final> "v2" \'draft\' ₹ 😀 漢字'


def _texts(flowables) -> list[str]:
    out: list[str] = []
    for f in flowables:
        if isinstance(f, Paragraph):
            out.append(f.text)
        elif isinstance(f, Table):
            for row in f._cellvalues:
                out.extend(_texts(row))
        elif hasattr(f, "_flowables"):          # ListFlowable -> ListItem -> Paragraph
            out.extend(_texts(f._flowables))
    return out


def _result(schema="ecommerce", metrics=None, anomalies=None, narration=None, source="fallback", **extra):
    return {
        "schema_type": schema, "filename": extra.get("filename", "data.csv"), "data_source": "sql_server",
        "analytics": {"schema_type": schema, "metrics": metrics if metrics is not None else {},
                      "computed_at": "2026-10-03T00:00:00+00:00"},
        "anomalies": anomalies or [], "narration": narration if narration is not None else [],
        "narration_source": source,
        "db_persistence": {"status": "saved", "run_id": 7, "anomalies_persisted": 0, "insights_persisted": 0},
    }


def _render(tmp_path, result) -> bytes:
    out = tmp_path / "r.pdf"
    R._build_pdf("00000000-0000-0000-0000-000000000000", result, {}, str(out))
    data = out.read_bytes()
    assert data[:5] == b"%PDF-" and b"/Type /Page" in data
    return data


def test_markup_characters_are_escaped_and_render(tmp_path):
    result = _result(
        metrics={"total_revenue": 10.0, "revenue_by_category": {"R&D <lab>": 5.0, "toys": 5.0},
                 "top_products": {"Fish & <Chips>": 10.0}},
        narration=["Revenue grew <b>12%</b> & margins held", "R&D spend <unclosed", 'Quote "x" and \'y\''],
        filename=NASTY,
    )
    texts = " ".join(_texts(R.build_story("fid", result, {})))
    assert "&amp;" in texts and "&lt;b&gt;12%&lt;/b&gt;" in texts and "&lt;unclosed" in texts
    assert "<unclosed" not in texts and "<b>12%" not in texts
    _render(tmp_path, result)


def test_unicode_handling(tmp_path):
    result = _result(narration=[NASTY], filename=NASTY, metrics={"total_revenue": 1234.5})
    texts = " ".join(_texts(R.build_story("fid", result, {})))
    assert "😀" not in texts                     # unsupported glyph replaced, not passed through
    if R._fonts()["glyphs"] is not None:
        assert "₹" in texts                     # Unicode font: rupee kept
    _render(tmp_path, result)


def test_latin1_fallback_font(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_font_state", {"regular": "Helvetica", "bold": "Helvetica-Bold", "glyphs": None})
    result = _result(narration=[NASTY], filename=NASTY, metrics={"total_revenue": 1234.5})
    texts = " ".join(_texts(R.build_story("fid", result, {})))
    assert "Rs. " in texts and "₹" not in texts and "漢" not in texts and "?" in texts
    _render(tmp_path, result)


def test_missing_values_and_empty_sections(tmp_path):
    result = _result(metrics={"total_revenue": None, "total_orders": 3}, narration=[], anomalies=[])
    texts = " ".join(_texts(R.build_story("fid", result, {})))
    assert "N/A" in texts
    assert "No anomalies were detected" in texts
    assert "No narration was generated" in texts
    _render(tmp_path, result)


def test_unknown_schema_and_no_metrics(tmp_path):
    result = _result(schema="mystery", metrics={})
    texts = " ".join(_texts(R.build_story("fid", result, {})))
    assert "No metrics were produced" in texts
    _render(tmp_path, result)


def test_many_anomalies_and_long_names(tmp_path):
    anomalies = [{"column": "total_amount" * 20, "row_index": i, "value": 1e9 + i, "z_score": 9.9, "type": "spike"}
                 for i in range(40)]
    result = _result(metrics={"total_revenue": 1.0, "top_products": {("X" * 400): 1.0}}, anomalies=anomalies)
    texts = " ".join(_texts(R.build_story("fid", result, {})))
    assert "… and 25 more." in texts
    _render(tmp_path, result)


def test_narration_source_labels():
    gem = " ".join(_texts(R.build_story("fid", _result(narration=["a"], source="gemini"), {})))
    fb = " ".join(_texts(R.build_story("fid", _result(narration=["a"], source="fallback"), {})))
    assert "Narrated by Google Gemini" in gem and "Gemini not used" in fb


def test_monthly_chart_is_chronological():
    drawing = R._bar_chart({"2024-03": 3.0, "2024-01": 9.0, "2024-02": 1.0}, chronological=True)
    chart = drawing.contents[0]
    assert chart.categoryAxis.categoryNames == ["2024-01", "2024-02", "2024-03"]


@pytest.mark.parametrize("schema,builder,expected,absent", [
    ("ecommerce", ecommerce_df, ["Total orders", "Cancellation rate", "₹1,323,090.95"], ["Busiest day"]),
    ("cafe", cafe_df, ["Busiest day", "Total revenue"], ["Total orders", "Unique customers"]),
    ("gym", gym_df, ["Members", "Average attendance (days)"], ["Total orders", "Busiest day"]),
])
def test_schema_specific_report_from_api(client, schema, builder, expected, absent):
    up = upload_ok(client, builder(), f"{schema}.csv", schema)
    res = analyze_ok(client, up["file_id"])
    texts = " ".join(_texts(R.build_story(up["file_id"], res, {})))
    for e in expected:
        assert e in texts, e
    for a in absent:
        assert a not in texts, a
    r = client.get(f"/report/{up['file_id']}")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"


def test_report_cache_is_versioned(client):
    up = upload_ok(client, ecommerce_df())
    run_id = analyze_ok(client, up["file_id"])["db_persistence"]["run_id"]
    assert client.get(f"/report/{up['file_id']}").status_code == 200
    assert os.path.exists(os.path.join("uploads", f"{up['file_id']}_run{run_id}_report_v{R.REPORT_VERSION}.pdf"))
