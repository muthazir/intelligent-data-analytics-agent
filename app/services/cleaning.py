"""
cleaning.py
-----------
Schema-aware cleaning pipeline. Runs after schema validation.

Position in pipeline:
    Upload → Structural Validation → Normalization → Schema Validation
    → [Cleaning] → Analytics → Anomaly Detection → AI Insights → PDF

Preconditions (guaranteed by upstream pipeline):
    - DataFrame is structurally valid (min rows/cols, no empty dataset)
    - Column names are normalized (lowercase, underscores)
    - Empty strings are already NaN (handled by normalization.py N3)
    - Thousands-separator commas removed from numeric strings (N5)

Cleaning steps (strict order):
    C1  Remove duplicate rows
    C3  Remove currency symbols    ₹ $ € £  — MUST run before C2 so that
                                   numeric columns are symbol-free before
                                   median computation attempts to parse them
    C2  Handle null values         schema-aware: drop / median / mode / skip
    C4  Type casting               string → int / float / datetime (coerce)
    C4b Drop unparseable required  rows whose required int/float/datetime
                                   value could not be cast in C4
    C5  Remove invalid values      schema min / max bounds
    C6  Standardize categoricals   lowercase + strip on string columns

Why C3 before C2?
    _handle_nulls computes pd.to_numeric() on int/float columns to find the
    median fill value. If currency symbols are still present (e.g. "₹1299"),
    pd.to_numeric returns NaN for every value — median computation is skipped
    entirely and no null-filling occurs. Running C3 first strips symbols so
    numeric parsing works correctly.

Public API:
    clean_data(df, schema) -> tuple[pd.DataFrame, dict]

Report contract:
    {
        "rows_before":          int,
        "rows_after":           int,
        "duplicates_removed":   int,
        "invalid_rows_removed": int,   # C4b
    }
"""

from __future__ import annotations

import pandas as pd

from app.services.dtypes import is_text_series


# ---------------------------------------------------------------------------
# Currency symbol translation table — compiled once at module level
# ---------------------------------------------------------------------------

_CURRENCY_SYMBOLS = str.maketrans("", "", "₹$€£")


# ---------------------------------------------------------------------------
# C1 — Remove duplicate rows
# ---------------------------------------------------------------------------

