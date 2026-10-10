"""
drop_logic.py
-------------
The DROP LOGIC. Completely separate from the 7-step filter: the filter is a
one-time ENTRY gate for new coins; drop_logic only watches coins ALREADY in
active / dropped. It runs daily after discover -> filter -> save -> tracking.

It uses ONE set of 4 health criteria, both for dropping and for recovery:
    * volume falling      - last 5 daily Codex readings, day5 more than 5%
                            below day1 (rising or flat = healthy)
    * liquidity down > 5% - today's Codex reading vs the one up to 30 days
                            ago (or since it entered active, if shorter)
    * liquidity under $5K - today's reading is under $5,000 on BOTH Codex and
                            DexScreener (the higher one counts; no reading
                            from either = unknown = healthy)
    * holders down > 10%  - vs the count when it entered active
                            (coins.holders_at_entry; no baseline -> skipped)
    * honeypot unsafe     - GoPlus
Volume + liquidity come ONLY from the daily Codex readings that tracking.py
stores on each day's snapshot (~3 bulk Codex requests/day for all coins).
NO CoinGecko here - CoinGecko is only the one-time entry check.
"Unknown" (not enough readings yet, no data) counts as healthy - never drop,
or keep out, on missing data. Revenue is NOT part of this.

0. CRASH OVERRIDE (checked first, on EVERY active coin - even in grace):
   price OR liquidity down CRASH_PCT (70%) or more vs its highest reading in
   the previous CRASH_WINDOW_DAYS (3) days -> dropped immediately (reason
   "Crash - ..."), then the normal 7-day recovery window. Liquidity per day
   = the higher of Codex and DexScreener, so one bad Codex reading can't
   fake a crash. The grace period is for normal volatility, not a crash.

1. GRACE: a coin is untouched for its first GRACE_DAYS (5) calendar days in
   active - during which it collects the 5 daily readings the volume trend
   needs. Entered on day D -> first re-check on day D+5. Counted from
   coins.active_since, which resets when a coin recovers.

2. ACTIVE, past grace: re-checked daily. If ANY criterion fires -> DEGRADED:
   status "dropped" + a dropped_coins row with the reason.

3. DROPPED: a 7-day recovery window. Re-checked daily on the SAME 4 criteria
   (not the full filter):
     all 4 healthy              -> back to active, fresh 5-day grace, and the
                                   holders baseline restarts from today
     still unhealthy on day 7   -> deleted (coin, snapshots and drop rows -
                                   it disappears from the site). It may come
                                   back ONCE through the full entry filter 15
                                   days later (rotation.py second chance).
   Dropped on day D -> re-checked on days D+1 ... D+7 (7 chances); still
   failing on D+7 -> deleted.

ever_qualified: a coin that never legitimately passed the filter is removed
silently instead of being recorded in dropped_coins.

Run it standalone (after tracking.py has recorded today's readings):
    python drop_logic.py
"""

from datetime import date, timedelta

from database import SessionLocal, init_db, utcnow
from models import Coin, DailySnapshot, DroppedCoin, RankHistory

GRACE_DAYS = 5               # calendar days untouched after entering active
RECOVERY_DAYS = 7            # daily recovery re-checks before permanent delete
MAX_HOLDER_DROP_PCT = 10     # holders down more than this -> unhealthy
MAX_LIQ_DROP_PCT = 5         # liquidity down more than this -> unhealthy
# VOLUME FALLING: the last VOLUME_AVG_DAYS complete days' AVERAGE volume vs
# the VOLUME_AVG_DAYS days before it (each coin's stored daily history,
# entry_zone_bars). Falling = more than VOLUME_AVG_FALL_PCT lower. Fewer than
# 2 x VOLUME_AVG_DAYS days of history = unknown = healthy.
VOLUME_AVG_DAYS = 5          # compare 5-day averages ...
VOLUME_AVG_FALL_PCT = 20     # ... falling = the newer average more than 20% lower
VOLUME_READINGS = 5          # (old single-day rule - no longer used)
VOLUME_FALL_PCT = 5          # (old single-day rule - no longer used)
                             # rising or flat (within 5%) stays healthy
