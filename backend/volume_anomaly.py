"""
volume_anomaly.py
-----------------
"Volume Alert": coins whose trading volume today is unusually high compared
with their OWN recent average. DISPLAY + ALERT ONLY - the filter, drop logic
and score never use it. No API calls: it reads daily_snapshots.volume (the
DexScreener 24h volume the Vol/Liq score uses), stored by the daily run.

  spike ratio = today's volume / average volume of the previous 30 days
  >= 5x  -> "big"       🚨 Telegram alert + badge
  3-5x   -> "medium"    ⚠️ Telegram alert + badge
  2-3x   -> "elevated"  📈 badge only
  < 2x   -> nothing stored

Dollar floors (top of the code): nothing below MIN_STORE_VOLUME_USD of
volume that day; Telegram alerts only from MIN_ALERT_VOLUME_USD.

Only coins with at least MIN_HISTORY_DAYS earlier days of volume qualify
(snapshots started 2026-10-05, so the first coins qualify ~2026-10-20). A
coin is alerted at most once every REALERT_DAYS days. The table, badge and
card show a spike for SHOW_DAYS days.
"""

import logging
from datetime import timedelta

log = logging.getLogger("volume_anomaly")

# DOLLAR FLOORS (tune here). A big multiple on a tiny base is noise
# (edeXa $40 -> $138 = 3.5x):
MIN_STORE_VOLUME_USD = 5_000    # that day's volume below this: nothing stored, no badge, no alert
MIN_ALERT_VOLUME_USD = 20_000   # 🚨 / ⚠️ Telegram alert only at or above this (badge from $5K)

AVG_DAYS = 30              # the average covers the previous 30 days
MIN_HISTORY_DAYS = 14      # earlier days with volume needed to qualify
REALERT_DAYS = 3           # no second alert for the same coin within this
SHOW_DAYS = 7              # a spike is shown this many days
KEEP_DAYS = 90             # volume_spikes rows kept
LEVELS = [(5.0, "big"), (3.0, "medium"), (2.0, "elevated")]
ALERT_LEVELS = ("big", "medium")


def severity_of(ratio):
    for low, name in LEVELS:
        if ratio is not None and ratio >= low:
            return name
    return None


def calculate_spike(session, coin, day=None):
    """{"day", "volume_today", "volume_avg_30d", "spike_ratio", "severity",
    "history_days"} for the coin on `day` (default: its latest snapshot), or
    None when there is no volume that day / fewer than MIN_HISTORY_DAYS
    earlier days / a zero average."""
    from models import DailySnapshot
    q = session.query(DailySnapshot.date, DailySnapshot.volume).filter(
        DailySnapshot.coin_id == coin.id, DailySnapshot.volume.isnot(None))
    if day is None:
        last = q.order_by(DailySnapshot.date.desc()).first()
        if last is None:
            return None
        day = last[0]
    rows = q.filter(DailySnapshot.date <= day, DailySnapshot.date >= day - timedelta(days=AVG_DAYS)).all()
    today = [v for d, v in rows if d == day]
    earlier = [v for d, v in rows if d < day]
    if not today or len(earlier) < MIN_HISTORY_DAYS:
        return None
    avg = sum(earlier) / len(earlier)
    if avg <= 0:
        return None
    ratio = today[0] / avg
    return {"day": day, "volume_today": today[0], "volume_avg_30d": avg, "spike_ratio": round(ratio, 2),
            "severity": severity_of(ratio), "history_days": len(earlier)}


