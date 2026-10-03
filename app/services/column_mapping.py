"""
column_mapping.py
-----------------
Maps synonym column names onto a schema's canonical column names, using the
per-column "aliases" lists declared in app/schema/*_schema.py.

Position in pipeline:
    Upload → Structural Validation → Normalization → [Column Mapping]
    → Pre-validation cleaning → Schema Validation → Cleaning → ...

Rules (deliberately conservative — a wrong rename is worse than none):
    - Names are compared after normalization (lowercase snake_case), so
      "Qty", "QTY" and "qty" all match the alias "qty".
    - A canonical column that is already present is never replaced.
    - An alias that is itself a canonical column of the same schema is ignored.
    - If two aliases match one canonical column, or one alias column matches
      two canonical columns, nothing is renamed and a warning is returned.

Public API:
    resolve_aliases(columns, schema) -> (renames, warnings)   # names only
    apply_aliases(df, schema)        -> (df, renames, warnings)
"""

from __future__ import annotations

from collections import defaultdict

import pandas as pd

from app.schema import column_rules


def resolve_aliases(columns, schema: dict) -> tuple[dict[str, str], list[str]]:
    """
    Decide which present columns should be renamed to which canonical names.

    Returns ({present_alias_column: canonical_column}, [warning, ...]).
    Works on column names only, so schema detection can score every schema
    cheaply without copying the DataFrame.
    """
    present = set(columns)
    rules_by_col = column_rules(schema)
    canonical = set(rules_by_col)

    candidates: dict[str, list[str]] = {}
    for col, rules in rules_by_col.items():
        if col in present:
            continue
        matches = [
            alias for alias in rules.get("aliases", [])
            if alias in present and alias not in canonical
        ]
        if matches:
            candidates[col] = matches

    warnings: list[str] = []
    claimed_by: dict[str, list[str]] = defaultdict(list)
    for col, matches in candidates.items():
        if len(matches) > 1:
            warnings.append(
                f"Column '{col}' could come from several columns {matches} — "
                f"none was renamed; rename one of them to '{col}'"
            )
            continue
        claimed_by[matches[0]].append(col)

    renames: dict[str, str] = {}
    for alias, cols in claimed_by.items():
        if len(cols) > 1:
            warnings.append(
                f"Column '{alias}' could mean any of {sorted(cols)} — it was not renamed"
            )
            continue
        renames[alias] = cols[0]

    return renames, warnings


def apply_aliases(df: pd.DataFrame, schema: dict) -> tuple[pd.DataFrame, dict[str, str], list[str]]:
    """Rename alias columns to their canonical names (see resolve_aliases)."""
    renames, warnings = resolve_aliases(df.columns, schema)
    if renames:
        df = df.rename(columns=renames)
    return df, renames, warnings
