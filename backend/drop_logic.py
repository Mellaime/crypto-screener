"""
drop_logic.py
-------------
Phase 2 - the DROP LOGIC. This keeps the main list clean: it moves coins that
stop qualifying into the `dropped_coins` table, brings back ones that recover,
and permanently removes coins that stay dropped too long.

It runs as part of the daily pipeline, AFTER discover -> filter -> save.

For each coin it re-checks, using FRESH live data from DexScreener:
    Filters (same 4 as saving):
      1. MCap $1M-$500M
      2. Age > 10 days
      3. Volume > $50,000 (temporary rule)
      4. Liquidity > $30,000
    Extra DROP triggers (compare today's live value to the PREVIOUS snapshot):
      5. Liquidity dropped more than 20% in 24h
      6. Holders dropped more than 10% in 24h  (skipped - no holders data yet)

Actions:
    * active coin that now FAILS  -> move to dropped_coins (with date + reason),
                                     set status = "dropped".
    * dropped coin that now PASSES -> remove from dropped_coins,
                                      set status = "active" (RECOVERY).
    * coin dropped for > 7 days     -> removed completely (CLEANUP).

Run it:
    python drop_logic.py
"""

from datetime import date, timedelta

from database import SessionLocal, init_db, utcnow
from models import Coin, DailySnapshot, DroppedCoin
from fetch_dexscreener import fetch_pairs_for_addresses, build_token_records

# Reuse the SAME thresholds the saver uses, so the rules never drift apart.
from save_to_db import (
    to_float,
    MIN_MARKET_CAP,
    MAX_MARKET_CAP,
    MIN_AGE_DAYS,
    MIN_VOLUME_24H,
    MIN_LIQUIDITY,
)

# Extra drop-trigger thresholds.
MAX_LIQ_DROP_PCT = 20        # drop if liquidity fell more than 20% vs yesterday
MAX_HOLDER_DROP_PCT = 10     # drop if holders fell more than 10% vs yesterday
DROPPED_RETENTION_DAYS = 7   # remove for good after this long still dropped


def _delete_coin(session, coin):
    """Remove a coin completely: its snapshots, any dropped_coins rows, itself."""
    session.query(DailySnapshot).filter_by(coin_id=coin.id).delete(
        synchronize_session=False
    )
    session.query(DroppedCoin).filter_by(coin_id=coin.id).delete(
        synchronize_session=False
    )
    session.delete(coin)


def previous_snapshot(session, coin_id, today):
    """The most recent snapshot from a day BEFORE today (i.e. 'yesterday')."""
    return (
        session.query(DailySnapshot)
        .filter(DailySnapshot.coin_id == coin_id, DailySnapshot.date < today)
        .order_by(DailySnapshot.date.desc(), DailySnapshot.id.desc())
        .first()
    )


def check_failures(rec, prev_snap):
    """
    Return a list of REASONS the coin fails right now. Empty list = it passes.
    `rec` is fresh live data (or None if the coin returned no market data).
    """
    if rec is None:
        return ["No live market data (possibly delisted)"]

    reasons = []
    mcap = to_float(rec.get("market_cap"))
    age = rec.get("age_days")
    volume = to_float(rec.get("volume_24h")) or 0
    liquidity = to_float(rec.get("liquidity_usd")) or 0
    holders = rec.get("holders")  # DexScreener doesn't provide this yet -> None

    # --- Filter 1: market cap window ---
    if mcap is None:
        reasons.append("No market cap data")
    elif mcap < MIN_MARKET_CAP:
        reasons.append("MCap below $1M")
    elif mcap > MAX_MARKET_CAP:
        reasons.append("MCap above $500M")

    # --- Filter 2: age ---
    if age is None:
        reasons.append("No age data")
    elif age <= MIN_AGE_DAYS:
        reasons.append("Age <= 10 days (too new)")

    # --- Filter 3: volume (temporary rule) ---
    if volume <= MIN_VOLUME_24H:
        reasons.append("Volume fell (<= $50K)")

    # --- Filter 4: liquidity floor ---
    if liquidity <= MIN_LIQUIDITY:
        reasons.append("Liquidity below $30K")

    # --- Trigger 5: liquidity dropped >20% vs yesterday ---
    if prev_snap and prev_snap.liquidity:
        change = (liquidity - prev_snap.liquidity) / prev_snap.liquidity * 100
        if change < -MAX_LIQ_DROP_PCT:
            reasons.append(f"Liquidity dropped {change:.0f}%")

    # --- Trigger 6: holders dropped >10% vs yesterday ---
    # SKIPPED when we have no holders data (current or previous). Ready for
    # when a holders source is added.
    if holders is not None and prev_snap and prev_snap.holders:
        hchange = (holders - prev_snap.holders) / prev_snap.holders * 100
        if hchange < -MAX_HOLDER_DROP_PCT:
            reasons.append(f"Holders dropped {hchange:.0f}%")

    return reasons


