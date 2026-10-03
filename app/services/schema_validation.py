"""
schema_validation.py
--------------------
3-Phase modular schema validation engine.

Architecture:
    validate_phase1  → Core checks   : existence, dtype, nulls, basic format
    validate_phase2  → Field checks  : range, regex, uniqueness, categoricals
    validate_phase3  → Business logic: cross-column, conditionals, date rules
    validate_schema  → Orchestrator  : runs phases, merges reports, returns final result

Report Contract (consistent across all phases):
    {
        "status"  : "pass" | "fail",
        "errors"  : [ { phase, column, rule, message } ],
        "warnings": [ { phase, column, rule, message } ],
        "summary" : { total_rows, total_columns, phases_run, error_count, warning_count }
    }
"""

from __future__ import annotations

import re                   # FIX #2: moved from inside validate_phase2 to top-level

import pandas as pd
from typing import Any


# ---------------------------------------------------------------------------
# Internal helpers — not exported
# ---------------------------------------------------------------------------

def _make_report(
    phase: int,
    df: pd.DataFrame,
    errors: list[dict],
    warnings: list[dict],
) -> dict:
    """
    Builds a standardised report dict for a single phase.
    Keeps report construction in one place so the contract never drifts.
    """
    return {
        "status": "fail" if errors else "pass",
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "total_rows": len(df),
            "total_columns": len(df.columns),
            "phases_run": [phase],
            "error_count": len(errors),
            "warning_count": len(warnings),
        },
    }


def _error(phase: int, column: str, rule: str, message: str) -> dict:
    """Constructs a typed error entry."""
    return {"phase": phase, "column": column, "rule": rule, "message": message}


def _warning(phase: int, column: str, rule: str, message: str) -> dict:
    """Constructs a typed warning entry."""
    return {"phase": phase, "column": column, "rule": rule, "message": message}


# ---------------------------------------------------------------------------
# Phase 1 — Core Schema Checks
# ---------------------------------------------------------------------------

