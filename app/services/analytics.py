"""
analytics.py
------------
Schema-aware analytics engine. Runs after the cleaning pipeline.

Position in pipeline:
    Upload → Normalization → Structural Validation → Schema Validation
    → Cleaning → [Analytics] → Anomaly Detection → Insight Builder → AI Narration → PDF

Preconditions (guaranteed by upstream pipeline):
    - DataFrame is structurally valid and schema-validated
    - Column names are normalized (lowercase, underscores)
    - Data types are cast (numerics as float/int, dates as datetime)
    - No null values in required columns
    - Categorical strings are lowercased and stripped

Public API:
    run_analytics(df, schema_type) -> dict

Output contract:
    {
        "schema_type": str,
        "metrics":     dict,      # schema-specific metrics
        "computed_at": str        # ISO 8601 timestamp
    }
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Internal helper — numpy → native Python type conversion
# ---------------------------------------------------------------------------

def _to_python(value):
    """
    Recursively convert numpy scalar types to native Python types so the
    output dict is always JSON-serializable.

    Handles: np.integer, np.floating, np.bool_, np.ndarray, dict, list.
    Plain Python types (str, int, float, None) pass through unchanged.
    """
    if isinstance(value, dict):
        return {k: _to_python(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_python(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.ndarray):
        return [_to_python(v) for v in value.tolist()]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _safe_groupby_sum(df: pd.DataFrame, group_col: str, value_col: str) -> dict:
    """
    Vectorised groupby → sum → dict helper.
    Returns empty dict if either column is missing.
    """
    if group_col not in df.columns or value_col not in df.columns:
        return {}
    return (
        df.groupby(group_col, observed=True)[value_col]
        .sum()
        .round(2)
        .to_dict()
    )


def _safe_groupby_count(df: pd.DataFrame, group_col: str) -> dict:
    """
    Vectorised groupby → count → dict helper.
    Returns empty dict if column is missing.
    """
    if group_col not in df.columns:
        return {}
    return (
        df.groupby(group_col, observed=True)[group_col]
        .count()
        .to_dict()
    )


# ---------------------------------------------------------------------------
# Ecommerce Analytics
# ---------------------------------------------------------------------------

def _ecommerce_analytics(df: pd.DataFrame) -> dict:
    """
    Compute all ecommerce metrics from the cleaned DataFrame.

    Expected columns (post-cleaning):
        order_id, order_date, order_status, customer_id, product_name,
        category, unit_price, quantity, discount, total_amount,
        payment_method, return_requested
    """

    # ── Core financials ────────────────────────────────────────────────────

    total_revenue   = round(float(df["total_amount"].sum()), 2)     if "total_amount"  in df.columns else 0.0
    avg_order_value = round(float(df["total_amount"].mean()), 2)    if "total_amount"  in df.columns else 0.0
    total_orders    = len(df)
    unique_customers = int(df["customer_id"].nunique())              if "customer_id"   in df.columns else 0

    # ── Monthly revenue trend ──────────────────────────────────────────────
    # order_date is already cast to datetime by the cleaning pipeline (C4).
    # Format key as "YYYY-MM" for clean, sortable month labels.

    monthly_revenue: dict = {}
    if "order_date" in df.columns and "total_amount" in df.columns:
        df_dated = df.copy()
        df_dated["_month"] = pd.to_datetime(df_dated["order_date"], errors="coerce").dt.to_period("M").astype(str)
        monthly_revenue = (
            df_dated.groupby("_month", observed=True)["total_amount"]
            .sum()
            .round(2)
            .to_dict()
        )

    # ── Top 5 products by revenue ──────────────────────────────────────────

    top_products: dict = {}
    if "product_name" in df.columns and "total_amount" in df.columns:
        top_products = (
            df.groupby("product_name", observed=True)["total_amount"]
            .sum()
            .nlargest(5)
            .round(2)
            .to_dict()
        )

    # ── Revenue breakdowns ─────────────────────────────────────────────────

    revenue_by_category       = _safe_groupby_sum(df, "category",       "total_amount")
    revenue_by_payment_method = _safe_groupby_sum(df, "payment_method", "total_amount")

    # ── Cancellation rate ──────────────────────────────────────────────────

    cancellation_rate = 0.0
    if "order_status" in df.columns and total_orders > 0:
        cancelled      = df["order_status"].str.lower().eq("cancelled").sum()
        cancellation_rate = round(float(cancelled / total_orders) * 100, 2)

    # ── Return rate ────────────────────────────────────────────────────────

    return_rate = 0.0
    if "return_requested" in df.columns and total_orders > 0:
        # astype("string"): the column may be entirely blank (all-NaN float when
        # re-read from CSV); blanks count as "not returned".
        returned    = df["return_requested"].astype("string").str.lower().eq("yes").sum()
        return_rate = round(float(returned / total_orders) * 100, 2)

    return {
        "total_revenue":             total_revenue,
        "avg_order_value":           avg_order_value,
        "total_orders":              total_orders,
        "unique_customers":          unique_customers,
        "monthly_revenue":           monthly_revenue,
        "top_products":              top_products,
        "revenue_by_category":       revenue_by_category,
        "revenue_by_payment_method": revenue_by_payment_method,
        "cancellation_rate_pct":     cancellation_rate,
        "return_rate_pct":           return_rate,
    }


# ---------------------------------------------------------------------------
# Cafe Analytics
# ---------------------------------------------------------------------------

def _cafe_analytics(df: pd.DataFrame) -> dict:
    """
    Compute all cafe metrics from the cleaned DataFrame.

    Expected columns (post-cleaning):
        order_id, order_date, customer_id, item_name, category,
        price, quantity, payment_method, store_location
    """

    # ── Revenue = price × quantity ─────────────────────────────────────────
    # Computed as a derived column — never mutates df (uses .assign())

    df_rev = df.copy()

    if "price" in df_rev.columns and "quantity" in df_rev.columns:
        df_rev["_revenue"] = (
            pd.to_numeric(df_rev["price"],    errors="coerce").fillna(0) *
            pd.to_numeric(df_rev["quantity"], errors="coerce").fillna(0)
        )
    else:
        df_rev["_revenue"] = 0.0

    total_revenue = round(float(df_rev["_revenue"].sum()), 2)

    # ── Top 5 items by revenue ─────────────────────────────────────────────

    top_items: dict = {}
    if "item_name" in df_rev.columns:
        top_items = (
            df_rev.groupby("item_name", observed=True)["_revenue"]
            .sum()
            .nlargest(5)
            .round(2)
            .to_dict()
        )

    # ── Revenue breakdowns ─────────────────────────────────────────────────

    revenue_by_category       = _safe_groupby_sum(df_rev, "category",       "_revenue")
    revenue_by_payment_method = _safe_groupby_sum(df_rev, "payment_method", "_revenue")

    # ── Busiest day — date with the highest number of orders ───────────────

    busiest_day: str | None = None
    if "order_date" in df_rev.columns:
        dates       = pd.to_datetime(df_rev["order_date"], errors="coerce").dt.normalize()
        count_by_day = dates.value_counts()
        if not count_by_day.empty:
            busiest_ts  = count_by_day.idxmax()
            busiest_day = busiest_ts.strftime("%Y-%m-%d") if pd.notna(busiest_ts) else None

    return {
        "total_revenue":             total_revenue,
        "top_items":                 top_items,
        "revenue_by_category":       revenue_by_category,
        "revenue_by_payment_method": revenue_by_payment_method,
        "busiest_day":               busiest_day,
    }


# ---------------------------------------------------------------------------
# Gym Analytics
# ---------------------------------------------------------------------------

def _gym_analytics(df: pd.DataFrame) -> dict:
    """
    Compute all gym metrics from the cleaned DataFrame.

    Expected columns (post-cleaning):
        member_id, member_name, gender, age, membership_type,
        membership_start_date, membership_end_date, monthly_fee,
        payment_status, trainer_assigned, height_cm, weight_kg,
        attendance_days, fitness_goal
    """

    total_members  = len(df)
    total_revenue  = round(float(df["monthly_fee"].sum()), 2)       if "monthly_fee"      in df.columns else 0.0
    avg_age        = round(float(df["age"].mean()), 1)               if "age"              in df.columns else 0.0
    avg_attendance = round(float(df["attendance_days"].mean()), 1)   if "attendance_days"  in df.columns else 0.0

    membership_split    = _safe_groupby_count(df, "membership_type")
    payment_status_split = _safe_groupby_count(df, "payment_status")
    gender_split        = _safe_groupby_count(df, "gender")

    return {
        "total_members":       total_members,
        "total_revenue":       total_revenue,
        "avg_age":             avg_age,
        "avg_attendance_days": avg_attendance,
        "membership_split":    membership_split,
        "payment_status_split": payment_status_split,
        "gender_split":        gender_split,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_analytics(df: pd.DataFrame, schema_type: str) -> dict:
    """
    Entry point for the analytics engine.

    Dispatches to the correct schema-specific function based on schema_type
    and returns a unified result dict.

    Args:
        df          : Cleaned, validated DataFrame (upstream preconditions met)
        schema_type : One of "ecommerce", "cafe", "gym"

    Returns:
        {
            "schema_type": str,
            "metrics":     dict,   # schema-specific computed metrics
            "computed_at": str     # UTC ISO 8601 timestamp
        }

    Never raises. Returns an error-flagged dict if input is invalid.
    """
    computed_at = datetime.now(timezone.utc).isoformat()

    # ── Guard: empty or None DataFrame ────────────────────────────────────

    if df is None or df.empty:
        return {
            "schema_type": schema_type,
            "metrics":     {},
            "computed_at": computed_at,
            "error":       "Empty DataFrame — no metrics computed",
        }

    # ── Guard: unknown schema type ─────────────────────────────────────────

    _DISPATCHERS = {
        "ecommerce": _ecommerce_analytics,
        "cafe":      _cafe_analytics,
        "gym":       _gym_analytics,
    }

    dispatcher = _DISPATCHERS.get(schema_type)

    if dispatcher is None:
        return {
            "schema_type": schema_type,
            "metrics":     {},
            "computed_at": computed_at,
            "error":       f"Unknown schema_type '{schema_type}'. "
                           f"Valid options: {list(_DISPATCHERS.keys())}",
        }

    # ── Compute metrics ────────────────────────────────────────────────────

    metrics = dispatcher(df)

    # ── Convert all numpy scalars to native Python for JSON safety ─────────

    metrics = _to_python(metrics)

    return {
        "schema_type": schema_type,
        "metrics":     metrics,
        "computed_at": computed_at,
    }