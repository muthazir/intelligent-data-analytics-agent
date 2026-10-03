"""
result_store.py
---------------
Single place that answers "what is the canonical analysis result for this
file_id?" — shared by GET /results/{file_id} and GET /report/{file_id}.

Lookup order:
    1. SQL Server  — AnalysisRun.result_json of the dataset's latest run
                     (authoritative since migration 001)
    2. Local file  — uploads/{file_id}_result.json (legacy runs recorded before
                     migration 001, or DATABASE_URL not configured)
    3. 404         — the dataset has never been analysed

Returns (result_dict, source) where source is "database" or "file".
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import HTTPException

from app.db.session import db_enabled, get_session
from app.db import crud

UPLOAD_DIR = "uploads"
_logger = logging.getLogger(__name__)

SOURCE_DATABASE = "database"
SOURCE_FILE     = "file"


def result_file_path(file_id: str) -> str:
    return os.path.join(UPLOAD_DIR, f"{file_id}_result.json")


def _load_from_database(file_id: str) -> dict | None:
    if not db_enabled():
        return None
    db = get_session()
    try:
        dataset = crud.get_dataset_by_file_id(db, file_id)
        if dataset is None:
            return None
        return crud.get_latest_result(db, dataset.dataset_id)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Stored analysis result for '{file_id}' in SQL Server is malformed: {exc}. "
                   f"Try re-running POST /analyze/{file_id}.",
        )
    except Exception as exc:
        # DB unreachable etc. — fall back to the local file rather than failing.
        _logger.warning("SQL Server result lookup failed for file_id=%s: %s", file_id, exc)
        return None
    finally:
        db.close()


def _load_from_file(file_id: str) -> dict | None:
    path = result_file_path(file_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Stored analysis result for '{file_id}' is malformed or "
                   f"unreadable: {exc}. Try re-running POST /analyze/{file_id}.",
        )


def load_result(file_id: str) -> tuple[dict, str]:
    """Canonical analysis result for file_id — see module docstring."""
    result = _load_from_database(file_id)
    if result is not None:
        return result, SOURCE_DATABASE

    result = _load_from_file(file_id)
    if result is not None:
        return result, SOURCE_FILE

    raise HTTPException(
        status_code=404,
        detail=f"No analysis result found for file_id '{file_id}'. "
               f"Call POST /analyze/{file_id} first.",
    )
