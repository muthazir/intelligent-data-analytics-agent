import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.routes import upload, analyze, dataset, report
from app.db.session import db_enabled, engine
from app.schema import SCHEMA_REGISTRY

APP_VERSION = "1.1.0"
FRONTEND_FILE = Path(__file__).resolve().parent.parent / "idaa_fixed.html"

app = FastAPI(title="IDAA — Intelligent Data Analytics Agent", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # lets the page also work when opened directly as a file
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(upload.router)
app.include_router(analyze.router)
app.include_router(dataset.router)
app.include_router(report.router)


@app.get("/", include_in_schema=False)
def frontend():
    """Serve the single-page dashboard from the same origin as the API."""
    return FileResponse(FRONTEND_FILE, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    """
    Runtime status for the dashboard header. Booleans only — never returns
    connection strings, keys or other configuration values.
    """
    db_connected = False
    if db_enabled():
        try:
            from sqlalchemy import text
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            db_connected = True
        except Exception:
            db_connected = False

    from app.services.ai_narration import _GENAI_AVAILABLE
    return {
        "status":            "ok",
        "version":           APP_VERSION,
        "db_configured":     db_enabled(),
        "db_connected":      db_connected,
        "gemini_configured": bool(os.environ.get("GEMINI_API_KEY")),
        "gemini_sdk":        _GENAI_AVAILABLE,
        "schemas":           list(SCHEMA_REGISTRY) + ["auto"],
    }