VOLUME_LOOKBACK_DAYS = 10    # ...taken from at most the last 10 days
LIQ_WINDOW_DAYS = 30         # liquidity: vs up to 30 days back
CRASH_PCT = 70               # price or liquidity down this much ...
CRASH_WINDOW_DAYS = 3        # ... vs the peak of the last 3 days = crash
# Two kinds of crash (both need two sources to agree):
#   PRICE crash      -> the coin is removed from the site and BLOCKED for
#                       CRASH_BLOCK_DAYS: no recovery; after that it can only
#                       come back by passing the entry filter again (once).
#   LIQUIDITY-only   -> price roughly held: a normal drop (7-day recovery).
CRASH_BLOCK_DAYS = 15        # = rotation.RECHECK_AFTER_DAYS
# A LIQUIDITY-crash drop only recovers once liquidity is back to at least
# this share of its pre-crash peak (the highest reading in the
# CRASH_WINDOW_DAYS days before the drop), on top of the 4 health rules.
LIQ_CRASH_RECOVER_SHARE = 0.5


def _delete_coin(session, coin, outcome="deleted", when=None):
    """Remove a coin completely: its snapshots, dropped_coins rows, weekly
    rank history, itself (every table that points at the coin - otherwise
    the database refuses the delete). The entry queue records the deletion
    (outcome "deleted", or "crashed" for a 15-day crash block), so the coin
    can come back through the entry filter ONCE, 15 days after `when`
    (rotation.py)."""
    from rotation import mark_deleted
    mark_deleted(session, coin.chain, coin.contract, when=when, outcome=outcome)
    for table in (DailySnapshot, DroppedCoin, RankHistory):
        session.query(table).filter_by(coin_id=coin.id).delete(
            synchronize_session=False
        )
    session.delete(coin)


def days_since(when, today):
    """Whole calendar days from `when` (a datetime) to `today`."""
    return (today - when.date()).days


def _fall_pct(values):
    """% fall of the LAST value vs the highest earlier one (None if unknown)."""
    vals = list(values)
    now_v = vals[-1] if vals else None
    peak = max((v for v in vals[:-1] if v), default=None)
    if now_v is None or not peak:
        return None
    return (1 - now_v / peak) * 100


def _second_source_price_fall(coin, days):
    """Price fall % over the last `days` days from an INDEPENDENT source:
    CoinGecko market_chart (by contract), else Codex daily closes. None if
    neither has the coin. One call; only made for a suspected price crash."""
    import time as _time
    import requests
    from age_check import CHAIN_TO_CG_PLATFORM
    from discovery import CG_KEY
    plat = CHAIN_TO_CG_PLATFORM.get((coin.chain or "").lower())
    if plat and CG_KEY:
        try:
            r = requests.get(f"https://api.coingecko.com/api/v3/coins/{plat}/contract/{coin.contract.lower()}/market_chart",
                             params={"vs_currency": "usd", "days": days + 1},
                             headers={"x-cg-demo-api-key": CG_KEY}, timeout=20)
            prices = [p[1] for p in (r.json().get("prices") or [])] if r.status_code == 200 else []
            if len(prices) >= 2:
                return "CoinGecko", _fall_pct(prices)
        except (requests.exceptions.RequestException, ValueError):
            pass
    from liquidity_check import CHAIN_TO_NETWORK, CODEX_KEY, CODEX_URL
    net = CHAIN_TO_NETWORK.get((coin.chain or "").lower())
    if net and CODEX_KEY:
        now = int(_time.time())
        q = ('query ($s: String!, $f: Int!, $t: Int!) { getBars(symbol: $s, from: $f, to: $t, '
             'resolution: "60", removeLeadingNullValues: true) { c } }')
        try:
            body = requests.post(CODEX_URL, json={"query": q, "variables": {
                "s": f"{coin.contract}:{net}", "f": now - (days + 1) * 86400, "t": now}},
                headers={"Authorization": CODEX_KEY}, timeout=30).json()
            closes = [float(c) for c in (((body.get("data") or {}).get("getBars") or {}).get("c") or []) if c]
            if len(closes) >= 2:
                return "Codex", _fall_pct(closes)
        except (requests.exceptions.RequestException, ValueError, TypeError):
            pass
    return None, None


