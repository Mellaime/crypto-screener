"""
main.py
-------
Phase 2 - the WEB API (FastAPI). This serves the screener's data to the
frontend, and also serves the frontend page itself.

Endpoints:
    GET /api/coins    -> ACTIVE coins, ranked by score (highest first)
    GET /api/dropped  -> dropped coins with date + reason
    GET /api/queue    -> the next 600 coins waiting in the entry queue
    GET /api/coin/<contract> -> one coin: header info, 15-day transaction bars,
                                Volume/MCap + Volume/Liquidity (4h, 24h, 7d, total)
    GET /             -> the dashboard web page (from the frontend/ folder)

Run it (from the backend/ folder, with the venv active):
    uvicorn main:app --reload
Then visit:  http://localhost:8000
"""

import os
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func

from database import SessionLocal
from models import Coin, DailySnapshot, DroppedCoin
from scoring import score_snapshot

app = FastAPI(title="Crypto Screener API", version="0.1")

# --- CORS ------------------------------------------------------------------
# Allow the frontend (even if opened from a different origin / a file) to call
# this API from the browser. "*" is fine for a local project like this.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def latest_snapshot(session, coin_id):
    """The most recent snapshot row for a coin."""
    return (
        session.query(DailySnapshot)
        .filter(DailySnapshot.coin_id == coin_id)
        .order_by(DailySnapshot.date.desc(), DailySnapshot.id.desc())
        .first()
    )


def make_narrative(chain, mcap, vol_liq_ratio):
    """
    A short, human-readable summary of the coin.

    NOTE: auto-generated from the numbers for now. Later this can be replaced
    with the real project description from DexScreener's token profiles, or an
    AI-written summary.
    """
    if mcap and mcap >= 100_000_000:
        size = "Large-cap"
    elif mcap and mcap >= 10_000_000:
        size = "Mid-cap"
    else:
        size = "Small-cap"

    if vol_liq_ratio is None:
        activity = "unknown activity"
    elif vol_liq_ratio > 5:
        activity = "very high trading activity"
    elif vol_liq_ratio >= 2:
        activity = "strong trading activity"
    elif vol_liq_ratio >= 1:
        activity = "moderate trading activity"
    else:
        activity = "light trading activity"

    return f"{size} token on {chain} with {activity}."


