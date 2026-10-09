"""
narratives.py
-------------
"Narratives" tab: how each CoinGecko category (sector) moves day by day.
CoinGecko only gives a category's CURRENT numbers, so we keep our OWN daily
history. DISPLAY ONLY - the filter and drop_logic never read it.

Daily job (scheduler.py) - save_daily_snapshot():
    ONE call to CoinGecko /coins/categories. Only sectors whose total market
    cap is at least MIN_SECTOR_MCAP ($200M) are stored - one
    narrative_snapshots row per sector for today (date, category_id, name,
    market_cap, volume_24h, market_cap_change_24h). Re-running the same day
    overwrites today's rows (and removes any of today's rows that are now under
    the cut). Rows older than KEEP_DAYS are deleted.

Dashboard (/api/narratives) - get_narratives():
    Reads narrative_snapshots only (the same $200M cut is applied again before
    sorting, so nothing under it ever reaches the page). Per sector, one slot
    per day for the last BAR_DAYS days:
        mcap     = that day's market cap            -> the bar's height
        volume   = that day's 24h volume
        mcap_chg = market cap vs the PREVIOUS day's, in %
        vol_chg  = 24h volume vs the PREVIOUS day's, in %
    (both changes shown in the bar's tooltip). The first slot (day 1) has no
    changes; neither does a day whose previous day has no snapshot.
    Rows are sorted hottest first by CoinGecko's 24h market-cap change (the
    Relative Strength 30-minute categories cache when it has the sector, else
    the snapshot's value); that number itself isn't shown.
"""

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("narratives")

KEEP_DAYS = 7          # keep today + the previous 7 days
BAR_DAYS = 7           # the page shows one bar per day for the last 7 days
# Hard, fixed server-side cut: sectors whose TOTAL market cap is under this
# are neither stored nor shown (small categories swing wildly on tiny volume).
MIN_SECTOR_MCAP = 200_000_000


def _utc_today():
    return datetime.now(timezone.utc).date()


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def save_daily_snapshot(session):
    """One CoinGecko call -> today's rows for sectors >= MIN_SECTOR_MCAP;
    prune old rows. Returns {"fetched", "saved", "removed_today", "deleted"}
    (saved = None if CoinGecko failed - then nothing of today's is touched)."""
    from models import NarrativeSnapshot
    from relative_strength import _cg_get

    today = _utc_today()
    cats, transient = _cg_get("/coins/categories")
    res = {"fetched": None, "saved": None, "removed_today": 0, "deleted": 0}
    if isinstance(cats, list) and cats:
        res["fetched"] = len(cats)
        kept = [c for c in cats if c.get("id") and (_num(c.get("market_cap")) or 0) >= MIN_SECTOR_MCAP]
        existing = {r.category_id: r for r in
                    session.query(NarrativeSnapshot).filter(NarrativeSnapshot.date == today)}
        for c in kept:
            row = existing.pop(c["id"], None)
            if row is None:
                row = NarrativeSnapshot(date=today, category_id=c["id"])
                session.add(row)
            row.category_name = c.get("name") or c["id"]
            row.market_cap = _num(c.get("market_cap"))
            row.volume_24h = _num(c.get("volume_24h"))
            row.market_cap_change_24h = _num(c.get("market_cap_change_24h"))
        for row in existing.values():            # today's rows now under the cut
            session.delete(row)
        res["saved"], res["removed_today"] = len(kept), len(existing)
    else:
        log.warning("CoinGecko /coins/categories failed (%s) - no snapshot today",
                    "transient" if transient else "no data")
    res["deleted"] = (session.query(NarrativeSnapshot)
                      .filter(NarrativeSnapshot.date < today - timedelta(days=KEEP_DAYS))
                      .delete(synchronize_session=False))
    session.commit()
    return res


def run_narrative_snapshot():
    """Daily job entry point (own DB session)."""
    from database import SessionLocal
    session = SessionLocal()
    try:
        res = save_daily_snapshot(session)
        log.info("Narrative snapshots: %s categories fetched, %s sectors >= $%dM saved, "
                 "%d of today's removed (under the cut), %d old rows pruned",
                 res["fetched"], res["saved"], MIN_SECTOR_MCAP // 1_000_000,
                 res["removed_today"], res["deleted"])
        return res
    finally:
        session.close()


def _pct(new, old):
    return round((new - old) / old * 100, 1) if new is not None and old else None


def get_narratives(session):
    """The Narratives table: one row per sector (total market cap >=
    MIN_SECTOR_MCAP), hottest 24h first, each with BAR_DAYS daily slots."""
    from sqlalchemy import func
    from models import NarrativeSnapshot

    empty = {"as_of": None, "rows": [], "days_of_history": 0, "bar_dates": [],
             "min_mcap": MIN_SECTOR_MCAP, "count_before": 0, "count_after": 0}
    try:
        latest = session.query(func.max(NarrativeSnapshot.date)).scalar()
    except Exception:                       # table missing (before the first daily run)
        session.rollback()
        return empty
    if latest is None:
        return empty

    bar_dates = [latest - timedelta(days=i) for i in range(BAR_DAYS - 1, -1, -1)]
    rows = (session.query(NarrativeSnapshot)
            .filter(NarrativeSnapshot.date >= bar_dates[0]).all())
    by_cat = {}
    for r in rows:
        by_cat.setdefault(r.category_id, {})[r.date] = r
    current = {cid: days for cid, days in by_cat.items() if latest in days}

    # HARD CUT before anything is ranked or sorted.
    count_before = len(current)
    current = {cid: days for cid, days in current.items()
               if (days[latest].market_cap or 0) >= MIN_SECTOR_MCAP}
    count_after = len(current)
    log.info("Narratives: %d sectors before the $%dM market-cap cut, %d after",
             count_before, MIN_SECTOR_MCAP // 1_000_000, count_after)

    try:                                    # fresher 24h % from the shared 30-min cache
        from relative_strength import shared_market
        live = shared_market()[1]
    except Exception:
        live = {}

    out = []
    for cid, days in current.items():
        now = days[latest]
        daily = []
        for i, d in enumerate(bar_dates):
            r = days.get(d)
            if r is None:
                daily.append(None)
                continue
            # day 1 has no previous day on the chart -> no changes
            prev = None if i == 0 else days.get(d - timedelta(days=1))
            daily.append({"date": d.isoformat(), "mcap": r.market_cap, "volume": r.volume_24h,
                          "mcap_chg": None if prev is None else _pct(r.market_cap, prev.market_cap),
                          "vol_chg": None if prev is None else _pct(r.volume_24h, prev.volume_24h)})
        chg_24h = live.get(now.category_name, now.market_cap_change_24h)
        out.append({"id": cid, "name": now.category_name,
                    "chg_24h": None if chg_24h is None else round(chg_24h, 2),
                    "daily": daily, "market_cap": now.market_cap, "volume_24h": now.volume_24h})
    out.sort(key=lambda x: (x["chg_24h"] is None, -(x["chg_24h"] or 0)))
    first = session.query(func.min(NarrativeSnapshot.date)).scalar()
    return {"as_of": latest.isoformat(), "rows": out,
            "days_of_history": min(BAR_DAYS, (latest - first).days + 1) if first else 0,
            "bar_dates": [d.isoformat() for d in bar_dates],
            "min_mcap": MIN_SECTOR_MCAP, "count_before": count_before, "count_after": count_after}