def crash_check(session, coin, today, suspects=None):
    """The crash override: "Crash - ..." if today's price or liquidity is
    down CRASH_PCT% or more from its highest reading in the previous
    CRASH_WINDOW_DAYS days - and TWO SOURCES AGREE:
      * liquidity: Codex AND DexScreener (each vs its own 3-day peak) both
        down >= CRASH_PCT%;
      * price: the stored (DexScreener) price down >= CRASH_PCT% AND an
        independent source (CoinGecko, else Codex) shows the same over the
        same days (one live call, only for a suspected price crash).
    A fall seen by ONE source only is a "suspect reading": nothing is dropped,
    a note is added to `suspects` (a list) and tomorrow's run looks again."""
    snaps = (session.query(DailySnapshot)
             .filter(DailySnapshot.coin_id == coin.id, DailySnapshot.date <= today,
                     DailySnapshot.date >= today - timedelta(days=CRASH_WINDOW_DAYS))
             .order_by(DailySnapshot.date).all())
    if len(snaps) < 2:
        return None, None

    from liquidity_check import codex_unreliable, MISMATCHES
    falls, notes = [], []
    # Liquidity: each source against its own peak. On a source mismatch
    # (Codex > 10x DexScreener on any day in the window) Codex is left out -
    # DexScreener alone can then only ever make a "suspect" reading.
    mismatch = any(codex_unreliable(sn.codex_liquidity, sn.liquidity) for sn in snaps)
    if mismatch:
        MISMATCHES.append((coin.name, snaps[-1].codex_liquidity or 0, snaps[-1].liquidity or 0))
    liq = {"Codex": None if mismatch else _fall_pct(sn.codex_liquidity for sn in snaps),
           "DexScreener": _fall_pct(sn.liquidity for sn in snaps)}
    big = {src: f for src, f in liq.items() if f is not None and f >= CRASH_PCT}
    if len(big) == 2:
        falls.append(f"liquidity down {min(big.values()):.1f}%")
    elif big:
        (src, f), = big.items()
        other = "Codex" if src == "DexScreener" else "DexScreener"
        seen = (f"{-liq[other]:+.1f}%" if liq[other] is not None
                else "unreliable - source mismatch" if other == "Codex" and mismatch else "no reading")
        notes.append(f"liquidity down {f:.1f}% on {src} only ({other}: {seen})")

    # Price: the stored price, confirmed by an independent source.
    pf = _fall_pct(sn.price_usd for sn in snaps)
    if pf is not None and pf >= CRASH_PCT:
        src, f2 = _second_source_price_fall(coin, CRASH_WINDOW_DAYS)
        if f2 is not None and f2 >= CRASH_PCT:
            falls.append(f"price down {pf:.1f}%")
        else:
            seen = f"{src} {-f2:+.1f}%" if f2 is not None else "no second source"
            notes.append(f"price down {pf:.1f}% on DexScreener only ({seen})")

    if notes and suspects is not None:
        suspects.append((coin.name, "; ".join(notes)))
    if not falls:
        return None, None
    falls.sort(key=lambda x: not x.startswith("price"))      # "price ... and liquidity ..."
    if falls[0].startswith("price"):
        # REAL CRASH: the price itself collapsed -> 15-day block.
        return "price", f"Crash - {' and '.join(falls)} in {CRASH_WINDOW_DAYS} days"
    # Price roughly held, liquidity collapsed -> a normal drop (7-day recovery).
    return "liquidity", f"Liquidity crash - {falls[0]} in {CRASH_WINDOW_DAYS} days"


def crash_reason(session, coin, today, suspects=None):
    """crash_check's reason only (None = no confirmed crash)."""
    return crash_check(session, coin, today, suspects)[1]


def is_price_crash_reason(reason):
    """A stored drop reason that records a confirmed PRICE crash (also the
    older "Crash - price down ..." wording, before the 15-day block existed)."""
    r = reason or ""
    return r.startswith("Crash - ") and "price down" in r


def is_liquidity_crash_reason(reason):
    """A stored drop reason for a liquidity-only crash: "Liquidity crash - ..."
    (or the older "Crash - liquidity down ..." wording without a price fall)."""
    r = reason or ""
    return r.startswith("Liquidity crash - ") or (r.startswith("Crash - ") and "price down" not in r)


