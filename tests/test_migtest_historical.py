"""SQL Server-only checks for the migrated copy of real data (IDAA_DB_MIGTEST).
Skipped on the default SQLite run."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text

from app.db.models import AnalysisRun, Dataset

pytestmark = pytest.mark.usefixtures("client")


@pytest.fixture(autouse=True)
def _sql_server_only(uses_sql_server):
    if not uses_sql_server:
        pytest.skip("requires IDAA_TEST_DATABASE_URL (SQL Server rehearsal database)")


def _historical_datasets(db):
    # Datasets that existed before this test session: they have runs recorded
    # before migration 001 (result_json IS NULL).
    return (db.query(Dataset)
            .join(AnalysisRun, AnalysisRun.dataset_id == Dataset.dataset_id)
            .filter(AnalysisRun.result_json.is_(None))
            .distinct().order_by(Dataset.dataset_id).all())


def test_historical_rows_backfilled(db):
    hist = _historical_datasets(db)
    assert hist, "expected pre-existing datasets in the rehearsal copy"
    for ds in hist:
        n_cust = db.execute(text("SELECT COUNT(*) FROM dbo.DatasetCustomer WHERE dataset_id=:d"), {"d": ds.dataset_id}).scalar()
        n_prod = db.execute(text("SELECT COUNT(*) FROM dbo.DatasetProduct WHERE dataset_id=:d"), {"d": ds.dataset_id}).scalar()
        exp_c = db.execute(text("SELECT COUNT(DISTINCT customer_id) FROM dbo.EcommerceOrder WHERE dataset_id=:d"), {"d": ds.dataset_id}).scalar()
        exp_p = db.execute(text("SELECT COUNT(DISTINCT product_id) FROM dbo.EcommerceOrder WHERE dataset_id=:d"), {"d": ds.dataset_id}).scalar()
        assert (n_cust, n_prod) == (exp_c, exp_p)


def test_historical_dataset_reanalysed_from_sql_server(client, db):
    # Earliest dataset = genuinely pre-existing data (test-created datasets
    # always get higher ids), never one that another test manipulated.
    ds = _historical_datasets(db)[0]
    assert ds.uploaded_at < datetime.datetime.utcnow() - datetime.timedelta(hours=1)
    old_runs = {r.run_id for r in db.query(AnalysisRun).filter(AnalysisRun.dataset_id == ds.dataset_id)}
    # Runs recorded before migration 001 (re-runnable: earlier test sessions may
    # already have added post-001 runs with result_json to this dataset).
    pre_001_runs = {r.run_id for r in db.query(AnalysisRun).filter(
        AnalysisRun.dataset_id == ds.dataset_id, AnalysisRun.result_json.is_(None))}
    assert pre_001_runs

    r = client.post(f"/analyze/{ds.file_id}")
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["data_source"] == "sql_server"
    m = res["analytics"]["metrics"]
    assert m["total_orders"] == ds.rows_ingested
    assert res["db_persistence"]["status"] == "saved"
    assert res["db_persistence"]["run_id"] not in old_runs

    # Historical (pre-001) runs untouched
    db.expire_all()
    for run_id in pre_001_runs:
        run = db.get(AnalysisRun, run_id)
        assert run.result_json is None and run.narration_source is None

    assert client.get(f"/results/{ds.file_id}").json()["result_source"] == "database"
    assert client.get(f"/report/{ds.file_id}").status_code == 200
