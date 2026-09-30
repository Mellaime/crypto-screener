"""
drop_logic.py
-------------
The DROP LOGIC. Completely separate from the 7-step filter: the filter only
brings NEW coins into the active list; drop_logic only watches coins ALREADY
there. It runs as part of the daily pipeline, after discover -> filter -> save.

It uses ONE set of 4 health criteria, both for dropping and for recovery:
    * volume falling over the last 5 complete days        (CoinGecko)
    * liquidity down > 5% over min(30 days, age)           (Codex)
    * holders down > 10% vs the count when it entered active
      (coins.holders_at_entry; no baseline yet -> this check is skipped)
    * honeypot flagged unsafe                              (GoPlus)
"Unknown" on any check counts as healthy - never drop, or keep out, on
missing data. Revenue is NOT part of this.

1. GRACE: a coin is untouched for its first GRACE_DAYS (5) calendar days in
   active. Entered on day D -> first re-check on day D+5. Counted from
   coins.active_since, which resets when a coin recovers.

2. ACTIVE, past grace: re-checked daily. If ANY criterion fires -> DEGRADED:
   status "dropped" + a dropped_coins row with the reason.

3. DROPPED: a 7-day recovery window. Re-checked daily on the SAME 4 criteria
   (not the full filter):
     all 4 healthy              -> back to active, fresh 5-day grace, and the
                                   holders baseline restarts from today
     still unhealthy on day 7   -> deleted permanently (coin, snapshots and
                                   drop rows - it disappears from the site)
   Dropped on day D -> re-checked on days D+1 ... D+7 (7 chances); still
   failing on D+7 -> deleted.

Checks stop at the first criterion that fires, with CoinGecko (the tightest
free quota) last, so a failing coin doesn't spend CoinGecko calls.

ever_qualified: a coin that never legitimately passed the filter is removed
silently instead of being recorded in dropped_coins.

Run it standalone:
    python drop_logic.py
"""

from datetime import date

from database import SessionLocal, init_db, utcnow
from models import Coin, DailySnapshot, DroppedCoin

GRACE_DAYS = 5               # calendar days untouched after entering active
RECOVERY_DAYS = 7            # daily recovery re-checks before permanent delete
MAX_HOLDER_DROP_PCT = 10     # holders down more than this -> unhealthy

# VOLUME without a CoinGecko call per coin per day. Every volume check (the
# filter's rotation or this module) stores its verdict (volume_verdicts); here
# the latest verdict is REUSED while it's recent, and only refreshed with a
# new CoinGecko call when it's older than the window - within a per-run budget,
# oldest verdicts first. Over budget -> the last stored verdict is used as-is
# ("unknown" only if the coin was never checked).
VOLUME_REUSE_DAYS = {"active": 7,     # filter re-checks every coin each cycle
                     "dropped": 2}    # recovery window is 7 days: look again often
VOLUME_REFRESH_BUDGET = {"active": 10, "dropped": 20}   # CoinGecko calls per run


def _delete_coin(session, coin):
    """Remove a coin completely: its snapshots, any dropped_coins rows, itself."""
    session.query(DailySnapshot).filter_by(coin_id=coin.id).delete(
        synchronize_session=False
    )
    session.query(DroppedCoin).filter_by(coin_id=coin.id).delete(
        synchronize_session=False
    )
    session.delete(coin)


def days_since(when, today):
    """Whole calendar days from `when` (a datetime) to `today`."""
    return (today - when.date()).days


def in_grace(coin, today):
    """True during the coin's first GRACE_DAYS calendar days in active."""
    since = coin.active_since or coin.first_seen
    return since is not None and days_since(since, today) < GRACE_DAYS


def volume_verdict(session, coin, kind, budget, now):
    """The coin's volume verdict, reusing the stored one when it's recent (see
    VOLUME_REUSE_DAYS) and refreshing it within `budget` otherwise."""
    from volume_check import check_volume_rising, latest_verdict, record_verdict

    verdict, checked_at = latest_verdict(session, coin.contract)
    fresh = (checked_at is not None
             and days_since(checked_at, now.date()) <= VOLUME_REUSE_DAYS[kind])
    if fresh:
        budget["reused"] += 1
        return verdict
    if budget[kind] > 0:
        budget[kind] -= 1
        budget["fetched"] += 1
        verdict = check_volume_rising(coin.contract, coin.chain)["verdict"]
        record_verdict(session, coin.contract, verdict, now)
        return verdict
    budget["stale"] += 1
    return verdict or "unknown"          # over budget: last stored verdict


