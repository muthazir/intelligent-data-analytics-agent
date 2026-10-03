"""
normalization.py
----------------
Light, non-destructive normalization layer.

Position in pipeline:
    Upload → [Normalization] → Structural Validation → Schema Validation → Cleaning → ...

Contract:
    - Input : raw pd.DataFrame straight from pd.read_csv / pd.read_excel
    - Output: normalized pd.DataFrame — same shape, no rows dropped, no dtypes changed
    - Never raises — all issues are surfaced via NormalizationResult.warnings

Normalization steps (in execution order):
    N1  Column name normalization     strip + camelCase split + lowercase + separators→underscores
    N2  String value trimming         strip leading/trailing whitespace
    N3  Empty string → NaN            "" and "   " become pd.NA
    N4  Invisible character removal   zero-width spaces + hidden unicode control chars
    N5  Numeric string cleanup        "1,000.50" → "1000.50" (commas only, no dtype cast)

Design rules:
    - All operations are vectorised (pandas Series / str accessor methods)
    - No row-level Python loops anywhere in this module
    - Dtype of each column is preserved — this is NOT a type-casting layer
    - Input DataFrame is never mutated — a copy is made on entry
    - NormalizationResult tracks a change log for auditability
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from app.services.dtypes import is_text_series


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class NormalizationResult:
    """
    Returned by normalize_dataframe alongside the cleaned DataFrame.

    Attributes:
        df              : Normalized copy of the input DataFrame
        column_renames  : Mapping of original column name → normalized name
                          (only includes columns that actually changed)
        changes         : Human-readable log of every transformation applied,
                          one entry per step that produced at least one change
        warnings        : Non-fatal issues (e.g. column name collision after lowercasing)
    """
    df:             pd.DataFrame
    column_renames: dict[str, str] = field(default_factory=dict)
    changes:        list[str]      = field(default_factory=list)
    warnings:       list[str]      = field(default_factory=list)


# ---------------------------------------------------------------------------
# Module-level compiled patterns — compiled once, reused on every call
# ---------------------------------------------------------------------------

# Invisible / control characters:
#   - Named invisibles : zero-width space, ZWNJ, ZWJ, word joiner, BOM, soft hyphen
#   - C0 control chars : U+0000-U+0008, U+000B, U+000C, U+000E-U+001F (excl. \t \n \r)
#   - C1 control chars : U+007F-U+009F
_INVISIBLE_CHAR_RE = re.compile(
    r"[\u200b\u200c\u200d\u2060\ufeff\u00ad"
    r"\u0000-\u0008\u000b\u000c\u000e-\u001f"
    r"\u007f-\u009f]"
)

# Thousands-separator comma: only matches a comma that sits between two digit characters.
# Lookahead / lookbehind ensure "a,b" or free-text commas are not stripped.
_NUMERIC_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")

# Column-name word boundaries (N1):
#   lower/digit followed by upper  -> "customerEmail" | "order2Date"
#   acronym followed by a word     -> "XMLFile" -> "XML_File" ("OrderID" stays one acronym)
_CAMEL_LOWER_UPPER_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_ACRONYM_RE     = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NON_WORD_RE          = re.compile(r"[^\w]+")


# ---------------------------------------------------------------------------
# Step N1 — Column name normalization
# ---------------------------------------------------------------------------

def _normalize_column_names(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, str], list[str], list[str]]:
    """
    N1 — Normalize every column name:
        1. Cast to str  (handles edge cases like integer column names)
        2. Strip leading/trailing whitespace
        3. Split camelCase / PascalCase words, keeping acronyms together
           ("customerEmail" -> "customer_Email", "OrderID" -> "Order_ID")
        4. Convert to lowercase
        5. Replace every run of non-word characters (whitespace, "-", ".",
           "/", brackets, ...) with a single underscore
        6. Collapse consecutive underscores  ("a__b" -> "a_b")
        7. Strip leading/trailing underscores left over from step 5/6
        8. Fall back to "unnamed_column" for any name that is empty after all steps

    Collision guard:
        If two originals normalize to the same name (e.g. "Price " and "price"),
        a warning is emitted and the later column gets a numeric suffix (_2, _3 ...).
        Structural validation will then catch the semantic duplicate as expected.
    """
    original_names: list[str] = list(df.columns)
    normalized:     list[str] = []
    warnings:       list[str] = []
    seen:           dict[str, int] = {}

    for name in original_names:
        new_name = str(name).strip()
        new_name = _CAMEL_LOWER_UPPER_RE.sub("_", new_name)
        new_name = _CAMEL_ACRONYM_RE.sub("_", new_name)
        new_name = new_name.lower()
        new_name = _NON_WORD_RE.sub("_", new_name)
        new_name = re.sub(r"_+",  "_", new_name)
        new_name = new_name.strip("_")

        if not new_name:
            new_name = "unnamed_column"

        if new_name in seen:
            seen[new_name] += 1
            suffixed = f"{new_name}_{seen[new_name]}"
            warnings.append(
                f"Column name collision after normalization: "
                f"'{name}' -> '{new_name}' already exists — renamed to '{suffixed}'"
            )
            new_name = suffixed
        else:
            seen[new_name] = 1

        normalized.append(new_name)

    renames: dict[str, str] = {
        orig: norm
        for orig, norm in zip(original_names, normalized)
        if orig != norm
    }

    change_log: list[str] = []
    if renames:
        change_log.append(
            f"N1 — Renamed {len(renames)} column(s): "
            + ", ".join(f"'{k}' -> '{v}'" for k, v in renames.items())
        )

    df_out = df.copy()
    df_out.columns = normalized
    return df_out, renames, change_log, warnings


# ---------------------------------------------------------------------------
# Step N2 — Trim string values
# ---------------------------------------------------------------------------

def _trim_string_values(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    N2 — Strip leading/trailing whitespace from every object-dtype column.

    Uses pd.Series.str.strip() — vectorised, NaN-safe (NaN passes through unchanged).
    Numeric, datetime, and boolean columns are untouched.
    """
    df_out   = df.copy()
    affected = []

    for col in df_out.columns:
        if not is_text_series(df_out[col]):
            continue

        before      = df_out[col].copy()
        df_out[col] = df_out[col].str.strip()

        changed = (before != df_out[col]) & before.notna() & df_out[col].notna()
        if changed.any():
            affected.append(col)

    change_log = (
        [f"N2 — Trimmed whitespace in {len(affected)} column(s): {affected}"]
        if affected else []
    )
    return df_out, change_log