def run_drop_logic():
    """Do a full drop/recovery/cleanup pass and print a summary. Returns stats."""
    today = date.today()
    stats = {
        "active_remaining": 0,
        "newly_dropped": 0,
        "recovered": 0,
        "removed_never_qualified": 0,
        "removed_7d": 0,
    }
    dropped_details = []          # (name, reason)
    recovered_details = []        # name
    removed_never_details = []    # name (never legitimately qualified)
    removed_details = []          # name (dropped > 7 days)

    session = SessionLocal()
    try:
        coins = session.query(Coin).all()
        if not coins:
            print("No coins in the database yet - nothing to check.")
            return stats

        # --- Fetch FRESH live data for every coin, by contract address ------
        addresses = [c.contract for c in coins]
        pairs = fetch_pairs_for_addresses(addresses)

        # SAFETY: if we got nothing at all, it's almost certainly a network/API
        # problem. Do NOT drop everything on a blip - skip this pass instead.
        if not pairs:
            print("[WARN] No live data returned at all - skipping drop pass "
                  "to avoid false drops.")
            stats["active_remaining"] = (
                session.query(Coin).filter_by(status="active").count()
            )
            return stats

        records = build_token_records(
            pairs, keep_addresses={a.lower() for a in addresses}
        )
        by_contract = {r["contract"].lower(): r for r in records}

        # --- Re-check every coin --------------------------------------------
        for coin in coins:
            rec = by_contract.get(coin.contract.lower())
            prev = previous_snapshot(session, coin.id, today)
            reasons = check_failures(rec, prev)
            passes = not reasons

            if passes:
                if coin.status == "dropped":
                    # Dropped coin that now passes again -> RECOVER it.
                    session.query(DroppedCoin).filter_by(coin_id=coin.id).delete(
                        synchronize_session=False
                    )
                    coin.status = "active"
                    coin.ever_qualified = True
                    stats["recovered"] += 1
                    recovered_details.append(coin.name)
                # (active & passes -> stays active)

            else:
                # The coin fails right now.
                if not coin.ever_qualified:
                    # It NEVER legitimately qualified (e.g. it was too new and
                    # only ever got in before the age filter existed). This was
                    # never a real opportunity, so remove it SILENTLY - do not
                    # record it in dropped_coins.
                    name = coin.name
                    _delete_coin(session, coin)
                    stats["removed_never_qualified"] += 1
                    removed_never_details.append(name)

                elif coin.status == "active":
                    # It genuinely qualified before and is now failing for a
                    # (market) reason -> record it as a real, tracked drop.
                    reason_text = "; ".join(reasons)
                    coin.status = "dropped"
                    session.add(DroppedCoin(
                        coin_id=coin.id,
                        dropped_date=utcnow(),
                        reason=reason_text,
                    ))
                    stats["newly_dropped"] += 1
                    dropped_details.append((coin.name, reason_text))

                # (already dropped & still failing -> stays dropped; the
                #  7-day cleanup below will remove it eventually)

        session.flush()

        # --- CLEANUP: remove coins dropped for more than 7 days -------------
        cutoff = utcnow() - timedelta(days=DROPPED_RETENTION_DAYS)
        old_drops = (
            session.query(DroppedCoin)
            .filter(DroppedCoin.dropped_date < cutoff)
            .all()
        )
        seen_coin_ids = set()
        for d in old_drops:
            if d.coin_id in seen_coin_ids:
                continue
            seen_coin_ids.add(d.coin_id)
            coin = session.get(Coin, d.coin_id)
            if coin and coin.status == "dropped":
                name = coin.name  # capture before we delete it
                _delete_coin(session, coin)
                stats["removed_7d"] += 1
                removed_details.append(name)

        session.flush()
        stats["active_remaining"] = (
            session.query(Coin).filter_by(status="active").count()
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _print_summary(stats, dropped_details, recovered_details,
                   removed_never_details, removed_details)
    return stats


def _print_summary(stats, dropped_details, recovered_details,
                   removed_never_details, removed_details):
    line = "=" * 52
    print(line)
    print("  DROP LOGIC SUMMARY")
    print(line)
    print(f"  Still active ..................... {stats['active_remaining']:>5}")
    print(f"  Newly dropped (real drops) ...... {stats['newly_dropped']:>5}")
    for name, reason in dropped_details:
        print(f"      - {(name or 'unknown')[:16]:<16}  ({reason})")
    print(f"  Recovered (back to active) ...... {stats['recovered']:>5}")
    for name in recovered_details:
        print(f"      - {(name or 'unknown')[:16]}")
    print(f"  Removed (never qualified) ....... {stats['removed_never_qualified']:>5}")
    for name in removed_never_details:
        print(f"      - {(name or 'unknown')[:16]}")
    print(f"  Removed (dropped > 7 days) ...... {stats['removed_7d']:>5}")
    for name in removed_details:
        print(f"      - {(name or 'unknown')[:16]}")
    print(line)


def main():
    init_db()
    run_drop_logic()


if __name__ == "__main__":
    main()