def _verdict_age_key(session, coin):
    """Sort key: coins whose volume verdict is oldest (or missing) first."""
    from volume_check import latest_verdict
    _, checked_at = latest_verdict(session, coin.contract)
    return checked_at.isoformat() if checked_at else ""


def unhealthy_reason(coin, session=None, kind="active", budget=None, now=None):
    """
    The 4 health criteria. Returns the first one that fires, or None if the
    coin is healthy. Unknown on any check counts as healthy.
    """
    from liquidity_check import check_liquidity_trend
    from holders_check import get_holder_count
    from security_check import check_token_security
    from volume_check import check_volume_rising

    liq = check_liquidity_trend(coin.contract, coin.chain)
    if liq["verdict"] == "drop":
        return f"Liquidity down {abs(liq['pct']):.1f}% since {liq['base_date']}"

    base = coin.holders_at_entry
    now_h = get_holder_count(coin.contract, coin.chain)
    if base and isinstance(now_h, int):
        pct = (now_h - base) / base * 100
        if pct < -MAX_HOLDER_DROP_PCT:
            return f"Holders down {abs(pct):.1f}% ({base:,} -> {now_h:,})"

    sec = check_token_security(coin.contract, coin.chain)
    if sec["verdict"] == "unsafe":
        return f"Honeypot flagged ({sec['reason']})"

    if session is not None and budget is not None:
        vol = volume_verdict(session, coin, kind, budget, now or utcnow())
    else:                                  # standalone use: plain live check
        vol = check_volume_rising(coin.contract, coin.chain)["verdict"]
    if vol == "falling":
        return "Volume falling (5 complete days)"

    return None


def run_drop_logic():
    """Grace -> degradation -> recovery / 7-day delete. Returns stats."""
    from holders_check import get_holder_count

    now = utcnow()
    today = now.date()
    stats = {"active_remaining": 0, "in_grace": 0, "checked": 0, "newly_dropped": 0,
             "recovered": 0, "still_dropped": 0, "removed_7d": 0,
             "removed_never_qualified": 0}
    dropped_details, recovered_details, removed_details = [], [], []
    just_dropped = set()
    budget = dict(VOLUME_REFRESH_BUDGET, reused=0, fetched=0, stale=0)
    stats["volume"] = budget

    session = SessionLocal()
    try:
        # --- ACTIVE: grace, then the 4 health criteria -----------------------
        # Oldest volume verdicts first, so the refresh budget goes where needed.
        active = sorted(session.query(Coin).filter_by(status="active").all(),
                        key=lambda c: _verdict_age_key(session, c))
        for coin in active:
            if in_grace(coin, today):
                stats["in_grace"] += 1
                continue
            stats["checked"] += 1
            reason = unhealthy_reason(coin, session, "active", budget, now)
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
        dropped = sorted(session.query(Coin).filter_by(status="dropped").all(),
                         key=lambda c: _verdict_age_key(session, c))
        for coin in dropped:
            if coin.id in just_dropped:
                continue                                   # dropped a moment ago
            last = (session.query(DroppedCoin.dropped_date)
                    .filter_by(coin_id=coin.id)
                    .order_by(DroppedCoin.dropped_date.desc()).first())
            days = days_since(last[0], today) if last else RECOVERY_DAYS

            if unhealthy_reason(coin, session, "dropped", budget, now) is None:
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

    _print_summary(stats, dropped_details, recovered_details, removed_details)
    return stats


def _print_summary(stats, dropped_details, recovered_details, removed_details):
    line = "=" * 52
    print(line)
    print("  DROP LOGIC SUMMARY")
    print(line)
    print(f"  Active - in {GRACE_DAYS}-day grace (untouched) .. {stats['in_grace']:>5}")
    print(f"  Active - re-checked ............... {stats['checked']:>5}")
    print(f"  Newly dropped (unhealthy) ......... {stats['newly_dropped']:>5}")
    for name, reason in dropped_details:
        print(f"      - {(name or 'unknown')[:18]:<18}  ({reason})")
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
    v = stats.get("volume") or {}
    print(f"  Volume verdicts: reused {v.get('reused', 0)} | fetched "
          f"{v.get('fetched', 0)} CoinGecko calls (budget "
          f"{VOLUME_REFRESH_BUDGET['active']} active + "
          f"{VOLUME_REFRESH_BUDGET['dropped']} dropped) | stale, over budget "
          f"{v.get('stale', 0)}")
    print(line)


def main():
    init_db()
    run_drop_logic()


if __name__ == "__main__":
    main()
