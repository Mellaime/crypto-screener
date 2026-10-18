"""
main.py
-------
Phase 2 - the WEB API (FastAPI). This serves the screener's data to the
frontend, and also serves the frontend page itself.

Endpoints:
    GET /api/coins    -> ACTIVE coins, ranked by score (highest first)
    GET /api/dropped  -> dropped coins with date + reason
    GET /api/queue    -> the next 600 coins waiting in the entry queue
    GET /api/check?contract=... -> "Check a Coin": the 7-step filter on any contract
    GET /api/narratives -> CoinGecko categories: 24h %, 7d % (our daily snapshots), mcap, volume
    GET /api/coin/<contract> -> one coin: header info, 15-day transaction bars,
                                Volume/MCap + Volume/Liquidity (4h, 24h, 7d, total),
                                Liquidity, CoinGecko + CMC rank vs last week,
                                Holders (total, weekly change, daily bars),
                                Revenue (DefiLlama: daily bars, MCap/Revenue, TVL),
                                Price Range (All-Time): CoinGecko ATH / ATL / current position,
                                Relative Strength: 24h change vs Bitcoin + its CoinGecko sector,
                                Supply (total, circulating %, max; burned + unlock placeholders)
    GET /             -> the dashboard web page (from the frontend/ folder)

Run it (from the backend/ folder, with the venv active):
    uvicorn main:app --reload
Then visit:  http://localhost:8000
"""

from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func

from database import SessionLocal
from models import Coin, DailySnapshot, DroppedCoin
from scoring import score_snapshot, score_inputs, previous_scores, score_history
from socials import x_url

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


def make_narrative(chain, mcap, vol_liq_ratio, arabic=False):
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

    if arabic:      # detail page: the same sentence in Arabic (chain name stays LTR)
        from relative_strength import _ltr
        size_ar = {"Large-cap": "عملة كبيرة القيمة السوقية", "Mid-cap": "عملة متوسطة القيمة السوقية",
                   "Small-cap": "عملة صغيرة القيمة السوقية"}[size]
        activity_ar = {"unknown activity": "نشاط تداول غير معروف",
                       "very high trading activity": "نشاط تداول مرتفع جداً",
                       "strong trading activity": "نشاط تداول قوي",
                       "moderate trading activity": "نشاط تداول معتدل",
                       "light trading activity": "نشاط تداول خفيف"}[activity]
        return f"{size_ar} على شبكة {_ltr(chain)}، مع {activity_ar}."
    return f"{size} token on {chain} with {activity}."


def reason_ar(reason):
    """A drop reason (drop_logic.py, stored in English) as an Arabic sentence.
    Numbers, %, dates, amounts and source names stay left-to-right (U+2066
    isolates). Unknown / older free-text reasons are returned unchanged."""
    import re
    from relative_strength import _ltr
    r = (reason or "").strip()

    def pct(v):
        return _ltr(v + "%")

    m = re.fullmatch(r"Crash - (.+) in (\d+) days", r)
    if m:
        parts = []
        for p in m.group(1).split(" and "):
            mm = re.fullmatch(r"(price|liquidity) down ([\d.]+)%", p.strip())
            if not mm:
                return reason
            parts.append(("السعر انخفض " if mm.group(1) == "price" else "السيولة انخفضت ")
                         + pct(mm.group(2)))
        return f"انهيار — {' و'.join(parts)} خلال {_ltr(m.group(2))} أيام"
    m = re.fullmatch(r"Liquidity crash - liquidity down ([\d.]+)% in (\d+) days", r)
    if m:
        return f"انهيار في السيولة — السيولة انخفضت {pct(m.group(1))} خلال {_ltr(m.group(2))} أيام"
    m = re.fullmatch(r"Volume falling \((\d+)-day average ([+-][\d.]+)% vs the previous \d+ days\)", r)
    if m:
        return (f"حجم التداول يتراجع (متوسط آخر {_ltr(m.group(1))} أيام {pct(m.group(2))}"
                f" مقارنة بـ{_ltr(m.group(1))} أيام قبلها)")
    m = re.fullmatch(r"Volume falling \(last (\d+) daily readings\)", r)
    if m:
        return f"حجم التداول يتراجع (آخر {_ltr(m.group(1))} قراءات يومية)"
    m = re.fullmatch(r"Liquidity down ([\d.]+)% since (\S+)", r)
    if m:
        return f"السيولة انخفضت {pct(m.group(1))} منذ {_ltr(m.group(2))}"
    m = re.fullmatch(r"Liquidity under (\$[\d,]+) \((\$[\d,]+) on Codex and DexScreener\)", r)
    if m:
        return (f"السيولة أقل من {_ltr(m.group(1))} ({_ltr(m.group(2))} على "
                f"{_ltr('Codex')} و{_ltr('DexScreener')})")
    m = re.fullmatch(r"Liquidity under (\$[\d,]+) \((\$[\d,]+) on DexScreener; Codex unreliable - source mismatch\)", r)
    if m:
        return (f"السيولة أقل من {_ltr(m.group(1))} ({_ltr(m.group(2))} على {_ltr('DexScreener')}؛ "
                f"قراءة {_ltr('Codex')} غير موثوقة)")
    m = re.fullmatch(r"Holders down ([\d.]+)% \((.+) -> (.+)\)", r)
    if m:
        return f"عدد الحاملين انخفض {pct(m.group(1))} ({_ltr(m.group(2) + ' → ' + m.group(3))})"
    m = re.fullmatch(r"Honeypot flagged \((.*)\)", r)
    if m:
        return f"مُصنَّفة كفخ (honeypot) لا يمكن البيع منه ({_ltr(m.group(1))})"
    return reason


