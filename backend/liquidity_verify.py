"""
liquidity_verify.py
-------------------
3-SOURCE LIQUIDITY VERIFICATION: DexScreener + Codex + Birdeye, majority vote.
Replaces the old "liquidity under $5K on BOTH Codex and DexScreener (the
higher one counts, no reading = unknown = healthy)" rule.

Readings per coin (USD; zero / missing = no data from that source):
  DexScreener - today's sum of the token's pools (tracking.py)
  Codex       - today's bulk filterTokens liquidity (tracking.py)
  Birdeye     - /defi/price?include_liquidity=true (birdeye.py, 3 CU):
                active coins every daily run, dropped coins every
                DROPPED_RECHECK_DAYS days (run_birdeye_fetch below)

evaluate() decides, in this order:
  1. PRESENCE - how many sources have a reading:
       0 -> removed_insufficient_sources  "No liquidity data available"
       1 -> removed_insufficient_sources  "Insufficient liquidity sources
                                           (chain not supported)"
  2. DISAGREEMENT (two readings more than AGREE_X apart = disagree):
       all agree              -> all count; value = median
       one outlier            -> it is ignored; the agreeing two count
       a chain (A~B, B~C)     -> all count; value = median (shown with ⚠️)
       no two agree           -> the LOWEST reading decides (conservative)
  3. AMOUNT - how many counted readings are >= MIN_LIQ ($5K; $10K for an
     "abandoned" coin - Birdeye's last price update older than
     ABANDONED_HOURS):
       2 or 3 -> healthy (stays / may recover)
       1      -> dropped_low_liquidity "Low liquidity confirmed by only
                 1 source" (the normal 7-day recovery window)
       0      -> removed_dead "Dead liquidity across all sources"
     (no two agree: healthy only if the lowest reading is >= the minimum)
A source whose request FAILED (outage, not "no data") makes any verdict
other than healthy "unknown" - nothing is removed or dropped on missing
data caused by our own fetch problems.

drop_logic.py applies the verdicts (removed_* = deleted for good: the entry
queue's "removed_liquidity" outcome is never re-checked); save_to_db.py
applies the same rule at entry; every decision is logged in
liquidity_check_log.
"""

import logging
from datetime import timedelta

from database import utcnow

log = logging.getLogger("liquidity_verify")

from liquidity_check import MIN_LIQUIDITY_USD as MIN_LIQ   # $5K
ABANDONED_MIN_LIQ = 10_000     # stricter minimum for an "abandoned" coin
ABANDONED_HOURS = 48           # Birdeye's last price update older than this = abandoned
AGREE_X = 3.0                  # two readings within 3x of each other agree
DROPPED_RECHECK_DAYS = 3       # Birdeye refresh for dropped coins
MAX_CALLS_PER_RUN = 320        # Birdeye safety cap (~175 active + ~25 dropped a day)

SOURCES = ("dexscreener", "codex", "birdeye")
LABELS = {"dexscreener": "DexScreener", "codex": "Codex", "birdeye": "Birdeye"}

HEALTHY = "healthy"
DROPPED = "dropped_low_liquidity"
REMOVED_SOURCES = "removed_insufficient_sources"
REMOVED_DEAD = "removed_dead"
UNKNOWN = "unknown"

REASON_NO_DATA = "No liquidity data available"
REASON_ONE_SOURCE = "Insufficient liquidity sources (chain not supported)"
REASON_LOW = "Low liquidity confirmed by only 1 source"
REASON_DEAD = "Dead liquidity across all sources"

# Coin ids whose Birdeye request failed in THIS process's run (not "no data").
BIRDEYE_ERRORS = set()
# This process's Birdeye fetch stats (daily report); FETCH_FAILED = the
# whole daily fetch step crashed (every Birdeye reading counts as failed).
LAST_FETCH = {}
FETCH_FAILED = False


def _agree(a, b):
    return max(a, b) / min(a, b) <= AGREE_X


