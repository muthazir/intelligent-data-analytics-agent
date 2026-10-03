"""
analyze.py
----------
Integration endpoint — orchestrates the full post-upload analytics pipeline.

Position in pipeline:
    POST /upload  (upload.py)  →  [POST /analyze/{file_id}]  →  GET /results/{file_id}

What this route does:
    1. Resolve schema_type + filename (uploads/{file_id}_meta.json, or the
       Dataset row in SQL Server when the meta file is not present)
    2. Load the dataset (SQL Server for ecommerce, cleaned CSV otherwise)
    3. Run analytics       → run_analytics(df_clean, schema_type)
    4. Run anomaly detect  → detect_anomalies(df_clean, schema_type)
    5. build_insights()      → structured insight dict
    6. generate_narration()  → narration lines + source ("gemini" | "fallback")
    7. Persist AnalysisRun + Anomaly + Insight + result_json in ONE transaction
       (every schema that has a Dataset row), mirror to the local result file,
       and return the unified JSON result

Design rules (enforced here):
    - NEVER use raw CSV — always load the cleaned dataset
    - NEVER send DataFrame to AI — only the insight payload (structured dict)
    - Each service has a single responsibility — this route only wires them
    - No business logic lives here — all logic lives in the service modules

Endpoints:
    POST /analyze/{file_id}   → run full pipeline, return JSON result
    GET  /results/{file_id}   → retrieve the latest stored result (SQL Server
                                result_json first, legacy local file second)
"""

from __future__ import annotations

import json
import logging
import os
import uuid

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.services.ai_narration   import generate_narration
from app.services.analytics      import run_analytics
from app.services.anomaly        import detect_anomalies
from app.services.insight_builder import build_insights
from app.services                import result_store
from app.db.session import db_enabled, get_session
from app.db import crud

router    = APIRouter()
UPLOAD_DIR = "uploads"
_logger   = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def validate_file_id(file_id: str) -> None:
    try:
        uuid.UUID(file_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid file_id")


def _load_clean_df(file_id: str) -> pd.DataFrame:
    """
    Load the cleaned DataFrame saved by the upload pipeline.

    Path convention: uploads/{file_id}_clean.csv
    Raises HTTP 404 if the file does not exist — it means /upload was never
    called for this file_id, or the file was deleted.
    """
    clean_path = os.path.join(UPLOAD_DIR, f"{file_id}_clean.csv")

    if not os.path.exists(clean_path):
        raise HTTPException(
            status_code=404,
            detail=f"Cleaned dataset not found for file_id '{file_id}'. "
                   f"Ensure /upload was called successfully first.",
        )

    try:
        return pd.read_csv(clean_path, encoding="utf-8-sig")
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load cleaned dataset: {exc}",
        )


def _load_meta_file(file_id: str) -> dict | None:
    """uploads/{file_id}_meta.json ({schema_type, filename}) or None if absent."""
    meta_path = os.path.join(UPLOAD_DIR, f"{file_id}_meta.json")
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r") as f:
            return json.load(f)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load file metadata: {exc}",
        )


def _load_meta_from_db(file_id: str) -> dict | None:
    """{schema_type, filename} from the Dataset row, or None if not in SQL Server."""
    if not db_enabled():
        return None
    db = get_session()
    try:
        dataset = crud.get_dataset_by_file_id(db, file_id)
        if dataset is None:
            return None
        return {"schema_type": dataset.schema_type, "filename": dataset.filename}
    except Exception as exc:
        _logger.warning("SQL Server metadata lookup failed for file_id=%s: %s", file_id, exc)
        return None
    finally:
        db.close()


def _resolve_meta(file_id: str) -> dict:
    """
    schema_type + filename for this upload. Prefers the local meta file
    (written by /upload), falls back to the Dataset row so datasets that
    exist only in SQL Server can still be (re-)analysed. 404 if neither.
    """
    meta = _load_meta_file(file_id) or _load_meta_from_db(file_id)
    if meta is None:
        raise HTTPException(
            status_code=404,
            detail=f"Metadata not found for file_id '{file_id}'. "
                   f"Ensure /upload was called successfully first.",
        )
    return meta


def _save_result(file_id: str, result: dict) -> None:
    """
    Mirror the full analysis result to uploads/{file_id}_result.json.

    SQL Server (AnalysisRun.result_json) is the authoritative copy whenever
    DATABASE_URL is configured; this file keeps file-only mode and older
    tooling working. Silently skips on failure — storage errors must not
    break the API response.
    """
    result_path = os.path.join(UPLOAD_DIR, f"{file_id}_result.json")
    try:
        with open(result_path, "w") as f:
            json.dump(result, f, default=str)   # default=str handles any remaining
    except Exception as e:
        _logger.error("Failed to save analysis result for %s: %s", file_id, e)


def _load_df_for_analysis(file_id: str, schema_type: str) -> tuple[pd.DataFrame, str]:
    """
    Load the dataset to analyze, preferring SQL Server when it holds this
    dataset's rows (ecommerce), falling back to the cleaned CSV otherwise.

    When a dataset was persisted at upload time, /analyze re-derives the
    DataFrame from SQL Server rather than re-reading the CSV, and the
    existing analytics/anomaly code (which only cares about column names)
    runs completely unchanged on top of it.

    Returns:
        (dataframe, data_source) where data_source is "sql_server" or "file"
    """
    if schema_type == "ecommerce" and db_enabled():
        db = get_session()
        try:
            dataset = crud.get_dataset_by_file_id(db, file_id)
            if dataset is not None:
                df = crud.load_ecommerce_dataframe(db, dataset.dataset_id)
                if not df.empty:
                    return df, "sql_server"
        except Exception as exc:
            _logger.warning(
                "SQL Server read failed for file_id=%s, falling back to file: %s",
                file_id, exc,
            )
        finally:
            db.close()

    return _load_clean_df(file_id), "file"