def validate_phase1(df: pd.DataFrame, schema_config: dict) -> dict:
    """
    Phase 1: Core Schema Checks
    ----------------------------
    Runs four sequential sub-checks:
        1.1  Column existence     — required columns present in DataFrame
        1.2  Data type            — actual dtype matches expected dtype (with safe cast attempt)
        1.3  Null / missing       — non-nullable columns contain no nulls
        1.4  Basic format         — datetime columns parseable; strings are not blank

    Design decisions:
        - Reads flat schema format: { "col_name": { "dtype": ..., "required": ..., ... } }
          This matches cafe_schema, gym_schema, and ecommerce_schema directly.
        - dtype is read from rules["dtype"] — no separate "types" dict needed
        - Vectorised Pandas operations throughout (no row-level loops)
        - Missing optional columns are skipped, not errored
        - Dtype check uses a two-pass strategy: native check first, cast attempt second
          so "123" stored as object can still pass an int check with a warning
        - Blank string ("", "  ") treated as null-equivalent for string columns
        - Returns report dict — never raises; caller (orchestrator) decides on HTTPException
    """
    PHASE = 1
    errors:   list[dict] = []
    warnings: list[dict] = []

    # ------------------------------------------------------------------
    # Parse flat schema — single source of truth for all 4 sub-checks
    # Flat format: { "col_name": { "dtype": "string", "required": True, ... } }
    # Filter defensively — skip any non-dict values at the top level
    # ------------------------------------------------------------------

    columns_config: dict[str, dict] = {
        col: rules
        for col, rules in schema_config.items()
        if isinstance(rules, dict)
    }

    if not columns_config:
        warnings.append(_warning(PHASE, "_schema", "config",
            "schema_config has no column definitions — nothing to validate"))
        return _make_report(PHASE, df, errors, warnings)

    present_columns: set[str] = set(df.columns)

    # ------------------------------------------------------------------
    # 1.1  Column Existence Validation
    # ------------------------------------------------------------------
    # Only required columns are hard errors.
    # Missing optional columns emit a warning and are added to missing_columns
    # so all downstream sub-checks skip them cleanly.
    # ------------------------------------------------------------------

    missing_columns: set[str] = set()

    for col, rules in columns_config.items():
        if col not in present_columns:
            if rules.get("required", False):
                errors.append(_error(PHASE, col, "existence",
                    f"Required column '{col}' is missing from dataset"))
            else:
                warnings.append(_warning(PHASE, col, "existence",
                    f"Optional column '{col}' not found — skipping its checks"))
            missing_columns.add(col)  # track both required and optional misses

    # ------------------------------------------------------------------
    # 1.2  Data Type Validation
    # ------------------------------------------------------------------
    # dtype is read from rules.get("dtype") — lives on each column in flat schema.
    #
    # Two-pass strategy:
    #   Pass 1 — native dtype check via pandas dtype utilities
    #   Pass 2 — if pass 1 fails, attempt safe cast (e.g. object → int)
    #             success → warning (implicit cast needed in cleaning)
    #             failure → error  (genuinely wrong type, unrecoverable)
    # ------------------------------------------------------------------

    _DTYPE_CHECKERS: dict[str, Any] = {
        "string":   pd.api.types.is_string_dtype,
        "int":      pd.api.types.is_integer_dtype,
        "float":    pd.api.types.is_float_dtype,
        "bool":     pd.api.types.is_bool_dtype,
        "datetime": pd.api.types.is_datetime64_any_dtype,
    }

    for col, rules in columns_config.items():

        if col in missing_columns:
            continue  # existence already flagged — skip

        expected_dtype: str = rules.get("dtype") or ""

        if not expected_dtype:
            warnings.append(_warning(PHASE, col, "dtype",
                f"Column '{col}' has no dtype defined in schema — skipping type check"))
            continue

        if expected_dtype not in _DTYPE_CHECKERS:
            warnings.append(_warning(PHASE, col, "dtype",
                f"Column '{col}' has unknown dtype '{expected_dtype}' — skipping type check"))
            continue

        checker = _DTYPE_CHECKERS[expected_dtype]

        # Pass 1: native dtype check
        if checker(df[col]):
            continue  # matches natively — all good

        # Pass 2: safe cast attempt
        if expected_dtype == "datetime":
            parsed   = pd.to_datetime(df[col], errors="coerce")
            all_nat  = parsed.isna().all()
            some_nat = parsed.isna().any() and not all_nat

            if all_nat:
                errors.append(_error(PHASE, col, "dtype",
                    f"Column '{col}' cannot be parsed as datetime — all values are invalid"))
            elif some_nat:
                warnings.append(_warning(PHASE, col, "dtype",
                    f"Column '{col}' has mixed datetime values — some rows are unparseable"))
            else:
                warnings.append(_warning(PHASE, col, "dtype",
                    f"Column '{col}' is stored as object but castable to datetime "
                    f"— cleaning pipeline will handle conversion"))
            continue

        if expected_dtype in ("int", "float"):
            cast_attempt  = pd.to_numeric(df[col], errors="coerce")
            coerced_nulls = cast_attempt.isna().sum() - df[col].isna().sum()

            if coerced_nulls > 0:
                errors.append(_error(PHASE, col, "dtype",
                    f"Column '{col}' expected {expected_dtype} but contains "
                    f"{int(coerced_nulls)} non-numeric value(s)"))
            else:
                warnings.append(_warning(PHASE, col, "dtype",
                    f"Column '{col}' is castable to {expected_dtype} but stored as object "
                    f"— implicit cast needed in cleaning pipeline"))
            continue

        if expected_dtype == "string":
            # Everything is castable to string — warn, never error
            warnings.append(_warning(PHASE, col, "dtype",
                f"Column '{col}' is not stored as string/object dtype — will be cast automatically"))
            continue

        # Fallback: no cast path exists
        errors.append(_error(PHASE, col, "dtype",
            f"Column '{col}' expected {expected_dtype}, got {df[col].dtype}"))

    # ------------------------------------------------------------------
    # 1.3  Null / Missing Value Validation
    # ------------------------------------------------------------------
    # nullable=False → any NaN/None or blank string is a hard error
    # nullable=True  → nulls allowed, but warn if null rate exceeds 50%
    #
    # Blank strings ("", "  ") treated as null-equivalent on object columns —
    # common in real-world CSVs exported from spreadsheets.
    # ------------------------------------------------------------------

    NULL_RATE_WARNING_THRESHOLD = 0.50

    for col, rules in columns_config.items():

        if col in missing_columns:
            continue

        nullable: bool = rules.get("nullable", True)

        null_mask = df[col].isna()
        if pd.api.types.is_string_dtype(df[col]) or df[col].dtype == object:
            blank_mask = df[col].astype(str).str.strip() == ""
            null_mask  = null_mask | blank_mask

        null_count: int   = int(null_mask.sum())
        null_rate:  float = null_count / len(df) if len(df) > 0 else 0.0

        if not nullable and null_count > 0:
            errors.append(_error(PHASE, col, "nullable",
                f"Column '{col}' is non-nullable but contains "
                f"{null_count} null/blank value(s) ({null_rate:.1%} of rows)"))

        elif nullable and null_rate > NULL_RATE_WARNING_THRESHOLD:
            warnings.append(_warning(PHASE, col, "nullable",
                f"Column '{col}' has a high null rate: "
                f"{null_rate:.1%} ({null_count}/{len(df)} rows)"))

    # ------------------------------------------------------------------
    # 1.4  Basic Format Validation
    # ------------------------------------------------------------------
    # Lightweight row-level sanity checks:
    #   - datetime: surface partial parse failures (some rows bad, not all)
    #   - string non-nullable: catch whitespace-only values not caught by 1.3
    #
    # 1.2 already catches complete type failures (all values unparseable).
    # 1.4 is for partial failures that slip through the dtype check.
    # ------------------------------------------------------------------

    for col, rules in columns_config.items():

        if col in missing_columns:
            continue

        expected_dtype = rules.get("dtype", "")

        if expected_dtype == "datetime":
            if not pd.api.types.is_datetime64_any_dtype(df[col]):
                parsed            = pd.to_datetime(df[col], errors="coerce")
                unparseable_count = int(parsed.isna().sum() - df[col].isna().sum())

                if unparseable_count > 0:
                    warnings.append(_warning(PHASE, col, "format",
                        f"Column '{col}' has {unparseable_count} row(s) with "
                        f"unparseable date format"))

        if expected_dtype == "string" and not rules.get("nullable", True):
            whitespace_only = df[col].astype(str).str.strip().eq("") & df[col].notna()
            ws_count        = int(whitespace_only.sum())
            if ws_count > 0:
                warnings.append(_warning(PHASE, col, "format",
                    f"Column '{col}' has {ws_count} whitespace-only string(s) "
                    f"— treated as blank"))

    # FIX #1: Dead code block (entire old Phase 1 implementation) that previously
    # existed below this return has been removed. It used the old nested schema
    # format (schema_config.get("columns", {})) which is incompatible with the
    # current flat schema structure used by cafe, gym, and ecommerce schemas.
    return _make_report(PHASE, df, errors, warnings)


