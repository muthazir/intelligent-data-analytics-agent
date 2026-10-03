"""Registry of supported dataset schemas — add new schemas here, nothing else changes."""

from app.schema.cafe_schema import CAFE_SCHEMA
from app.schema.ecommerce_schema import ECOMMERCE_SCHEMA
from app.schema.gym_schema import GYM_SCHEMA

SCHEMA_REGISTRY: dict[str, dict] = {
    "cafe":      CAFE_SCHEMA,
    "gym":       GYM_SCHEMA,
    "ecommerce": ECOMMERCE_SCHEMA,
}


def column_rules(schema: dict) -> dict[str, dict]:
    """Per-column rule dicts of a flat schema (skips keys like '_cross_rules')."""
    return {col: rules for col, rules in schema.items() if isinstance(rules, dict)}
