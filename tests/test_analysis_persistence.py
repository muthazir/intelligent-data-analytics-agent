"""AnalysisRun.result_json / narration_source + single-transaction persistence."""

from __future__ import annotations

import json
import os

import app.db.crud as crud
from app.db.models import AnalysisRun, Anomaly, Insight
from helpers import analyze_ok, ecommerce_df, local_file, remove_local, upload_ok


def test_run_stores_result_json_and_narration_source(client, db):
    up = upload_ok(client, ecommerce_df())
    res = analyze_ok(client, up["file_id"])

    dp = res["db_persistence"]
    assert dp["status"] == "saved" and dp["run_id"]
    assert res["narration_source"] == "fallback"
    assert res["dataset_id"] == up["dataset_id"]

    run = db.get(AnalysisRun, dp["run_id"])
    assert run.narration_source == "fallback"
    stored = json.loads(run.result_json)
    assert stored == res                               # exact snapshot of the API response

    assert db.query(Anomaly).filter(Anomaly.run_id == run.run_id).count() == dp["anomalies_persisted"] == len(res["anomalies"])
    assert db.query(Insight).filter(Insight.run_id == run.run_id).count() == dp["insights_persisted"] == len(res["narration"])


def test_results_and_report_served_from_db_without_local_file(client):
    up = upload_ok(client, ecommerce_df())
    res = analyze_ok(client, up["file_id"])
    remove_local(up["file_id"], "result.json")
    assert not os.path.exists(local_file(up["file_id"], "result.json"))

    r = client.get(f"/results/{up['file_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body.pop("result_source") == "database"
    assert body == res

    rep = client.get(f"/report/{up['file_id']}")
    assert rep.status_code == 200
    assert rep.headers["content-type"] == "application/pdf"
    assert rep.content[:5] == b"%PDF-"


def test_rerun_creates_new_run_and_latest_is_served(client):
    up = upload_ok(client, ecommerce_df())
    run1 = analyze_ok(client, up["file_id"])["db_persistence"]["run_id"]
    assert client.get(f"/report/{up['file_id']}").status_code == 200
    run2 = analyze_ok(client, up["file_id"])["db_persistence"]["run_id"]
    assert run2 != run1

    assert client.get(f"/results/{up['file_id']}").json()["db_persistence"]["run_id"] == run2
    assert client.get(f"/insights/{up['file_id']}").json()["run_id"] == run2
    assert client.get(f"/report/{up['file_id']}").status_code == 200
    from app.routes.report import REPORT_VERSION
    assert os.path.exists(local_file(up["file_id"], f"run{run2}_report_v{REPORT_VERSION}.pdf"))


def test_failure_mid_persistence_leaves_nothing_behind(client, db, monkeypatch):
    up = upload_ok(client, ecommerce_df())
    ds_id = up["dataset_id"]

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure while writing insights")
    monkeypatch.setattr(crud, "_add_insights", boom)

    res = analyze_ok(client, up["file_id"])        # analysis itself still returned
    assert res["db_persistence"]["status"] == "failed"
    assert res["db_persistence"]["run_id"] is None
    assert res["analytics"]["metrics"]["total_orders"] == 200

    assert db.query(AnalysisRun).filter(AnalysisRun.dataset_id == ds_id).count() == 0
    assert (db.query(Anomaly).join(AnalysisRun, Anomaly.run_id == AnalysisRun.run_id)
            .filter(AnalysisRun.dataset_id == ds_id).count()) == 0