def _iso(dt):
    """Datetime -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC), or None."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


@app.get("/api/coins")
def get_coins():
    """Return ACTIVE coins only, ranked by score (highest first)."""
    session = SessionLocal()
    try:
        coins = session.query(Coin).filter_by(status="active").all()

        rows = []
        for coin in coins:
            snap = latest_snapshot(session, coin.id)
            if snap is None:
                continue

            b = score_snapshot(snap, coin)  # fresh score + ratios

            # A single rank to display = average of the sources we have.
            rank_vals = [x for x in (coin.cg_rank, coin.cmc_rank) if x is not None]
            rank_display = round(sum(rank_vals) / len(rank_vals)) if rank_vals else None

            rows.append({
                "name": coin.name,
                "chain": coin.chain,
                "contract": coin.contract,
                "price": coin.price_usd,            # current USD price (refreshed each run)
                "price_updated": _iso(coin.price_updated_at),
                "mcap": snap.mcap,
                "liquidity": snap.liquidity,
                "volume": snap.volume,
                "vol_liq": b["vol_liq_ratio"],     # e.g. 4.10 (means 4.10x)
                "vol_mcap": b["vol_mcap_pct"],      # e.g. 25.4 (means 25.4%)
                "holders": snap.holders,            # None until we have a source
                "revenue": snap.revenue,            # DefiLlama daily revenue (None if unknown)
                "score": b["total"],
                "market_rank": rank_display,        # avg market rank, or None
                "rank_change": coin.rank_change,     # places moved vs last week (+ = up)
                "rank_signal": coin.rank_signal,     # strong_up / up / neutral / down
                "category": coin.category,          # real CoinGecko tag, or None
                "narrative": make_narrative(coin.chain, snap.mcap, b["vol_liq_ratio"]),
                # Security info (filter #7), stored on the coin.
                "security": coin.security_verdict,  # "safe" / "unsafe" / "unknown" / None
                "verified": coin.verified,
                "renounced": coin.renounced,
                "mint_disabled": coin.mint_disabled,
            })

        # Highest score first, then attach a rank number.
        rows.sort(key=lambda r: r["score"], reverse=True)
        for i, r in enumerate(rows, start=1):
            r["rank"] = i

        return {"count": len(rows), "coins": rows}
    finally:
        session.close()


@app.get("/api/dropped")
def get_dropped():
    """Return dropped coins with the date they dropped and the reason."""
    session = SessionLocal()
    try:
        results = (
            session.query(DroppedCoin, Coin)
            .join(Coin, Coin.id == DroppedCoin.coin_id)
            .order_by(DroppedCoin.dropped_date.desc())
            .all()
        )
        rows = [{
            "name": coin.name,
            "chain": coin.chain,
            "price": coin.price_usd,                # current USD price (refreshed each run)
            "price_updated": _iso(coin.price_updated_at),
            "date": dropped.dropped_date.strftime("%Y-%m-%d"),
            "reason": dropped.reason,
        } for dropped, coin in results]

        return {"count": len(rows), "dropped": rows}
    finally:
        session.close()


@app.get("/api/coin/{contract}")
def get_coin(contract: str):
    """One coin's detail page: its header info (all stored data) plus its
    daily transaction bars (Codex getBars, cached per coin per day)."""
    from models import CoinQueue
    from tx_bars import get_tx_bars, get_live_volume, WINDOW_DAYS

    session = SessionLocal()
    try:
        coin = (session.query(Coin)
                .filter(func.lower(Coin.contract) == contract.lower()).first())
        if coin is None:
            raise HTTPException(status_code=404, detail="coin not found")
        snap = latest_snapshot(session, coin.id)
        b = score_snapshot(snap, coin) if snap else None
        q = (session.query(CoinQueue.symbol)
             .filter_by(chain=(coin.chain or "").lower(), contract=coin.contract.lower())
             .first())
        tx = get_tx_bars(session, coin.contract, coin.chain)
        live = get_live_volume(session, coin.contract, coin.chain)

        # Volume / Market cap (%) and Volume / Liquidity (x) over 4 windows.
        # Market cap = the stored CoinGecko/CMC (global) one; liquidity = the
        # latest Codex reading tracking stored (DexScreener's if none).
        mcap = snap.mcap if snap else None
        liq = (snap.codex_liquidity or snap.liquidity) if snap else None
        volumes = {"4h": live["vol_4h"], "24h": live["vol_24h"],
                   "7d": tx["vol_7d"], "total": tx["vol_total"]}

        def ratio(v, base, scale):
            return round(v / base * scale, 4) if v is not None and base else None
        return {
            "name": coin.name,
            "symbol": q[0] if q else None,
            "chain": coin.chain,
            "contract": coin.contract,
            "status": coin.status,
            "price": coin.price_usd,
            "price_updated": _iso(coin.price_updated_at),
            "mcap": snap.mcap if snap else None,
            "score": b["total"] if b else None,
            "category": coin.category,
            "narrative": make_narrative(coin.chain, snap.mcap if snap else None,
                                        b["vol_liq_ratio"] if b else None),
            "security": coin.security_verdict,
            "tx_window_days": WINDOW_DAYS,
            "tx_bars": tx["bars"],
            "tx_error": tx["error"],
            "liquidity": liq,
            "volumes": volumes,
            "volume_since": tx["since"],
            "vol_mcap_pct": {k: ratio(v, mcap, 100) for k, v in volumes.items()},
            "vol_liq_x": {k: ratio(v, liq, 1) for k, v in volumes.items()},
            "volume_error": live["error"] or tx["error"],
        }
    finally:
        session.close()


@app.get("/api/queue")
def get_queue():
    """The next 600 coins in the entry queue (the next 2 daily runs), in the
    order they'll be analyzed. Reads stored data only - no API calls."""
    from queue_view import upcoming, QUEUE_DISPLAY_LIMIT
    session = SessionLocal()
    try:
        rows = upcoming(session)
        return {"count": len(rows), "limit": QUEUE_DISPLAY_LIMIT, "queue": rows}
    finally:
        session.close()


# --- Daily pipeline trigger (for Vercel Cron) ------------------------------
# Vercel Cron can't keep a process running 24/7 like our local scheduler, so
# instead it CALLS this URL once a day. This endpoint runs the same pipeline
# (discover -> filter -> save -> drop -> rank -> holders -> score).
#
# Security: if a CRON_SECRET env var is set, we require Vercel's matching
# "Authorization: Bearer <secret>" header, so randoms can't trigger it.
@app.get("/api/cron/daily")
def cron_daily(request: Request):
    secret = os.getenv("CRON_SECRET")
    if secret:
        if request.headers.get("authorization", "") != f"Bearer {secret}":
            raise HTTPException(status_code=401, detail="unauthorized")

    # Imported lazily so a normal dashboard request doesn't load the pipeline.
    from scheduler import run_pipeline
    run_pipeline()
    return {"status": "ok", "ran": "daily pipeline"}


# --- Serve the frontend page ----------------------------------------------
# Added LAST so it doesn't shadow the /api routes above. Locally this serves
# frontend/index.html at "/". On Vercel, "/" is served as a static file (see
# vercel.json), so this mount is dormant there - check_dir=False keeps it from
# erroring at import time if the folder isn't bundled into the function.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount(
    "/",
    StaticFiles(directory=str(FRONTEND_DIR), html=True, check_dir=False),
    name="frontend",
)
