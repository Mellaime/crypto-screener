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
from sqlalchemy.pool import NullPool

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
    # Normalize the URL to use the psycopg (v3) driver. psycopg v3 ships
    # reliable prebuilt wheels for serverless runtimes (Vercel), where the
    # psycopg2 binary can fail to import. It works exactly the same locally.
    url = DATABASE_URL
    if url.startswith("postgres://"):            # some providers use this alias
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):          # (won't touch an explicit +driver)
        url = "postgresql+psycopg://" + url[len("postgresql://"):]

    # prepare_threshold=None disables server-side prepared statements, which the
    # Supabase transaction pooler (port 6543) does not support.
    engine_kwargs = {"echo": False, "connect_args": {"prepare_threshold": None}}
    if os.getenv("VERCEL"):
        # On Vercel (serverless) DON'T keep a client-side connection pool
        # between invocations - let Supabase's pooler manage pooling. This
        # avoids stale/closed-connection errors.
        engine_kwargs["poolclass"] = NullPool
    else:
        engine_kwargs["pool_pre_ping"] = True     # local long-running process

    engine = create_engine(url, **engine_kwargs)
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
    _add_missing_columns()
    return DB_DESCRIPTION


# Columns added to EXISTING tables after they were first created. create_all()
# only creates missing tables, never missing columns, so add them here. Each
# entry: (table, column, SQL type, backfill statement or None).
_NEW_COLUMNS = [
    # When the coin (last) entered the active list - drop_logic's 5-day grace.
    ("coins", "active_since", "TIMESTAMP",
     "UPDATE coins SET active_since = first_seen WHERE active_since IS NULL"),
    # Holder count when it entered active - drop_logic's holders baseline.
    # (Kept on the coin because the daily holders update overwrites the
    # latest snapshot's count, which is often the entry snapshot.)
    ("coins", "holders_at_entry", "INTEGER",
     "UPDATE coins SET holders_at_entry = (SELECT s.holders FROM daily_snapshots s "
     "WHERE s.coin_id = coins.id AND s.holders IS NOT NULL "
     "ORDER BY s.date, s.id LIMIT 1) WHERE holders_at_entry IS NULL"),
    # USD price: per snapshot (history), and the current one on the coin,
    # refreshed every daily run for active + dropped coins.
    ("daily_snapshots", "price_usd", "FLOAT", None),
    ("coins", "price_usd", "FLOAT", None),
    ("coins", "price_updated_at", "TIMESTAMP", None),
    # Daily Codex readings (tracking.py) - drop_logic's trend history.
    ("daily_snapshots", "codex_liquidity", "FLOAT", None),
    ("daily_snapshots", "codex_volume24", "FLOAT", None),
    # Entry queue: last analysis result + how often analyzed (the 15-day
    # second chance). Rows analyzed before this existed count as 1 attempt;
    # their outcome is "saved" if the coin is in our list, else "rejected".
    ("coin_queue", "attempts", "INTEGER NOT NULL DEFAULT 0",
     "UPDATE coin_queue SET attempts = 1 WHERE last_checked IS NOT NULL"),
    ("coin_queue", "outcome", "VARCHAR",
     "UPDATE coin_queue SET outcome = CASE WHEN EXISTS (SELECT 1 FROM coins c "
     "WHERE lower(c.contract) = coin_queue.contract) THEN 'saved' ELSE 'rejected' END "
     "WHERE last_checked IS NOT NULL"),
    # Display-only fields for the dashboard's "Coming up" (queue) table.
    ("coin_queue", "name", "VARCHAR", None),
    ("coin_queue", "symbol", "VARCHAR", None),
    ("coin_queue", "mcap", "FLOAT", None),
    ("coin_queue", "narrative", "VARCHAR", None),
    ("coin_queue", "last_seen", "TIMESTAMP", None),
    # Detail page: 4h / 24h liquidity change (display-only cache; rows cached
    # before these existed are emptied so they're fetched again).
    ("vol_stats_cache", "liq_chg_4h", "FLOAT", "DELETE FROM vol_stats_cache"),
    ("vol_stats_cache", "liq_chg_24h", "FLOAT", None),
    # Detail page Supply section (global supply from discovery).
    ("coins", "total_supply", "FLOAT", None),
    ("coins", "circulating_supply", "FLOAT", None),
    ("coins", "max_supply", "FLOAT", None),
    ("coins", "supply_uncapped", "BOOLEAN", None),
    ("coins", "supply_updated_at", "TIMESTAMP", None),
    # Detail page / Check a Coin: X (Twitter) link from CoinGecko (socials.py).
    ("coins", "twitter_handle", "VARCHAR", None),
    ("coins", "twitter_checked_at", "TIMESTAMP", None),
    # Score history: the score's 6 parts per day (detail page breakdown).
    ("score_history", "vol_liq", "FLOAT", None),
    ("score_history", "vol_mcap", "FLOAT", None),
    ("score_history", "liq_growth", "FLOAT", None),
    ("score_history", "cheapness", "FLOAT", None),
    ("score_history", "buyers", "FLOAT", None),
    ("score_history", "rank_change", "FLOAT", None),
]


# Hourly live price (hourly_prices.py) - nullable, safe to leave if unused.
_NEW_COLUMNS += [
    ("coins", "live_price", "FLOAT", None),
    ("coins", "live_price_at", "TIMESTAMP", None),
]

# Favorites portfolio (added after the favorites tables were created).
_NEW_COLUMNS += [
    ("favorite_profiles", "tracking_since", "TIMESTAMP",
     "UPDATE favorite_profiles SET tracking_since = created_at WHERE tracking_since IS NULL"),
    ("favorite_profile_coins", "entry_price", "FLOAT",
     "UPDATE favorite_profile_coins SET entry_price = last_known_price WHERE entry_price IS NULL"),
]


def _add_missing_columns():
    from sqlalchemy import inspect, text

    for table, column, sql_type, backfill in _NEW_COLUMNS:
        existing = {c["name"] for c in inspect(engine).get_columns(table)}
        if column in existing:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
            if backfill:
                conn.execute(text(backfill))
        print(f"[DB] added column {table}.{column}")
