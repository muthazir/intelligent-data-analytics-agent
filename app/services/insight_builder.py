"""
insight_builder.py
------------------
Assembles analytics output + anomaly results into a compact, structured
insight payload ready for the AI narration layer.

Position in pipeline:
    Upload → Normalization → Structural Validation → Schema Validation
    → Cleaning → Analytics → Anomaly Detection → [Insight Builder]
    → AI Narration → PDF

Inputs:
    analytics_result : dict   — output of run_analytics()
    anomalies        : list   — output of detect_anomalies()

Design rules:
    - NO raw row data ever enters this function
    - Only aggregated / summary statistics are passed forward
    - Output must be fully JSON-serializable (plain Python types only)
    - Never raises — all edge cases return a safe fallback payload

Public API:
    build_insights(analytics_result, anomalies) -> dict

Output contract:
    {
        "schema_type":      str,
        "key_metrics":      dict,   # top 5–7 numbers for the AI prompt
        "top_performers":   dict,   # best product / item / membership etc.
        "anomaly_summary":  dict,   # count + worst offenders
        "trend_direction":  str,    # "up" | "down" | "stable" | "unavailable"
        "computed_at":      str     # passed through from analytics_result
    }
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _top_performer(grouped: dict) -> dict:
    """
    Return the single best entry from a groupby dict (key → revenue/count).
    Returns empty dict if input is empty or not a dict.
    """
    if not grouped or not isinstance(grouped, dict):
        return {}
    best_key = max(grouped, key=lambda k: grouped[k])
    return {"name": best_key, "value": grouped[best_key]}


def _trend_direction(monthly_revenue: dict) -> str:
    """
    Derive a simple trend label from a monthly revenue dict.

    Logic:
        - Need at least 2 months to determine a trend
        - Compare last month vs second-to-last month
        - > 5% change  → "up" or "down"
        - ≤ 5% change  → "stable"
        - < 2 months   → "unavailable"

    Keys are expected as "YYYY-MM" strings — sorted lexicographically
    which is chronological for this format.
    """
    if not monthly_revenue or not isinstance(monthly_revenue, dict):
        return "unavailable"

    sorted_months = sorted(monthly_revenue.keys())

    if len(sorted_months) < 2:
        return "unavailable"

    last_val  = monthly_revenue[sorted_months[-1]]
    prev_val  = monthly_revenue[sorted_months[-2]]

    if prev_val == 0:
        return "unavailable"

    change_pct = (last_val - prev_val) / prev_val * 100

    if change_pct > 5:
        return "up"
    elif change_pct < -5:
        return "down"
    else:
        return "stable"


def _anomaly_summary(anomalies: list[dict]) -> dict:
    """
    Summarise the anomaly list into a compact dict for the AI prompt.

    Extracts:
        - total count
        - spike count / drop count
        - top 3 worst anomalies (by abs z-score — already sorted by detector)
    """
    if not anomalies:
        return {
            "total":  0,
            "spikes": 0,
            "drops":  0,
            "top_anomalies": [],
        }

    spikes = sum(1 for a in anomalies if a.get("type") == "spike")
    drops  = sum(1 for a in anomalies if a.get("type") == "drop")

    # anomalies are already sorted worst-first by detect_anomalies()
    top_3 = [
        {
            "column":  a["column"],
            "value":   a["value"],
            "z_score": a["z_score"],
            "type":    a["type"],
        }
        for a in anomalies[:3]
    ]

    return {
        "total":         len(anomalies),
        "spikes":        spikes,
        "drops":         drops,
        "top_anomalies": top_3,
    }


# ---------------------------------------------------------------------------
# Schema-specific key metric extractors
# ---------------------------------------------------------------------------

def _ecommerce_key_metrics(metrics: dict) -> dict:
    """Extract the 6 most important ecommerce numbers for the AI prompt."""
    return {
        "total_revenue":         metrics.get("total_revenue",         0),
        "total_orders":          metrics.get("total_orders",          0),
        "avg_order_value":       metrics.get("avg_order_value",       0),
        "unique_customers":      metrics.get("unique_customers",      0),
        "cancellation_rate_pct": metrics.get("cancellation_rate_pct", 0),
        "return_rate_pct":       metrics.get("return_rate_pct",       0),
    }


def _ecommerce_top_performers(metrics: dict) -> dict:
    """Extract top product and top category for ecommerce."""
    return {
        "top_product":  _top_performer(metrics.get("top_products",        {})),
        "top_category": _top_performer(metrics.get("revenue_by_category", {})),
        "top_payment":  _top_performer(metrics.get("revenue_by_payment_method", {})),
    }


def _cafe_key_metrics(metrics: dict) -> dict:
    """Extract the most important cafe numbers for the AI prompt."""
    return {
        "total_revenue": metrics.get("total_revenue", 0),
        "busiest_day":   metrics.get("busiest_day",   "N/A"),
    }


def _cafe_top_performers(metrics: dict) -> dict:
    """Extract top item and top category for cafe."""
    return {
        "top_item":     _top_performer(metrics.get("top_items",            {})),
        "top_category": _top_performer(metrics.get("revenue_by_category",  {})),
        "top_payment":  _top_performer(metrics.get("revenue_by_payment_method", {})),
    }


def _gym_key_metrics(metrics: dict) -> dict:
    """Extract the most important gym numbers for the AI prompt."""
    return {
        "total_members":       metrics.get("total_members",       0),
        "total_revenue":       metrics.get("total_revenue",       0),
        "avg_age":             metrics.get("avg_age",             0),
        "avg_attendance_days": metrics.get("avg_attendance_days", 0),
    }


def _gym_top_performers(metrics: dict) -> dict:
    """Extract dominant membership type, payment status, and gender split."""
    return {
        "top_membership":     _top_performer(metrics.get("membership_split",     {})),
        "top_payment_status": _top_performer(metrics.get("payment_status_split", {})),
        "top_gender":         _top_performer(metrics.get("gender_split",         {})),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_insights(
    analytics_result: dict,
    anomalies:        list[dict],
) -> dict:
    """
    Entry point for the insight builder.

    Extracts the most business-relevant numbers from analytics_result,
    summarises anomalies, and returns a compact payload ready for the
    AI narration prompt.

    Args:
        analytics_result : Output of run_analytics() —
                           { schema_type, metrics, computed_at }
        anomalies        : Output of detect_anomalies() —
                           list of { column, row_index, value, z_score, type }

    Returns:
        {
            "schema_type":     str,
            "key_metrics":     dict,
            "top_performers":  dict,
            "anomaly_summary": dict,
            "trend_direction": str,
            "computed_at":     str
        }

    Never raises. Returns a safe empty payload on bad input.
    """

    # ── Guard: missing or malformed analytics_result ───────────────────────
    if not analytics_result or not isinstance(analytics_result, dict):
        return {
            "schema_type":     "unknown",
            "key_metrics":     {},
            "top_performers":  {},
            "anomaly_summary": _anomaly_summary([]),
            "trend_direction": "unavailable",
            "computed_at":     "",
        }

    schema_type  = analytics_result.get("schema_type", "unknown")
    metrics      = analytics_result.get("metrics",     {})
    computed_at  = analytics_result.get("computed_at", "")
    anomalies    = anomalies or []

    # ── Schema dispatcher ──────────────────────────────────────────────────
    _METRIC_EXTRACTORS = {
        "ecommerce": (_ecommerce_key_metrics, _ecommerce_top_performers),
        "cafe":      (_cafe_key_metrics,      _cafe_top_performers),
        "gym":       (_gym_key_metrics,       _gym_top_performers),
    }

    extractor_pair = _METRIC_EXTRACTORS.get(schema_type)

    if extractor_pair is None:
        # Unknown schema — return what we have without crashing
        return {
            "schema_type":     schema_type,
            "key_metrics":     {},
            "top_performers":  {},
            "anomaly_summary": _anomaly_summary(anomalies),
            "trend_direction": "unavailable",
            "computed_at":     computed_at,
        }

    metrics_extractor, performers_extractor = extractor_pair

    # ── Extract key metrics + top performers ───────────────────────────────
    key_metrics    = metrics_extractor(metrics)
    top_performers = performers_extractor(metrics)

    # ── Trend direction — only available for schemas with monthly_revenue ──
    monthly_revenue = metrics.get("monthly_revenue", {})
    trend_direction = _trend_direction(monthly_revenue)

    # ── Anomaly summary ────────────────────────────────────────────────────
    anomaly_summary = _anomaly_summary(anomalies)

    return {
        "schema_type":     schema_type,
        "key_metrics":     key_metrics,
        "top_performers":  top_performers,
        "anomaly_summary": anomaly_summary,
        "trend_direction": trend_direction,
        "computed_at":     computed_at,
    }