def _iso(dt):
    """Datetime -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC), or None."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


# ---------------------------------------------------------------- API caching
# The data changes once a day (daily run ~11:00 UTC), so read-only responses
# are cached by Vercel's CDN (s-maxage) and served stale for a bit while a
# fresh copy is fetched - fewer function runs, far less Supabase egress.
# Writes (POST / DELETE) and /api/check (its own per-day cache) are not.
import re as _re
CACHE_RULES = [
    (_re.compile(r"^/api/coins$"), 300, 60),
    (_re.compile(r"^/api/dropped$"), 300, 60),
    (_re.compile(r"^/api/queue$"), 300, 60),
    (_re.compile(r"^/api/coin/[^/]+$"), 300, 60),
    (_re.compile(r"^/api/coins/\d+/score-history$"), 300, 60),
    (_re.compile(r"^/api/compare$"), 300, 60),
    (_re.compile(r"^/api/unlocks$"), 600, 120),
    (_re.compile(r"^/api/favorites/profiles$"), 60, 30),
    (_re.compile(r"^/api/favorites/profiles/[^/]+$"), 60, 30),
]


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.method == "GET" and response.status_code == 200 and "cache-control" not in response.headers:
        for pattern, max_age, stale in CACHE_RULES:
            if pattern.match(request.url.path):
                response.headers["Cache-Control"] = f"public, s-maxage={max_age}, stale-while-revalidate={stale}"
                break
    return response


@app.get("/api/coins")
def get_coins():
    """Return ACTIVE coins only, ranked by score (highest first)."""
    session = SessionLocal()
    try:
        coins = session.query(Coin).filter_by(status="active").all()

        # 🆕 = first recorded during the most recent filter run (run_log.run_at
        # is stamped when that run's saving finishes; a run takes < 3 h). Uses
        # first_seen, so a re-homed coin (fresh active_since) isn't "new".
        from datetime import timedelta
        from models import RunLog
        last_run = session.query(RunLog.run_at).order_by(RunLog.run_at.desc()).first()
        new_from = new_to = None
        if last_run:
            new_from, new_to = last_run[0] - timedelta(hours=3), last_run[0] + timedelta(minutes=5)

        rows = []
        snaps = {c.id: latest_snapshot(session, c.id) for c in coins}
        # Score inputs saved by the daily scoring step (recomputed only for a
        # coin whose saved row isn't from its latest snapshot day).
        from scoring import stored_score_inputs
        inputs = stored_score_inputs(session, coins, {cid: s.date for cid, s in snaps.items() if s})
        for coin in coins:
            snap = snaps[coin.id]
            if snap is None:
                continue

            # fresh score + ratios
            b = score_snapshot(snap, coin, inputs.get(coin.id))

            # A single rank to display = average of the sources we have.
            rank_vals = [x for x in (coin.cg_rank, coin.cmc_rank) if x is not None]
            rank_display = round(sum(rank_vals) / len(rank_vals)) if rank_vals else None

            rows.append({
                "id": coin.id,
                "_snap_day": snap.date,
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
                "is_new": bool(new_from and coin.first_seen
                               and new_from <= coin.first_seen <= new_to),
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

        # Score vs the previous daily run (score_history: the latest recorded
        # day before this coin's snapshot day) -> the table's delta arrow.
        prev = previous_scores(session, {r["id"]: r.pop("_snap_day") for r in rows})
        for r in rows:
            y = prev.get(r["id"])
            r["score_yesterday"] = None if y is None else round(y, 1)
            r["score_delta"] = None if y is None else round(r["score"] - y, 1)

        # Exchange listing badges (🆕 new listing / 📅 scheduled), stored.
        from listings import badges
        xb = badges(session, [r["id"] for r in rows])
        for r in rows:
            r["exchange_badges"] = xb.get(r["id"], [])
        # Volume spike of the last 7 days (🚨 / ⚠️ / 📈 badge), stored.
        from volume_anomaly import recent_spikes
        vs = recent_spikes(session, [r["id"] for r in rows])
        for r in rows:
            r["volume_spike"] = vs.get(r["id"])
        _add_symbols(session, rows, coins)
        # Performance since the coin entered Active (stored prices, no API call).
        from performance import safe_compute, brief
        perf = safe_compute(session, coins)
        for r in rows:
            r["performance"] = brief(perf.get(r["id"]))

        # Highest score first, then attach a rank number.
        rows.sort(key=lambda r: r["score"], reverse=True)
        for i, r in enumerate(rows, start=1):
            r["rank"] = i

        return {"count": len(rows), "coins": rows}
    finally:
        session.close()


def _add_symbols(session, rows, coins):
    """Ticker ("symbol") + Compare page "slug" on each table row (stored; no
    API call). Never raises."""
    try:
        from compare import slugs_for
        from listings import _tickers
        tick = _tickers(session)
        slugs = slugs_for(session, coins, tick)
        for r in rows:
            r["symbol"], r["slug"] = tick.get(r["id"]) or None, slugs.get(r["id"])
    except Exception:
        session.rollback()


# ---------------------------------------------------------------- favorites profiles
def _client_ip(request: Request):
    """The caller's IP (Vercel puts it first in X-Forwarded-For)."""
    fwd = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or ""
    return (fwd.split(",")[0].strip() or (request.client.host if request.client else "?"))[:64]


