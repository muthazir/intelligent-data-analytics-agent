"""
dataset.py
----------
Read-only SQL Server retrieval endpoints — separate from /analyze so the
faculty demo can show "data/results really are in SQL Server" without
re-running the full analytics pipeline each time.

Endpoints:
    GET /dataset/{file_id}    → Dataset metadata (+ row count + row preview for ecommerce)
    GET /anomalies/{file_id}  → Anomaly rows for the dataset's latest AnalysisRun
    GET /insights/{file_id}   → Insight rows for the dataset's latest AnalysisRun

All three work for every schema that has a Dataset row (ecommerce, cafe,
gym). cafe / gym are stored at db_scope "metadata" — their row-level data
stays in the cleaned CSV, so /dataset returns no SQL row preview for them.

All three return 404 if:
    - DATABASE_URL isn't configured, or
    - no Dataset row exists for this file_id (uploaded before SQL Server
      was configured)

/anomalies and /insights additionally return an empty list (200, not 404)
when the dataset exists but POST /analyze/{file_id} hasn't been called yet,
or its latest run simply had nothing to report — both are valid states.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException, Query

from app.db.session import db_enabled, get_session
from app.db import crud

router = APIRouter()


def _validate_file_id(file_id: str) -> None:
    try:
        uuid.UUID(file_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid file_id")


def _require_dataset(db, file_id: str):
    """Shared lookup used by all three endpoints below — raises 404 consistently."""
    dataset = crud.get_dataset_by_file_id(db, file_id)
    if dataset is None:
        raise HTTPException(
            status_code=404,
            detail=f"No SQL Server dataset found for file_id '{file_id}'. "
                   f"Either /upload was never called for it, or it ran before "
                   f"SQL Server was configured.",
        )
    return dataset


@router.get("/dataset/{file_id}")
def get_dataset(file_id: str, preview_rows: int = Query(default=10, ge=1, le=200)):
    _validate_file_id(file_id)

    if not db_enabled():
        raise HTTPException(
            status_code=404,
            detail="SQL Server is not configured (DATABASE_URL unset) — nothing to retrieve. "
                   "See .env.example.",
        )

    db = get_session()
    try:
        dataset = _require_dataset(db, file_id)
        response = {
            "dataset_id":    dataset.dataset_id,
            "file_id":       str(dataset.file_id),
            "filename":      dataset.filename,
            "schema_type":   dataset.schema_type,
            "uploaded_at":   dataset.uploaded_at.isoformat(),
            "rows_ingested": dataset.rows_ingested,
        }
        if dataset.schema_type == "ecommerce":
            df = crud.load_ecommerce_dataframe(db, dataset.dataset_id)
            response.update({
                "db_scope":   "full",
                "rows_in_db": len(df),
                "preview":    json.loads(df.head(preview_rows).to_json(orient="records", date_format="iso")),
            })
        else:
            # Row-level data for this schema lives in the cleaned CSV only.
            response.update({"db_scope": "metadata", "rows_in_db": None, "preview": []})
        return response
    finally:
        db.close()


@router.get("/anomalies/{file_id}")
def get_anomalies(file_id: str):
    """
    Anomaly rows for this dataset's most recent AnalysisRun, read live from
    SQL Server (Phase 2A). Returns [] (not 404) if /analyze hasn't run yet
    or found nothing anomalous — only a missing/unpersisted Dataset is 404.
    """
    _validate_file_id(file_id)

    if not db_enabled():
        raise HTTPException(
            status_code=404,
            detail="SQL Server is not configured (DATABASE_URL unset) — nothing to retrieve. "
                   "See .env.example.",
        )

    db = get_session()
    try:
        dataset = _require_dataset(db, file_id)
        anomalies = crud.get_anomalies_for_dataset(db, dataset.dataset_id)
        return {
            "file_id":    file_id,
            "dataset_id": dataset.dataset_id,
            "count":      len(anomalies),
            "anomalies": [
                {
                    "anomaly_id":   a.anomaly_id,
                    "run_id":       a.run_id,
                    "column":       a.column_name,
                    "row_index":    a.row_index,
                    "value":        float(a.value) if a.value is not None else None,
                    "z_score":      float(a.z_score),
                    "type":         a.anomaly_type,
                }
                for a in anomalies
            ],
        }
    finally:
        db.close()


@router.get("/insights/{file_id}")
def get_insights(file_id: str):
    """
    Insight rows (AI narration lines) for this dataset's most recent
    AnalysisRun, read live from SQL Server. Returns [] (not 404) if /analyze
    hasn't run yet — only a missing/unpersisted Dataset is 404.
    narration_source is "gemini" | "fallback" (None for runs recorded before
    migration 001).
    """
    _validate_file_id(file_id)

    if not db_enabled():
        raise HTTPException(
            status_code=404,
            detail="SQL Server is not configured (DATABASE_URL unset) — nothing to retrieve. "
                   "See .env.example.",
        )

    db = get_session()
    try:
        dataset = _require_dataset(db, file_id)
        latest_run = crud.get_latest_run(db, dataset.dataset_id)
        insights = crud.get_insights_for_dataset(db, dataset.dataset_id)
        return {
            "file_id":          file_id,
            "dataset_id":       dataset.dataset_id,
            "run_id":           latest_run.run_id if latest_run else None,
            "narration_source": latest_run.narration_source if latest_run else None,
            "count":            len(insights),
            "insights": [
                {
                    "insight_id": i.insight_id,
                    "run_id":     i.run_id,
                    "text":       i.insight_text,
                    "category":   i.category,
                }
                for i in insights
            ],
        }
    finally:
        db.close()
