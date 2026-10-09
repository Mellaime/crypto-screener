"""
narratives.py
-------------
"Narratives" tab: how each CoinGecko category (sector) performs over time.
CoinGecko only gives a category's 24h change, so we keep our OWN daily
history and compute the 7-day change from it. DISPLAY ONLY - the filter and
drop_logic never read it.

Daily job (scheduler.py) - save_daily_snapshot():
    ONE call to CoinGecko /coins/categories. Every category with a market cap
    becomes a narrative_snapshots row for today (date, category_id, name,
    market_cap, volume_24h, market_cap_change_24h). Re-running the same day
    overwrites today's rows. Rows older than KEEP_DAYS are deleted, so the
    table holds today + the previous KEEP_DAYS days per category.

Dashboard (/api/narratives) - get_narratives():
    Reads narrative_snapshots only. Sectors under MIN_SECTOR_MCAP ($50M) total
    market cap are cut first. Per sector: one slot per day for the last
    BAR_DAYS days (that day's 24h change + market cap, or empty if no
    snapshot yet). Market cap / volume = the newest snapshot;
    24h % = the Relative Strength 30-minute categories cache when it has the
    category (fresher), else the snapshot's value; 7d % = newest market cap vs
    the OLDEST snapshot we have (up to KEEP_DAYS days back).
"""

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("narratives")

KEEP_DAYS = 7          # keep today + the previous 7 days
BAR_DAYS = 7           # the page shows one bar per day for the last 7 days
# Hard, fixed server-side cut: sectors whose TOTAL market cap is under this
# never reach the page (tiny categories swing +2000% on a few hundred dollars).
# The daily snapshot still stores every category, so a sector that grows past
# the cut already has its history.
MIN_SECTOR_MCAP = 50_000_000


def _utc_today():
    return datetime.now(timezone.utc).date()


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def save_daily_snapshot(session):
    """One CoinGecko call -> today's narrative_snapshots rows; prune old ones.
    Returns {"saved": n, "deleted": n} (saved = None if CoinGecko failed)."""
    from models import NarrativeSnapshot
    from relative_strength import _cg_get

    today = _utc_today()
    cats, transient = _cg_get("/coins/categories")
    saved = None
    if isinstance(cats, list) and cats:
        existing = {r.category_id: r for r in
                    session.query(NarrativeSnapshot).filter(NarrativeSnapshot.date == today)}
        saved = 0
        for c in cats:
            cid, mcap = c.get("id"), _num(c.get("market_cap"))
            if not cid or not mcap:
                continue                         # no data for this category
            row = existing.get(cid)
            if row is None:
                row = NarrativeSnapshot(date=today, category_id=cid)
                session.add(row)
            row.category_name = c.get("name") or cid
            row.market_cap = mcap
            row.volume_24h = _num(c.get("volume_24h"))
            row.market_cap_change_24h = _num(c.get("market_cap_change_24h"))
            saved += 1
    else:
        log.warning("CoinGecko /coins/categories failed (%s) - no snapshot today",
                    "transient" if transient else "no data")
    deleted = (session.query(NarrativeSnapshot)
               .filter(NarrativeSnapshot.date < today - timedelta(days=KEEP_DAYS))
               .delete(synchronize_session=False))
    session.commit()
    return {"saved": saved, "deleted": deleted}


def run_narrative_snapshot():
    """Daily job entry point (own DB session)."""
    from database import SessionLocal
    session = SessionLocal()
    try:
        res = save_daily_snapshot(session)
        log.info("Narrative snapshots: saved %s categories, pruned %d old rows",
                 res["saved"], res["deleted"])
        return res
    finally:
        session.close()


def get_narratives(session):
    """The Narratives table: one row per category (total market cap >=
    MIN_SECTOR_MCAP), hottest 24h first, each with one slot per day for the
    last BAR_DAYS days (None = no snapshot that day yet)."""
    from sqlalchemy import func
    from models import NarrativeSnapshot

    try:
        latest = session.query(func.max(NarrativeSnapshot.date)).scalar()
    except Exception:                       # table missing (before the first daily run)
        session.rollback()
        latest = None
    if latest is None:
        return {"as_of": None, "rows": [], "days_of_history": 0, "bar_dates": [],
                "min_mcap": MIN_SECTOR_MCAP, "count_before": 0, "count_after": 0}

    rows = (session.query(NarrativeSnapshot)
            .filter(NarrativeSnapshot.date >= latest - timedelta(days=KEEP_DAYS)).all())
    by_cat = {}
    for r in rows:
        by_cat.setdefault(r.category_id, []).append(r)
    # Today's categories (the newest snapshot = what CoinGecko returned today).
    current = {cid: sorted(snaps, key=lambda r: r.date) for cid, snaps in by_cat.items()
               if any(r.date == latest for r in snaps)}

    # HARD CUT: sectors under MIN_SECTOR_MCAP total market cap never leave the
    # server - applied before anything is ranked or sorted.
    count_before = len(current)
    current = {cid: snaps for cid, snaps in current.items()
               if (snaps[-1].market_cap or 0) >= MIN_SECTOR_MCAP}
    count_after = len(current)
    log.info("Narratives: %d sectors before the $%dM market-cap cut, %d after",
             count_before, MIN_SECTOR_MCAP // 1_000_000, count_after)

    try:                                    # fresher 24h % from the shared 30-min cache
        from relative_strength import shared_market
        live = shared_market()[1]
    except Exception:
        live = {}

    bar_dates = [latest - timedelta(days=i) for i in range(BAR_DAYS - 1, -1, -1)]
    out = []
    for cid, snaps in current.items():
        now, oldest = snaps[-1], snaps[0]
        days = (now.date - oldest.date).days
        chg_7d = (round((now.market_cap - oldest.market_cap) / oldest.market_cap * 100, 2)
                  if days > 0 and oldest.market_cap else None)
        chg_24h = live.get(now.category_name, now.market_cap_change_24h)
        by_day = {r.date: r for r in snaps}
        daily = [None if d not in by_day else
                 {"date": d.isoformat(), "mcap": by_day[d].market_cap,
                  "chg": (None if by_day[d].market_cap_change_24h is None
                          else round(by_day[d].market_cap_change_24h, 2))}
                 for d in bar_dates]
        if daily[-1] is not None and chg_24h is not None:
            daily[-1]["chg"] = round(chg_24h, 2)   # today's bar = the 24h % shown next to it
        out.append({"id": cid, "name": now.category_name,
                    "chg_24h": None if chg_24h is None else round(chg_24h, 2),
                    "chg_7d": chg_7d, "days": days, "daily": daily,
                    "market_cap": now.market_cap, "volume_24h": now.volume_24h})
    out.sort(key=lambda x: (x["chg_24h"] is None, -(x["chg_24h"] or 0)))
    first = session.query(func.min(NarrativeSnapshot.date)).scalar()
    return {"as_of": latest.isoformat(), "rows": out,
            "days_of_history": (latest - first).days if first else 0,
            "bar_dates": [d.isoformat() for d in bar_dates],
            "min_mcap": MIN_SECTOR_MCAP, "count_before": count_before, "count_after": count_after}