# ---------------------------------------------------------------------------
# Step N3 — Empty strings -> NaN
# ---------------------------------------------------------------------------

def _empty_strings_to_null(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    N3 — Convert empty or whitespace-only strings to pd.NA.

    Applies only to object-dtype columns (post-trim from N2).
    After this step, schema validation's null checks work uniformly —
    no validator needs to special-case the empty-string pattern.

    Uses a vectorised boolean mask; no apply/lambda.
    """
    df_out = df.copy()
    total  = 0

    for col in df_out.columns:
        if not is_text_series(df_out[col]):
            continue

        blank_mask = df_out[col].str.strip().eq("") & df_out[col].notna()
        count      = int(blank_mask.sum())

        if count > 0:
            df_out.loc[blank_mask, col] = pd.NA
            total += count

    change_log = (
        [f"N3 — Converted {total} empty/whitespace-only string(s) to NaN"]
        if total > 0 else []
    )
    return df_out, change_log


# ---------------------------------------------------------------------------
# Step N4 — Remove invisible / control characters
# ---------------------------------------------------------------------------

def _remove_invisible_characters(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    N4 — Strip invisible and zero-width unicode characters from string columns.

    Targets:
        - Zero-width space (U+200B), ZWNJ, ZWJ, word joiner (U+2060)
        - BOM (U+FEFF), soft hyphen (U+00AD)
        - C0 control characters (U+0000-U+001F, excluding tab/LF/CR)
        - C1 control characters (U+007F-U+009F)

    Uses the module-level pre-compiled _INVISIBLE_CHAR_RE via str.replace —
    vectorised and NaN-safe. Does not alter numeric or datetime columns.
    """
    df_out   = df.copy()
    affected = []

    for col in df_out.columns:
        if not is_text_series(df_out[col]):
            continue

        before      = df_out[col].copy()
        df_out[col] = df_out[col].str.replace(_INVISIBLE_CHAR_RE, "", regex=True)

        changed = (before != df_out[col]) & before.notna()
        if changed.any():
            affected.append(col)

    change_log = (
        [f"N4 — Removed invisible/control characters from {len(affected)} column(s): {affected}"]
        if affected else []
    )
    return df_out, change_log


# ---------------------------------------------------------------------------
# Step N5 — Numeric string cleanup (comma removal)
# ---------------------------------------------------------------------------

def _clean_numeric_strings(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    N5 — Remove thousands-separator commas from numeric-like string columns.

    Heuristic gate (prevents mangling address/free-text columns):
        A column is treated as "numeric-like" only if >= 60% of its non-null
        values can be parsed as float after stripping inter-digit commas.

    Transformation applied:  "1,000,500.25" -> "1000500.25"

    Only commas that sit between two digit characters are removed
    (via _NUMERIC_COMMA_RE lookahead/lookbehind). Currency symbols
    (Rs, $, EUR) are deliberately NOT removed — that is cleaning-pipeline work.

    Dtype is preserved — pd.to_numeric() is never called on the column itself.
    """
    df_out   = df.copy()
    affected = []

    NUMERIC_LIKE_THRESHOLD = 0.60

    for col in df_out.columns:
        if not is_text_series(df_out[col]):
            continue

        non_null = df_out[col].dropna()
        if non_null.empty:
            continue

        # Quick gate: does the column contain any comma-between-digits pattern?
        has_commas = non_null.str.contains(r"\d,\d", regex=True, na=False)
        if not has_commas.any():
            continue

        # Heuristic: strip commas then try numeric parse — check parse success rate
        stripped   = non_null.str.replace(_NUMERIC_COMMA_RE, "", regex=True)
        parseable  = pd.to_numeric(stripped, errors="coerce").notna()
        parse_rate = parseable.sum() / len(non_null)

        if parse_rate < NUMERIC_LIKE_THRESHOLD:
            continue  # column is not primarily numeric — leave untouched

        before      = df_out[col].copy()
        df_out[col] = df_out[col].str.replace(_NUMERIC_COMMA_RE, "", regex=True)

        changed = (before != df_out[col]) & before.notna()
        if changed.any():
            affected.append(col)

    change_log = (
        [f"N5 — Removed thousands-separator commas from {len(affected)} "
         f"numeric-like column(s): {affected}"]
        if affected else []
    )
    return df_out, change_log


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_dataframe(df: pd.DataFrame) -> NormalizationResult:
    """
    Entry point for the normalization layer.

    Executes all five steps in sequence:
        N1 -> column names
        N2 -> trim strings
        N3 -> empty -> NaN
        N4 -> invisible characters
        N5 -> numeric comma cleanup

    Args:
        df : Raw DataFrame from pd.read_csv() or pd.read_excel()

    Returns:
        NormalizationResult:
            .df             : normalized DataFrame (new object — input is untouched)
            .column_renames : { original_name -> normalized_name } for changed cols
            .changes        : ordered list of human-readable change descriptions
            .warnings       : non-fatal issues (column collisions, etc.)

    Never raises. All issues are captured in .warnings.
    The caller (upload route) decides whether to surface warnings to the client.
    """
    if df is None or df.empty:
        return NormalizationResult(
            df=df if df is not None else pd.DataFrame(),
            warnings=["normalize_dataframe received an empty DataFrame — nothing to normalize"],
        )

    all_changes:    list[str]       = []
    all_warnings:   list[str]       = []
    column_renames: dict[str, str]  = {}

    # N1 — Column names
    df, renames, log, warns = _normalize_column_names(df)
    column_renames.update(renames)
    all_changes.extend(log)
    all_warnings.extend(warns)

    # N2 — Trim strings
    df, log = _trim_string_values(df)
    all_changes.extend(log)

    # N3 — Empty -> NaN
    df, log = _empty_strings_to_null(df)
    all_changes.extend(log)

    # N4 — Invisible characters
    df, log = _remove_invisible_characters(df)
    all_changes.extend(log)

    # N5 — Numeric commas
    df, log = _clean_numeric_strings(df)
    all_changes.extend(log)

    return NormalizationResult(
        df=df,
        column_renames=column_renames,
        changes=all_changes,
        warnings=all_warnings,
    )