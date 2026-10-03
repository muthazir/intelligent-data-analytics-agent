"""narration_source = "gemini" | "fallback" — all with stubbed clients, no network."""

from __future__ import annotations

import pytest

import app.services.ai_narration as narration
from app.db.models import AnalysisRun
from helpers import analyze_ok, ecommerce_df, upload_ok

PAYLOAD = {"schema_type": "ecommerce", "key_metrics": {"total_revenue": 1.0}}
FOUR_LINES = "Revenue was 1.0\nTop product leads sales\nOne spike detected\nTrend is stable"


def _stub_client(monkeypatch, text=None, exc=None):
    class _Resp:
        def __init__(self, t):
            self.text = t

    class _Models:
        def generate_content(self, **kwargs):
            if exc is not None:
                raise exc
            return _Resp(text)

    class _Client:
        def __init__(self, **kwargs):
            self.models = _Models()

    monkeypatch.setenv("GEMINI_API_KEY", "dummy-test-key-not-real")
    monkeypatch.setattr(narration.genai, "Client", _Client)


pytestmark = pytest.mark.skipif(not narration._GENAI_AVAILABLE, reason="google-genai not installed")


def test_no_key_is_fallback():
    out = narration.generate_narration(PAYLOAD)
    assert out["source"] == "fallback" and out["reason"] == "no_key"
    assert 3 <= len(out["lines"]) <= 5
    known = narration._payload_numbers(PAYLOAD)
    assert all(not narration._ungrounded_numbers(line, known) for line in out["lines"])


def test_empty_payload_is_fallback(monkeypatch):
    _stub_client(monkeypatch, text=FOUR_LINES)
    assert narration.generate_narration({})["source"] == "fallback"


def test_valid_output_is_gemini(monkeypatch):
    _stub_client(monkeypatch, text=FOUR_LINES)
    out = narration.generate_narration(PAYLOAD)
    assert out["source"] == "gemini"
    assert len(out["lines"]) == 4


@pytest.mark.parametrize("exc", [
    ConnectionError("network down"),
    TimeoutError("timed out"),
    narration.genai_errors.APIError(429, {"error": {"message": "quota"}}) if narration._GENAI_AVAILABLE else None,
])
def test_failures_are_fallback(monkeypatch, exc):
    _stub_client(monkeypatch, exc=exc)
    assert narration.generate_narration(PAYLOAD)["source"] == "fallback"


@pytest.mark.parametrize("text", ["", None, "Only one line."])
def test_short_or_empty_output_is_fallback(monkeypatch, text):
    _stub_client(monkeypatch, text=text)
    assert narration.generate_narration(PAYLOAD)["source"] == "fallback"


def test_backcompat_wrapper_returns_lines():
    assert narration.generate_ai_insights(PAYLOAD) == narration.generate_narration(PAYLOAD)["lines"]


def test_gemini_source_flows_to_api_and_db(client, db, monkeypatch):
    _stub_client(monkeypatch, text=FOUR_LINES)
    up = upload_ok(client, ecommerce_df())
    res = analyze_ok(client, up["file_id"])
    assert res["narration_source"] == "gemini"
    assert res["narration"] == FOUR_LINES.splitlines()
    run = db.get(AnalysisRun, res["db_persistence"]["run_id"])
    assert run.narration_source == "gemini"
    assert client.get(f"/insights/{up['file_id']}").json()["narration_source"] == "gemini"