def _best_liquidity(sn, label=None):
    """The liquidity that counts for a snapshot (liquidity_check.best_liquidity:
    higher of Codex / DexScreener, or DexScreener on a source mismatch)."""
    from liquidity_check import best_liquidity
    return best_liquidity(sn.codex_liquidity, sn.liquidity, label)[0]


def liquidity_recovery(session, coin, dropped_at, today):
    """(recovered, now_liq, pre_crash_peak) for a liquidity-crash drop.
    Liquidity = the higher of Codex and DexScreener, or DexScreener alone on
    a source mismatch (as for the $5K minimum).
    pre_crash_peak = highest reading in the CRASH_WINDOW_DAYS days before the
    drop; recovered = today's (latest) reading >= LIQ_CRASH_RECOVER_SHARE of it.
    No pre-crash reading -> nothing to compare against -> recovered."""
    drop_day = dropped_at.date()
    before = (session.query(DailySnapshot)
              .filter(DailySnapshot.coin_id == coin.id, DailySnapshot.date < drop_day,
                      DailySnapshot.date >= drop_day - timedelta(days=CRASH_WINDOW_DAYS)).all())
    peak = max((v for v in map(_best_liquidity, before) if v), default=None)
    latest = (session.query(DailySnapshot)
              .filter(DailySnapshot.coin_id == coin.id, DailySnapshot.date <= today)
              .order_by(DailySnapshot.date.desc()).first())
    now_liq = _best_liquidity(latest, coin.name) if latest else None
    if not peak:
        return True, now_liq, peak
    return (now_liq or 0) >= LIQ_CRASH_RECOVER_SHARE * peak, now_liq, peak


def _block_crashed(session, coin, reason, when):
    """REAL CRASH: remove the coin from the site now and block it for
    CRASH_BLOCK_DAYS from `when` (the entry queue's "crashed" outcome; after
    that it can only come back through the full entry filter)."""
    _delete_coin(session, coin, outcome="crashed", when=when)


def in_grace(coin, today):
    """True during the coin's first GRACE_DAYS calendar days in active."""
    since = coin.active_since or coin.first_seen
    return since is not None and days_since(since, today) < GRACE_DAYS


def codex_volume_trend(session, coin, today):
    """'falling' if the last VOLUME_AVG_DAYS complete days' average volume is
    more than VOLUME_AVG_FALL_PCT below the VOLUME_AVG_DAYS days before it
    (stored daily history, entry_zone_bars); 'rising' / 'flat' otherwise;
    'unknown' (healthy) without 2 x VOLUME_AVG_DAYS days of history."""
    pct = volume_avg_change(session, coin, today)
    if pct is None:
        return "unknown"
    if pct < -VOLUME_AVG_FALL_PCT:
        return "falling"
    return "rising" if pct > 0 else "flat"


def volume_avg_change(session, coin, today):
    """% change: last VOLUME_AVG_DAYS complete days' average volume vs the
    VOLUME_AVG_DAYS before (stored daily history), or None."""
    from entry_zone import load_bars
    try:
        bars, _ = load_bars(session, coin)
    except Exception:
        session.rollback()
        return None
    vols = [v for d, _, v, _ in (bars or []) if d < today and v is not None]
    n = VOLUME_AVG_DAYS
    if len(vols) < 2 * n:
        return None
    last, prev = sum(vols[-n:]) / n, sum(vols[-2 * n:-n]) / n
    return (last / prev - 1) * 100 if prev else None


def codex_volume_trend_single_day(session, coin, today):
    """From the last 5 daily Codex volume readings: 'falling' only if day5 is
    CLEARLY lower than day1 (more than VOLUME_FALL_PCT lower); otherwise
    'rising' (day5 > day1) or 'flat' - both healthy. 'unknown' if there
    aren't 5 readings yet."""
    rows = (session.query(DailySnapshot.codex_volume24)
            .filter(DailySnapshot.coin_id == coin.id,
                    DailySnapshot.date <= today,
                    DailySnapshot.date > today - timedelta(days=VOLUME_LOOKBACK_DAYS),
                    DailySnapshot.codex_volume24.isnot(None))
            .order_by(DailySnapshot.date.desc())
            .limit(VOLUME_READINGS).all())
    if len(rows) < VOLUME_READINGS:
        return "unknown"
    day5, day1 = rows[0][0], rows[-1][0]           # newest first
    if day5 < day1 * (1 - VOLUME_FALL_PCT / 100):
        return "falling"                           # clearly lower -> unhealthy
    return "rising" if day5 > day1 else "flat"     # rising or flat -> healthy