# ---------------------------------------------------------------------------
# Phase 2 — Field-Level Advanced Validation
# ---------------------------------------------------------------------------

def validate_phase2(df: pd.DataFrame, schema_config: dict) -> dict:
    """
    Phase 2: Field-Level Advanced Validation
    -----------------------------------------
    Runs four sequential sub-checks:
        2.1  Range validation   — min / max constraints on numeric columns
        2.2  Regex validation   — pattern matching on string columns
        2.3  Uniqueness         — duplicate value detection per column
        2.4  Allowed values     — categorical / enum enforcement

    Design decisions:
        - Reads directly from flat per-column schema — same structure as cafe/gym/ecommerce
        - Each sub-check reads its own key (min, max, regex, unique, allowed_values)
        - Columns not present in the DataFrame are skipped — Phase 1 already flagged them
        - All operations are vectorised — no row-level loops
        - Null values are excluded before range/regex/allowed checks so nullable columns
          don't produce false positives on their empty rows
        - Regex failures report the violation count, not individual values (privacy + brevity)
        - allowed_values comparison is case-insensitive via .str.lower() normalisation
    """
    PHASE = 2
    errors:   list[dict] = []
    warnings: list[dict] = []

    present_columns: set[str] = set(df.columns)

    # Phase 2 reads rules directly from the flat schema dict
    columns_config: dict[str, dict] = {
        col: rules
        for col, rules in schema_config.items()
        if isinstance(rules, dict)
    }

    # ------------------------------------------------------------------
    # 2.1  Range Validation
    # ------------------------------------------------------------------

    for col, rules in columns_config.items():

        if col not in present_columns:
            continue

        has_min = rules.get("min") is not None
        has_max = rules.get("max") is not None

        if not has_min and not has_max:
            continue

        numeric_series = pd.to_numeric(df[col], errors="coerce")
        non_null = numeric_series.dropna()

        if non_null.empty:
            continue

        if has_min:
            min_val = rules["min"]
            violations = (non_null < min_val).sum()
            if violations > 0:
                errors.append(_error(PHASE, col, "min",
                    f"Column '{col}' has {violations} value(s) below minimum allowed ({min_val})"))

        if has_max:
            max_val = rules["max"]
            violations = (non_null > max_val).sum()
            if violations > 0:
                errors.append(_error(PHASE, col, "max",
                    f"Column '{col}' has {violations} value(s) above maximum allowed ({max_val})"))

    # ------------------------------------------------------------------
    # 2.2  Regex Validation
    # ------------------------------------------------------------------
    # FIX #2: `import re` removed from here — now lives at top of file.

    for col, rules in columns_config.items():

        if col not in present_columns:
            continue

        pattern = rules.get("regex")
        if not pattern:
            continue

        try:
            re.compile(pattern)
        except re.error as exc:
            warnings.append(_warning(PHASE, col, "regex",
                f"Column '{col}' has an invalid regex pattern in schema: {exc}"))
            continue

        non_null_series = df[col].dropna().astype(str)

        if non_null_series.empty:
            continue

        match_mask = non_null_series.str.match(pattern, na=False)
        violations = (~match_mask).sum()

        if violations > 0:
            errors.append(_error(PHASE, col, "regex",
                f"Column '{col}' has {violations} value(s) that do not match the required pattern"))

    # ------------------------------------------------------------------
    # 2.3  Uniqueness Validation
    # ------------------------------------------------------------------

    for col, rules in columns_config.items():

        if col not in present_columns:
            continue

        if not rules.get("unique", False):
            continue

        non_null_series = df[col].dropna()

        if non_null_series.empty:
            continue

        duplicate_mask   = non_null_series.duplicated(keep=False)
        duplicate_count  = duplicate_mask.sum()

        if duplicate_count > 0:
            duplicate_values = non_null_series[duplicate_mask].unique().tolist()
            display_values = duplicate_values[:5]
            suffix = f" ... and {len(duplicate_values) - 5} more" if len(duplicate_values) > 5 else ""

            errors.append(_error(PHASE, col, "unique",
                f"Column '{col}' must be unique but has {duplicate_count} duplicate value(s): "
                f"{display_values}{suffix}"))

    # ------------------------------------------------------------------
    # 2.4  Allowed Values (Categorical) Validation
    # ------------------------------------------------------------------

    for col, rules in columns_config.items():

        if col not in present_columns:
            continue

        allowed_values: list | None = rules.get("allowed_values")
        if not allowed_values:
            continue

        allowed_set = {str(v).lower() for v in allowed_values}
        non_null_series = df[col].dropna().astype(str).str.strip().str.lower()

        if non_null_series.empty:
            continue

        invalid_mask   = ~non_null_series.isin(allowed_set)
        invalid_count  = invalid_mask.sum()

        if invalid_count > 0:
            invalid_values = non_null_series[invalid_mask].unique().tolist()
            display_values = invalid_values[:10]
            suffix = f" ... and {len(invalid_values) - 10} more" if len(invalid_values) > 10 else ""

            errors.append(_error(PHASE, col, "allowed_values",
                f"Column '{col}' has {invalid_count} value(s) not in allowed list "
                f"{allowed_values}. Found: {display_values}{suffix}"))

    return _make_report(PHASE, df, errors, warnings)


