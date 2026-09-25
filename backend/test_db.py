"""
test_db.py
----------
A simple check that our database is set up correctly.

It does three things:
  1. Connects to the SQLite database.
  2. Confirms the three tables we expect actually exist.
  3. Lists each table's columns so we can eyeball them.

Run it with:   python test_db.py
"""

from sqlalchemy import inspect

from database import engine, DB_DESCRIPTION, init_db

# The tables we expect to find, and the columns each should have.
EXPECTED = {
    "coins": ["id", "contract", "chain", "name", "first_seen", "status"],
    "daily_snapshots": [
        "id", "coin_id", "date", "volume", "liquidity",
        "mcap", "holders", "revenue", "score",
    ],
    "dropped_coins": ["id", "coin_id", "dropped_date", "reason"],
}


def main():
    # Make sure the tables exist (safe to call repeatedly; only creates missing ones).
    init_db()

    print("=" * 55)
    print("  CRYPTO SCREENER — DATABASE CHECK")
    print("=" * 55)
    print(f"Database: {DB_DESCRIPTION}")
    print("-" * 55)

    # 'inspect' lets us ask the live database what tables/columns it has.
    inspector = inspect(engine)
    actual_tables = inspector.get_table_names()

    all_ok = True
    for table, expected_cols in EXPECTED.items():
        if table in actual_tables:
            actual_cols = [c["name"] for c in inspector.get_columns(table)]
            missing = [c for c in expected_cols if c not in actual_cols]
            if missing:
                all_ok = False
                print(f"[!] Table '{table}' exists but is MISSING columns: {missing}")
            else:
                print(f"[OK] Table '{table}' exists  ->  columns: {actual_cols}")
        else:
            all_ok = False
            print(f"[X] Table '{table}' is MISSING!")

    print("-" * 55)
    if all_ok:
        print("RESULT: SUCCESS - all tables and columns are present.")
    else:
        print("RESULT: PROBLEM - see the messages above.")
    print("=" * 55)


if __name__ == "__main__":
    main()