def codex_liquidity_trend(session, coin, today):
    """(verdict, pct, base_date): today's Codex liquidity vs the reading from up
    to 30 days ago, or since the coin entered active if that's shorter.
    verdict 'drop' if down > 5%, 'keep' otherwise, 'unknown' if < 2 readings.
    On a source mismatch today (Codex > 10x DexScreener) the trend uses the
    DexScreener readings instead."""
    from liquidity_check import codex_unreliable
    last = (session.query(DailySnapshot)
            .filter(DailySnapshot.coin_id == coin.id, DailySnapshot.date <= today)
            .order_by(DailySnapshot.date.desc()).first())
    col = (DailySnapshot.liquidity if last is not None
           and codex_unreliable(last.codex_liquidity, last.liquidity) else DailySnapshot.codex_liquidity)
    since = coin.active_since or coin.first_seen
    start = today - timedelta(days=LIQ_WINDOW_DAYS)
    if since is not None and since.date() > start:
        start = since.date()
    rows = (session.query(DailySnapshot.date, col)
            .filter(DailySnapshot.coin_id == coin.id,
                    DailySnapshot.date >= start,
                    DailySnapshot.date <= today,
                    col.isnot(None))
            .order_by(DailySnapshot.date).all())
    if len(rows) < 2 or not rows[0][1]:
        return "unknown", None, None
    (base_date, base), (_, latest) = rows[0], rows[-1]
    pct = (latest - base) / base * 100
    return ("drop" if pct < -MAX_LIQ_DROP_PCT else "keep"), pct, base_date


def unhealthy_reason(coin, session, today):
    """
    The 4 health criteria. Returns the first one that fires, or None if the
    coin is healthy. Unknown on any check counts as healthy. The two free
    checks (stored Codex history) go first; the network ones after.
    """
    from holders_check import get_holder_count
    from security_check import check_token_security

    if codex_volume_trend(session, coin, today) == "falling":
        pct = volume_avg_change(session, coin, today)
        return f"Volume falling ({VOLUME_AVG_DAYS}-day average {pct:+.0f}% vs the previous {VOLUME_AVG_DAYS} days)"

    liq, pct, base_date = codex_liquidity_trend(session, coin, today)
    if liq == "drop":
        return f"Liquidity down {abs(pct):.1f}% since {base_date}"

    from liquidity_check import too_thin, MIN_LIQUIDITY_USD
    snap = (session.query(DailySnapshot)
            .filter(DailySnapshot.coin_id == coin.id, DailySnapshot.date <= today)
            .order_by(DailySnapshot.date.desc()).first())
    if snap is not None:
        from liquidity_check import codex_unreliable
        thin, best = too_thin(snap.codex_liquidity, snap.liquidity, coin.name)
        if thin:
            if codex_unreliable(snap.codex_liquidity, snap.liquidity):
                return (f"Liquidity under ${MIN_LIQUIDITY_USD:,} (${best:,.0f} on DexScreener;"
                        f" Codex unreliable - source mismatch)")
            return f"Liquidity under ${MIN_LIQUIDITY_USD:,} (${best:,.0f} on Codex and DexScreener)"

    base = coin.holders_at_entry
    now_h = get_holder_count(coin.contract, coin.chain)
    if base and isinstance(now_h, int):
        hpct = (now_h - base) / base * 100
        if hpct < -MAX_HOLDER_DROP_PCT:
            return f"Holders down {abs(hpct):.1f}% ({base:,} -> {now_h:,})"

    sec = check_token_security(coin.contract, coin.chain)
    if sec["verdict"] == "unsafe":
        return f"Honeypot flagged ({sec['reason']})"

    return None


