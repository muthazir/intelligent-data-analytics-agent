"""Optional text fields must never be filled with invented values (cleaning C2),
while required fields keep their validation / cleaning treatment."""

from __future__ import annotations

import numpy as np
import pytest

from app.db.models import DatasetCustomer, EcommerceOrder
from app.schema.cafe_schema import CAFE_SCHEMA
from app.schema.ecommerce_schema import ECOMMERCE_SCHEMA
from app.services.cleaning import clean_data
from app.services.normalization import normalize_dataframe
from helpers import analyze_ok, cafe_df, ecommerce_df, upload, upload_ok


def _clean(df, schema=ECOMMERCE_SCHEMA):
    return clean_data(normalize_dataframe(df).df, schema)


def _orders(db, dataset_id) -> dict:
    rows = db.query(EcommerceOrder).filter(EcommerceOrder.dataset_id == dataset_id).all()
    return {o.order_id: o for o in rows}


# ---------------------------------------------------------------------------
# 1. customer_email
# ---------------------------------------------------------------------------

def test_blank_email_is_not_replaced_by_another_customers_email(client, db):
    df = ecommerce_df()
    cust = df["customer_id"].iloc[0]
    df.loc[df["customer_id"] == cust, "customer_email"] = np.nan      # no email at all for this customer

    cleaned, _ = _clean(df)
    assert cleaned.loc[cleaned["customer_id"] == cust.lower(), "customer_email"].isna().all()

    up = upload_ok(client, df)
    stored = db.get(DatasetCustomer, (up["dataset_id"], cust.lower()))
    assert stored is not None and stored.customer_email is None

    preview = client.get(f"/dataset/{up['file_id']}?preview_rows=200").json()["preview"]
    assert all(r["customer_email"] is None for r in preview if r["customer_id"] == cust.lower())


def test_partly_blank_email_keeps_the_customers_own_email(client, db):
    df = ecommerce_df()
    cust = df["customer_id"].iloc[0]
    rows = df.index[df["customer_id"] == cust]
    assert len(rows) >= 2
    own_email = f"{cust.lower()}@own.example"
    df.loc[rows, "customer_email"] = own_email
    df.loc[rows[0], "customer_email"] = np.nan                        # first row blank

    up = upload_ok(client, df)
    assert db.get(DatasetCustomer, (up["dataset_id"], cust.lower())).customer_email == own_email


def test_sample_has_no_invented_emails(client, db):
    """Every stored email must appear in the uploaded CSV for that same customer."""
    df = ecommerce_df()
    up = upload_ok(client, df)
    src = df.assign(cid=df["customer_id"].str.lower(), em=df["customer_email"].str.lower())
    for c in db.query(DatasetCustomer).filter(DatasetCustomer.dataset_id == up["dataset_id"]):
        own = set(src.loc[src["cid"] == c.customer_id, "em"].dropna())
        if c.customer_email is None:
            assert not own
        else:
            assert c.customer_email in own


# ---------------------------------------------------------------------------
# 2. shipping_city
# ---------------------------------------------------------------------------

def test_blank_shipping_city_is_not_replaced(client, db):
    df = ecommerce_df()
    blank_ids = df["order_id"].iloc[:3].str.lower().tolist()
    df.loc[:2, "shipping_city"] = np.nan

    cleaned, _ = _clean(df)
    assert cleaned.loc[cleaned["order_id"].isin(blank_ids), "shipping_city"].isna().all()
    assert cleaned["shipping_city"].notna().sum() == len(df) - 3

    up = upload_ok(client, df)
    orders = _orders(db, up["dataset_id"])
    assert all(orders[i].shipping_city is None for i in blank_ids)
    assert sum(o.shipping_city is not None for o in orders.values()) == len(df) - 3


# ---------------------------------------------------------------------------
# 3. return_requested
# ---------------------------------------------------------------------------

def test_blank_return_requested_is_not_replaced(client, db):
    df = ecommerce_df()
    blank_ids = df.loc[df["return_requested"].isna(), "order_id"].str.lower().tolist()
    assert blank_ids, "sample is expected to contain blank return_requested values"

    cleaned, _ = _clean(df)
    assert cleaned["return_requested"].isna().sum() == len(blank_ids)

    up = upload_ok(client, df)
    orders = _orders(db, up["dataset_id"])
    assert all(orders[i].return_requested is None for i in blank_ids)

    # Return rate = explicit "yes" / all orders; blanks are not counted as returns.
    res = analyze_ok(client, up["file_id"])
    yes = int(df["return_requested"].str.lower().eq("yes").sum())
    assert res["analytics"]["metrics"]["return_rate_pct"] == round(yes / len(df) * 100, 2)


@pytest.mark.parametrize("db_on", [True, False])
def test_entirely_blank_return_requested_column_does_not_break_analysis(client, monkeypatch, db_on):
    if not db_on:
        import app.routes.analyze as analyze_mod
        import app.routes.upload as upload_mod
        import app.services.result_store as store_mod
        for mod in (upload_mod, analyze_mod, store_mod):
            monkeypatch.setattr(mod, "db_enabled", lambda: False)
    df = ecommerce_df()
    df["return_requested"] = np.nan
    up = upload_ok(client, df)
    res = analyze_ok(client, up["file_id"])
    assert res["data_source"] == ("sql_server" if db_on else "file")
    assert res["analytics"]["metrics"]["return_rate_pct"] == 0.0


def test_optional_cafe_customer_id_is_not_invented():
    df = cafe_df()
    df.loc[:4, "customer_id"] = np.nan
    cleaned, _ = _clean(df, CAFE_SCHEMA)
    assert cleaned["customer_id"].isna().sum() == 5


# ---------------------------------------------------------------------------
# 4. Required fields keep their treatment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("column", ["product_name", "customer_id", "payment_method", "total_amount", "order_date"])
def test_blank_required_field_is_rejected_by_validation(client, column):
    df = ecommerce_df()
    df.loc[0, column] = np.nan
    r = upload(client, df)
    assert r.status_code == 400
    errors = r.json()["detail"]["errors"]
    assert any(e["column"] == column and e["rule"] == "nullable" for e in errors)


def test_cleaning_still_drops_rows_missing_required_values():
    df = normalize_dataframe(ecommerce_df()).df
    df.loc[0, "product_name"] = np.nan        # required string
    df.loc[1, "total_amount"] = np.nan        # required float
    df.loc[2, "order_date"] = "not a date"    # required datetime, unparseable
    cleaned, report = clean_data(df, ECOMMERCE_SCHEMA)
    assert len(cleaned) == len(df) - 3
    assert report["invalid_rows_removed"] == 1
    assert cleaned[["product_name", "total_amount", "order_date"]].notna().all().all()


def test_required_categoricals_still_standardised(client):
    df = ecommerce_df()
    df["order_status"] = df["order_status"].str.upper()
    df["category"] = " " + df["category"].str.title() + " "
    cleaned, _ = _clean(df)
    assert set(cleaned["order_status"]) <= {"pending", "confirmed", "shipped", "delivered", "cancelled", "returned"}
    assert (cleaned["category"] == cleaned["category"].str.strip().str.lower()).all()
    assert upload(client, df).status_code == 200


def test_sample_kpis_unchanged_by_fix(client):
    up = upload_ok(client, ecommerce_df())
    m = analyze_ok(client, up["file_id"])["analytics"]["metrics"]
    assert (m["total_revenue"], m["avg_order_value"], m["total_orders"], m["unique_customers"],
            m["cancellation_rate_pct"], m["return_rate_pct"]) == (1323090.95, 6615.45, 200, 55, 9.0, 7.5)
