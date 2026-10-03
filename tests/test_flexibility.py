"""Stage 2 — dataset/schema flexibility: column-name normalization, synonyms,
automatic schema detection, pre-validation cleaning and validation error detail."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.db.models import DatasetProduct
from app.schema import SCHEMA_REGISTRY
from app.services.column_mapping import resolve_aliases
from app.services.normalization import normalize_dataframe
from app.services.schema_detection import AMBIGUOUS, DETECTED, UNSUPPORTED, detect_schema
from helpers import analyze_ok, cafe_df, ecommerce_df, gym_df, upload, upload_ok

SAMPLE_KPIS = (1323090.95, 6615.45, 200, 55, 9.0, 7.5)


def _kpis(metrics: dict) -> tuple:
    return (metrics["total_revenue"], metrics["avg_order_value"], metrics["total_orders"],
            metrics["unique_customers"], metrics["cancellation_rate_pct"], metrics["return_rate_pct"])


# ---------------------------------------------------------------------------
# Column-name normalization (N1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("OrderID", "order_id"), ("customerEmail", "customer_email"), ("Order Date", "order_date"),
    ("  Order   Date ", "order_date"), ("TOTAL_AMOUNT", "total_amount"), ("order-status", "order_status"),
    ("unit.price", "unit_price"), ("shipping/city", "shipping_city"), ("Price (₹)", "price"),
    ("returnRequested", "return_requested"), ("ProductName", "product_name"), ("order_id", "order_id"),
])
def test_column_name_normalization(raw, expected):
    df = pd.DataFrame([[1]], columns=[raw])
    assert list(normalize_dataframe(df).df.columns) == [expected]


# ---------------------------------------------------------------------------
# Column variations — manual schema
# ---------------------------------------------------------------------------

STYLE_VARIANTS = {
    "camel":  lambda c: "".join(w.capitalize() if i else w for i, w in enumerate(c.split("_"))),
    "pascal": lambda c: "".join(w.capitalize() for w in c.split("_")),
    "hyphen": lambda c: c.replace("_", "-"),
    "dotted": lambda c: c.replace("_", "."),
    "spaced_upper": lambda c: c.replace("_", " ").upper(),
    "title_padded": lambda c: "  " + c.replace("_", " ").title() + " ",
}


@pytest.mark.parametrize("style", list(STYLE_VARIANTS))
def test_column_name_styles_are_accepted(client, style):
    df = ecommerce_df()
    df.columns = [STYLE_VARIANTS[style](c) for c in df.columns]
    up = upload_ok(client, df, f"{style}.csv", "ecommerce")
    assert up["rows"] == 200
    assert _kpis(analyze_ok(client, up["file_id"])["analytics"]["metrics"]) == SAMPLE_KPIS


def test_shuffled_columns_with_extras(client):
    df = ecommerce_df()
    df = df[df.columns[::-1]].assign(coupon_code="X10", internal_note="ignore me")
    up = upload_ok(client, df)
    assert {"coupon_code", "internal_note"} <= set(up["columns"])
    assert _kpis(analyze_ok(client, up["file_id"])["analytics"]["metrics"]) == SAMPLE_KPIS


ECOM_SYNONYMS = {
    "order_id": "Order No", "order_date": "Purchase Date", "order_status": "Status",
    "customer_id": "Cust ID", "customer_email": "Email", "product_id": "SKU",
    "product_name": "Product", "category": "Product Category", "unit_price": "MRP",
    "quantity": "Qty", "discount": "Discount %", "total_amount": "Order Total",
    "payment_method": "Payment Mode", "shipping_city": "City", "shipping_state": "State",
    "return_requested": "Returned",
}


def test_ecommerce_synonyms_map_and_persist_correctly(client, db):
    df = ecommerce_df().rename(columns=ECOM_SYNONYMS)
    up = upload_ok(client, df, "synonyms.csv", "ecommerce")
    assert up["db_scope"] == "full"
    # Every synonym is mapped; "Discount %" normalizes straight to the canonical "discount".
    assert set(up["column_mapping"].values()) == set(ECOM_SYNONYMS) - {"discount"}
    assert up["column_mapping"]["order_total"] == "total_amount"
    res = analyze_ok(client, up["file_id"])
    assert res["data_source"] == "sql_server"
    assert _kpis(res["analytics"]["metrics"]) == SAMPLE_KPIS
    products = {p.product_name for p in db.query(DatasetProduct).filter(DatasetProduct.dataset_id == up["dataset_id"])}
    assert products == set(ecommerce_df()["product_name"].str.lower())


def test_cafe_synonyms(client):
    base = cafe_df()
    renamed = base.rename(columns={"order_id": "Bill No", "order_date": "Bill Date", "item_name": "Menu Item",
                                   "price": "Rate", "quantity": "Qty", "payment_method": "Payment Mode",
                                   "store_location": "Outlet"})
    a = analyze_ok(client, upload_ok(client, base, "cafe.csv", "cafe")["file_id"])["analytics"]["metrics"]
    b_up = upload_ok(client, renamed, "cafe_syn.csv", "cafe")
    assert b_up["column_mapping"]["outlet"] == "store_location"
    b = analyze_ok(client, b_up["file_id"])["analytics"]["metrics"]
    assert a == b


def test_gym_synonyms(client):
    base = gym_df()
    renamed = base.rename(columns={"member_id": "MemberID", "member_name": "Full Name", "gender": "Sex",
                                   "membership_type": "Plan", "membership_start_date": "Join Date",
                                   "membership_end_date": "Expiry Date", "monthly_fee": "Fee",
                                   "payment_status": "Fee Status", "attendance_days": "Attendance"})
    a = analyze_ok(client, upload_ok(client, base, "gym.csv", "gym")["file_id"])["analytics"]["metrics"]
    b = analyze_ok(client, upload_ok(client, renamed, "gym_syn.csv", "gym")["file_id"])["analytics"]["metrics"]
    assert a == b


def test_canonical_column_wins_over_alias(client):
    df = ecommerce_df().assign(qty=999)          # canonical "quantity" also present
    up = upload_ok(client, df)
    assert "qty" not in up["column_mapping"]
    assert "qty" in up["columns"]                # kept as an extra column, not renamed
    assert analyze_ok(client, up["file_id"])["analytics"]["metrics"]["total_orders"] == 200


def test_ambiguous_aliases_are_not_guessed(client):
    df = ecommerce_df().rename(columns={"quantity": "qty"}).assign(units=1)
    r = upload(client, df)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "quantity" in detail["missing_required"]
    assert any("quantity" in w["message"] and w["rule"] == "column_mapping" for w in detail["warnings"])


def test_resolve_aliases_one_column_two_meanings():
    schema = {"a": {"required": True, "aliases": ["x"]}, "b": {"required": True, "aliases": ["x"]}}
    renames, warnings = resolve_aliases(["x"], schema)
    assert renames == {} and warnings


def test_alias_never_shadows_a_canonical_column():
    schema = {"a": {"aliases": ["b"]}, "b": {}}
    renames, _ = resolve_aliases(["b"], schema)
    assert renames == {}


# ---------------------------------------------------------------------------
# Automatic schema detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("schema,builder", [("ecommerce", ecommerce_df), ("cafe", cafe_df), ("gym", gym_df)])
def test_auto_detects_each_schema(client, schema, builder):
    up = upload_ok(client, builder(), f"{schema}_auto.csv", "auto")
    assert up["schema_type"] == schema
    assert up["schema_requested"] == "auto"
    assert up["schema_detection"]["status"] == DETECTED
    res = analyze_ok(client, up["file_id"])
    assert res["schema_type"] == schema


def test_auto_detects_synonym_file(client):
    up = upload_ok(client, ecommerce_df().rename(columns=ECOM_SYNONYMS), "auto_syn.csv", "auto")
    assert up["schema_type"] == "ecommerce"
    assert _kpis(analyze_ok(client, up["file_id"])["analytics"]["metrics"]) == SAMPLE_KPIS


def test_manual_selection_still_skips_detection(client):
    up = upload_ok(client, ecommerce_df(), "manual.csv", "ecommerce")
    assert up["schema_detection"] is None and up["schema_requested"] == "ecommerce"


def test_auto_rejects_unrelated_csv_with_422(client):
    rnd = pd.DataFrame({"city": list("abcdefgh"), "temp_c": np.arange(8), "humidity": np.arange(8)})
    r = upload(client, rnd, "weather.csv", "auto")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["reason"] == UNSUPPORTED
    assert "does not match any supported dataset" in detail["message"]
    cands = {c["schema_type"]: c for c in detail["detection"]["candidates"]}
    assert set(cands) == set(SCHEMA_REGISTRY)
    assert all(c["coverage"] < 1 for c in cands.values())


def test_auto_partial_match_names_missing_columns(client):
    r = upload(client, ecommerce_df().drop(columns=["total_amount"]), "partial.csv", "auto")
    assert r.status_code == 422
    best = r.json()["detail"]["detection"]["candidates"][0]
    assert best["schema_type"] == "ecommerce" and best["missing_required"] == ["total_amount"]
    assert "total_amount" in r.json()["detail"]["message"]


def test_detection_ambiguous_tie():
    twin = {"k": {"required": True}, "v": {"required": True}}
    out = detect_schema(["k", "v"], registry={"one": twin, "two": dict(twin)})
    assert out["status"] == AMBIGUOUS and out["schema_type"] is None


def test_wrong_manual_schema_suggests_the_right_one(client):
    r = upload(client, ecommerce_df(), "ecom.csv", "gym")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["schema_type"] == "gym"
    assert "member_id" in detail["missing_required"]
    assert "ecommerce" in detail["suggestion"]
    assert "order_id" in detail["unknown_columns"]


def test_missing_required_column_detail(client):
    r = upload(client, ecommerce_df().drop(columns=["total_amount"]), "missing.csv", "ecommerce")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["missing_required"] == ["total_amount"]
    assert detail["suggestion"] is None
    assert any(e["column"] == "total_amount" and e["rule"] == "existence" for e in detail["errors"])


def test_unknown_schema_lists_auto(client):
    r = upload(client, ecommerce_df(), "x.csv", "retail")
    assert r.status_code == 400 and "auto" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Pre-validation cleaning
# ---------------------------------------------------------------------------

def test_currency_and_thousands_formatting_accepted(client):
    df = ecommerce_df()
    df["unit_price"] = df["unit_price"].map(lambda v: f"₹{v:,.2f}")
    df["total_amount"] = df["total_amount"].map(lambda v: f"Rs. {v:,.2f}")
    up = upload_ok(client, df)
    assert up["pre_cleaning"]["numeric_values_cleaned"]["unit_price"] == 200
    assert up["pre_cleaning"]["numeric_values_cleaned"]["total_amount"] == 200
    assert _kpis(analyze_ok(client, up["file_id"])["analytics"]["metrics"]) == SAMPLE_KPIS


def test_genuinely_non_numeric_value_still_rejected(client):
    df = ecommerce_df()
    df["unit_price"] = df["unit_price"].astype(object)
    df.loc[3, "unit_price"] = "call for price"
    r = upload(client, df)
    assert r.status_code == 400
    assert any(e["column"] == "unit_price" and e["rule"] == "dtype" for e in r.json()["detail"]["errors"])


def test_categorical_spelling_variants_accepted(client):
    df = ecommerce_df()
    df["payment_method"] = df["payment_method"].str.replace("_", " ").str.title()   # "Credit Card"
    df["order_status"] = df["order_status"].str.upper()
    yes_no = {"yes": "Y", "no": "FALSE"}
    df["return_requested"] = df["return_requested"].map(lambda v: yes_no.get(v, v))
    up = upload_ok(client, df)
    assert up["pre_cleaning"]["categorical_values_normalized"]["payment_method"] == 200
    assert _kpis(analyze_ok(client, up["file_id"])["analytics"]["metrics"]) == SAMPLE_KPIS


def test_unmappable_category_still_rejected(client):
    df = ecommerce_df()
    df.loc[0, "payment_method"] = "bitcoin"
    r = upload(client, df)
    assert r.status_code == 400
    assert any(e["column"] == "payment_method" and e["rule"] == "allowed_values" for e in r.json()["detail"]["errors"])


def test_exact_duplicate_rows_removed_before_validation(client):
    df = pd.concat([ecommerce_df(), ecommerce_df().head(2)])
    up = upload_ok(client, df)
    assert up["pre_cleaning"]["duplicate_rows_removed"] == 2
    assert up["rows"] == 200


def test_conflicting_duplicate_order_ids_still_rejected(client):
    df = ecommerce_df()
    dup = df.head(1).copy()
    dup["total_amount"] = dup["total_amount"] + 1        # same order_id, different data
    r = upload(client, pd.concat([df, dup]))
    assert r.status_code == 400
    assert any(e["column"] == "order_id" and e["rule"] == "unique" for e in r.json()["detail"]["errors"])


def test_blank_values_are_not_invented_by_pre_cleaning(client):
    df = ecommerce_df()
    df.loc[:4, "return_requested"] = np.nan
    up = upload_ok(client, df)
    assert "return_requested" not in up["pre_cleaning"]["categorical_values_normalized"]