def run_drop_logic():
    """Grace -> degradation -> recovery / 7-day delete. Returns stats."""
    from holders_check import get_holder_count

    now = utcnow()
    today = now.date()
    stats = {"active_remaining": 0, "in_grace": 0, "checked": 0, "newly_dropped": 0, "crashed": 0,
             "crash_blocked": 0, "recovered": 0, "still_dropped": 0, "removed_7d": 0,
             "removed_never_qualified": 0}
    dropped_details, recovered_details, removed_details = [], [], []
    crash_suspects = []          # one-source crash readings: logged, not dropped
    blocked_details = []         # confirmed price crashes: removed + 15-day block
    from liquidity_check import MISMATCHES
    mismatch_start = len(MISMATCHES)   # this run's "liquidity source mismatch" notes
    held_details = []            # liquidity-crash drops healthy but liquidity < 50% of peak
    just_dropped = set()
    # Snapshots are dated with date.today() (like save_to_db / tracking), so
    # the Codex trend history is read against that same date.
    snap_day = date.today()

    session = SessionLocal()
    try:
        # --- ACTIVE: grace, then the 4 health criteria -----------------------
        for coin in session.query(Coin).filter_by(status="active").all():
            # Crash override FIRST - applies even during the grace period.
            kind, reason = crash_check(session, coin, snap_day, crash_suspects)
            if kind == "price":                            # REAL crash -> 15-day block
                _block_crashed(session, coin, reason, now)
                stats["crash_blocked"] += 1
                blocked_details.append((coin.name, reason))
                continue
            if reason is not None:
                stats["crashed"] += 1
            elif in_grace(coin, today):
                stats["in_grace"] += 1
                continue
            else:
                stats["checked"] += 1
                reason = unhealthy_reason(coin, session, snap_day)
            if reason is None:
                continue                                   # healthy -> stays
            if not coin.ever_qualified:
                _delete_coin(session, coin)                # never a real opportunity
                stats["removed_never_qualified"] += 1
                continue
            coin.status = "dropped"
            session.add(DroppedCoin(coin_id=coin.id, dropped_date=now, reason=reason))
            just_dropped.add(coin.id)
            stats["newly_dropped"] += 1
            dropped_details.append((coin.name, reason))
        session.flush()

        # --- DROPPED: re-check the same 4 criteria; recover or, on day 7, delete
        for coin in session.query(Coin).filter_by(status="dropped").all():
            if coin.id in just_dropped:
                continue                                   # dropped a moment ago
            last = (session.query(DroppedCoin.dropped_date, DroppedCoin.reason)
                    .filter_by(coin_id=coin.id)
                    .order_by(DroppedCoin.dropped_date.desc()).first())
            days = days_since(last[0], today) if last else RECOVERY_DAYS

            # Crash block: a coin dropped for a confirmed PRICE crash (e.g. an
            # older "Crash - price down ..." drop), or whose price crashes
            # while dropped, is never recovered - it's removed and blocked
            # for CRASH_BLOCK_DAYS from the crash.
            if last and is_price_crash_reason(last[1]):
                _block_crashed(session, coin, last[1], last[0])
                stats["crash_blocked"] += 1
                blocked_details.append((coin.name, last[1] + f" (dropped {last[0]:%Y-%m-%d})"))
                continue
            kind, reason = crash_check(session, coin, snap_day, crash_suspects)
            if kind == "price":
                _block_crashed(session, coin, reason, now)
                stats["crash_blocked"] += 1
                blocked_details.append((coin.name, reason))
                continue

            healthy = unhealthy_reason(coin, session, snap_day) is None
            if healthy and last and is_liquidity_crash_reason(last[1]):
                # Liquidity-crash drop: also needs liquidity back to half its
                # pre-crash peak; until then it stays dropped (7-day clock runs).
                ok, now_liq, peak = liquidity_recovery(session, coin, last[0], snap_day)
                if not ok:
                    healthy = False
                    held_details.append((coin.name, now_liq, peak))
            if healthy:
                # all 4 healthy -> RECOVER
                session.query(DroppedCoin).filter_by(coin_id=coin.id).delete(
                    synchronize_session=False)
                h = get_holder_count(coin.contract, coin.chain)
                coin.status = "active"
                coin.ever_qualified = True
                coin.active_since = now                    # fresh 5-day grace
                coin.holders_at_entry = h if isinstance(h, int) else None
                stats["recovered"] += 1
                recovered_details.append(coin.name)
            elif days >= RECOVERY_DAYS:                    # failed all 7 days -> DELETE
                removed_details.append(coin.name)
                _delete_coin(session, coin)
                stats["removed_7d"] += 1
            else:
                stats["still_dropped"] += 1

        session.flush()
        stats["active_remaining"] = session.query(Coin).filter_by(status="active").count()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    stats["crash_suspects"] = len(crash_suspects)
    stats["held_liquidity"] = len(held_details)
    from liquidity_check import MISMATCHES
    mismatches = {}
    for name, cx, dx in MISMATCHES[mismatch_start:]:
        mismatches.setdefault(name, (cx, dx))
    stats["liq_source_mismatch"] = len(mismatches)
    _print_summary(stats, dropped_details, recovered_details, removed_details, crash_suspects,
                   blocked_details, held_details, mismatches)
    return stats


