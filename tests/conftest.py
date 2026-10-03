"""
Test harness for IDAA.

Default target: a throw-away SQLite database in a temp working directory
(foreign keys enforced), so the suite never touches SQL Server.

Optional target: set IDAA_TEST_DATABASE_URL to a SQL Server test database
(e.g. the IDAA_DB_MIGTEST rehearsal copy). The session aborts if the
connected database is the real IDAA_DB.

Gemini: GEMINI_API_KEY is forced empty and genai.Client is replaced by a
guard that fails the test if it is ever constructed, so no real Gemini API
call can happen. Tests that need a "Gemini" response install their own stub.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_STATE: dict = {}


def pytest_configure(config):
    # The app resolves "uploads/" relative to the CWD and reads DATABASE_URL
    # at import time — so all of this must happen before `app` is imported.
    sys.path.insert(0, str(PROJECT_ROOT))
    _STATE["orig_cwd"] = os.getcwd()
    workdir = tempfile.mkdtemp(prefix="idaa_tests_")
    _STATE["workdir"] = workdir
    os.chdir(workdir)

    # Empty (not unset) so load_dotenv() can never fill them from .env.
    os.environ["GEMINI_API_KEY"] = ""
    os.environ["GOOGLE_API_KEY"] = ""

    target = os.environ.get("IDAA_TEST_DATABASE_URL", "").strip()
    if target:
        os.environ["DATABASE_URL"] = target
    else:
        db_file = pathlib.Path(workdir, "idaa_test.db").as_posix()
        os.environ["DATABASE_URL"] = f"sqlite:///{db_file}"

    from sqlalchemy import event, text
    from app.db import session as db_session

    engine = db_session.engine
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_fk_on(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
        db_session.init_db()
    else:
        with engine.connect() as conn:
            db_name = conn.execute(text("SELECT DB_NAME()")).scalar()
        if str(db_name).upper() == "IDAA_DB":
            pytest.exit("Refusing to run the test suite against the real IDAA_DB.", returncode=2)
        _STATE["db_name"] = db_name


def pytest_report_header(config):
    return f"IDAA test database: {os.environ.get('DATABASE_URL', '').split(':', 1)[0]}" + (
        f" ({_STATE['db_name']})" if "db_name" in _STATE else " (temporary SQLite)")


def pytest_unconfigure(config):
    try:
        from app.db import session as db_session
        if db_session.engine is not None:
            db_session.engine.dispose()
    except Exception:
        pass
    if "orig_cwd" in _STATE:
        os.chdir(_STATE["orig_cwd"])
    if "workdir" in _STATE:
        shutil.rmtree(_STATE["workdir"], ignore_errors=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def no_real_gemini(monkeypatch):
    """Any construction of the real genai.Client fails the test."""
    import app.services.ai_narration as narration

    calls: list = []

    class _GuardClient:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)
            raise AssertionError("A real Gemini client was constructed during tests")

    if narration._GENAI_AVAILABLE:
        monkeypatch.setattr(narration.genai, "Client", _GuardClient)
    yield
    assert not calls, "Unexpected Gemini client construction during test"


@pytest.fixture
def db():
    from app.db.session import get_session
    session = get_session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def uses_sql_server() -> bool:
    return "db_name" in _STATE