def _median(vals):
    v = sorted(vals)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def consensus(readings):
    """readings = {source: USD > 0}. Returns {"kept": [sources that count],
    "outlier": source or None, "value": the liquidity to show, "disagree":
    any pair more than AGREE_X apart, "no_majority": no two agree}.
    None for fewer than 2 readings (a single reading is returned as-is)."""
    srcs = [s for s in SOURCES if readings.get(s)]
    if not srcs:
        return None
    if len(srcs) == 1:
        return {"kept": srcs, "outlier": None, "value": readings[srcs[0]], "disagree": False,
                "no_majority": False}
    pairs = [(a, b) for i, a in enumerate(srcs) for b in srcs[i + 1:]]
    agreeing = [(a, b) for a, b in pairs if _agree(readings[a], readings[b])]
    vals = [readings[s] for s in srcs]
    if len(agreeing) == len(pairs):                       # all agree
        return {"kept": srcs, "outlier": None, "value": _median(vals), "disagree": False, "no_majority": False}
    if not agreeing:                                       # no two agree -> lowest
        low = min(srcs, key=lambda s: readings[s])
        return {"kept": srcs, "outlier": None, "value": readings[low], "disagree": True, "no_majority": True}
    if len(agreeing) == 1:                                 # one outlier (3 sources)
        a, b = agreeing[0]
        out = next(s for s in srcs if s not in (a, b))
        return {"kept": [a, b], "outlier": out, "value": (readings[a] + readings[b]) / 2,
                "disagree": True, "no_majority": False}
    return {"kept": srcs, "outlier": None, "value": _median(vals), "disagree": True, "no_majority": False}


def evaluate(readings, birdeye_updated_at=None, failed=(), now=None):
    """The verdict for one coin. readings = {source: USD or None}; failed =
    sources whose request failed this run. See the module docstring."""
    now = now or utcnow()
    clean = {s: float(v) for s, v in readings.items() if s in SOURCES and v is not None and float(v) > 0}
    failed = {s for s in failed if s not in clean}
    abandoned = bool(birdeye_updated_at and now - birdeye_updated_at > timedelta(hours=ABANDONED_HOURS))
    threshold = ABANDONED_MIN_LIQ if abandoned else MIN_LIQ
    c = consensus(clean)
    out = {"readings": {s: clean.get(s) for s in SOURCES}, "sources_count": len(clean),
           "threshold": threshold, "abandoned": abandoned, "failed": sorted(failed),
           "value": c["value"] if c else None, "kept": c["kept"] if c else [],
           "outlier": c["outlier"] if c else None, "disagree": bool(c and c["disagree"]),
           "n_above": 0}
    if len(clean) < 2:
        verdict, reason = REMOVED_SOURCES, (REASON_NO_DATA if not clean else REASON_ONE_SOURCE)
    else:
        above = [s for s in c["kept"] if clean[s] >= threshold]
        n = len(above)
        if c["no_majority"] and min(clean[s] for s in c["kept"]) < threshold:
            n = min(n, 1)                              # the lowest reading decides
        out["n_above"] = n
        if n >= 2:
            verdict, reason = HEALTHY, None
        elif n == 1:
            verdict, reason = DROPPED, REASON_LOW
        else:
            verdict, reason = REMOVED_DEAD, REASON_DEAD
        if abandoned and verdict != HEALTHY:
            reason += f" (abandoned: no Birdeye price update for {ABANDONED_HOURS}h+, minimum ${threshold:,})"
    if verdict != HEALTHY and failed:
        verdict, reason = UNKNOWN, "not decided - " + ", ".join(LABELS[s] for s in sorted(failed)) + " request failed"
    out["verdict"], out["reason"] = verdict, reason
    return out


def money(v):
    return "—" if v is None else f"${v:,.0f}"


def describe(ev):
    """'DexScreener $63,574 / Codex $74 / Birdeye $81' (+ outlier note)."""
    parts = [f"{LABELS[s]} {money(ev['readings'][s])}" for s in SOURCES]
    note = f"; {LABELS[ev['outlier']]} ignored (outlier)" if ev.get("outlier") else ""
    return " / ".join(parts) + note