def _fav(fn, *args, **kwargs):
    """Run a favorites.py action in its own session; FavError -> HTTP error."""
    from favorites import FavError
    session = SessionLocal()
    try:
        return fn(session, *args, **kwargs)
    except FavError as e:
        session.rollback()
        raise HTTPException(status_code=e.status, detail=e.message)
    finally:
        session.close()


async def _body(request: Request):
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


@app.get("/api/favorites/profiles")
def fav_list_profiles():
    from favorites import list_profiles
    return _fav(list_profiles)


@app.post("/api/favorites/profiles")
async def fav_create_profile(request: Request):
    from favorites import create_profile
    b = await _body(request)
    return _fav(create_profile, b.get("name"), b.get("pin"))


@app.post("/api/favorites/profiles/{name}/verify")
async def fav_verify(name: str, request: Request):
    from favorites import verify_pin
    b = await _body(request)
    return _fav(verify_pin, name, b.get("pin"), _client_ip(request))


@app.get("/api/favorites/profiles/{name}")
def fav_get_profile(name: str):
    from favorites import get_profile
    return _fav(get_profile, name)


@app.post("/api/favorites/profiles/{name}/coins")
async def fav_add_coin(name: str, request: Request):
    from favorites import add_coin
    b = await _body(request)
    return _fav(add_coin, name, b.get("contract"), token=b.get("session_token"), pin=b.get("pin"),
                ip=_client_ip(request))


@app.delete("/api/favorites/profiles/{name}/coins/{coin_ref}")
async def fav_remove_coin(name: str, coin_ref: str, request: Request):
    from favorites import remove_coin
    b = await _body(request)
    token = b.get("session_token") or request.headers.get("x-session-token")
    return _fav(remove_coin, name, coin_ref, token=token, pin=b.get("pin"), ip=_client_ip(request))


