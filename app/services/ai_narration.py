"""
ai_narration.py
---------------
Turns the structured insight payload (insight_builder) into 3–5 short
business-language sentences.

Division of responsibility:
    - Python (analytics / anomaly / insight_builder) owns every number.
    - Gemini only narrates those numbers. Its output is parsed and checked:
      any sentence containing a number that does not appear in the payload is
      dropped, so Gemini can never introduce a figure of its own.
    - When Gemini is not configured, unavailable, slow, or returns unusable
      text, a deterministic schema-aware narration is generated from the same
      payload. It is labelled narration_source = "fallback".

SDK: google-genai (`from google import genai`). Configuration:
    GEMINI_API_KEY   required to call Gemini (never logged or returned)
    GEMINI_MODEL     optional model override (default: _DEFAULT_MODEL)

Public API:
    generate_narration(insight_payload) -> {"lines": [...], "source": "gemini"|"fallback", "reason": str}
    generate_ai_insights(insight_payload) -> list[str]        # backwards-compatible wrapper
"""

from __future__ import annotations

import json
import logging
import math
import os
import re

try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types
    _GENAI_AVAILABLE = True
except ImportError:
    # Narration is optional: without google-genai the deterministic fallback is used.
    _GENAI_AVAILABLE = False

_logger = logging.getLogger(__name__)

NARRATION_SOURCE_GEMINI   = "gemini"
NARRATION_SOURCE_FALLBACK = "fallback"

# Gemini model IDs are retired/renamed periodically; override with GEMINI_MODEL
# (see https://ai.google.dev/gemini-api/docs/models) without code changes.
_DEFAULT_MODEL      = "gemini-3.5-flash-lite"
_REQUEST_TIMEOUT_MS = 20_000
_MIN_LINES, _MAX_LINES, _MAX_LINE_CHARS = 3, 5, 300


# ---------------------------------------------------------------------------
# Number grounding — every number Gemini writes must exist in the payload
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(
    r"(?<![\w.])(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>%|k\b|thousand\b|l\b|lakh\b|lakhs\b|lac\b|m\b|mn\b|million\b|cr\b|crore\b|crores\b)?",
    re.IGNORECASE,
)
_UNIT_SCALE = {
    "k": 1e3, "thousand": 1e3, "l": 1e5, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5,
    "m": 1e6, "mn": 1e6, "million": 1e6, "cr": 1e7, "crore": 1e7, "crores": 1e7,
}
_SMALL_INT_ALLOWANCE = 5      # "top 3", "5 products" — small counts are not facts worth policing


