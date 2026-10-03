"""
session.py
----------
SQLAlchemy engine + session factory for the SQL Server persistence layer.

Configuration is entirely environment-driven — no credentials are ever
hard-coded here. Set DATABASE_URL in a .env file (see .env.example at the
project root).

Expected DATABASE_URL formats:

    SQL Server, SQL login:
        mssql+pyodbc://USER:PASSWORD@localhost\\SQLEXPRESS/IDAA_DB?driver=ODBC+Driver+17+for+SQL+Server

    SQL Server, Windows Authentication (trusted connection):
        mssql+pyodbc://localhost\\SQLEXPRESS/IDAA_DB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes

If DATABASE_URL is not set at all, `engine` is left as None and every
function in app/db/crud.py degrades gracefully (callers check
`db_enabled()` first) — this lets the rest of the app keep working
file-only, exactly like before Phase 1, in any environment where SQL
Server isn't reachable (e.g. this review sandbox).
"""

from __future__ import annotations

import os
import logging

_logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is a convenience, not a hard requirement — if it's
    # missing, DATABASE_URL can still be set via a real environment variable.
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

engine = None
SessionLocal = None
Base = None

if DATABASE_URL:
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker, declarative_base

        # pool_pre_ping avoids handing out dead connections after SQL Server
        # idles a session out — cheap check, big reliability win.
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        Base = declarative_base()
    except ImportError:
        _logger.warning(
            "DATABASE_URL is set but SQLAlchemy/pyodbc are not installed — "
            "run `pip install -r requirements.txt`. Falling back to file-only mode."
        )
        engine = None
        SessionLocal = None
        Base = None
else:
    # No DATABASE_URL configured — build a Base anyway so app/db/models.py
    # can still be imported without raising (models just won't be usable).
    try:
        from sqlalchemy.orm import declarative_base
        Base = declarative_base()
    except ImportError:
        Base = object  # last-resort placeholder if SQLAlchemy isn't installed at all


def db_enabled() -> bool:
    """True only if DATABASE_URL was set AND SQLAlchemy engine construction succeeded."""
    return engine is not None and SessionLocal is not None


def get_session():
    """
    Yield-style session getter for use as:

        with get_session() as db:
            ...

    Raises RuntimeError if the DB layer isn't configured — callers must
    check db_enabled() first if they want to degrade gracefully instead.
    """
    if not db_enabled():
        raise RuntimeError(
            "Database not configured. Set DATABASE_URL in your .env file "
            "and ensure sqlalchemy + pyodbc are installed."
        )
    return SessionLocal()


def init_db() -> None:
    """
    Create all tables from the ORM models if they don't already exist.

    In normal use you run sql/schema.sql once via SSMS instead — this is a
    convenience for automated testing / first-time setup so `models.py`
    and `schema.sql` can be checked against each other.
    """
    if not db_enabled():
        raise RuntimeError("Database not configured — cannot init_db().")
    import app.db.models  # noqa: F401 — ensure models are registered on Base
    Base.metadata.create_all(bind=engine)
