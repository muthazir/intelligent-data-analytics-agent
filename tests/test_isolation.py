"""Dataset isolation — the same customer/product ids in two datasets must not
contaminate each other (migration 001: dataset-scoped entities)."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import DatasetCustomer, DatasetProduct, EcommerceOrder
from helpers import analyze_ok, ecommerce_df, upload_ok


def _dataset_b():
    df = ecommerce_df()
    df["product_name"] = "Brand New Widget"
    df["category"] = "toys"
    df["customer_email"] = "b@other.example"
    return df


def test_reused_ids_keep_their_own_attributes(client, db):
    df_a = ecommerce_df()
    a = upload_ok(client, df_a, "dataset_A.csv")
    b = upload_ok(client, _dataset_b(), "dataset_B.csv")

    ra = analyze_ok(client, a["file_id"])
    rb = analyze_ok(client, b["file_id"])
    assert ra["data_source"] == rb["data_source"] == "sql_server"

    mb = rb["analytics"]["metrics"]
    assert set(mb["top_products"]) == {"brand new widget"}
    assert set(mb["revenue_by_category"]) == {"toys"}

    ma = ra["analytics"]["metrics"]
    assert set(ma["revenue_by_category"]) == set(df_a["category"].str.lower())
    assert "brand new widget" not in ma["top_products"]

    # Same product_id exists once per dataset, each with its own attributes.
    pid = str(df_a["product_id"].iloc[0]).lower()
    rows = db.query(DatasetProduct).filter(DatasetProduct.product_id == pid,
                                           DatasetProduct.dataset_id.in_([a["dataset_id"], b["dataset_id"]])).all()
    by_ds = {r.dataset_id: r for r in rows}
    assert by_ds[b["dataset_id"]].product_name == "brand new widget"
    assert by_ds[a["dataset_id"]].product_name == str(df_a["product_name"].iloc[0]).lower()

    # /dataset previews come from each dataset's own scoped rows.
    prev_a = client.get(f"/dataset/{a['file_id']}?preview_rows=5").json()["preview"]
    prev_b = client.get(f"/dataset/{b['file_id']}?preview_rows=5").json()["preview"]
    assert all(r["product_name"] == "brand new widget" for r in prev_b)
    assert all(r["customer_email"] == "b@other.example" for r in prev_b)
    assert not any(r["product_name"] == "brand new widget" for r in prev_a)


def test_reanalysing_a_after_b_is_unchanged(client):
    a = upload_ok(client, ecommerce_df(), "A.csv")
    first = analyze_ok(client, a["file_id"])["analytics"]["metrics"]
    upload_ok(client, _dataset_b(), "B.csv")
    again = analyze_ok(client, a["file_id"])["analytics"]["metrics"]
    assert first == again


def test_order_cannot_reference_another_datasets_customer(client, db):
    df_a = ecommerce_df()
    df_b = ecommerce_df()
    df_b["customer_id"] = "B-" + df_b["customer_id"]
    a = upload_ok(client, df_a, "A.csv")
    b = upload_ok(client, df_b, "B.csv")

    customer_only_in_a = str(df_a["customer_id"].iloc[0]).lower()
    assert db.get(DatasetCustomer, (a["dataset_id"], customer_only_in_a)) is not None
    assert db.get(DatasetCustomer, (b["dataset_id"], customer_only_in_a)) is None

    product_in_b = str(df_b["product_id"].iloc[0]).lower()
    db.add(EcommerceOrder(
        dataset_id=b["dataset_id"], order_id="CROSS-1", order_date=datetime.date(2024, 1, 1),
        order_status="delivered", customer_id=customer_only_in_a, product_id=product_in_b,
        unit_price=1, quantity=1, total_amount=1, payment_method="upi",
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_anomalies_stay_attached_to_their_own_run(client):
    a = upload_ok(client, ecommerce_df(), "A.csv")
    df_b = ecommerce_df()
    df_b.loc[10, "total_amount"] = 999999.0
    b = upload_ok(client, df_b, "B.csv")
    run_a = analyze_ok(client, a["file_id"])["db_persistence"]["run_id"]
    run_b = analyze_ok(client, b["file_id"])["db_persistence"]["run_id"]
    assert run_a != run_b

    an_a = client.get(f"/anomalies/{a['file_id']}").json()["anomalies"]
    an_b = client.get(f"/anomalies/{b['file_id']}").json()["anomalies"]
    assert {x["run_id"] for x in an_a} <= {run_a}
    assert {x["run_id"] for x in an_b} == {run_b}
    assert any(x["value"] == 999999.0 for x in an_b)
    assert not any(x["value"] == 999999.0 for x in an_a)
