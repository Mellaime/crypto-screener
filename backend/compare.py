"""
compare.py
----------
"Compare Coins" page: up to 5 tracked coins side by side. STORED DATA ONLY -
no API calls (the same tables the Active table and detail page read):

  price / market cap / volume / liquidity  latest daily snapshot
  score, vol/liq, vol/mcap                 scoring.score_snapshot (as the table)
  rank                                     position in the Active table (active only)
  entry zone                               entry_zone.get_entry_zone (stored history)
  price position (60 days)                 scoring.score_inputs price_pos (Price Cheapness)
  volume spike                             volume_anomaly (last 7 days)
  holders + weekly change                  daily snapshots
  revenue 30d                              revenue_cache (last saved; never fetched here)
  next unlock                              dropstab.get_unlocks
  exchange count                           listings.get_listings

Coins are identified by "slug": the lower-case ticker, or the contract when
another tracked coin shares that ticker.
"""

import json
from datetime import timedelta

MAX_COINS = 5


def slugs_for(session, coins, every=None):
    """{coin_id: slug} - lower-case ticker, contract when the ticker is shared.
    every = listings._tickers(session) when the caller already has it."""
    from listings import _tickers
    if every is None:
        every = _tickers(session)                               # all active + dropped
    count = {}
    for t in every.values():
        if t:
            count[t] = count.get(t, 0) + 1
    out = {}
    for c in coins:
        t = every.get(c.id) or ""
        out[c.id] = t.lower() if t and count.get(t) == 1 else (c.contract or "").lower()
    return out


def resolve(session, tokens, tick):
    """[(token, coin or None)] - each token is a slug (ticker) or a contract.
    Active coins win over dropped ones on a shared ticker. tick =
    listings._tickers(session)."""
    from sqlalchemy import func
    from models import Coin
    by_ticker = {}
    coins = {c.id: c for c in session.query(Coin).filter(Coin.id.in_(list(tick)))}
    for cid, t in tick.items():
        if t:
            by_ticker.setdefault(t.lower(), []).append(coins[cid])
    out = []
    for tok in tokens:
        t = tok.strip().lower()
        if not t:
            continue
        c = (session.query(Coin).filter(func.lower(Coin.contract) == t, Coin.status.in_(["active", "dropped"])).first()
             or next(iter(sorted(by_ticker.get(t, []), key=lambda c: c.status != "active")), None))
        out.append((tok.strip(), c))
    return out


def _holders(session, coin_id):
    from models import DailySnapshot
    hist = (session.query(DailySnapshot.date, DailySnapshot.holders)
            .filter(DailySnapshot.coin_id == coin_id, DailySnapshot.holders.isnot(None))
            .order_by(DailySnapshot.date).all())
    if not hist:
        return {"count": None, "weekly_change_pct": None}
    now_d, now_h = hist[-1]
    week = [h for d, h in hist if d <= now_d - timedelta(days=7)]
    pct = round((now_h - week[-1]) / week[-1] * 100, 2) if week and week[-1] else None
    return {"count": now_h, "weekly_change_pct": pct}


def _revenue_30d(session, coin):
    from models import RevenueCache
    row = (session.query(RevenueCache).filter_by(chain=(coin.chain or "").lower(),
                                                 contract=(coin.contract or "").lower()).first())
    if row is None:
        return None
    try:
        d = json.loads(row.data)
    except ValueError:
        return None
    return d.get("revenue_30d") if d.get("status") == "ok" else None


def _unlock(session, coin):
    from dropstab import get_unlocks
    u = get_unlocks(session, coin)
    if not u or u.get("status") != "ok":
        return {"status": "unknown"}
    n = u.get("next")
    if not n:
        return {"status": "none"}                                  # tracked, nothing upcoming
    return {"status": "ok", "date": n.get("at"), "amount": n.get("amount"), "usd": n.get("usd"),
            "supply_pct": n.get("supply_pct"), "daily": bool(n.get("daily"))}