def _print_summary(stats, dropped_details, recovered_details, removed_details, crash_suspects=(),
                   blocked_details=(), held_details=(), mismatches=None):
    line = "=" * 52
    print(line)
    print("  DROP LOGIC SUMMARY")
    print(line)
    print(f"  Active - in {GRACE_DAYS}-day grace (untouched) .. {stats['in_grace']:>5}")
    print(f"  Active - re-checked ............... {stats['checked']:>5}")
    print(f"  PRICE crash -> removed, {CRASH_BLOCK_DAYS}-day block {stats.get('crash_blocked', 0):>5}")
    for name, reason in blocked_details:
        print(f"      x {(name or 'unknown')[:18]:<18}  ({reason})")
    print(f"  Liquidity crash (normal drop) .... {stats['crashed']:>5}")
    print(f"  Suspect crash readings (1 source) . {len(crash_suspects):>5}   (not dropped - re-checked tomorrow)")
    for name, note in crash_suspects:
        print(f"      ? {(name or 'unknown')[:18]:<18}  ({note})")
    print(f"  Newly dropped (unhealthy) ......... {stats['newly_dropped']:>5}")
    for name, reason in dropped_details:
        print(f"      - {(name or 'unknown')[:18]:<18}  ({reason})")
    mismatches = mismatches or {}
    print(f"  Liquidity source mismatch ......... {len(mismatches):>5}   (Codex > 10x DexScreener - using DexScreener)")
    for name, (cx, dx) in mismatches.items():
        print(f"      ~ {(name or 'unknown')[:18]:<18}  (liquidity source mismatch - using DexScreener: "
              f"Codex ${cx:,.0f} vs DexScreener ${dx:,.0f})")
    print(f"  Held: liquidity < {LIQ_CRASH_RECOVER_SHARE:.0%} of pre-crash . {len(held_details):>5}")
    for name, now_liq, peak in held_details:
        print(f"      = {(name or 'unknown')[:18]:<18}  (liquidity ${now_liq or 0:,.0f} vs pre-crash ${peak:,.0f}; "
              f"needs ${LIQ_CRASH_RECOVER_SHARE * peak:,.0f})")
    print(f"  Recovered (back to active) ........ {stats['recovered']:>5}")
    for name in recovered_details[:20]:
        print(f"      - {(name or 'unknown')[:18]}")
    if len(recovered_details) > 20:
        print(f"      ... +{len(recovered_details) - 20} more")
    print(f"  Still dropped (recovery window) ... {stats['still_dropped']:>5}")
    print(f"  Deleted (unhealthy {RECOVERY_DAYS} days) ....... {stats['removed_7d']:>5}")
    for name in removed_details[:20]:
        print(f"      - {(name or 'unknown')[:18]}")
    print(f"  Removed (never qualified) ......... {stats['removed_never_qualified']:>5}")
    print(f"  Still active ...................... {stats['active_remaining']:>5}")
    print(line)


def main():
    init_db()
    run_drop_logic()


if __name__ == "__main__":
    main()
