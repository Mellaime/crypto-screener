"""
database.py
-----------
Sets up the connection to our database and gives the rest of the app the
tools to talk to it. It does NOT define the tables themselves - that's models.py.

Two modes, chosen automatically:
  * If DATABASE_URL is set in backend/.env  -> use that (Supabase / cloud
    Postgres). This is what runs online 24/7 (Vercel, etc.).
  * If DATABASE_URL is NOT set              -> fall back to a local SQLite file
    in the project's /database folder, so you can still develop offline.

SQLAlchemy speaks both dialects, so the models don't change.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Load backend/.env so DATABASE_URL (and the API keys) are available.
load_dotenv(Path(__file__).resolve().parent / ".env")


def utcnow():
    """
    Current UTC time as a naive datetime (no timezone attached).

    We use this everywhere instead of the deprecated datetime.utcnow().
    Keeping it naive keeps timestamps consistent across SQLite and Postgres.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- Pick the database -----------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # --- Cloud Postgres (Supabase) ---
    # SQLAlchemy uses the psycopg2 driver for "postgresql://" URLs.
    # pool_pre_ping avoids errors from stale/dropped cloud connections.
    engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
    DB_FILE = None  # not a file-based DB
    DB_DESCRIPTION = "Supabase Postgres (cloud)"
else:
    # --- Local SQLite fallback ---
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    DATABASE_DIR = PROJECT_ROOT / "database"
    DATABASE_DIR.mkdir(exist_ok=True)
    DB_FILE = DATABASE_DIR / "crypto.db"
    engine = create_engine(
        f"sqlite:///{DB_FILE}",
        echo=False,
        # check_same_thread=False lets the scheduler + web server share the file.
        connect_args={"check_same_thread": False},
    )
    DB_DESCRIPTION = f"local SQLite ({DB_FILE})"

# --- Sessions --------------------------------------------------------------
# A "session" is a temporary workspace for reading/writing rows.
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# --- Base ------------------------------------------------------------------
# Every model in models.py inherits from this Base.
Base = declarative_base()


def init_db():
    """Create all tables defined in models.py (only creates missing ones)."""
    # Importing models registers the table classes on Base before we create them.
    import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    return DB_DESCRIPTION