def _ranks(session):
    """({coin_id: rank}, coins ranked) from the latest recorded day of
    score_history (the daily run's scores of the active coins - what the
    Active table ranks by; one query instead of re-scoring every coin)."""
    from sqlalchemy import func
    from models import Coin, ScoreHistory
    try:
        day = session.query(func.max(ScoreHistory.recorded_at)).scalar()
        if day is None:
            return {}, 0
        rows = (session.query(ScoreHistory.coin_id, ScoreHistory.score).join(Coin, Coin.id == ScoreHistory.coin_id)
                .filter(ScoreHistory.recorded_at == day, Coin.status == "active").all())
    except Exception:
        session.rollback()
        return {}, 0
    scores = [s for _, s in rows]
    return {cid: 1 + sum(1 for x in scores if x > s) for cid, s in rows}, len(rows)


def build(session, tokens):
    """The /api/compare payload (stored data only)."""
    from main import latest_snapshot
    from scoring import score_snapshot, previous_scores
    from entry_zone import get_entry_zone, ZONE_RANK, ZONES as ZONE_LABEL
    from volume_anomaly import get_recent_spike
    from listings import get_listings, _tickers
    tick = _tickers(session)
    pairs = resolve(session, tokens[:MAX_COINS * 2], tick)
    found = [c for _, c in pairs if c is not None]
    slugs = slugs_for(session, found, tick)
    ranks, rank_of = _ranks(session)
    out, errors, seen = [], [], set()
    for tok, c in pairs:
        if c is None:
            errors.append({"slug": tok, "error": f"Coin '{tok}' not tracked, skipped"})
            continue
        if c.id in seen or len(out) >= MAX_COINS:
            continue
        seen.add(c.id)
        snap = latest_snapshot(session, c.id)
        from scoring import stored_score_inputs
        inp = (stored_score_inputs(session, [c], {c.id: snap.date}).get(c.id) if snap else None) or {}
        # Score + ratios exactly as the Active table computes them (a dropped
        # coin: from its last snapshot, no delta / rank).
        b = score_snapshot(snap, c, inp) if snap else None
        prev = previous_scores(session, {c.id: snap.date}).get(c.id) if snap and c.status == "active" else None
        score = {"total": b["total"] if b else None,
                 "delta": round(b["total"] - prev, 1) if b and prev is not None else None}
        vol_liq, vol_mcap = (b or {}).get("vol_liq_ratio"), (b or {}).get("vol_mcap_pct")
        mcap, liq, vol = (snap.mcap, snap.liquidity, snap.volume) if snap else (None, None, None)
        ez = get_entry_zone(session, c)
        zone = ez.get("zone") if ez.get("status") == "ok" else None
        lst = get_listings(session, c, ticker=tick.get(c.id) or "")
        listed = [x["exchange"] for x in lst.get("listed", [])] if lst.get("status") == "ok" else None
        out.append({
            "slug": slugs.get(c.id), "id": c.id, "name": c.name, "symbol": tick.get(c.id) or None,
            "chain": c.chain, "contract": c.contract, "status": c.status,
            "price": c.price_usd, "mcap": mcap,
            "score": score, "rank": ranks.get(c.id) if c.status == "active" else None, "rank_of": rank_of,
            "volume_24h": vol, "vol_liq_ratio": vol_liq, "vol_mcap_pct": vol_mcap, "liquidity": liq,
            "entry_zone": {"zone": zone, "label": ZONE_LABEL.get(zone) if zone else None,
                           "rank": ZONE_RANK.get(zone) if zone else None},
            "price_position_pct": inp.get("price_pos"),
            "volume_spike": get_recent_spike(session, c.id),
            "holders": _holders(session, c.id),
            "revenue_30d": _revenue_30d(session, c),
            "next_unlock": _unlock(session, c),
            "exchange_count": len(listed) if listed is not None else None,
            "exchanges": listed, "exchanges_monitored": lst.get("monitored", 12),
        })
    return {"coins": out, "errors": errors, "max": MAX_COINS}
