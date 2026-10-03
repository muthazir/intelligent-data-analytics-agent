"""Stage 3 — Gemini output parsing, number grounding, deterministic fallback, client config.
All Gemini calls are stubbed; no network."""

from __future__ import annotations

import pandas as pd
import pytest

import app.services.ai_narration as N
from app.schema import SCHEMA_REGISTRY
from app.services.analytics import run_analytics
from app.services.anomaly import detect_anomalies
from app.services.cleaning import clean_data
from app.services.insight_builder import build_insights
from app.services.normalization import normalize_dataframe
from helpers import cafe_df, ecommerce_df, gym_df

pytestmark = pytest.mark.skipif(not N._GENAI_AVAILABLE, reason="google-genai not installed")


def _payload(schema: str, df: pd.DataFrame) -> dict:
    clean, _ = clean_data(normalize_dataframe(df).df, SCHEMA_REGISTRY[schema])
    return build_insights(run_analytics(clean, schema), detect_anomalies(clean, schema))


@pytest.fixture(scope="module")
def ecom_payload():
    return _payload("ecommerce", ecommerce_df())


def _stub(monkeypatch, text, captured: dict | None = None):
    class _Resp:
        def __init__(self, t):
            self.text = t

    class _Models:
        def generate_content(self, **kwargs):
            if captured is not None:
                captured["call"] = kwargs
            return _Resp(text)

    class _Client:
        def __init__(self, **kwargs):
            if captured is not None:
                captured["client"] = kwargs
            self.models = _Models()

    monkeypatch.setenv("GEMINI_API_KEY", "dummy-test-key-not-real")
    monkeypatch.setattr(N.genai, "Client", _Client)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def test_parser_strips_markdown_preamble_and_noise():
    text = ("Here are your insights:\n"
            "## Summary\n"
            "1. **Revenue** is strong.\n"
            "- *Beauty* leads categories.\n"
            "• Returns are low.\n"
            "---\n"
            "4) Keep monitoring anomalies.")
    assert N._parse_insights_from_text(text) == [
        "Revenue is strong.", "Beauty leads categories.", "Returns are low.", "Keep monitoring anomalies.",
    ]


def test_parser_splits_single_paragraph_and_caps_length():
    long_par = " ".join(["Revenue is strong and stable across every month we observed."] * 6)
    lines = N._parse_insights_from_text(long_par)
    assert len(lines) == 6 and all(len(line) <= N._MAX_LINE_CHARS for line in lines)
    assert len(N._parse_insights_from_text("x" * 1000)[0]) == N._MAX_LINE_CHARS


# ---------------------------------------------------------------------------
# Number grounding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,grounded", [
    ("Total revenue reached ₹1,323,090.95 across 200 orders.", True),
    ("Revenue is about ₹13.2 lakh.", True),
    ("Cancellation rate is 9% and return rate is 7.5%.", True),
    ("The top 3 products drive most sales.", True),
    ("Average order value is ₹6,615.45.", True),
    ("Revenue grew 23% last month.", False),
    ("Revenue was ₹999,999.", False),
    ("There were 4,000 orders.", False),
])
def test_number_grounding(ecom_payload, line, grounded):
    bad = N._ungrounded_numbers(line, N._payload_numbers(ecom_payload))
    assert (not bad) is grounded, bad


def test_invented_numbers_drop_lines_and_force_fallback(monkeypatch, ecom_payload):
    _stub(monkeypatch, "Revenue grew 23% last month.\nOrders rose by 4,000.\n"
                       "Total revenue is ₹1,323,090.95.\nReturn rate is 7.5%.")
    out = N.generate_narration(ecom_payload)
    assert out["source"] == "fallback" and out["reason"] == "invalid_output"


def test_valid_gemini_output_is_kept(monkeypatch, ecom_payload):
    _stub(monkeypatch, "Here are the insights:\n"
                       "1. **Total revenue** is ₹1,323,090.95 across 200 orders.\n"
                       "2. Beauty is the top category.\n"
                       "3. One total_amount spike was detected (z = 2.94).\n"
                       "4. Keep the cancellation rate of 9% under watch.")
    out = N.generate_narration(ecom_payload)
    assert out["source"] == "gemini" and out["reason"] == "ok"
    assert out["lines"][0] == "Total revenue is ₹1,323,090.95 across 200 orders."
    assert len(out["lines"]) == 4


def test_client_uses_timeout_and_model_override(monkeypatch, ecom_payload):
    captured: dict = {}
    _stub(monkeypatch, "Revenue is ₹1,323,090.95.\nBeauty leads.\nReturn rate is 7.5%.", captured)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")
    N.generate_narration(ecom_payload)
    assert captured["client"]["http_options"].timeout == N._REQUEST_TIMEOUT_MS
    assert captured["call"]["model"] == "gemini-test-model"
    assert "Use ONLY numbers that appear in the JSON" in captured["call"]["contents"]


def test_default_model_when_not_overridden(monkeypatch, ecom_payload):
    captured: dict = {}
    _stub(monkeypatch, "a\nb\nc", captured)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    N.generate_narration(ecom_payload)
    assert captured["call"]["model"] == N._DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------

def _assert_grounded(payload, lines):
    known = N._payload_numbers(payload)
    for line in lines:
        assert not N._ungrounded_numbers(line, known), line


def test_ecommerce_fallback_states_real_figures(ecom_payload):
    out = N.generate_narration(ecom_payload)
    text = " ".join(out["lines"])
    assert out["source"] == "fallback" and 3 <= len(out["lines"]) <= 5
    assert "₹1,323,090.95" in text and "200 orders" in text and "9%" in text and "7.5%" in text
    assert "1 unusual value(s)" in text
    _assert_grounded(ecom_payload, out["lines"])


def test_cafe_and_gym_fallbacks_are_schema_specific():
    cafe = _payload("cafe", cafe_df())
    gym = _payload("gym", gym_df())
    cafe_text = " ".join(N.generate_narration(cafe)["lines"])
    gym_text = " ".join(N.generate_narration(gym)["lines"])
    assert "busiest day" in cafe_text and "orders" not in cafe_text
    assert "members" in gym_text and "Average member age" in gym_text
    _assert_grounded(cafe, N.generate_narration(cafe)["lines"])
    _assert_grounded(gym, N.generate_narration(gym)["lines"])


def test_fallback_never_claims_anomalies_that_do_not_exist():
    df = ecommerce_df()
    df["total_amount"] = 100.0
    df["unit_price"] = 100.0
    df["quantity"] = 1
    df["discount"] = 0.0
    payload = _payload("ecommerce", df)
    assert payload["anomaly_summary"]["total"] == 0
    lines = N.generate_narration(payload)["lines"]
    assert any("No unusual values" in line for line in lines)
    assert not any("unusual value(s) detected" in line for line in lines)


def test_empty_payload_reason():
    out = N.generate_narration({})
    assert out["source"] == "fallback" and out["reason"] == "no_payload"
    # Nothing to describe -> say so; never state facts (e.g. "no anomalies") without data.
    assert out["lines"] == ["No analysis results were available to describe."]