# ---------------------------------------------------------------- readings
def coin_readings(session, coin, today):
    """(readings, failed) for a tracked coin: today's DexScreener / Codex
    readings (tracking.py, this run) + its latest Birdeye reading."""
    from models import DailySnapshot
    import tracking
    snap = (session.query(DailySnapshot)
            .filter(DailySnapshot.coin_id == coin.id, DailySnapshot.date == today).first())
    failed = set()
    health = tracking.LAST_HEALTH
    if health.get("ran"):
        dex = tracking.LAST_DEX_LIQ.get(coin.id)          # today's reading only (never carried forward)
        if dex is None and health.get("dex_lost"):
            failed.add("dexscreener")
        if snap is None or snap.codex_liquidity is None:
            if health.get("codex_failed"):
                failed.add("codex")
    else:                                                  # standalone run: today's snapshot
        dex = snap.liquidity if snap is not None else None
    codex = snap.codex_liquidity if snap is not None else None
    be = None
    import birdeye
    if birdeye.supported(coin.chain):
        fresh = (coin.birdeye_checked_at is not None
                 and (today - coin.birdeye_checked_at.date()).days <= DROPPED_RECHECK_DAYS)
        if FETCH_FAILED or coin.id in BIRDEYE_ERRORS or not fresh:
            failed.add("birdeye")
        else:
            be = coin.birdeye_liquidity
    return {"dexscreener": dex, "codex": codex, "birdeye": be}, failed


def coin_evaluation(session, coin, today):
    readings, failed = coin_readings(session, coin, today)
    return evaluate(readings, coin.birdeye_updated_at, failed)


def store_verdict(coin, ev):
    if ev["verdict"] != UNKNOWN:
        coin.liquidity_verdict = ev["verdict"]
    coin.liquidity_sources_count = ev["sources_count"]


def log_decision(session, coin, today, ev, action):
    from models import LiquidityCheckLog
    r = ev["readings"]
    session.add(LiquidityCheckLog(coin_id=coin.id, coin_name=coin.name, chain=coin.chain, check_date=today,
                                  ds_liq=r["dexscreener"], codex_liq=r["codex"], birdeye_liq=r["birdeye"],
                                  verdict=ev["verdict"], action_taken=action, reason=ev.get("reason")))


# ---------------------------------------------------------------- daily Birdeye fetch
def run_birdeye_fetch():
    """Daily step (after tracking, before drop logic): Birdeye liquidity for
    every ACTIVE coin (not already looked up today, e.g. at entry) and every
    DROPPED coin last looked up DROPPED_RECHECK_DAYS+ days ago. Stored on the
    coin (birdeye_liquidity / birdeye_updated_at / birdeye_checked_at) and
    on today's snapshot. A failed request keeps the previous reading and
    marks the coin (BIRDEYE_ERRORS) so nothing is decided on it. Returns stats."""
    from datetime import date
    import birdeye
    from database import SessionLocal
    from models import Coin, DailySnapshot

    now = utcnow()
    today = now.date()
    snap_day = date.today()
    cu_start = birdeye.CU_USED
    stats = {"active": 0, "dropped_due": 0, "calls": 0, "ok": 0, "no_data": 0, "unsupported": 0,
             "error": 0, "skipped_cap": 0}
    session = SessionLocal()
    try:
        coins = session.query(Coin).filter(Coin.status.in_(["active", "dropped"])).all()
        todo = []
        for c in coins:
            checked = c.birdeye_checked_at.date() if c.birdeye_checked_at else None
            if c.status == "active":
                stats["active"] += 1
                if checked != today:
                    todo.append(c)
            elif checked is None or (today - checked).days >= DROPPED_RECHECK_DAYS:
                stats["dropped_due"] += 1
                todo.append(c)
        for c in todo:
            if not birdeye.supported(c.chain):
                stats["unsupported"] += 1
                c.birdeye_liquidity, c.birdeye_updated_at, c.birdeye_checked_at = None, None, now
                continue
            if stats["calls"] >= MAX_CALLS_PER_RUN:
                stats["skipped_cap"] += 1
                BIRDEYE_ERRORS.add(c.id)
                continue
            stats["calls"] += 1
            res = birdeye.lookup(c.chain, c.contract)
            stats[res["status"] if res["status"] in stats else "error"] += 1
            if res["status"] == "error":
                BIRDEYE_ERRORS.add(c.id)
                continue
            c.birdeye_liquidity = res["liquidity"]
            c.birdeye_updated_at = res["updated_at"]
            c.birdeye_checked_at = now
            snap = session.query(DailySnapshot).filter_by(coin_id=c.id, date=snap_day).first()
            if snap is not None:
                snap.birdeye_liquidity = res["liquidity"]
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    stats["cu"] = birdeye.CU_USED - cu_start
    LAST_FETCH.clear()
    LAST_FETCH.update(stats)
    log.info("Birdeye: %d calls (%d CU) - ok %d, no data %d, unsupported chain %d, failed %d, over cap %d "
             "(active %d, dropped due %d)", stats["calls"], stats["cu"], stats["ok"], stats["no_data"],
             stats["unsupported"], stats["error"], stats["skipped_cap"], stats["active"], stats["dropped_due"])
    return stats