# ---------------------------------------------------------------------------
# Phase 3 — Business Logic & Cross-Column Validation
# ---------------------------------------------------------------------------

def validate_phase3(df: pd.DataFrame, schema_config: dict) -> dict:
    """
    Phase 3: Business Logic & Cross-Column Validation
    ---------------------------------------------------
    Executes declarative cross-column rules defined under the "_cross_rules"
    top-level key in schema_config.

    Supported rule types:
        date_order      → col_a must be strictly before col_b (both datetime)
        compare         → col_a {op} col_b for numeric columns
        formula         → result_col ≈ df.eval(expression) within tolerance %
        conditional     → if if_col is not null → then_col must also not be null
        date_not_future → col must not contain dates after today

    Schema contract example:
        "_cross_rules": [
            {"type": "date_order",      "col_a": "start_date",   "col_b": "end_date"},
            {"type": "formula",         "result_col": "total",   "expression": "price * qty"},
            {"type": "conditional",     "if_col": "trainer",     "then_col": "fitness_goal"},
            {"type": "date_not_future", "col": "order_date"},
            {"type": "compare",         "col_a": "unit_price",   "op": "<=", "col_b": "total_amount"},
        ]
    """
    PHASE = 3
    errors:   list[dict] = []
    warnings: list[dict] = []

    cross_rules: list[dict] = schema_config.get("_cross_rules", [])

    if not cross_rules:
        warnings.append(_warning(PHASE, "_cross_rules", "config",
            "No '_cross_rules' key defined in schema — Phase 3 skipped"))
        return _make_report(PHASE, df, errors, warnings)

    present_columns: set[str] = set(df.columns)

    def _rule_date_order(rule: dict) -> None:
        col_a = rule.get("col_a")
        col_b = rule.get("col_b")
        label = f"{col_a} → {col_b}"

        if not col_a or not col_b:
            warnings.append(_warning(PHASE, label, "date_order",
                "Rule 'date_order' missing col_a or col_b — skipping"))
            return

        if col_a not in present_columns or col_b not in present_columns:
            warnings.append(_warning(PHASE, label, "date_order",
                f"Skipping — column(s) not in dataset: '{col_a}', '{col_b}'"))
            return

        a = pd.to_datetime(df[col_a], errors="coerce")
        b = pd.to_datetime(df[col_b], errors="coerce")

        both_valid = a.notna() & b.notna()
        if not both_valid.any():
            warnings.append(_warning(PHASE, label, "date_order",
                f"No rows with valid dates in both '{col_a}' and '{col_b}' — skipping"))
            return

        violations = int((a[both_valid] >= b[both_valid]).sum())
        if violations > 0:
            msg = rule.get("message", f"'{col_a}' must be before '{col_b}'")
            errors.append(_error(PHASE, label, "date_order",
                f"{msg} — {violations} row(s) violate this constraint"))

    def _rule_compare(rule: dict) -> None:
        col_a = rule.get("col_a")
        col_b = rule.get("col_b")
        op    = rule.get("op")
        label = f"{col_a} {op} {col_b}"

        _VALID_OPS = {"<", "<=", ">", ">=", "==", "!="}

        if not col_a or not col_b or not op:
            warnings.append(_warning(PHASE, label, "compare",
                "Rule 'compare' missing col_a, col_b, or op — skipping"))
            return

        if op not in _VALID_OPS:
            warnings.append(_warning(PHASE, label, "compare",
                f"Unsupported op '{op}' in rule 'compare' — valid ops: {sorted(_VALID_OPS)}"))
            return

        if col_a not in present_columns or col_b not in present_columns:
            warnings.append(_warning(PHASE, label, "compare",
                f"Skipping — column(s) not in dataset: '{col_a}', '{col_b}'"))
            return

        a = pd.to_numeric(df[col_a], errors="coerce")
        b = pd.to_numeric(df[col_b], errors="coerce")

        both_valid = a.notna() & b.notna()
        if not both_valid.any():
            return

        _OPS = {
            "<":  lambda x, y: x < y,
            "<=": lambda x, y: x <= y,
            ">":  lambda x, y: x > y,
            ">=": lambda x, y: x >= y,
            "==": lambda x, y: x == y,
            "!=": lambda x, y: x != y,
        }
        violations = int((~_OPS[op](a[both_valid], b[both_valid])).sum())
        if violations > 0:
            msg = rule.get("message", f"'{col_a}' {op} '{col_b}' constraint violated")
            errors.append(_error(PHASE, label, "compare",
                f"{msg} — {violations} row(s) violate this constraint"))

    def _rule_formula(rule: dict) -> None:
        result_col = rule.get("result_col")
        expression = rule.get("expression")
        tolerance  = rule.get("tolerance", 0.01)
        label      = f"{result_col} ≈ {expression}"

        if not result_col or not expression:
            warnings.append(_warning(PHASE, label, "formula",
                "Rule 'formula' missing result_col or expression — skipping"))
            return

        if result_col not in present_columns:
            warnings.append(_warning(PHASE, label, "formula",
                f"result_col '{result_col}' not in dataset — skipping"))
            return

        try:
            eval_result = df.eval(expression)
        except Exception as exc:
            warnings.append(_warning(PHASE, label, "formula",
                f"Cannot evaluate expression '{expression}': {exc} — skipping"))
            return

        # df.eval() return type is broad: Scalar | Series | DataFrame | ndarray.
        # Narrow to pd.Series explicitly so pd.to_numeric() receives the correct
        # type and Pylance overload resolution succeeds.
        if isinstance(eval_result, pd.DataFrame):
            # Ambiguous multi-column result — squeeze to Series if single column,
            # otherwise bail: we cannot determine which column was intended.
            squeezed = eval_result.squeeze(axis=1)
            if not isinstance(squeezed, pd.Series):
                warnings.append(_warning(PHASE, label, "formula",
                    f"Expression '{expression}' produced a multi-column DataFrame "
                    f"— cannot evaluate formula rule, skipping"))
                return
            computed_series: pd.Series = squeezed
        elif isinstance(eval_result, pd.Series):
            computed_series = eval_result
        else:
            # Scalar / ndarray / other — not usable for row-level comparison
            warnings.append(_warning(PHASE, label, "formula",
                f"Expression '{expression}' did not produce a Series "
                f"(got {type(eval_result).__name__}) — skipping"))
            return

        actual   = pd.to_numeric(df[result_col], errors="coerce")
        computed = pd.to_numeric(computed_series, errors="coerce")

        both_valid = actual.notna() & computed.notna()
        if not both_valid.any():
            return

        denominator        = computed[both_valid].abs().clip(lower=1)
        relative_deviation = (actual[both_valid] - computed[both_valid]).abs() / denominator
        violations         = int((relative_deviation > tolerance).sum())

        if violations > 0:
            msg = rule.get("message",
                f"'{result_col}' should equal {expression} "
                f"(within {tolerance * 100:.1f}% tolerance)")
            errors.append(_error(PHASE, label, "formula",
                f"{msg} — {violations} row(s) deviate beyond tolerance"))

    def _rule_conditional(rule: dict) -> None:
        if_col   = rule.get("if_col")
        then_col = rule.get("then_col")
        label    = f"if {if_col} → {then_col}"

        if not if_col or not then_col:
            warnings.append(_warning(PHASE, label, "conditional",
                "Rule 'conditional' missing if_col or then_col — skipping"))
            return

        if if_col not in present_columns or then_col not in present_columns:
            warnings.append(_warning(PHASE, label, "conditional",
                f"Skipping — column(s) not in dataset: '{if_col}', '{then_col}'"))
            return

        if_has_value = df[if_col].notna()
        if pd.api.types.is_string_dtype(df[if_col]) or df[if_col].dtype == object:
            if_has_value = if_has_value & (df[if_col].astype(str).str.strip() != "")

        then_missing = df[then_col].isna()
        if pd.api.types.is_string_dtype(df[then_col]) or df[then_col].dtype == object:
            then_missing = then_missing | (df[then_col].astype(str).str.strip() == "")

        violations = int((if_has_value & then_missing).sum())
        if violations > 0:
            msg = rule.get("message",
                f"If '{if_col}' is set, '{then_col}' must also be set")
            errors.append(_error(PHASE, label, "conditional",
                f"{msg} — {violations} row(s) violate this constraint"))

    def _rule_date_not_future(rule: dict) -> None:
        col   = rule.get("col")
        label = col or "_unknown"

        if not col:
            warnings.append(_warning(PHASE, label, "date_not_future",
                "Rule 'date_not_future' missing col — skipping"))
            return

        if col not in present_columns:
            warnings.append(_warning(PHASE, label, "date_not_future",
                f"Column '{col}' not in dataset — skipping"))
            return

        parsed = pd.to_datetime(df[col], errors="coerce")
        today  = pd.Timestamp.now().normalize()

        valid = parsed.notna()
        if not valid.any():
            return

        violations = int((parsed[valid] > today).sum())
        if violations > 0:
            msg = rule.get("message", f"'{col}' must not contain future dates")
            errors.append(_error(PHASE, label, "date_not_future",
                f"{msg} — {violations} row(s) have future dates"))

    # Rule handler registry — only place to register new rule types
    _RULE_HANDLERS: dict[str, Any] = {
        "date_order":      _rule_date_order,
        "compare":         _rule_compare,
        "formula":         _rule_formula,
        "conditional":     _rule_conditional,
        "date_not_future": _rule_date_not_future,
    }

    for rule in cross_rules:
        if not isinstance(rule, dict):
            warnings.append(_warning(PHASE, "_rule", "config",
                f"Cross-rule entry is not a dict — skipping: {rule}"))
            continue

        rule_type = rule.get("type")

        if not rule_type:
            warnings.append(_warning(PHASE, "_rule", "type",
                f"Cross-rule missing 'type' field — skipping: {rule}"))
            continue

        handler = _RULE_HANDLERS.get(rule_type)
        if not handler:
            warnings.append(_warning(PHASE, "_rule", "type",
                f"Unknown rule type '{rule_type}' — skipping. "
                f"Registered types: {list(_RULE_HANDLERS.keys())}"))
            continue

        handler(rule)

    return _make_report(PHASE, df, errors, warnings)