def _remove_duplicates(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """
    C1 — Drop exact duplicate rows across all columns.

    Returns:
        Deduplicated DataFrame and count of rows removed.
    """
    rows_before = len(df)
    df_out      = df.drop_duplicates()
    removed     = rows_before - len(df_out)
    return df_out, removed


# ---------------------------------------------------------------------------
# C3 — Remove currency symbols  (runs BEFORE C2 — see module docstring)
# ---------------------------------------------------------------------------

def _remove_currency_symbols(df: pd.DataFrame) -> pd.DataFrame:
    """
    C3 — Strip ₹, $, €, £ from object-dtype columns.

    Must execute before _handle_nulls (C2) so that int/float columns are
    symbol-free when pd.to_numeric() is called for median computation.

    Uses str.translate with a pre-built translation table — vectorised via
    pandas str accessor. Numeric and datetime columns are untouched.
    NaN values pass through unchanged (str accessor is NaN-safe).
    """
    df_out = df.copy()

    for col in df_out.columns:
        if not is_text_series(df_out[col]):
            continue
        df_out[col] = df_out[col].str.translate(_CURRENCY_SYMBOLS)

    return df_out


# ---------------------------------------------------------------------------
# C2 — Handle null values (schema-aware)
# ---------------------------------------------------------------------------

def _handle_nulls(df: pd.DataFrame, schema: dict) -> pd.DataFrame:
    """
    C2 — Fill or drop nulls based on schema rules per column.

    Precondition: C3 (_remove_currency_symbols) has already run.
    Currency symbols are stripped so pd.to_numeric() parses correctly.

    Rules:
        nullable=False           → drop rows where this column is null
        nullable=True + int      → fill NaN with column median
        nullable=True + float    → fill NaN with column median
        nullable=True + string   → leave as-is (missing stays missing / NULL)
        nullable=True + datetime → leave as-is (NaT handled after casting in C4)

    Optional text values are never imputed: filling a blank customer_email,
    shipping_city or return_requested with the column's most common value
    invents facts (e.g. another customer's email) that are then persisted to
    SQL Server and reported as real data.

    Columns missing from the DataFrame are silently skipped.
    All operations are vectorised — no row-level loops.
    """
    df_out = df.copy()

    for col, rules in schema.items():

        if not isinstance(rules, dict):
            continue

        if col not in df_out.columns:
            continue

        nullable: bool = rules.get("nullable", True)
        dtype:    str  = rules.get("dtype", "")

        # Non-nullable column → drop any row where this column is null
        if not nullable:
            df_out = df_out.dropna(subset=[col])
            continue

        # Nullable int/float: fill NaN with median.
        #
        # FIX: use numeric_series.fillna(median_val) instead of the original
        #      df_out[col].where(df_out[col].notna(), median_val).
        #
        # Why the original was wrong:
        #   df_out[col].where(..., median_val) operated on the object-dtype
        #   column and filled a float into string positions, producing a
        #   mixed-type object Series. Downstream C4 would eventually cast it
        #   correctly, but the intermediate state was semantically wrong and
        #   fragile. Filling directly on numeric_series gives a clean float
        #   Series from the start.
        #
        # This also relies on C3 having run first: with currency symbols
        # stripped, pd.to_numeric correctly parses "1299" instead of returning
        # NaN for "₹1299".
        if dtype in ("int", "float"):
            numeric_series = pd.to_numeric(df_out[col], errors="coerce")
            null_mask      = numeric_series.isna()

            if null_mask.any() and numeric_series.notna().any():
                median_val  = numeric_series.median()
                df_out[col] = numeric_series.fillna(median_val)

        # dtype == "string" → intentionally skipped: a genuinely missing optional
        # text value stays missing (see docstring).
        # dtype == "datetime" → intentionally skipped here.
        # NaT values remain; they are safe for nullable datetime columns and
        # will not cause issues in downstream analytics.

    return df_out


# ---------------------------------------------------------------------------
# C4 — Type casting + datetime parsing
# ---------------------------------------------------------------------------

def _cast_types(df: pd.DataFrame, schema: dict) -> pd.DataFrame:
    """
    C4 — Cast columns to schema-declared types.

    Strategy:
        int / float  → pd.to_numeric(errors="coerce")
                       Uncastable values become NaN — no crash.
        datetime     → pd.to_datetime(errors="coerce")
                       Unparseable values become NaT — no crash.

    String columns are left as object dtype (already correct post-normalization).
    Columns missing from the DataFrame are silently skipped.
    """
    df_out = df.copy()

    for col, rules in schema.items():

        if not isinstance(rules, dict):
            continue

        if col not in df_out.columns:
            continue

        dtype: str = rules.get("dtype", "")

        if dtype in ("int", "float"):
            df_out[col] = pd.to_numeric(df_out[col], errors="coerce")

        elif dtype == "datetime":
            df_out[col] = pd.to_datetime(df_out[col], errors="coerce")

    return df_out


# ---------------------------------------------------------------------------
# C4b — Drop rows whose REQUIRED values failed to cast
# ---------------------------------------------------------------------------

def _drop_unparseable_required(df: pd.DataFrame, schema: dict) -> tuple[pd.DataFrame, int]:
    """
    C4b — Drop rows where a non-nullable int/float/datetime column became
    NaN/NaT during C4's coerce-casting (e.g. order_date = "not a date").

    C2 only drops rows that were null BEFORE casting, so without this step an
    unparseable value in a required column would survive cleaning as NaT/NaN
    and crash later (SQL Server rejects NULL in NOT NULL columns).

    Returns the filtered DataFrame and the number of rows removed.
    """
    rows_before = len(df)
    required_typed = [
        col for col, rules in schema.items()
        if isinstance(rules, dict)
        and col in df.columns
        and not rules.get("nullable", True)
        and rules.get("dtype") in ("int", "float", "datetime")
    ]
    if required_typed:
        df = df.dropna(subset=required_typed)
    return df, rows_before - len(df)


# ---------------------------------------------------------------------------
# C5 — Remove invalid values (schema min/max bounds)
# ---------------------------------------------------------------------------

def _remove_invalid_values(df: pd.DataFrame, schema: dict) -> pd.DataFrame:
    """
    C5 — Drop rows that violate schema min / max constraints.

    Reads "min" and "max" keys directly from each column's schema rules.
    Applies to any column where these keys exist — fully schema-driven,
    no column names hardcoded.

    Precondition: C4 has already cast numeric columns to float/int dtype.
    pd.to_numeric() here is a safe no-op for already-numeric columns.

    Only rows where the value is present AND outside the bound are dropped.
    Null values are left in place (handled by C2; remaining nulls are benign).
    Columns missing from the DataFrame are silently skipped.
    """
    df_out = df.copy()

    for col, rules in schema.items():

        if not isinstance(rules, dict):
            continue

        if col not in df_out.columns:
            continue

        has_min = rules.get("min") is not None
        has_max = rules.get("max") is not None

        if not has_min and not has_max:
            continue

        numeric_col = pd.to_numeric(df_out[col], errors="coerce")

        if has_min:
            min_val    = rules["min"]
            valid_mask = numeric_col.isna() | (numeric_col >= min_val)
            df_out     = df_out[valid_mask]

        if has_max:
            # Re-read from the already-filtered df_out so the index is
            # consistent after the min filter may have removed rows.
            numeric_col = pd.to_numeric(df_out[col], errors="coerce")
            max_val     = rules["max"]
            valid_mask  = numeric_col.isna() | (numeric_col <= max_val)
            df_out      = df_out[valid_mask]

    return df_out


# ---------------------------------------------------------------------------
# C6 — Standardize categorical values
# ---------------------------------------------------------------------------

def _standardize_categoricals(df: pd.DataFrame, schema: dict) -> pd.DataFrame:
    """
    C6 — Lowercase and strip string columns declared in schema.

    Only targets columns where schema dtype == "string".
    Whitespace trimming was already applied by normalization.py (N2),
    but is repeated here as a safety net for values introduced during
    C2 (mode fill) or C3 (post-symbol-strip residuals).

    NaN values pass through unchanged (str accessor is NaN-safe).
    """
    df_out = df.copy()

    for col, rules in schema.items():

        if not isinstance(rules, dict):
            continue

        if col not in df_out.columns:
            continue

        if rules.get("dtype") != "string":
            continue

        if not is_text_series(df_out[col]):
            continue

        df_out[col] = df_out[col].str.lower().str.strip()

    return df_out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def clean_data(
    df: pd.DataFrame,
    schema: dict,
) -> tuple[pd.DataFrame, dict]:
    """
    Entry point for the cleaning pipeline.

    Executes all six steps. C3 runs before C2 — see module docstring for why.

        C1 → remove duplicates
        C3 → remove currency symbols   (before C2 so numeric parsing works)
        C2 → handle nulls (schema-aware)
        C4 → type casting + datetime parsing
        C4b→ drop rows whose required values failed to cast
        C5 → remove invalid values (min/max bounds)
        C6 → standardize categoricals

    Args:
        df     : Validated, normalized DataFrame (upstream preconditions met)
        schema : Flat per-column schema dict (cafe / gym / ecommerce format)

    Returns:
        Tuple of:
            [0] Cleaned DataFrame (new object — input is never mutated)
            [1] Report dict:
                {
                    "rows_before":          int,
                    "rows_after":           int,
                    "duplicates_removed":   int,
                    "invalid_rows_removed": int,
                }

    Never raises. All cleaning steps are safe on malformed/partial data.
    """
    if df is None or df.empty:
        return (
            df if df is not None else pd.DataFrame(),
            {"rows_before": 0, "rows_after": 0, "duplicates_removed": 0, "invalid_rows_removed": 0},
        )

    rows_before = len(df)

    # Work on a copy — never mutate the caller's DataFrame
    df_clean = df.copy()

    # C1 — Remove duplicates
    df_clean, duplicates_removed = _remove_duplicates(df_clean)

    # C3 — Strip currency symbols FIRST so numeric columns are clean
    #       before C2 attempts median computation via pd.to_numeric()
    df_clean = _remove_currency_symbols(df_clean)

    # C2 — Handle nulls (drop non-nullable / fill nullable with median or mode)
    df_clean = _handle_nulls(df_clean, schema)

    # C4 — Cast types (object → float/int/datetime)
    df_clean = _cast_types(df_clean, schema)

    # C4b — Drop rows whose required values could not be cast
    df_clean, invalid_rows_removed = _drop_unparseable_required(df_clean, schema)

    # C5 — Drop rows outside schema min/max bounds
    df_clean = _remove_invalid_values(df_clean, schema)

    # C6 — Lowercase + strip all string columns
    df_clean = _standardize_categoricals(df_clean, schema)

    # Reset index after row drops — downstream engines expect a clean 0-based index
    df_clean = df_clean.reset_index(drop=True)

    report = {
        "rows_before":          rows_before,
        "rows_after":           len(df_clean),
        "duplicates_removed":   duplicates_removed,
        "invalid_rows_removed": invalid_rows_removed,
    }

    return df_clean, report