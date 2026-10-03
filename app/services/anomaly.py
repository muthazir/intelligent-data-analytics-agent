"""
anomaly.py
----------
Schema-aware anomaly detection engine. Runs after the analytics pipeline.

Position in pipeline:
    Upload → Normalization → Structural Validation → Schema Validation
    → Cleaning → Analytics → [Anomaly Detection] → Insight Builder → AI Narration → PDF

Preconditions (guaranteed by upstream pipeline):
    - DataFrame is structurally valid and schema-validated
    - Column names are normalized (lowercase, underscores)
    - Data types are cast (numerics as float/int)
    - No null values in required columns
    - Categorical strings are lowercased and stripped

Method:
    Z-score — standard deviation based outlier detection.
    z = (value - mean) / std
    Rows where abs(z) > Z_THRESHOLD are flagged as anomalies.
    z > +2.5  → "spike"
    z < -2.5  → "drop"

Public API:
    detect_anomalies(df, schema_type) -> list[dict]

Output contract:
    [
        {
            "column":    str,    # column where anomaly was detected
            "row_index": int,    # original DataFrame index of the anomalous row
            "value":     float,  # actual value in that cell
            "z_score":   float,  # computed Z-score (rounded to 2 dp)
            "type":      str     # "spike" or "drop"
        },
        ...
    ]
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Module-level constant
# ---------------------------------------------------------------------------

Z_THRESHOLD = 2.5   # rows with abs(z) above this are flagged as anomalies


# ---------------------------------------------------------------------------
# Core Z-score detector — shared across all schema types
# ---------------------------------------------------------------------------

def _zscore_anomalies(df: pd.DataFrame, columns: list[str]) -> list[dict]:
    """
    Run Z-score detection over a list of numeric columns.

    For each column:
        1. Convert to numeric (coerce bad values to NaN)
        2. Drop NaN rows before computing statistics
        3. Skip column if std == 0 (all values identical — no variance)
        4. Compute Z-score for every non-null row
        5. Flag rows where abs(z) > Z_THRESHOLD

    Uses fully vectorised pandas/numpy operations — no row-level loops.
    Original DataFrame index is preserved in output so the caller can
    trace anomalies back to the source data.

    Args:
        df      : Cleaned DataFrame (upstream preconditions met)
        columns : List of column names to check

    Returns:
        List of anomaly dicts — empty list if no anomalies found.
    """
    anomalies: list[dict] = []

    for col in columns:

        # ── Guard: column must exist ───────────────────────────────────────
        if col not in df.columns:
            continue

        # ── Cast to numeric — coerce non-numeric to NaN ───────────────────
        series = pd.to_numeric(df[col], errors="coerce")

        # ── Drop NaN before computing stats ───────────────────────────────
        non_null = series.dropna()

        if non_null.empty:
            continue

        # ── Compute mean and std ───────────────────────────────────────────
        col_mean = non_null.mean()
        col_std  = non_null.std()

        # ── Guard: skip if std == 0 (no variance, z-score undefined) ──────
        if col_std == 0:
            continue

        # ── Compute Z-scores — vectorised, preserves original index ───────
        z_scores = (non_null - col_mean) / col_std

        # ── Filter to anomalous rows only ──────────────────────────────────
        flagged = z_scores[z_scores.abs() > Z_THRESHOLD]

        if flagged.empty:
            continue

        # ── Build anomaly records — one dict per flagged row ───────────────
        # .items() iterates (index, z_score) pairs — no row-level Python loop
        # over the full DataFrame; only iterates the already-filtered subset.
        for idx, z_val in flagged.items():
            anomalies.append({
                "column":    col,
                "row_index": int(idx),
                "value":     round(float(series.at[idx]), 4),
                "z_score":   round(float(z_val), 2),
                "type":      "spike" if z_val > 0 else "drop",
            })

    # Sort by absolute z-score descending — worst anomalies first
    anomalies.sort(key=lambda x: abs(x["z_score"]), reverse=True)

    return anomalies


# ---------------------------------------------------------------------------
# Schema-specific detectors
# ---------------------------------------------------------------------------

def _detect_ecommerce(df: pd.DataFrame) -> list[dict]:
    """
    Anomaly detection for ecommerce schema.

    Columns checked:
        total_amount  — unusually large or small order totals
        quantity      — suspiciously high or low item quantities
        discount      — discount values outside normal range
    """
    return _zscore_anomalies(df, ["total_amount", "quantity", "discount"])


def _detect_cafe(df: pd.DataFrame) -> list[dict]:
    """
    Anomaly detection for cafe schema.

    Columns checked:
        price     — item prices outside normal range
        quantity  — unusually high or low order quantities
    """
    return _zscore_anomalies(df, ["price", "quantity"])


def _detect_gym(df: pd.DataFrame) -> list[dict]:
    """
    Anomaly detection for gym schema.

    Columns checked:
        monthly_fee      — fee values outside normal range
        attendance_days  — unusually high or low attendance
    """
    return _zscore_anomalies(df, ["monthly_fee", "attendance_days"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_anomalies(df: pd.DataFrame, schema_type: str) -> list[dict]:
    """
    Entry point for the anomaly detection engine.

    Dispatches to the correct schema-specific detector and returns
    a unified list of anomaly records sorted by severity (abs z-score).

    Args:
        df          : Cleaned, validated DataFrame (upstream preconditions met)
        schema_type : One of "ecommerce", "cafe", "gym"

    Returns:
        List of anomaly dicts — empty list if no anomalies or invalid input.
        Each dict: { column, row_index, value, z_score, type }

    Never raises. All edge cases (empty df, unknown schema, missing columns)
    are handled defensively and return an empty list.
    """

    # ── Guard: empty or None DataFrame ────────────────────────────────────
    if df is None or df.empty:
        return []

    # ── Schema dispatcher ──────────────────────────────────────────────────
    _DISPATCHERS = {
        "ecommerce": _detect_ecommerce,
        "cafe":      _detect_cafe,
        "gym":       _detect_gym,
    }

    dispatcher = _DISPATCHERS.get(schema_type)

    # ── Guard: unknown schema type ─────────────────────────────────────────
    if dispatcher is None:
        return []

    return dispatcher(df)