def _payload_numbers(obj) -> list[float]:
    """Every number in the payload, including digits embedded in strings (dates)."""
    out: list[float] = []
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float)):
        if math.isfinite(obj):
            out.append(float(obj))
    elif isinstance(obj, str):
        out.extend(float(m) for m in re.findall(r"\d+(?:\.\d+)?", obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_payload_numbers(k))
            out.extend(_payload_numbers(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_payload_numbers(v))
    return out


def _close(a: float, b: float) -> bool:
    # rounding tolerance: 0.5 % of the value, at least one unit in the last shown place
    return abs(a - b) <= max(0.5 * 10 ** -2, abs(b) * 0.005)


def _number_is_grounded(value: float, unit: str | None, known: list[float]) -> bool:
    unit = (unit or "").lower()
    if not unit and value.is_integer() and 0 <= value <= _SMALL_INT_ALLOWANCE:
        return True
    scale = _UNIT_SCALE.get(unit, 1.0)
    candidates = {value * scale} if unit and unit != "%" else {value}
    if unit == "%":
        candidates.add(value / 100)               # payload may hold a fraction
    for v in candidates:
        for k in known:
            if _close(v, k) or (unit in _UNIT_SCALE and abs(v - k) <= abs(k) * 0.05):
                return True
    return False


def _ungrounded_numbers(line: str, known: list[float]) -> list[str]:
    bad = []
    for m in _NUMBER_RE.finditer(line):
        value = float(m.group("num").replace(",", ""))
        if not _number_is_grounded(value, m.group("unit"), known):
            bad.append(m.group(0).strip())
    return bad


# ---------------------------------------------------------------------------
# Parsing Gemini text
# ---------------------------------------------------------------------------

_PREAMBLE_RE = re.compile(r"^(here (are|is)|sure|certainly|based on|insights?\b|summary\b)", re.IGNORECASE)


def _clean_insight_line(line: str) -> str:
    """Strip markdown, bullets, numbering and surrounding quotes from one line."""
    s = line.strip()
    s = re.sub(r"^#{1,6}\s*", "", s)                 # markdown headings
    s = re.sub(r"^(\d+[\.\)]|[-•*–])\s+", "", s)      # numbering / bullets
    s = s.replace("**", "").replace("__", "").replace("`", "")
    s = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", s)   # *emphasis*
    return s.strip().strip('"').strip()


def _parse_insights_from_text(text: str) -> list[str]:
    """Split model output into candidate sentences; drop preambles and noise."""
    if not text:
        return []
    raw_lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(raw_lines) == 1 and len(raw_lines[0]) > 200:          # one paragraph
        raw_lines = re.split(r"(?<=[.!?])\s+", raw_lines[0])
    lines: list[str] = []
    for raw in raw_lines:
        s = _clean_insight_line(raw)
        if not s or not re.search(r"[A-Za-z]", s):
            continue
        if s.endswith(":") or (_PREAMBLE_RE.match(s) and len(s) < 80):
            continue
        lines.append(s[:_MAX_LINE_CHARS].rstrip())
    return lines


def _validated_lines(text: str, payload: dict) -> list[str]:
    known = _payload_numbers(payload)
    kept = []
    for line in _parse_insights_from_text(text):
        bad = _ungrounded_numbers(line, known)
        if bad:
            _logger.info("Dropped Gemini sentence with ungrounded numbers %s", bad)
            continue
        kept.append(line)
    return kept[:_MAX_LINES]


# ---------------------------------------------------------------------------
# Deterministic fallback narration (schema-aware, built from the payload)
# ---------------------------------------------------------------------------

def _money(v) -> str | None:
    return f"₹{v:,.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _count(v) -> str | None:
    return f"{v:,}" if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pct(v) -> str | None:
    return f"{v:g}%" if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _sentence(text: str) -> str:
    """Capitalise the first letter only (keeps product names' own casing)."""
    return text[:1].upper() + text[1:]


def _performer(top: dict, label: str, money: bool = True) -> str | None:
    if not top or top.get("name") is None:
        return None
    value = _money(top.get("value")) if money else _count(top.get("value"))
    return f"{label} is {top['name']}" + (f" ({value})" if value else "")


def _anomaly_sentence(summary: dict | None) -> str | None:
    if summary is None:
        return None                      # anomaly detection result not available — say nothing
    total = summary.get("total", 0) or 0
    if not total:
        return "No unusual values were detected by the Z-score check."
    worst = (summary.get("top_anomalies") or [{}])[0]
    detail = ""
    if worst.get("column") is not None:
        detail = f"; the largest is {worst['column']} = {worst.get('value')} (z = {worst.get('z_score')})"
    return (f"{total} unusual value(s) detected ({summary.get('spikes', 0)} spike(s), "
            f"{summary.get('drops', 0)} drop(s)){detail}.")


def _trend_sentence(trend: str) -> str:
    if trend in ("up", "down", "stable"):
        word = {"up": "rising", "down": "falling", "stable": "stable"}[trend]
        return f"Revenue in the latest month is {word} compared with the previous month."
    return "There is not enough monthly data to determine a revenue trend."


def _fallback_lines(payload: dict) -> list[str]:
    if not payload:
        return ["No analysis results were available to describe."]
    schema = payload.get("schema_type")
    km = payload.get("key_metrics") or {}
    tp = payload.get("top_performers") or {}
    lines: list[str | None] = []

    if schema == "ecommerce":
        rev, orders = _money(km.get("total_revenue")), _count(km.get("total_orders"))
        if rev:
            aov, cust = _money(km.get("avg_order_value")), _count(km.get("unique_customers"))
            lines.append(f"Total revenue is {rev}"
                         + (f" across {orders} orders" if orders else "")
                         + (f" (average order value {aov})" if aov else "")
                         + (f" from {cust} unique customers." if cust else "."))
        perf = [p for p in (_performer(tp.get("top_product"), "the top product"),
                            _performer(tp.get("top_category"), "the top category")) if p]
        if perf:
            lines.append(_sentence(" and ".join(perf) + "."))
        c, r = _pct(km.get("cancellation_rate_pct")), _pct(km.get("return_rate_pct"))
        if c and r:
            lines.append(f"Cancellation rate is {c} and return rate is {r}.")
        lines.append(_trend_sentence(payload.get("trend_direction")))
    elif schema == "cafe":
        rev = _money(km.get("total_revenue"))
        if rev:
            lines.append(f"Total revenue is {rev}.")
        perf = [p for p in (_performer(tp.get("top_item"), "the best-selling item"),
                            _performer(tp.get("top_category"), "the top category")) if p]
        if perf:
            lines.append(_sentence(" and ".join(perf) + "."))
        if km.get("busiest_day") and km.get("busiest_day") != "N/A":
            lines.append(f"The busiest day was {km['busiest_day']}.")
        pay = _performer(tp.get("top_payment"), "the most-used payment method")
        if pay:
            lines.append(_sentence(pay + "."))
    elif schema == "gym":
        members, rev = _count(km.get("total_members")), _money(km.get("total_revenue"))
        if members:
            lines.append(f"There are {members} members" + (f" with total monthly fees of {rev}." if rev else "."))
        age, att = km.get("avg_age"), km.get("avg_attendance_days")
        if isinstance(age, (int, float)) and isinstance(att, (int, float)):
            lines.append(f"Average member age is {age:g} and average attendance is {att:g} days.")
        perf = _performer(tp.get("top_membership"), "the most common membership type", money=False)
        if perf:
            lines.append(_sentence(perf + " (members)."))
        pay = _performer(tp.get("top_payment_status"), "the most common payment status", money=False)
        if pay:
            lines.append(_sentence(pay + " (members)."))
    else:
        for k, v in km.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                lines.append(f"{k.replace('_', ' ').capitalize()} is {v:,}.")

    lines.append(_anomaly_sentence(payload.get("anomaly_summary")))
    clean = [ln for ln in lines if ln]
    if len(clean) < _MIN_LINES:
        clean.append("These figures are computed directly from the uploaded data.")
    return clean[:_MAX_LINES]


def _fallback(payload: dict, reason: str) -> dict:
    return {"lines": _fallback_lines(payload or {}), "source": NARRATION_SOURCE_FALLBACK, "reason": reason}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _prompt(payload: dict) -> str:
    summary_json = json.dumps(payload, indent=2, default=str)
    return f"""You are a business analyst writing for a small-business owner.

Data summary (JSON) — computed by the analytics engine and authoritative:
{summary_json}

Write exactly 4 insights.

Rules:
- One sentence per insight, at most 35 words, one insight per line.
- Use ONLY numbers that appear in the JSON, copied exactly; do not calculate,
  round differently, convert units, or estimate any new number.
- Do not invent products, categories, causes, or facts that are not in the JSON.
- Cover: (1) revenue/performance, (2) top performer, (3) anomalies or risk,
  (4) trend or a practical recommendation.
- No headings, bullets, numbering, markdown, or introductory sentence."""


def generate_narration(insight_payload: dict) -> dict:
    """
    Narrate an insight_builder payload.

    Returns {"lines": list[str], "source": "gemini" | "fallback", "reason": str}.
    Never raises. "reason" explains a fallback (no_payload, no_key,
    sdk_missing, api_error, error, invalid_output) or is "ok".
    """
    if not insight_payload:
        return _fallback({}, "no_payload")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return _fallback(insight_payload, "no_key")
    if not _GENAI_AVAILABLE:
        return _fallback(insight_payload, "sdk_missing")

    try:
        client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS),
        )
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL") or _DEFAULT_MODEL,
            contents=_prompt(insight_payload),
        )
        text = (getattr(response, "text", None) or "").strip()
    except genai_errors.APIError as exc:
        _logger.warning("Gemini API error (%s) — using fallback narration", getattr(exc, "code", "?"))
        return _fallback(insight_payload, "api_error")
    except Exception as exc:
        _logger.warning("Gemini call failed (%s) — using fallback narration", type(exc).__name__)
        return _fallback(insight_payload, "error")

    lines = _validated_lines(text, insight_payload)
    if len(lines) < _MIN_LINES:
        return _fallback(insight_payload, "invalid_output")
    return {"lines": lines, "source": NARRATION_SOURCE_GEMINI, "reason": "ok"}


def generate_ai_insights(insight_payload: dict) -> list[str]:
    """Backwards-compatible wrapper: narration lines only (see generate_narration)."""
    return generate_narration(insight_payload)["lines"]
