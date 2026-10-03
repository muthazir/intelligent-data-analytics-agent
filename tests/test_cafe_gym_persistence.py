"""cafe / gym: Dataset + AnalysisRun + Anomaly + Insight rows (no row-level tables)."""

from __future__ import annotations

import pytest

from app.db.models import AnalysisRun, Anomaly, Dataset, Insight
from helpers import analyze_ok, cafe_df, gym_df, remove_local, upload_ok

BUILDERS = {"cafe": cafe_df, "gym": gym_df}


@pytest.mark.parametrize("schema", ["cafe", "gym"])
def test_metadata_and_analysis_rows_persisted(client, db, schema):
    up = upload_ok(client, BUILDERS[schema](), f"{schema}.csv", schema)
    assert up["db_persisted"] is True
    assert up["db_scope"] == "metadata"

    ds = db.get(Dataset, up["dataset_id"])
    assert ds.schema_type == schema and ds.rows_ingested == up["rows"]

    res = analyze_ok(client, up["file_id"])
    assert res["data_source"] == "file"
    dp = res["db_persistence"]
    assert dp["status"] == "saved"
    assert len(res["anomalies"]) >= 1

    runs = db.query(AnalysisRun).filter(AnalysisRun.dataset_id == ds.dataset_id).all()
    assert len(runs) == 1 and runs[0].schema_type == schema and runs[0].result_json
    assert db.query(Anomaly).filter(Anomaly.run_id == runs[0].run_id).count() == len(res["anomalies"])
    assert db.query(Insight).filter(Insight.run_id == runs[0].run_id).count() == len(res["narration"])

    an = client.get(f"/anomalies/{up['file_id']}")
    assert an.status_code == 200 and an.json()["count"] == len(res["anomalies"])
    ins = client.get(f"/insights/{up['file_id']}")
    assert ins.status_code == 200 and ins.json()["narration_source"] == "fallback"

    meta = client.get(f"/dataset/{up['file_id']}").json()
    assert meta["db_scope"] == "metadata" and meta["rows_in_db"] is None and meta["preview"] == []

    remove_local(up["file_id"], "result.json")
    assert client.get(f"/results/{up['file_id']}").json()["result_source"] == "database"
    assert client.get(f"/report/{up['file_id']}").status_code == 200