@app.post("/api/favorites/profiles/{name}/reset")
async def fav_reset(name: str, request: Request):
    """Wipe the profile's coins and restart its portfolio (PIN always required)."""
    from favorites import reset_profile
    b = await _body(request)
    return _fav(reset_profile, name, b.get("pin"), _client_ip(request))


@app.delete("/api/favorites/profiles/{name}")
async def fav_delete_profile(name: str, request: Request):
    from favorites import delete_profile
    b = await _body(request)
    return _fav(delete_profile, name, b.get("pin"), _client_ip(request))


@app.get("/api/compare")
def compare_coins(coins: str = ""):
    """Compare Coins page: up to 5 tracked coins side by side, by slug
    (lower-case ticker) or contract, comma-separated. Stored data only."""
    from compare import build
    session = SessionLocal()
    try:
        return build(session, [t for t in coins.split(",") if t.strip()])
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
        from listings import badges
        xb = badges(session, [coin.id for _, coin in results])
        rows = [{
            "id": coin.id,
            "name": coin.name,
            "chain": coin.chain,
            "contract": coin.contract,              # opens the coin's detail page
            "exchange_badges": xb.get(coin.id, []),  # 🆕 / 📅 exchange listings
            "price": coin.price_usd,                # current USD price (refreshed each run)
            "price_updated": _iso(coin.price_updated_at),
            "date": dropped.dropped_date.strftime("%Y-%m-%d"),
            "reason": dropped.reason,
            "reason_ar": reason_ar(dropped.reason),
        } for dropped, coin in results]

        _add_symbols(session, rows, [coin for _, coin in results])
        from performance import safe_compute, brief
        perf = safe_compute(session, [coin for _, coin in results])
        for r in rows:
            r["performance"] = brief(perf.get(r["id"]))   # change up to the day it was dropped
        return {"count": len(rows), "dropped": rows}
    finally:
        session.close()