# ---------------------------------------------------------------- live (Check a Coin)
def live_sources(chain, contract, dex_liq, codex_liq):
    """3-source verdict for Check a Coin: a live Birdeye lookup + the
    DexScreener / Codex readings the check already has."""
    import birdeye
    res = birdeye.lookup(chain, contract)
    failed = {"birdeye"} if res["status"] == "error" else set()
    ev = evaluate({"dexscreener": dex_liq, "codex": codex_liq, "birdeye": res["liquidity"]},
                  res["updated_at"], failed)
    return sources_payload(ev, res["updated_at"], utcnow(), birdeye_status=res["status"])


def sources_payload(ev, birdeye_updated_at, birdeye_checked_at, birdeye_status=None):
    """The "Liquidity Sources" section's data (API)."""
    def iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None
    return {"dexscreener": ev["readings"]["dexscreener"], "codex": ev["readings"]["codex"],
            "birdeye": ev["readings"]["birdeye"], "birdeye_status": birdeye_status,
            "birdeye_updated_at": iso(birdeye_updated_at), "birdeye_checked_at": iso(birdeye_checked_at),
            "value": ev["value"], "disagree": ev["disagree"], "outlier": ev["outlier"],
            "kept": ev["kept"], "sources_count": ev["sources_count"], "n_above": ev["n_above"],
            "threshold": ev["threshold"], "abandoned": ev["abandoned"],
            "verdict": ev["verdict"], "reason": ev["reason"]}


def stored_sources(coin, snap):
    """The section's data for a tracked coin from what is stored (no API call)."""
    import birdeye
    be_unsupported = not birdeye.supported(coin.chain)
    ev = evaluate({"dexscreener": snap.liquidity if snap else None,
                   "codex": snap.codex_liquidity if snap else None,
                   "birdeye": coin.birdeye_liquidity}, coin.birdeye_updated_at)
    status = ("unsupported" if be_unsupported else None if coin.birdeye_checked_at is None
              else "ok" if coin.birdeye_liquidity else "no_data")
    out = sources_payload(ev, coin.birdeye_updated_at, coin.birdeye_checked_at, status)
    if coin.liquidity_verdict:                 # the daily run's decision (today's readings)
        out["daily_verdict"] = coin.liquidity_verdict
    return out


# ---------------------------------------------------------------- recovery alert
def recovery_alert(name, readings, threshold, days, since):
    """The Arabic Telegram alert for a coin back in Active."""
    def line(src):
        v = readings.get(src)
        mark = "✅" if v and v >= threshold else "⚠️"
        return f"  {mark} {LABELS[src]}: {money(v) if v else 'ما كاين داتا'}"
    return "\n".join([
        "🔄 عملة رجعت لـ Active", "",
        f"العملة: {name}",
        f"السبب: Liquidity طلعت فوق ${threshold // 1000}K",
        "المصادر:",
        *[line(s) for s in SOURCES], "",
        f"غابت فـ Dropped: {days} أيام (من {since:%Y-%m-%d})"])
