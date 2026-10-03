"""
pre_clean.py
------------
Light, schema-aware cleaning that runs BEFORE schema validation, so that
harmless formatting differences are not rejected as invalid data.

Position in pipeline:
    Normalization → Column Mapping → [Pre-validation cleaning]
    → Schema Validation → Cleaning → ...

Steps (each only touches values it can fix unambiguously):
    P1  Exact duplicate rows      dropped (the full-row duplicate would
                                   otherwise fail the unique order_id check
                                   although cleaning step C1 removes it anyway)
    P2  Numeric formatting        "₹1,299.00", "Rs. 450", "$ 12" -> "1299.00", "450", "12"
                                   for int/float schema columns; a value is only
                                   rewritten if the result parses as a number,
                                   so genuinely non-numeric values still fail
                                   validation with a clear error
    P3  Categorical spelling      "Credit Card" / "credit-card" -> "credit_card",
                                   "Y"/"TRUE"/"1" -> "yes" for yes/no columns;
                                   only when the result is an allowed value

Never raises; never invents values (blank stays blank).

Public API:
    pre_clean(df, schema) -> (df, report)
"""

from __future__ import annotations

import re

import pandas as pd

from app.schema import column_rules
from app.services.dtypes import is_text_series

_CURRENCY_PREFIX_RE = re.compile(r"^\s*(?:rs\.?|inr|usd|eur|gbp)\s*", re.IGNORECASE)
_CURRENCY_SYMBOL_RE = re.compile(r"[₹$€£\s]")
_DIGIT_COMMA_RE     = re.compile(r"(?<=\d),(?=\d)")
_SEPARATORS_RE      = re.compile(r"[\s\-]+")

_YES = {"y", "yes", "true", "t", "1", "1.0"}
_NO  = {"n", "no", "false", "f", "0", "0.0"}


def _clean_numeric(series: pd.Series) -> tuple[pd.Series, int]:
    if not is_text_series(series):
        return series, 0
    text = series.astype("string")
    stripped = (text.str.replace(_CURRENCY_PREFIX_RE, "", regex=True)
                    .str.replace(_CURRENCY_SYMBOL_RE, "", regex=True)
                    .str.replace(_DIGIT_COMMA_RE, "", regex=True))
    parses = pd.to_numeric(stripped, errors="coerce").notna()
    fix = parses & series.notna() & (stripped != text)
    if not fix.any():
        return series, 0
    out = series.copy()
    out[fix] = stripped[fix].astype(object)
    return out, int(fix.sum())


def _clean_categorical(series: pd.Series, allowed: list) -> tuple[pd.Series, int]:
    allowed_set = {str(v).lower() for v in allowed}
    yes_no = allowed_set == {"yes", "no"}

    def fix(value):
        if pd.isna(value):
            return value
        raw = str(value).strip()
        key = _SEPARATORS_RE.sub("_", raw.lower())
        if yes_no:
            if key in _YES:
                key = "yes"
            elif key in _NO:
                key = "no"
        return key if key in allowed_set else value

    fixed = series.map(fix)
    changed = (fixed.astype("string") != series.astype("string")) & series.notna()
    count = int(changed.fillna(False).sum())
    return (fixed if count else series), count


def pre_clean(df: pd.DataFrame, schema: dict) -> tuple[pd.DataFrame, dict]:
    report = {"duplicate_rows_removed": 0, "numeric_values_cleaned": {}, "categorical_values_normalized": {}}
    if df is None or df.empty:
        return df, report

    rows_before = len(df)
    df = df.drop_duplicates().reset_index(drop=True)
    report["duplicate_rows_removed"] = rows_before - len(df)

    for col, rules in column_rules(schema).items():
        if col not in df.columns:
            continue
        dtype = rules.get("dtype")
        if dtype in ("int", "float"):
            df[col], n = _clean_numeric(df[col])
            if n:
                report["numeric_values_cleaned"][col] = n
        if rules.get("allowed_values"):
            df[col], n = _clean_categorical(df[col], rules["allowed_values"])
            if n:
                report["categorical_values_normalized"][col] = n

    return df, report
