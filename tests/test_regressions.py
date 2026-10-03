"""Regression tests for Stage 1 fixes + a locked-in baseline of current
upload-validation behaviour (to be deliberately updated in Stage 2)."""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest

import app.db.crud as crud
from app.services.anomaly import detect_anomalies
from helpers import UPLOAD_DIR, analyze_ok, ecommerce_df, local_file, upload, upload_ok


# ---------------------------------------------------------------------------
# Bad dates / DB failure — no 500 with leaked SQL
# ---------------------------------------------------------------------------

def test_some_unparseable_dates_are_dropped_not_500(client):
    df = ecommerce_df()
    df.loc[:4, "order_date"] = "not a date"
    r = upload(client, df)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cleaning"]["invalid_rows_removed"] == 5
    assert body["rows"] == 195
    assert analyze_ok(client, body["file_id"])["analytics"]["metrics"]["total_orders"] == 195


def test_all_unparseable_dates_is_400(client):
    df = ecommerce_df()
    df["order_date"] = "not a date"
    r = upload(client, df)
    assert r.status_code == 400


def test_db_failure_is_generic_500_and_cleans_up(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("INSERT INTO EcommerceOrder ... cust1043@example.com")
    monkeypatch.setattr(crud, "persist_ecommerce_dataset", boom)

    before = set(os.listdir(UPLOAD_DIR))
    r = upload(client, ecommerce_df())
    assert r.status_code == 500
    assert "INSERT" not in r.text and "@example.com" not in r.text
    assert set(os.listdir(UPLOAD_DIR)) == before


# ---------------------------------------------------------------------------
# row_index stable between SQL path and file path
# ---------------------------------------------------------------------------

def test_row_index_matches_file_path(client):
    up = upload_ok(client, ecommerce_df())
    res = analyze_ok(client, up["file_id"])
    assert res["data_source"] == "sql_server"
    clean = pd.read_csv(local_file(up["file_id"], "clean.csv"))
    expected = [(a["column"], a["row_index"], a["value"]) for a in detect_anomalies(clean, "ecommerce")]
    actual = [(a["column"], a["row_index"], a["value"]) for a in res["anomalies"]]
    assert actual == expected and actual


# ---------------------------------------------------------------------------
# File-only mode (DATABASE_URL not configured) still works
# ---------------------------------------------------------------------------

def test_file_only_mode(client, monkeypatch):
    import app.routes.analyze as analyze_mod
    import app.routes.upload as upload_mod
    import app.services.result_store as store_mod
    for mod in (upload_mod, analyze_mod, store_mod):
        monkeypatch.setattr(mod, "db_enabled", lambda: False)

    up = upload_ok(client, ecommerce_df())
    assert up["db_persisted"] is False and up["db_scope"] is None and up["dataset_id"] is None
    res = analyze_ok(client, up["file_id"])
    assert res["data_source"] == "file"
    assert res["db_persistence"]["status"] == "skipped"
    assert client.get(f"/results/{up['file_id']}").json()["result_source"] == "file"
    assert client.get(f"/report/{up['file_id']}").status_code == 200


# ---------------------------------------------------------------------------
# Manual-schema flexibility baseline. Stage 2 deliberately changed "synonyms"
# from 400 to 200 (alias mapping); detailed coverage is in test_flexibility.py.
# ---------------------------------------------------------------------------

def _case(name):
    df = ecommerce_df()
    if name == "shuffled":
        return df[df.columns[::-1]]
    if name == "case_space":
        return df.rename(columns={"order_id": "Order ID", "order_date": " Order  Date ", "total_amount": "TOTAL_AMOUNT"})
    if name == "synonyms":
        return df.rename(columns={"order_id": "OrderID", "quantity": "qty", "total_amount": "amount"})
    if name == "missing_required":
        return df.drop(columns=["total_amount"])
    if name == "random":
        return pd.DataFrame({"city": list("abcdefgh"), "temp_c": np.arange(8), "humidity": np.arange(8)})
    if name == "extra_columns":
        return df.assign(coupon_code="X10", notes="gift wrap")
    raise ValueError(name)


@pytest.mark.parametrize("name,expected", [
    ("shuffled", 200), ("case_space", 200), ("synonyms", 200),
    ("missing_required", 400), ("random", 400), ("extra_columns", 200),
])
def test_flexibility_baseline(client, name, expected):
    assert upload(client, _case(name)).status_code == expected


@pytest.mark.parametrize("kwargs,expected", [
    ({"schema": "retail"}, 400),
    ({"name": "x.txt"}, 400),
    ({"df_or_bytes": b"", "name": "e.csv"}, 400),
])
def test_bad_upload_inputs(client, kwargs, expected):
    args = {"df_or_bytes": ecommerce_df(), "name": "t.csv", "schema": "ecommerce", **kwargs}
    assert upload(client, **args).status_code == expected


def test_too_few_rows(client):
    assert upload(client, ecommerce_df().head(3)).status_code == 400


def test_bad_ids(client):
    import uuid
    assert client.post("/analyze/not-a-uuid").status_code == 400
    assert client.post(f"/analyze/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/results/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/report/{uuid.uuid4()}").status_code == 404


def test_dataset_preview_is_json_safe(client):
    df = ecommerce_df()
    df.loc[:3, "discount"] = np.nan          # NULL discount in SQL → must serialise as null
    up = upload_ok(client, df)
    r = client.get(f"/dataset/{up['file_id']}?preview_rows=5")
    assert r.status_code == 200
    first = r.json()["preview"][0]
    assert isinstance(first["order_date"], str) and first["order_date"][:4].isdigit()


# ---------------------------------------------------------------------------
# pandas >= 3 stores text as the "str" dtype instead of object. Text cleaning
# must not depend on `dtype == object` (regression found with pandas 3.0.6).
# Building string-dtype columns explicitly reproduces it on pandas 2 as well.
# ---------------------------------------------------------------------------

def test_text_cleaning_works_with_string_dtype_columns():
    from app.schema import SCHEMA_REGISTRY
    from app.services.cleaning import clean_data
    from app.services.normalization import normalize_dataframe
    from app.services.pre_clean import pre_clean

    df = ecommerce_df()
    df["product_name"] = "  Running Shoes  "
    df["order_status"] = df["order_status"].str.upper()
    df["unit_price"] = df["unit_price"].map(lambda v: f"₹{v:,.2f}")
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype("string")

    norm = normalize_dataframe(df).df
    pre, report = pre_clean(norm, SCHEMA_REGISTRY["ecommerce"])
    assert report["numeric_values_cleaned"].get("unit_price") == 200
    cleaned, _ = clean_data(pre, SCHEMA_REGISTRY["ecommerce"])
    assert set(cleaned["product_name"]) == {"running shoes"}          # trimmed + lower-cased
    assert set(cleaned["order_status"]) <= {"pending", "confirmed", "shipped", "delivered", "cancelled", "returned"}
