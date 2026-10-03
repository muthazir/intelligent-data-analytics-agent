"""
schema_detection.py
-------------------
Automatic schema detection for schema_type=auto uploads, and "this looks like
another schema" hints when a manually chosen schema does not fit.

A schema is detected only when ALL of its required columns are present
(directly or via aliases) and it is the single best match; otherwise the
dataset is reported as unsupported (or ambiguous) with per-schema coverage,
so the user can see exactly which columns are missing.

Public API:
    detect_schema(columns, registry=SCHEMA_REGISTRY) -> dict
"""

from __future__ import annotations

from app.schema import SCHEMA_REGISTRY, column_rules
from app.services.column_mapping import resolve_aliases

DETECTED    = "detected"
UNSUPPORTED = "unsupported"
AMBIGUOUS   = "ambiguous"


def _score(columns, schema_type: str, schema: dict) -> dict:
    renames, _ = resolve_aliases(columns, schema)
    available = (set(columns) - set(renames)) | set(renames.values())
    rules = column_rules(schema)
    required = [c for c, r in rules.items() if r.get("required", False)]
    optional = [c for c, r in rules.items() if not r.get("required", False)]
    matched_required = [c for c in required if c in available]
    matched_optional = [c for c in optional if c in available]
    return {
        "schema_type":       schema_type,
        "required_total":    len(required),
        "required_matched":  len(matched_required),
        "optional_matched":  len(matched_optional),
        "coverage":          round(len(matched_required) / len(required), 3) if required else 0.0,
        "missing_required":  [c for c in required if c not in available],
        "column_mapping":    renames,
    }


def detect_schema(columns, registry: dict[str, dict] | None = None) -> dict:
    """
    Score every registered schema against the (normalized) column names.

    Returns:
        {
          "status":      "detected" | "unsupported" | "ambiguous",
          "schema_type": str | None,          # set only when detected
          "candidates":  [ per-schema score, best first ],
          "message":     str,
        }
    """
    registry = registry or SCHEMA_REGISTRY
    columns = list(columns)
    scores = sorted(
        (_score(columns, name, schema) for name, schema in registry.items()),
        key=lambda s: (s["coverage"], s["optional_matched"]),
        reverse=True,
    )
    complete = [s for s in scores if s["coverage"] == 1.0]

    if not complete:
        best = scores[0] if scores else None
        hint = (f" Closest match: '{best['schema_type']}' "
                f"(missing {', '.join(best['missing_required'])})." if best else "")
        return {
            "status":      UNSUPPORTED,
            "schema_type": None,
            "candidates":  scores,
            "message":     "This file does not match any supported dataset "
                           f"({', '.join(registry)}).{hint}",
        }

    if len(complete) > 1 and complete[0]["optional_matched"] == complete[1]["optional_matched"]:
        names = [s["schema_type"] for s in complete]
        return {
            "status":      AMBIGUOUS,
            "schema_type": None,
            "candidates":  scores,
            "message":     f"This file matches several schemas ({', '.join(names)}) — "
                           "choose the schema manually.",
        }

    chosen = complete[0]["schema_type"]
    return {
        "status":      DETECTED,
        "schema_type": chosen,
        "candidates":  scores,
        "message":     f"Detected '{chosen}' dataset.",
    }
