"""
main.py
-------
Phase 2 - the WEB API (FastAPI). This serves the screener's data to the
frontend, and also serves the frontend page itself.

Endpoints:
    GET /api/coins    -> ACTIVE coins, ranked by score (highest first)
    GET /api/dropped  -> dropped coins with date + reason
    GET /             -> the dashboard web page (from the frontend/ folder)

Run it (from the backend/ folder, with the venv active):
    uvicorn main:app --reload
Then visit:  http://localhost:8000
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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
            "date": dropped.dropped_date.strftime("%Y-%m-%d"),
            "reason": dropped.reason,
        } for dropped, coin in results]

        return {"count": len(rows), "dropped": rows}
    finally:
        session.close()


# --- Serve the frontend page ----------------------------------------------
# This is added LAST so it doesn't shadow the /api routes above. Visiting "/"
# serves frontend/index.html. StaticFiles reads from disk per request, so
# editing the page doesn't require restarting the server.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
