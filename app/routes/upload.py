from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from app.services.structural_validation import validate_structure
from app.services.schema_validation import validate_schema
from app.services.normalization import normalize_dataframe
from app.services.column_mapping import apply_aliases
from app.services.pre_clean import pre_clean
from app.services.schema_detection import detect_schema, DETECTED
from app.schema import SCHEMA_REGISTRY, column_rules
from app.services.cleaning import clean_data
from app.db.session import db_enabled, get_session
from app.db import crud
import io
import os
import uuid
import logging
import pandas as pd

_logger = logging.getLogger(__name__)

router = APIRouter()

MAX_SIZE   = 10 * 1024 * 1024   # 10 MB
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

AUTO_SCHEMA = "auto"   # schema_type value that asks the API to detect the schema


def _cleanup_file(path: str) -> None:
    """Silently remove a file — used to clean up on unexpected errors."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _read_csv_with_encoding_fallback(contents: bytes) -> pd.DataFrame:
    """
    Attempt to parse CSV bytes with UTF-8 first, then fall back to Latin-1.

    Rationale: CSV exports from Indian ERP/accounting tools (Tally, Zoho Books,
    Excel India locale) frequently use Latin-1 / cp1252 encoding. A plain
    pd.read_csv() call with no encoding arg silently produces garbled strings
    or raises UnicodeDecodeError in production.

    Returns a parsed DataFrame or raises HTTPException(400) if both attempts fail.
    """
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(contents), encoding=encoding, engine="python")
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid or unreadable CSV file: {exc}",
            )
    raise HTTPException(
        status_code=400,
        detail="Could not decode CSV — unsupported encoding (tried UTF-8 and Latin-1)",
    )


def _normalize_warning_format(raw_warnings: list[str]) -> list[dict]:
    """
    Convert plain-string normalization warnings into the unified warning struct
    used by schema_validation phases:  { phase, column, rule, message }

    Without this, merging schema warnings (dicts) + normalization warnings (strings)
    produces a mixed-type list that breaks any frontend consuming the response.
    """
    return [
        {
            "phase":   0,                   # phase 0 = pre-validation / normalization
            "column":  "_normalization",
            "rule":    "normalization",
            "message": w,
        }
        for w in raw_warnings
    ]


def _as_warnings(messages: list[str], column: str, rule: str) -> list[dict]:
    """Plain-string warnings -> the unified { phase, column, rule, message } struct."""
    return [{"phase": 0, "column": column, "rule": rule, "message": m} for m in messages]


def _resolve_schema(schema_type: str, columns) -> tuple[str, dict | None]:
    """
    Return (resolved schema_type, detection report or None).

    Manual selection is validated against the registry; "auto" runs schema
    detection and rejects unsupported / ambiguous files with HTTP 422.
    """
    if schema_type == AUTO_SCHEMA:
        detection = detect_schema(columns)
        if detection["status"] != DETECTED:
            raise HTTPException(status_code=422, detail={
                "message":     detection["message"],
                "reason":      detection["status"],          # "unsupported" | "ambiguous"
                "schema_type": AUTO_SCHEMA,
                "columns":     list(columns),
                "detection":   detection,
            })
        return detection["schema_type"], detection

    if schema_type not in SCHEMA_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown schema_type: '{schema_type}'. "
                   f"Valid options: {list(SCHEMA_REGISTRY.keys()) + [AUTO_SCHEMA]}",
        )
    return schema_type, None


def _validation_failure_detail(schema_type: str, df: pd.DataFrame, result: dict,
                               column_mapping: dict, mapping_warnings: list[str],
                               columns_before_mapping) -> dict:
    """Structured, user-facing explanation of a schema validation failure."""
    known = set(column_rules(SCHEMA_REGISTRY[schema_type]))
    missing = [e["column"] for e in result["errors"] if e["rule"] == "existence"]
    detail = {
        "message":          f"The file does not match the '{schema_type}' schema",
        "schema_type":      schema_type,
        "errors":           result["errors"],
        "warnings":         result["warnings"] + _as_warnings(mapping_warnings, "_column_mapping", "column_mapping"),
        "missing_required": missing,
        "unknown_columns":  [c for c in df.columns if c not in known],
        "column_mapping":   column_mapping,
        "suggestion":       None,
    }
    if missing:
        detection = detect_schema(columns_before_mapping)
        if detection["status"] == DETECTED and detection["schema_type"] != schema_type:
            detail["suggestion"] = (
                f"This file looks like a '{detection['schema_type']}' dataset — "
                f"choose that schema or Auto-detect."
            )
    return detail


@router.post("/upload")
def upload_csv(
    file: UploadFile = File(...),
    schema_type: str = Query(
        default="cafe",
        description="Schema to validate against: ecommerce | cafe | gym | auto (detect)",
    ),
):
    # ------------------------------------------------------------------ #
    # 1. Extension check — case-insensitive (.CSV must pass, .csv.exe must not)
    # ------------------------------------------------------------------ #
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are allowed")

    # ------------------------------------------------------------------ #
    # 2. Read the entire file into memory ONCE.
    #    This bytes object is reused for both pandas parsing and file saving,
    #    completely eliminating SpooledTemporaryFile seek() reliability issues.
    # ------------------------------------------------------------------ #
    contents = file.file.read()

    # ------------------------------------------------------------------ #
    # 3. File size check — runs on in-memory bytes, no seek() needed
    # ------------------------------------------------------------------ #
    if len(contents) > MAX_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds {MAX_SIZE // (1024 * 1024)} MB limit",
        )

    # ------------------------------------------------------------------ #
    # 4. Reject unknown schema names early (before any expensive processing);
    #    "auto" is resolved after normalization, in step 7b.
    # ------------------------------------------------------------------ #
    if schema_type != AUTO_SCHEMA and schema_type not in SCHEMA_REGISTRY:
        _resolve_schema(schema_type, [])

    # ------------------------------------------------------------------ #
    # 5. Parse CSV with encoding fallback (UTF-8 → Latin-1)
    # ------------------------------------------------------------------ #
    df_raw = _read_csv_with_encoding_fallback(contents)

    # ------------------------------------------------------------------ #
    # 6. Structural validation on RAW DataFrame — BEFORE normalization.
    #
    #    Why order matters:
    #    Normalization (N1) resolves column name collisions by appending suffixes
    #    ("price" + "Price " → "price" + "price_2"). If structural validation ran
    #    AFTER normalization, df.columns.duplicated() would never fire because the
    #    collision is already resolved — masking a real data quality problem.
    #
    #    Running structural checks on the raw DataFrame ensures the validator sees
    #    the true shape of what the user uploaded.
    # ------------------------------------------------------------------ #
    validate_structure(df_raw)

    # ------------------------------------------------------------------ #
    # 7. Normalization — standardise column names, trim strings, etc.
    #    Only runs after structural checks pass on the raw data.
    # ------------------------------------------------------------------ #
    norm_result = normalize_dataframe(df_raw)
    df = norm_result.df

    # ------------------------------------------------------------------ #
    # 7b. Schema resolution — manual selection, or detection for "auto"
    #     (unsupported / ambiguous datasets are rejected with HTTP 422).
    # ------------------------------------------------------------------ #
    schema_requested = schema_type
    columns_before_mapping = list(df.columns)
    schema_type, detection = _resolve_schema(schema_type, columns_before_mapping)
    schema = SCHEMA_REGISTRY[schema_type]

    # ------------------------------------------------------------------ #
    # 7c. Column mapping — synonyms ("qty", "amount", ...) -> canonical names
    # 7d. Pre-validation cleaning — currency/thousands formatting, categorical
    #     spelling variants, exact duplicate rows (see app/services/pre_clean.py)
    # ------------------------------------------------------------------ #
    df, column_mapping, mapping_warnings = apply_aliases(df, schema)
    df, pre_clean_report = pre_clean(df, schema)

    # ------------------------------------------------------------------ #
    # 8. Schema validation — runs on the normalised, mapped DataFrame
    # ------------------------------------------------------------------ #
    result = validate_schema(df, schema, fail_fast=False)
    if result["status"] == "fail":
        raise HTTPException(
            status_code=400,
            detail=_validation_failure_detail(
                schema_type, df, result, column_mapping, mapping_warnings, columns_before_mapping,
            ),
        )
    df_clean, clean_report = clean_data(df, schema)
    if df_clean.empty:
        raise HTTPException(status_code=400, detail={
            "message":  "No valid rows remain after cleaning",
            "cleaning": clean_report,
            "warnings": result["warnings"],
        })
    # ------------------------------------------------------------------ #
    # 9. Persist file — uses the bytes already in memory, no seek() required
    # ------------------------------------------------------------------ #
    file_id    = str(uuid.uuid4())
    safe_name  = os.path.basename(file.filename)   # strip any ../../ traversal sequences
    saved_name = f"{file_id}_{safe_name}"
    file_path  = os.path.join(UPLOAD_DIR, saved_name)

    try:
        with open(file_path, "wb") as buffer:
            buffer.write(contents)          # same bytes object from step 2
    except Exception:
        _cleanup_file(file_path)
        raise HTTPException(status_code=500, detail="Failed to save uploaded file")

    # ------------------------------------------------------------------ #
    # 9b. Persist cleaned DataFrame + schema metadata.
    #
    #     The /analyze/{file_id} endpoint needs two things:
    #       - df_clean  → the cleaned, validated DataFrame (not raw bytes)
    #       - schema_type → so the analyze route knows which analytics
    #                       and anomaly functions to dispatch to
    #
    #     Both are written here, immediately after the raw file is saved,
    #     so they share the same file_id and stay in sync.
    #
    #     Clean CSV path : uploads/{file_id}_clean.csv
    #     Metadata path  : uploads/{file_id}_meta.json
    # ------------------------------------------------------------------ #
    clean_path = os.path.join(UPLOAD_DIR, f"{file_id}_clean.csv")
    meta_path  = os.path.join(UPLOAD_DIR, f"{file_id}_meta.json")

    try:
        df_clean.to_csv(clean_path, index=False)
    except Exception:
        _cleanup_file(clean_path)
        _cleanup_file(file_path)
        raise HTTPException(status_code=500, detail="Failed to save cleaned dataset")

    try:
        import json
        with open(meta_path, "w") as f:
            json.dump({"schema_type": schema_type, "filename": file.filename}, f)
    except Exception:
        _cleanup_file(meta_path)
        _cleanup_file(clean_path)
        _cleanup_file(file_path)
        raise HTTPException(status_code=500, detail="Failed to save file metadata")

    # ------------------------------------------------------------------ #
    # 9c. Persist to SQL Server.
    #
    #     ecommerce   → db_scope "full": Dataset + dataset-scoped
    #                   DatasetCustomer / DatasetProduct + EcommerceOrder rows.
    #     cafe / gym  → db_scope "metadata": Dataset row only (no row-level
    #                   tables for these schemas yet); their rows stay in the
    #                   cleaned CSV, and their analysis runs are persisted by
    #                   /analyze like every other schema.
    #
    #     If DATABASE_URL isn't configured (db_enabled() is False), this
    #     block is skipped entirely and the upload still succeeds file-only.
    #     Once DATABASE_URL IS configured, SQL Server is authoritative: a
    #     persistence failure is surfaced as a 500 (generic message — the
    #     underlying driver error, which can contain row data, is only
    #     logged server-side) and the files saved above are removed.
    # ------------------------------------------------------------------ #
    db_persisted = False
    dataset_id   = None
    db_scope     = None
    db_message   = "DATABASE_URL not configured — running file-only (see .env.example)"

    if db_enabled():
        db = get_session()
        try:
            if schema_type == "ecommerce":
                dataset = crud.persist_ecommerce_dataset(
                    db,
                    file_id=file_id,
                    filename=file.filename,
                    df_clean=df_clean,
                    raw_file_path=file_path,
                )
                db_scope   = "full"
                db_message = "Persisted to SQL Server (Dataset + customers, products and orders)"
            else:
                dataset = crud.create_dataset_record(
                    db,
                    file_id=file_id,
                    filename=file.filename,
                    schema_type=schema_type,
                    rows_ingested=len(df_clean),
                    raw_file_path=file_path,
                )
                db_scope   = "metadata"
                db_message = (f"Dataset metadata persisted to SQL Server; '{schema_type}' rows "
                              f"are kept in the cleaned CSV (no row-level table for this schema)")
            db_persisted = True
            dataset_id   = dataset.dataset_id
        except Exception:
            db.rollback()
            _logger.exception("SQL Server persistence failed for file_id=%s", file_id)
            for path in (meta_path, clean_path, file_path):
                _cleanup_file(path)
            raise HTTPException(
                status_code=500,
                detail="The CSV was validated and cleaned, but saving it to SQL Server failed. "
                       "Nothing was stored. Check the server log for details.",
            )
        finally:
            db.close()

    # ------------------------------------------------------------------ #
    # 10. Build unified warnings list.
    #
    #     Schema warnings  → already structured: { phase, column, rule, message }
    #     Normalization warnings → plain strings — must be converted to same struct
    #     before merging, otherwise the response contains a mixed list of dicts
    #     and strings which breaks any typed frontend consumer.
    # ------------------------------------------------------------------ #
    unified_warnings = (
        result["warnings"]
        + _normalize_warning_format(norm_result.warnings)
        + _as_warnings(mapping_warnings, "_column_mapping", "column_mapping")
    )

    return {
        "file_id":          file_id,
        "filename":         file.filename,
        "schema_type":      schema_type,            # resolved schema (after auto-detection)
        "schema_requested": schema_requested,       # what the client asked for
        "schema_detection": detection,              # None for manual selection
        "column_mapping":   column_mapping,         # synonym column -> canonical column
        "pre_cleaning":     pre_clean_report,
        "rows":           len(df_clean),          # now reflects cleaned row count
        "columns":        list(df_clean.columns),
        "column_renames": norm_result.column_renames,
        "normalization":  norm_result.changes,
        "cleaning":       clean_report,           # add this
        "warnings":       unified_warnings,
        "db_persisted":   db_persisted,
        "db_scope":       db_scope,               # "full" | "metadata" | None (file-only)
        "dataset_id":     dataset_id,
        "db_message":     db_message,
    }