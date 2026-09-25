# Crypto Screener

A web app that automatically fetches crypto coin data, filters and ranks coins
by a set of metrics, and tracks how they change over time.

## What it does

- **Fetches** market data for coins (volume, liquidity, market cap, holders, revenue).
- **Filters & ranks** coins into a score so the most promising ones surface first.
- **Tracks history** by taking a daily snapshot of each coin's metrics.
- **Flags drop-offs** by recording coins that no longer meet the criteria.
- Runs on a **schedule** (unattended) and serves the results through a web UI.

## Project structure

```
crypto-screener/
├── backend/     Python engine: data fetching, filtering, ranking, scheduler, API
├── frontend/    The website UI that displays the screened coins
├── database/    The SQLite database file lives here
└── README.md    This file
```

## Tech stack

- **Backend:** Python 3.12
  - `requests` — call external crypto data APIs
  - `fastapi` + `uvicorn` — serve a web API for the frontend
  - `apscheduler` — run the data-collection job on a schedule
  - `sqlalchemy` — talk to the SQLite database
- **Database:** SQLite (a single file, no server needed) at `database/crypto.db`
- **Frontend:** web UI (to be built)

## Database tables

| Table | Purpose |
|-------|---------|
| `coins` | One row per coin ever seen (contract, chain, name, first seen, status) |
| `daily_snapshots` | A daily row of metrics per coin (volume, liquidity, mcap, holders, revenue, score) |
| `dropped_coins` | Coins that were dropped, with the date and reason |

## Getting started (backend)

```bash
cd backend
# activate the virtual environment (Windows PowerShell):
.\venv\Scripts\Activate.ps1
# run the API:
uvicorn main:app --reload
```

## Status

🚧 Early setup. Environment and database schema are in place.