HOLDER_BAR_DAYS = 30        # holders bars: up to the last 30 stored days


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
        from scoring import stored_score_inputs
        b = score_snapshot(snap, coin, stored_score_inputs(session, [coin], {coin.id: snap.date}).get(coin.id)) if snap else None
        q = (session.query(CoinQueue.symbol)
             .filter_by(chain=(coin.chain or "").lower(), contract=coin.contract.lower())
             .first())
        # The five outside lookups (Codex daily bars, Codex live stats,
        # DefiLlama revenue, CoinGecko all-time price range + relative
        # strength) run AT THE SAME TIME - each in its own DB session
        # (a session can't be shared between threads) - so a first open waits
        # for the slowest one, not the sum of all five.
        from revenue_detail import get_revenue
        from price_range import get_price_range
        from relative_strength import get_rel_strength
        from entry_zone import get_entry_zone
        from dropstab import get_unlocks
        from coinalyze import futures_data
        from listings import get_listings
        from volume_anomaly import get_recent_spike
        from performance import safe_compute
        from concurrent.futures import ThreadPoolExecutor

        def own_session(fn, *args):
            s2 = SessionLocal()
            try:
                return fn(s2, *args)
            finally:
                s2.close()
        snap_mcap = snap.mcap if snap else None
        with ThreadPoolExecutor(max_workers=6) as pool:
            # Entry Zone reads the coin's snapshots, so it gets its own session too.
            f_ez = pool.submit(lambda: own_session(lambda s2: get_entry_zone(
                s2, s2.get(Coin, coin.id))))
            f_tx = pool.submit(own_session, get_tx_bars, coin.contract, coin.chain)
            f_live = pool.submit(own_session, get_live_volume, coin.contract, coin.chain)
            f_rev = pool.submit(own_session, get_revenue, coin.contract, coin.chain, snap_mcap)
            f_pr = pool.submit(own_session, get_price_range, coin.contract, coin.chain)
            tx, live, revenue = f_tx.result(), f_live.result(), f_rev.result()
            f_rs = pool.submit(own_session, get_rel_strength, coin.contract, coin.chain)
            price_range = f_pr.result()
            rel_strength = f_rs.result()
            entry_zone = f_ez.result()

        # Volume / Market cap (%) and Volume / Liquidity (x) over 4 windows.
        # Market cap = the stored CoinGecko/CMC (global) one; liquidity = the
        # latest Codex reading tracking stored (DexScreener's if none).
        mcap = snap.mcap if snap else None
        liq = (snap.codex_liquidity or snap.liquidity) if snap else None
        volumes = {"4h": live["vol_4h"], "24h": live["vol_24h"],
                   "7d": tx["vol_7d"], "total": tx["vol_total"]}

        def ratio(v, base, scale):
            return round(v / base * scale, 4) if v is not None and base else None

        # Rank: current (stored on the coin by rank_check) vs the latest rank
        # stored for an EARLIER ISO week in rank_history (weekly, same rule
        # as the Active table's rank change). change = was - now, so a
        # positive number = moved up (a smaller rank number is better).
        from models import RankHistory
        from rank_check import _iso_week

        # Holders: entirely from OUR daily snapshots (holder count = CMC DEX,
        # stored by the daily holders update) - no API call here.
        hist = (session.query(DailySnapshot.date, DailySnapshot.holders)
                .filter(DailySnapshot.coin_id == coin.id, DailySnapshot.holders.isnot(None))
                .order_by(DailySnapshot.date).all())
        holder_bars, prev_h = [], None
        for d, h in hist[-HOLDER_BAR_DAYS:]:
            pct = (h - prev_h) / prev_h * 100 if prev_h else None
            holder_bars.append({"date": d.isoformat(), "holders": h,
                                "change_pct": None if pct is None else round(pct, 3)})
            prev_h = h
        holders_now = hist[-1][1] if hist else None
        from holders_check import CHAIN_TO_PLATFORM as HOLDER_CHAINS
        holders_missing_reason = None
        if holders_now is None:
            from relative_strength import _ltr
            holders_missing_reason = (
                f"{_ltr('CoinMarketCap')} لا يتتبّع عدد الحاملين على شبكة {_ltr(coin.chain)}."
                if (coin.chain or "").lower() not in HOLDER_CHAINS else
                f"لم يُحفظ عدد الحاملين بعد — لم يُرجع {_ltr('CoinMarketCap')} رقماً لهذه "
                "العملة. التشغيل اليومي يعيد المحاولة كل يوم.")
        week_ago = [(d, h) for d, h in hist if hist and d <= hist[-1][0] - timedelta(days=7)]
        holders_7d = week_ago[-1][1] if week_ago else None
        holders_week_pct = (round((holders_now - holders_7d) / holders_7d * 100, 2)
                            if holders_now is not None and holders_7d else None)

        def rank_box(source, current):
            prev = (session.query(RankHistory.rank)
                    .filter(RankHistory.coin_id == coin.id, RankHistory.source == source,
                            RankHistory.week < _iso_week(), RankHistory.rank.isnot(None))
                    .order_by(RankHistory.week.desc()).first())
            was = prev[0] if prev else None
            change = (was - current) if was is not None and current is not None else None
            return {"rank": current, "was": was, "change": change}

        # Liquidity section. Total + 7d growth + weekly Liq/MCap all from the
        # same Codex getBars series (same level as the tracked liquidity); the
        # 4h / 24h growth is getDetailedTokenStats' own now-vs-then change.
        liq_now, liq_7d = tx["liq_now"], tx["liq_7d_ago"]
        snap_7d = None
        if snap is not None and tx["liq_7d_ago_date"]:
            snap_7d = (session.query(DailySnapshot)
                       .filter(DailySnapshot.coin_id == coin.id,
                               DailySnapshot.date <= snap.date - timedelta(days=7),
                               DailySnapshot.mcap.isnot(None))
                       .order_by(DailySnapshot.date.desc()).first())
        mcap_7d = snap_7d.mcap if snap_7d else mcap       # fallback: today's
        growth_7d = ((liq_now - liq_7d) / liq_7d * 100) if liq_now is not None and liq_7d else None
        this_week, last_week = ratio(liq_now, mcap, 100), ratio(liq_7d, mcap_7d, 100)
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
            "score_history": score_history(session, coin.id),   # last 7 days, newest first
            "category": coin.category,
            "twitter": x_url(coin.twitter_handle),  # https://x.com/<handle>, or None
            "twitter_handle": coin.twitter_handle,
            "narrative": make_narrative(coin.chain, snap.mcap if snap else None,
                                        b["vol_liq_ratio"] if b else None, arabic=True),
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
            "revenue": revenue,
            "price_range": price_range,
            "rel_strength": rel_strength,
            "entry_zone": entry_zone,
            "unlocks": get_unlocks(session, coin),   # DropsTab, stored (no API call)
            "futures": futures_data(session, coin.id),  # Coinalyze, stored (no API call)
            "listings": get_listings(session, coin),    # exchange listings, stored (no API call)
            "volume_spike": get_recent_spike(session, coin.id),  # last 7 days, stored (no API call)
            "performance": safe_compute(session, [coin]).get(coin.id),  # since it entered Active
            "supply": {
                "total": coin.total_supply,
                "circulating": coin.circulating_supply,
                "circulating_pct": (round(coin.circulating_supply / coin.total_supply * 100, 2)
                                    if coin.circulating_supply is not None and coin.total_supply else None),
                "max": coin.max_supply,
                # No max: "no cap" unless CMC says the supply IS capped (then unknown).
                "uncapped": coin.max_supply is None and coin.supply_uncapped is not False
                            and (coin.total_supply is not None or coin.circulating_supply is not None),
                "burned": None,             # placeholder - no free source yet
                "upcoming_unlock": None,    # placeholder - needs a paid unlocks API
                "updated": _iso(coin.supply_updated_at),
            },
            "holders": {"now": holders_now, "week_ago": holders_7d,
                        "week_change_pct": holders_week_pct, "bars": holder_bars,
                        "missing_reason": holders_missing_reason},
            "ranks": {"coingecko": rank_box("coingecko", coin.cg_rank),
                      "cmc": rank_box("cmc", coin.cmc_rank)},
            "liquidity_now": liq_now if liq_now is not None else liq,
            "liq_growth_pct": {"4h": live["liq_chg_4h"], "24h": live["liq_chg_24h"],
                               "7d": growth_7d},
            "liq_mcap_pct": {"this_week": this_week, "last_week": last_week,
                             "change": (round(this_week - last_week, 4)
                                        if this_week is not None and last_week is not None else None),
                             "last_week_mcap_approx": snap_7d is None},
        }
    finally:
        session.close()