def _persist_analysis(
    file_id: str,
    schema_type: str,
    result: dict,
    anomalies: list[dict],
    narration: list[str],
    narration_source: str,
) -> None:
    """
    Record this run in SQL Server for any dataset that has a Dataset row
    (all schemas): AnalysisRun + Anomaly + Insight + result_json, in one
    transaction (crud.persist_analysis).

    Sets result["db_persistence"]:
        {"status": "saved",   "run_id": int, "anomalies_persisted": n, "insights_persisted": n}
        {"status": "skipped", ...}  — DATABASE_URL unset or no Dataset row
        {"status": "failed",  ...}  — DB error (logged); the analysis itself
                                      is still returned, nothing partial is stored
    """
    result["db_persistence"] = {
        "status": "skipped", "run_id": None, "anomalies_persisted": 0, "insights_persisted": 0,
    }
    if not db_enabled():
        return

    db = get_session()
    try:
        dataset = crud.get_dataset_by_file_id(db, file_id)
        if dataset is None:
            return
        result["dataset_id"] = dataset.dataset_id
        crud.persist_analysis(
            db,
            dataset=dataset,
            schema_type=schema_type,
            result=result,
            anomalies=anomalies,
            narration=narration,
            narration_source=narration_source,
        )
    except Exception as exc:
        _logger.error(
            "Could not persist AnalysisRun/Anomaly/Insight for file_id=%s: %s",
            file_id, exc,
        )
        result["db_persistence"] = {
            "status": "failed", "run_id": None, "anomalies_persisted": 0, "insights_persisted": 0,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /analyze/{file_id}
# ---------------------------------------------------------------------------

@router.post("/analyze/{file_id}")
def analyze(file_id: str):
    """
    Run the full analytics pipeline on a previously uploaded and cleaned file.

    Returns:
        {
            "file_id":          str,
            "dataset_id":       int | None,  # SQL Server Dataset row, if any
            "filename":         str | None,
            "schema_type":      str,
            "data_source":      str,         # "sql_server" or "file"
            "analytics":        dict,        # full metrics from run_analytics()
            "anomalies":        list,        # flagged rows from detect_anomalies()
            "insights":         dict,        # from build_insights()
            "narration":        list[str],   # Gemini or fallback lines
            "narration_source": str,         # "gemini" | "fallback"
            "narration_reason": str,         # "ok" | no_key | api_error | invalid_output | ...
            "db_persistence":   dict,        # {status, run_id, anomalies_persisted, insights_persisted}
        }

    Raises:
        404 if file_id not found (upload never completed)
        422 if analytics cannot run on the loaded data
    """
    validate_file_id(file_id)

    # ── Step 1: schema_type + filename ─────────────────────────────────────
    meta        = _resolve_meta(file_id)
    schema_type = meta.get("schema_type", "")

    if not schema_type:
        raise HTTPException(
            status_code=400,
            detail="schema_type missing from metadata — cannot run analytics",
        )

    # ── Step 2: dataset (SQL Server for ecommerce, cleaned CSV otherwise) ──
    df_clean, data_source = _load_df_for_analysis(file_id, schema_type)

    # ── Step 3: Analytics ──────────────────────────────────────────────────
    analytics_result = run_analytics(df_clean, schema_type)

    # run_analytics() returns an error key instead of raising on bad input
    if "error" in analytics_result:
        raise HTTPException(
            status_code=422,
            detail=f"Analytics failed: {analytics_result['error']}",
        )

    # ── Step 4: Anomaly Detection (returns [] on failure — never raises) ───
    anomalies = detect_anomalies(df_clean, schema_type)

    # ── Step 5: Insight Builder ────────────────────────────────────────────
    insights = build_insights(analytics_result, anomalies)

    # ── Step 6: AI narration (Gemini or fallback; never raises) ────────────
    narration_result = generate_narration(insights)
    narration        = narration_result["lines"]
    narration_source = narration_result["source"]
    narration_reason = narration_result["reason"]

    result = {
        "file_id":          file_id,
        "dataset_id":       None,
        "filename":         meta.get("filename"),
        "schema_type":      schema_type,
        "data_source":      data_source,   # "sql_server" or "file" — proves where this run's data came from
        "analytics":        analytics_result,
        "anomalies":        anomalies,
        "insights":         insights,
        "narration":        narration,
        "narration_source": narration_source,
        "narration_reason": narration_reason,   # "ok" or why the fallback was used
    }

    # ── Step 7: Persist (SQL Server, one transaction) + local mirror ───────
    _persist_analysis(file_id, schema_type, result, anomalies, narration, narration_source)
    _save_result(file_id, result)

    return JSONResponse(content=json.loads(json.dumps(result, default=str)))


# ---------------------------------------------------------------------------
# GET /results/{file_id}
# ---------------------------------------------------------------------------

@router.get("/results/{file_id}")
def get_results(file_id: str):
    """
    Retrieve the latest analysis result without re-running the pipeline.

    Source: SQL Server AnalysisRun.result_json of the dataset's latest run;
    falls back to the legacy local uploads/{file_id}_result.json. The
    response carries "result_source": "database" | "file".

    Raises:
        404 if /analyze was never called for this file_id
        422 if the stored result is malformed
    """
    validate_file_id(file_id)
    result, source = result_store.load_result(file_id)
    return JSONResponse(content={**result, "result_source": source})
