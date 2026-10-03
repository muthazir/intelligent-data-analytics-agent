"""
crud.py
-------
Persistence + retrieval functions for the SQL Server layer. Everything here
is deliberately thin: it moves data between the cleaned pandas DataFrame /
analysis result produced by the existing pipeline (app/services/*) and SQL
Server, without touching that pipeline's logic.

Public API:
    persist_ecommerce_dataset(db, file_id, filename, df_clean, raw_file_path) -> Dataset
        Insert Dataset + dataset-scoped DatasetCustomer/DatasetProduct rows +
        order rows. Single transaction — all-or-nothing.

    create_dataset_record(db, file_id, filename, schema_type, rows_ingested, raw_file_path) -> Dataset
        Dataset metadata row only — used for cafe / gym uploads, whose
        row-level data stays in the cleaned CSV.

    load_ecommerce_dataframe(db, dataset_id) -> pd.DataFrame
        SQL Server -> pandas DataFrame (in original ingestion order), with the
        exact column names analytics.py / anomaly.py already expect.

    persist_analysis(db, dataset, schema_type, result, anomalies, narration, narration_source) -> AnalysisRun
        One AnalysisRun + its Anomaly + Insight rows + the canonical
        result_json snapshot, committed in ONE transaction.

    get_dataset_by_file_id(db, file_id) -> Dataset | None
    get_latest_run(db, dataset_id) -> AnalysisRun | None
    get_latest_result(db, dataset_id) -> dict | None
    get_anomalies_for_dataset(db, dataset_id) -> list[Anomaly]
    get_insights_for_dataset(db, dataset_id) -> list[Insight]
"""

from __future__ import annotations

import json
import uuid
import pandas as pd

from app.db.models import (
    Dataset, DatasetCustomer, DatasetProduct, EcommerceOrder, AnalysisRun, Anomaly, Insight,
)


def get_dataset_by_file_id(db, file_id: str) -> Dataset | None:
    return db.query(Dataset).filter(Dataset.file_id == uuid.UUID(file_id)).one_or_none()


