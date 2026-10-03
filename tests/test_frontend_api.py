"""Stage 4 — frontend serving, /health, and frontend ↔ backend contract checks."""

from __future__ import annotations

import inspect
import re

import pytest

from app.main import FRONTEND_FILE
from app.schema import SCHEMA_REGISTRY
from app.services.analytics import run_analytics
from app.services.cleaning import clean_data
from app.services.normalization import normalize_dataframe
from helpers import cafe_df, ecommerce_df, gym_df

HTML = FRONTEND_FILE.read_text(encoding="utf-8")
SCRIPT = re.findall(r"<script>(.*?)</script>", HTML, re.S)[-1]
BUILDERS = {"ecommerce": ecommerce_df, "cafe": cafe_df, "gym": gym_df}


def test_root_serves_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert 'id="schemaSelect"' in r.text and '<option value="auto" selected>' in r.text


def test_health_reports_booleans_only(client):
    r = client.get("/health")
    assert r.status_code == 200
    h = r.json()
    assert set(h) == {"status", "version", "db_configured", "db_connected", "gemini_configured", "gemini_sdk", "schemas"}
    assert h["db_configured"] is True and h["db_connected"] is True
    assert h["gemini_configured"] is False                      # conftest forces an empty key
    assert set(h["schemas"]) == set(SCHEMA_REGISTRY) | {"auto"}
    lowered = r.text.lower()
    for secret_hint in ("mssql", "sqlite:", "driver", "trusted_connection", "password", "api_key"):
        assert secret_hint not in lowered


def test_health_without_database(client, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "db_enabled", lambda: False)
    h = client.get("/health").json()
    assert h["db_configured"] is False and h["db_connected"] is False


def test_route_handlers_run_in_threadpool():
    from app.routes import analyze, dataset, report, upload
    for fn in (upload.upload_csv, analyze.analyze, analyze.get_results, report.get_report,
               dataset.get_dataset, dataset.get_anomalies, dataset.get_insights):
        assert not inspect.iscoroutinefunction(fn), fn.__name__


def test_every_referenced_element_id_exists():
    ids_in_html = set(re.findall(r'\bid="([^"]+)"', HTML))
    referenced = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", SCRIPT))
    # setStatus('healthX', ...) also touches healthX + 'Dot'
    referenced |= {f"{name}Dot" for name in re.findall(r"setStatus\('([A-Za-z]+)'", SCRIPT)}
    assert referenced - ids_in_html == set()


def test_no_unsafe_html_injection():
    assert "innerHTML" not in SCRIPT and "insertAdjacentHTML" not in SCRIPT and "document.write" not in SCRIPT


def _view_blocks() -> dict[str, str]:
    body = SCRIPT[SCRIPT.index("const SCHEMA_VIEWS"):]
    starts = [(m.group(1), m.start()) for m in re.finditer(r"\n  (\w+): \{\n    label:", body)]
    blocks = {}
    for i, (name, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else body.index("\n};")
        blocks[name] = body[start:end]
    return blocks


def test_schema_views_cover_every_schema():
    assert set(_view_blocks()) == set(SCHEMA_REGISTRY)


@pytest.mark.parametrize("schema", sorted(SCHEMA_REGISTRY))
def test_schema_view_keys_exist_in_analytics_output(schema):
    block = _view_blocks()[schema]
    kpi_keys = set(re.findall(r"\['(\w+)', '", block))
    chart_keys = set(re.findall(r"key: '(\w+)'", block))
    clean, _ = clean_data(normalize_dataframe(BUILDERS[schema]()).df, SCHEMA_REGISTRY[schema])
    metrics = run_analytics(clean, schema)["metrics"]
    assert kpi_keys and chart_keys
    assert (kpi_keys | chart_keys) - set(metrics) == set()