# ---------------------------------------------------------------------------
# Orchestrator — validate_schema
# ---------------------------------------------------------------------------

def validate_schema(
    df: pd.DataFrame,
    schema_config: dict,
    fail_fast: bool = False,
) -> dict:
    """
    Orchestrator: runs all active phases and merges their reports.

    Args:
        df            : Input DataFrame (post-structural validation)
        schema_config : Schema rules dict (flat per-column format)
        fail_fast     : If True, halt after first phase that produces errors.
                        If False (default), run all phases and aggregate all issues.

    Returns:
        Merged report dict with unified status, errors, warnings, and summary.
    """
    if df is None or df.empty:
        return {
            "status": "fail",
            "errors": [_error(0, "_dataframe", "empty", "DataFrame is empty — cannot run schema validation")],
            "warnings": [],
            "summary": {
                "total_rows": 0,
                "total_columns": 0,
                "phases_run": [],
                "error_count": 1,
                "warning_count": 0,
            },
        }

    # Phase registry — ordered, extensible
    phase_runners = [
        validate_phase1,
        validate_phase2,
        validate_phase3,
    ]

    merged_errors:   list[dict] = []
    merged_warnings: list[dict] = []
    phases_run:      list[int]  = []

    for runner in phase_runners:
        report = runner(df, schema_config)
        phases_run.extend(report["summary"]["phases_run"])
        merged_errors.extend(report["errors"])
        merged_warnings.extend(report["warnings"])

        if fail_fast and report["errors"]:
            break

    return {
        "status": "fail" if merged_errors else "pass",
        "errors": merged_errors,
        "warnings": merged_warnings,
        "summary": {
            "total_rows": len(df),
            "total_columns": len(df.columns),
            "phases_run": phases_run,
            "error_count": len(merged_errors),
            "warning_count": len(merged_warnings),
        },
    }