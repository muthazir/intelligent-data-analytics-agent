"""Backwards compatibility: legacy file results, DB-only datasets, response shapes."""

from __future__ import annotations

from app.db.models import AnalysisRun
from helpers import analyze_ok, ecommerce_df, remove_local, upload_ok

OLD_UPLOAD_KEYS = {"file_id", "filename", "rows", "columns", "column_renames", "normalization",
                   "cleaning", "warnings", "db_persisted", "dataset_id", "db_message"}
OLD_ANALYZE_KEYS = {"file_id", "schema_type", "data_source", "analytics", "anomalies",
                    "insights", "narration", "db_persistence"}
OLD_PERSISTENCE_KEYS = {"run_id", "anomalies_persisted", "insights_persisted"}


def test_response_shapes_are_supersets(client):
    up = upload_ok(client, ecommerce_df())
    assert OLD_UPLOAD_KEYS <= set(up)
    res = analyze_ok(client, up["file_id"])
    assert OLD_ANALYZE_KEYS <= set(res)
    assert OLD_PERSISTENCE_KEYS <= set(res["db_persistence"])


def test_historical_run_without_result_json_falls_back_to_file(client, db):
    up = upload_ok(client, ecommerce_df())
    res = analyze_ok(client, up["file_id"])
    run = db.get(AnalysisRun, res["db_persistence"]["run_id"])
    run.result_json = None          # simulate a run recorded before migration 001
    run.narration_source = None
    db.commit()

    r = client.get(f"/results/{up['file_id']}")
    assert r.status_code == 200 and r.json()["result_source"] == "file"
    assert client.get(f"/report/{up['file_id']}").status_code == 200

    remove_local(up["file_id"], "result.json")
    assert client.get(f"/results/{up['file_id']}").status_code == 404
    assert client.get(f"/report/{up['file_id']}").status_code == 404


def test_dataset_present_only_in_sql_server_can_be_analysed(client):
    """Mirrors the pre-existing datasets whose local files live elsewhere."""
    up = upload_ok(client, ecommerce_df(), "ecommerce_sample.csv")
    remove_local(up["file_id"], "meta.json", "clean.csv", "result.json")

    res = analyze_ok(client, up["file_id"])
    assert res["data_source"] == "sql_server"
    assert res["schema_type"] == "ecommerce"
    assert res["filename"] == "ecommerce_sample.csv"
    assert res["db_persistence"]["status"] == "saved"
    assert client.get(f"/report/{up['file_id']}").status_code == 200
