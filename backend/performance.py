"""
performance.py
--------------
"Performance Since Caught": how much a coin's price moved since it entered
the Active list. DISPLAY ONLY. No API calls - stored data:

  entry date   coins.active_since (the MOST RECENT entry: a coin that was
               dropped and came back starts fresh)
  entry price  the first daily_snapshots.price_usd on or after that day; if
               there is none, the closest snapshot within ENTRY_WINDOW_DAYS
  now          active coin: coins.price_usd (refreshed by every run)
               dropped coin: its price on the day it was dropped (the last
               snapshot on or before the drop) - "the change when it was dropped"
  high / low   highest / lowest snapshot price from the entry day to now
               (to the drop day for a dropped coin)

change within +-FLAT_PCT counts as "flat".
"""

from datetime import datetime, timedelta, timezone

ENTRY_WINDOW_DAYS = 2      # no snapshot on/after the entry day -> nearest within +-2 days
FLAT_PCT = 1.0             # |change| below this = flat


def _day(v):
    return v.date() if isinstance(v, datetime) else v


def compute(session, coins):
    """{coin_id: full performance dict or None} for many coins (two queries)."""
    from models import DailySnapshot, DroppedCoin
    coins = [c for c in coins if c.active_since is not None]
    if not coins:
        return {}
    ids = [c.id for c in coins]
    start = min(_day(c.active_since) for c in coins) - timedelta(days=ENTRY_WINDOW_DAYS)
    series = {}
    for cid, d, p in (session.query(DailySnapshot.coin_id, DailySnapshot.date, DailySnapshot.price_usd)
                      .filter(DailySnapshot.coin_id.in_(ids), DailySnapshot.price_usd.isnot(None),
                              DailySnapshot.price_usd > 0, DailySnapshot.date >= start)
                      .order_by(DailySnapshot.date)):
        series.setdefault(cid, []).append((d, p))
    dropped_on = {}
    for cid, dd in (session.query(DroppedCoin.coin_id, DroppedCoin.dropped_date)
                    .filter(DroppedCoin.coin_id.in_([c.id for c in coins if c.status == "dropped"]))):
        if dd is not None and (cid not in dropped_on or _day(dd) > dropped_on[cid]):
            dropped_on[cid] = _day(dd)
    today = datetime.now(timezone.utc).date()
    out = {}
    for c in coins:
        rows = series.get(c.id) or []
        entry_day = _day(c.active_since)
        after = [(d, p) for d, p in rows if d >= entry_day]
        near = [(d, p) for d, p in rows if abs((d - entry_day).days) <= ENTRY_WINDOW_DAYS]
        entry = after[0] if after and (after[0][0] - entry_day).days <= ENTRY_WINDOW_DAYS else (
            min(near, key=lambda r: abs((r[0] - entry_day).days)) if near else None)
        if entry is None:
            out[c.id] = None
            continue
        if c.status == "dropped" and c.id in dropped_on:
            end_day = dropped_on[c.id]
            upto = [(d, p) for d, p in rows if entry[0] <= d <= end_day]
            if not upto:
                out[c.id] = None
                continue
            now_day, now_price = upto[-1]
        else:
            end_day, now_price = today, c.price_usd
            now_day = _day(c.price_updated_at) if c.price_updated_at else today
            upto = [(d, p) for d, p in rows if d >= entry[0]]
        if not now_price:
            out[c.id] = None
            continue
        hi = max(upto, key=lambda r: r[1]) if upto else (now_day, now_price)
        lo = min(upto, key=lambda r: r[1]) if upto else (now_day, now_price)
        pct = (now_price - entry[1]) / entry[1] * 100
        out[c.id] = {
            "entry_date": entry_day.isoformat(), "entry_snapshot_date": entry[0].isoformat(),
            "entry_price": entry[1], "days_held": max(0, (end_day - entry_day).days),
            "current_price": now_price, "price_date": now_day.isoformat(),
            "change_pct": round(pct, 2), "change_usd": now_price - entry[1],
            "status": "flat" if abs(pct) < FLAT_PCT else "up" if pct > 0 else "down",
            "highest_since": {"price": hi[1], "date": hi[0].isoformat()},
            "lowest_since": {"price": lo[1], "date": lo[0].isoformat()},
            "dropped": c.status == "dropped",
        }
    return out


def live_pnl(entry_price, live_price):
    """Hourly P&L calculation for Active table"""
    if not entry_price or not live_price:
        return None
    return (live_price - entry_price) / entry_price * 100


def brief(p):
    """The tables' short version."""
    if not p:
        return None
    return {k: p[k] for k in ("entry_date", "entry_price", "days_held", "change_pct", "status",
                              "current_price", "dropped")}


def safe_compute(session, coins):
    """compute() that never breaks a page: {} on any error."""
    try:
        return compute(session, coins)
    except Exception:
        session.rollback()
        return {}