@app.get("/api/coins/{coin_id}/score-history")
def get_score_history(coin_id: int):
    """A coin's last 7 recorded daily scores (score_history), oldest first."""
    session = SessionLocal()
    try:
        return {"coin_id": coin_id, "history": list(reversed(score_history(session, coin_id)))}
    finally:
        session.close()


@app.get("/api/check")
def check_coin(contract: str = ""):
    """'Check a Coin': run the 7-step filter + analysis on any contract, on
    demand (chain auto-detected). Read-only for the coin lists; the result is
    cached per contract per UTC day."""
    from coin_check import get_check
    session = SessionLocal()
    try:
        return get_check(session, contract)
    finally:
        session.close()


@app.get("/api/unlocks")
def get_all_unlocks():
    """Token Unlocks page: every coin in DropsTab's unlock overview (stored by
    the daily run) with its next unlock - no DropsTab call; cached 10 min."""
    from dropstab import all_unlocks
    session = SessionLocal()
    try:
        return all_unlocks(session)
    finally:
        session.close()


@app.get("/api/queue")
def get_queue():
    """The next 600 coins in the entry queue (the next 2 daily runs), in the
    order they'll be analyzed. Reads stored data only - no API calls."""
    from queue_view import upcoming, last_run, QUEUE_DISPLAY_LIMIT
    session = SessionLocal()
    try:
        rows = upcoming(session)
        return {"count": len(rows), "limit": QUEUE_DISPLAY_LIMIT, "queue": rows,
                "last_run": last_run(session)}
    finally:
        session.close()


# --- Never serve a stale dashboard -----------------------------------------
# Browsers may otherwise keep showing an old copy of the page after an
# update. "no-cache" = always check with the server first (it still answers
# "not modified" when nothing changed, so it costs almost nothing).
@app.middleware("http")
async def no_stale_pages(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


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