def run_volume_anomaly_detection():
    """Daily job step (after scoring). Returns stats with "alerts" =
    [(name, severity, ratio, volume_today, avg)] for the Telegram report."""
    from sqlalchemy import func
    from database import SessionLocal
    from models import Coin, DailySnapshot, VolumeSpike
    stats = {"checked": 0, "qualified": 0, "big": 0, "medium": 0, "elevated": 0, "alerts": []}
    session = SessionLocal()
    try:
        day = session.query(func.max(DailySnapshot.date)).scalar()      # this run's day
        if day is None:
            return stats
        existing = {r.coin_id: r for r in session.query(VolumeSpike).filter_by(day=day)}
        recently_alerted = {cid for (cid,) in session.query(VolumeSpike.coin_id).filter(
            VolumeSpike.alerted.is_(True), VolumeSpike.day >= day - timedelta(days=REALERT_DAYS),
            VolumeSpike.day < day)}
        for coin in session.query(Coin).filter_by(status="active"):
            stats["checked"] += 1
            sp = calculate_spike(session, coin, day)
            if sp is None:
                continue
            stats["qualified"] += 1
            row = existing.get(coin.id)
            if sp["severity"] is None or sp["volume_today"] < MIN_STORE_VOLUME_USD:
                if row is not None:                    # a same-day re-run fell below 2x / the floor
                    session.delete(row)
                continue
            stats[sp["severity"]] += 1
            if row is None:
                row = VolumeSpike(coin_id=coin.id, day=day, alerted=False)
                session.add(row)
            row.volume_today, row.volume_avg_30d = sp["volume_today"], sp["volume_avg_30d"]
            row.spike_ratio, row.severity = sp["spike_ratio"], sp["severity"]
            if (sp["severity"] in ALERT_LEVELS and sp["volume_today"] >= MIN_ALERT_VOLUME_USD
                    and not row.alerted and coin.id not in recently_alerted):
                row.alerted = True
                stats["alerts"].append((coin.name, sp["severity"], sp["spike_ratio"],
                                        sp["volume_today"], sp["volume_avg_30d"]))
        session.query(VolumeSpike).filter(VolumeSpike.day < day - timedelta(days=KEEP_DAYS)).delete(
            synchronize_session=False)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    stats["alerts"].sort(key=lambda a: -a[2])
    log.info("Volume anomaly: %d active coins, %d with %d+ days of history; %d big, %d medium, %d elevated; "
             "%d alerts.", stats["checked"], stats["qualified"], MIN_HISTORY_DAYS, stats["big"],
             stats["medium"], stats["elevated"], len(stats["alerts"]))
    return stats


def _spike_dict(r):
    return {"severity": r.severity, "ratio": r.spike_ratio, "volume_today": r.volume_today,
            "volume_avg_30d": r.volume_avg_30d, "day": r.day.isoformat(),
            "detected_at": r.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.created_at else None,
            "alerted": r.alerted}


def recent_spikes(session, coin_ids):
    """{coin_id: the most recent spike within SHOW_DAYS} for many coins (one
    query; the tables). Never raises (empty before the table exists)."""
    from datetime import datetime, timezone
    from models import VolumeSpike
    try:
        since = datetime.now(timezone.utc).date() - timedelta(days=SHOW_DAYS)
        out = {}
        for r in (session.query(VolumeSpike).filter(VolumeSpike.coin_id.in_(list(coin_ids)), VolumeSpike.day >= since)
                  .order_by(VolumeSpike.day)):
            out[r.coin_id] = _spike_dict(r)                 # later days overwrite earlier
        return out
    except Exception:
        session.rollback()
        return {}


def get_recent_spike(session, coin_id):
    """The coin's most recent spike within SHOW_DAYS days, or None."""
    return recent_spikes(session, [coin_id]).get(coin_id)


def spike_for_contract(contract):
    """Check a Coin: the stored spike of a coin we track (active / dropped),
    else None (untracked coins have no volume history here). Never raises."""
    from sqlalchemy import func
    from database import SessionLocal
    from models import Coin
    session = SessionLocal()
    try:
        coin = (session.query(Coin).filter(func.lower(Coin.contract) == (contract or "").lower(),
                                           Coin.status.in_(["active", "dropped"])).first())
        return get_recent_spike(session, coin.id) if coin else None
    except Exception:
        session.rollback()
        return None
    finally:
        session.close()