def create_dataset_record(
    db,
    file_id: str,
    filename: str,
    schema_type: str,
    rows_ingested: int,
    raw_file_path: str | None = None,
) -> Dataset:
    """Insert one Dataset metadata row (no row-level data) and commit."""
    dataset = Dataset(
        file_id=uuid.UUID(file_id),
        filename=filename,
        schema_type=schema_type,
        rows_ingested=rows_ingested,
        raw_file_path=raw_file_path,
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset


def persist_ecommerce_dataset(
    db,
    file_id: str,
    filename: str,
    df_clean: pd.DataFrame,
    raw_file_path: str | None = None,
) -> Dataset:
    """
    Insert one Dataset row, its dataset-scoped customers/products, and its
    EcommerceOrder rows from a cleaned ecommerce DataFrame.

    df_clean is expected to already have passed schema validation + cleaning
    (app/services/cleaning.py) against ECOMMERCE_SCHEMA, so columns and types
    are trustworthy at this point — this function does not re-validate.

    Customers/products are scoped to this dataset (migration 001): the same
    customer_id / product_id in another dataset is a different row, so
    datasets can never overwrite or borrow each other's names, categories or
    emails. Within one CSV the first non-null value per id wins.

    Runs as a single transaction: if any insert fails, everything rolls back
    and the exception propagates to the caller (upload.py decides how to
    respond to the client).
    """
    dataset = Dataset(
        file_id=uuid.UUID(file_id),
        filename=filename,
        schema_type="ecommerce",
        rows_ingested=len(df_clean),
        raw_file_path=raw_file_path,
    )
    db.add(dataset)
    db.flush()  # populates dataset.dataset_id without committing yet

    # ---- Dataset-scoped customers ---------------------------------------
    for cust_id, group in df_clean.groupby("customer_id"):
        email = None
        if "customer_email" in df_clean.columns:
            non_null = group["customer_email"].dropna()
            email = str(non_null.iloc[0]) if not non_null.empty else None
        db.add(DatasetCustomer(
            dataset_id=dataset.dataset_id,
            customer_id=str(cust_id),
            customer_email=email,
        ))

    # ---- Dataset-scoped products ----------------------------------------
    for prod_id, group in df_clean.groupby("product_id"):
        db.add(DatasetProduct(
            dataset_id=dataset.dataset_id,
            product_id=str(prod_id),
            product_name=str(group["product_name"].iloc[0]),
            category=str(group["category"].iloc[0]),
        ))

    db.flush()

    # ---- Bulk-insert order rows (in DataFrame order) --------------------
    orders = []
    for _, row in df_clean.iterrows():
        orders.append(EcommerceOrder(
            dataset_id=dataset.dataset_id,
            order_id=str(row["order_id"]),
            order_date=pd.to_datetime(row["order_date"]).date(),
            order_status=str(row["order_status"]),
            customer_id=str(row["customer_id"]),
            product_id=str(row["product_id"]),
            unit_price=float(row["unit_price"]),
            quantity=int(row["quantity"]),
            discount=float(row["discount"]) if pd.notna(row.get("discount")) else None,
            total_amount=float(row["total_amount"]),
            payment_method=str(row["payment_method"]),
            shipping_city=str(row["shipping_city"]) if pd.notna(row.get("shipping_city")) else None,
            shipping_state=str(row["shipping_state"]) if pd.notna(row.get("shipping_state")) else None,
            return_requested=str(row["return_requested"]) if pd.notna(row.get("return_requested")) else None,
        ))
    db.add_all(orders)

    db.commit()
    db.refresh(dataset)
    return dataset


def load_ecommerce_dataframe(db, dataset_id: int) -> pd.DataFrame:
    """
    SQL Server -> pandas DataFrame for a given dataset_id.

    Joins EcommerceOrder with the SAME dataset's DatasetCustomer and
    DatasetProduct rows, so the returned columns match what
    app/services/analytics.py and app/services/anomaly.py already expect.

    Rows are ordered by order_row_id (= ingestion order of the cleaned CSV),
    so anomaly row_index values are identical whether a dataset is analysed
    from SQL Server or from its cleaned CSV.
    """
    query = (
        db.query(EcommerceOrder, DatasetCustomer.customer_email,
                 DatasetProduct.product_name, DatasetProduct.category)
        .join(DatasetCustomer,
              (DatasetCustomer.dataset_id == EcommerceOrder.dataset_id)
              & (DatasetCustomer.customer_id == EcommerceOrder.customer_id))
        .join(DatasetProduct,
              (DatasetProduct.dataset_id == EcommerceOrder.dataset_id)
              & (DatasetProduct.product_id == EcommerceOrder.product_id))
        .filter(EcommerceOrder.dataset_id == dataset_id)
        .order_by(EcommerceOrder.order_row_id)
    )

    records = []
    for order, customer_email, product_name, category in query.all():
        records.append({
            "order_id":         order.order_id,
            "order_date":       order.order_date,
            "order_status":     order.order_status,
            "customer_id":      order.customer_id,
            "customer_email":   customer_email,
            "product_id":       order.product_id,
            "product_name":     product_name,
            "category":         category,
            "unit_price":       float(order.unit_price),
            "quantity":         order.quantity,
            "discount":         float(order.discount) if order.discount is not None else None,
            "total_amount":     float(order.total_amount),
            "payment_method":   order.payment_method,
            "shipping_city":    order.shipping_city,
            "shipping_state":   order.shipping_state,
            "return_requested": order.return_requested,
        })

    return pd.DataFrame.from_records(records)


# ---------------------------------------------------------------------------
# Analysis persistence — one transaction per /analyze call
# ---------------------------------------------------------------------------

def _add_anomalies(db, run_id: int, anomalies: list[dict]) -> list[Anomaly]:
    """Stage Anomaly rows (output of anomaly.detect_anomalies) — no commit."""
    rows = [
        Anomaly(
            run_id=run_id,
            column_name=str(a["column"]),
            row_index=int(a["row_index"]),
            value=float(a["value"]) if a.get("value") is not None else None,
            z_score=float(a["z_score"]),
            anomaly_type=str(a["type"]),
        )
        for a in anomalies or []
    ]
    db.add_all(rows)
    return rows


def _add_insights(db, run_id: int, narration: list[str]) -> list[Insight]:
    """
    Stage Insight rows (narration lines) — no commit.

    insight_text is capped at 1000 chars (column limit). category is left
    NULL: classifying narration lines by topic would be guesswork.
    """
    rows = [
        Insight(run_id=run_id, insight_text=str(text)[:1000], category=None)
        for text in narration or []
        if str(text).strip()
    ]
    db.add_all(rows)
    return rows


def persist_analysis(
    db,
    dataset: Dataset,
    schema_type: str,
    result: dict,
    anomalies: list[dict],
    narration: list[str],
    narration_source: str,
) -> AnalysisRun:
    """
    Record one /analyze execution: AnalysisRun + Anomaly rows + Insight rows +
    the canonical result_json snapshot, all in ONE transaction.

    result["db_persistence"] is filled in (run_id + counts + status "saved")
    BEFORE the snapshot is serialised, so the stored JSON is exactly what the
    API returns. On any failure everything is rolled back and the exception
    is re-raised — no partial run can be left behind.
    """
    metrics = (result.get("analytics") or {}).get("metrics", {}) or {}
    try:
        run = AnalysisRun(
            dataset_id=dataset.dataset_id,
            schema_type=schema_type,
            total_revenue=metrics.get("total_revenue"),
            total_orders=metrics.get("total_orders"),
            narration_source=narration_source,
        )
        db.add(run)
        db.flush()  # assigns run.run_id

        saved_anomalies = _add_anomalies(db, run.run_id, anomalies)
        saved_insights  = _add_insights(db, run.run_id, narration)
        db.flush()

        result["db_persistence"] = {
            "status":              "saved",
            "run_id":              run.run_id,
            "anomalies_persisted": len(saved_anomalies),
            "insights_persisted":  len(saved_insights),
        }
        run.result_json = json.dumps(result, default=str)

        db.commit()
        db.refresh(run)
        return run
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def get_latest_run(db, dataset_id: int) -> AnalysisRun | None:
    """Most recent AnalysisRun of a dataset (run_id breaks run_at ties)."""
    return (
        db.query(AnalysisRun)
        .filter(AnalysisRun.dataset_id == dataset_id)
        .order_by(AnalysisRun.run_at.desc(), AnalysisRun.run_id.desc())
        .first()
    )


def get_latest_result(db, dataset_id: int) -> dict | None:
    """
    Parsed result_json of the dataset's latest run, or None when there is no
    run yet or the latest run predates migration 001 (result_json NULL) — the
    caller then falls back to the legacy local result file.
    """
    run = get_latest_run(db, dataset_id)
    if run is None or not run.result_json:
        return None
    return json.loads(run.result_json)


def get_anomalies_for_dataset(db, dataset_id: int) -> list[Anomaly]:
    """
    Anomaly rows of the dataset's most recent AnalysisRun.

    Returns [] if the dataset has no runs yet, or its latest run had no
    anomalies (both are valid states, not errors).
    """
    latest_run = get_latest_run(db, dataset_id)
    if latest_run is None:
        return []
    return (
        db.query(Anomaly)
        .filter(Anomaly.run_id == latest_run.run_id)
        .order_by(Anomaly.z_score.desc())
        .all()
    )


def get_insights_for_dataset(db, dataset_id: int) -> list[Insight]:
    """Insight rows of the dataset's most recent AnalysisRun."""
    latest_run = get_latest_run(db, dataset_id)
    if latest_run is None:
        return []
    return (
        db.query(Insight)
        .filter(Insight.run_id == latest_run.run_id)
        .order_by(Insight.insight_id)
        .all()
    )
