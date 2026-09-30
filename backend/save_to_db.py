"""
save_to_db.py
-------------
Phase 2 - Step (b): run the filter chain on discovered tokens and save the
survivors into the database.

THE FILTER - exactly these 7 steps, in this order:
    1. Discovery  - CoinGecko + CoinMarketCap, MCap $1M-$500M (discovery.py).
                    MCap is enforced HERE only - there is no later mcap step.
    2. Meme +     - memes AND stablecoins removed right after discovery
       Stablecoin   (meme_filter.py).
    3. Age        - >= 10 days; DexScreener pairCreatedAt, else CMC/CoinGecko
                    listing date; too new or unknown -> DROP (age_check.py).
    -- ROTATION QUEUE (rotation.py): steps 4-7 run only on ROTATION_BUDGET
       age-passers per run, first-come-first-served; the rest wait. --
    4. Revenue    - DefiLlama; DROP only on confirmed NO_REVENUE.
    5. Volume     - CoinGecko 5 complete days, day5 > day1; falling or
                    unknown -> DROP (volume_check.py).
    6. Honeypot   - GoPlus; DROP only on confirmed unsafe. After volume
                    because free GoPlus is rate-limited (~20-30 calls/min).
    7. Liquidity  - Codex daily liquidity over min(30d, age); down > 5% ->
                    DROP; unknown -> PASS (liquidity_check.py). LAST step.
Steps 1-2 run inside discover_tokens(); steps 3-7 run in save_tokens() below.

Holders are NOT a filter: the holder count is only stored for display.

What gets saved:
    coins            -> contract, chain, name, first_seen, status="active"
                        (contract is UNIQUE, so we never insert duplicates)
    daily_snapshots  -> coin_id, date, volume, liquidity, mcap, revenue,
                        holders (stored for display/scoring, never a gate)

Run it:
    python save_to_db.py
"""

from datetime import date

from database import SessionLocal, init_db, utcnow
from models import Coin, DailySnapshot
from discovery import discover_tokens   # steps 1-2: discovery + meme/stablecoin
                                        # (DexScreener only enriches by contract)
from age_check import decide_age                    # step 3
from revenue_check import check_revenue             # step 4
from volume_check import check_volume_rising, record_verdict   # step 5
from rotation import pick_batch, mark_checked, ROTATION_BUDGET   # queue for 4-7
from security_check import check_token_security     # step 6 (honeypot)
from liquidity_check import check_liquidity_trend   # step 7 (LAST)
from narrative_check import lookup_category, CATEGORY_BUDGET
from holders_check import get_holder_count          # stored only - NOT a filter


def to_float(value):
    """Safely turn a value into a float, or None if it can't be converted."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_volume_rising(coin, days=5):
    """
    Return True only if this coin's 24h volume is trending UP over the last
    `days` daily snapshots stored in the database.

    *** NOT USED YET. ***
    This is the REAL volume rule we want. It needs at least `days` days of
    history, which we don't have until the scheduler has run daily for a
    while. It's defined now, ready to swap in for the temporary
    "volume > $50,000" check in Filter 3 once 5 days of snapshots exist.

    How "rising" is defined here (tunable later):
      - we must have at least `days` snapshots, each with a recorded volume;
      - volume must never DROP from one day to the next; and
      - the most recent day must be strictly higher than the oldest day
        (a real net increase, not just flat).
    This rolls forward automatically: each day it reads the latest 5 rows,
    so the 5-day window always ends on today's date.
    """
    session = SessionLocal()
    try:
        # Newest first, take the most recent `days` rows.
        snaps = (
            session.query(DailySnapshot)
            .filter(DailySnapshot.coin_id == coin.id)
            .order_by(DailySnapshot.date.desc())
            .limit(days)
            .all()
        )
    finally:
        session.close()

    # Not enough history yet -> can't say it's rising.
    if len(snaps) < days:
        return False

    # Flip to oldest-first so we read the trend left-to-right.
    volumes = [s.volume for s in reversed(snaps)]

    # If any day is missing its volume number, we can't judge the trend.
    if any(v is None for v in volumes):
        return False

    never_drops = all(volumes[i] >= volumes[i - 1] for i in range(1, len(volumes)))
    net_increase = volumes[-1] > volumes[0]
    return never_drops and net_increase


def save_tokens(tokens):
    """
    Save filtered tokens to the database. Returns a dict of counts for the
    summary. Everything happens inside one session (one 'workspace').
    """
    today = date.today()

    # Counters for the summary at the end.
    stats = {
        "discovered": len(tokens),
        "passed_age": 0,
        "skipped_too_new": 0,
        "skipped_no_age": 0,
        "age_via_fallback": 0,   # CG/CMC fallback was used for the age decision
        "passed_volume": 0,          # volume rising (CoinGecko 5-day)
        "skipped_vol_falling": 0,
        "skipped_vol_unknown": 0,    # no CoinGecko history / chain not on CG
        "queue_new": 0,              # age-passers seen for the first time
        "queue_known": 0,            # age-passers already in the queue
        "queue_picked": 0,           # got the expensive steps this run
        "queue_waiting": 0,          # waiting for a later run
        "passed_liquidity": 0,       # Codex liquidity trend (step 7)
        "skipped_liq_falling": 0,    # down more than 5% over the window
        "liquidity_unknown": 0,      # no Codex data -> passed, not dropped
        "passed_revenue": 0,
        "skipped_no_revenue": 0,
        "revenue_has": 0,
        "revenue_unknown": 0,
        "passed_security": 0,
        "skipped_unsafe": 0,
        "security_unknown": 0,
        "new_coins": 0,
        "existing_coins": 0,
        "snapshots_inserted": 0,
        "snapshots_updated": 0,
        "category_lookups": 0,       # live CoinGecko category calls this run
    }

    session = SessionLocal()
    try:
        # --- Step 3: AGE (two-step, by contract + chain) --------------------
        # Run over every token first, so we know exactly who reaches step 4.
        # DexScreener pairCreatedAt, else CoinMarketCap/CoinGecko listing date
        # (see age_check.decide_age). Younger than 10 days, OR of unknowable
        # age, is dropped here.
        age_passed = []
        for t in tokens:
            keep_age, age_days, age_source = decide_age(t)
            if age_source == "fallback":
                stats["age_via_fallback"] += 1
            if not keep_age:
                if age_days is None:
                    stats["skipped_no_age"] += 1     # unknown on DS + CG + CMC
                else:
                    stats["skipped_too_new"] += 1    # younger than 10 days
                continue
            stats["passed_age"] += 1
            age_passed.append(t)

        # --- ROTATION QUEUE (rotation.py) -----------------------------------
        # The expensive steps 4-7 run only on ROTATION_BUDGET age-passers per
        # run: the ones that have waited longest. New coins join at the back
        # of the queue (no priority). Everyone else waits for a later run -
        # skipped this run, neither saved nor dropped.
        picked, queue = pick_batch(session, age_passed)
        stats["queue_new"] = queue["new"]
        stats["queue_known"] = queue["known"]
        stats["queue_picked"] = len(picked)
        stats["queue_waiting"] = queue["queued"] - len(picked)

        for t in picked:
            # --- Step 4: revenue EXISTENCE (DefiLlama) ---------------------
            # Reject ONLY when NO_REVENUE is positively confirmed (DefiLlama
            # tracks the token but it has no fees mechanism). HAS_REVENUE (any
            # amount, even $0.01) and UNKNOWN (not listed) both PASS - we don't
            # want to lose good coins that DefiLlama simply hasn't indexed.
            revenue = check_revenue(t.get("name"), t.get("symbol"),
                                    t["contract"], t["chain"])
            if revenue["verdict"] == "NO_REVENUE":
                stats["skipped_no_revenue"] += 1
                continue
            if revenue["verdict"] == "HAS_REVENUE":
                stats["revenue_has"] += 1
            else:
                stats["revenue_unknown"] += 1
            stats["passed_revenue"] += 1

            # --- Step 5: VOLUME RISING (CoinGecko, 5 complete days) --------
            # rising -> keep; falling or unknown (no CoinGecko history) -> drop.
            vol = check_volume_rising(t["contract"], t["chain"])
            # Keep the verdict so drop_logic can reuse it (no 2nd CoinGecko call).
            record_verdict(session, t["contract"], vol["verdict"], utcnow())
            if vol["verdict"] != "rising":
                if vol["verdict"] == "falling":
                    stats["skipped_vol_falling"] += 1
                else:
                    stats["skipped_vol_unknown"] += 1
                continue
            stats["passed_volume"] += 1

            # --- Step 6: honeypot / security check (GoPlus) ----------------
            # Placed AFTER volume: free GoPlus is rate-limited, so it runs on
            # the small volume-passing set. Only a CONFIRMED honeypot /
            # cannot-sell rejects the coin; "unknown" (unsupported chain, no
            # data) does NOT reject.
            security = check_token_security(t["contract"], t["chain"])
            if security["verdict"] == "unsafe":
                stats["skipped_unsafe"] += 1
                continue
            if security["verdict"] == "unknown":
                stats["security_unknown"] += 1
            stats["passed_security"] += 1

            # --- Step 7 (LAST): LIQUIDITY TREND (Codex) --------------------
            # % change over min(30 days, age): down > 5% -> drop; rising /
            # stable / down <= 5% -> keep; unknown (no Codex data) -> pass.
            liq = check_liquidity_trend(t["contract"], t["chain"])
            if liq["verdict"] == "drop":
                stats["skipped_liq_falling"] += 1
                continue
            if liq["verdict"] == "unknown":
                stats["liquidity_unknown"] += 1
            stats["passed_liquidity"] += 1  # passed ALL 7 steps -> save it

            # --- Values STORED on the snapshot (display / scoring only) ----
            # None of these keep or drop a coin any more.
            volume_24h = to_float(t.get("volume_24h")) or 0   # DexScreener 24h
            liquidity = to_float(t.get("liquidity_usd")) or 0  # DexScreener now
            mcap = to_float(t.get("market_cap"))
            holders_result = get_holder_count(t["contract"], t["chain"])
            holder_num = holders_result if isinstance(holders_result, int) else None
            price = to_float(t.get("price_usd"))             # DexScreener, main pool

            # --- Find or create the coin (unique by contract) ---------------
            coin = session.query(Coin).filter_by(contract=t["contract"]).first()
            if coin is None:
                coin = Coin(
                    contract=t["contract"],
                    chain=t["chain"],
                    name=t.get("name") or t.get("symbol") or "unknown",
                    status="active",
                    ever_qualified=True,  # it passed all 7 filter steps to get here
                    # first_seen and active_since default to now.
                    # Baseline for drop_logic's "holders down > 10%" check.
                    holders_at_entry=holder_num,
                )
                session.add(coin)
                session.flush()  # flush so the new coin gets its id now
                stats["new_coins"] += 1
            else:
                # It qualified again -> mark it as a legitimate opportunity.
                # Status is never changed here: the filter only brings NEW
                # coins into active. Recovering a DROPPED coin is drop_logic's
                # job, on its own 4 health criteria.
                coin.ever_qualified = True
                if coin.status == "active" and coin.holders_at_entry is None:
                    coin.holders_at_entry = holder_num   # fill a missing baseline
                stats["existing_coins"] += 1

            # Store the security info (step 6) on the coin so the dashboard
            # can show it. Applies to both new and existing coins.
            coin.security_verdict = security["verdict"]
            coin.verified = security["verified"]
            coin.renounced = security["renounced"]
            coin.mint_disabled = security["mint_disabled"]

            # Current price for the dashboard. price_update.py refreshes it
            # again at the end of every run, for active + dropped coins.
            if price is not None:
                coin.price_usd = price
                coin.price_updated_at = utcnow()

            # Real category/narrative from CoinGecko - looked up ONCE per coin:
            #   None -> not looked up yet (the dashboard shows the generic
            #           sentence); tried within CATEGORY_BUDGET per run
            #   ""   -> looked up, CoinGecko has no usable category (final -
            #           never looked up again)
            # A transient failure leaves it None, so it's retried later.
            if (coin.category is None
                    and stats["category_lookups"] < CATEGORY_BUDGET):
                cat, definitive, called = lookup_category(t["contract"], t["chain"])
                stats["category_lookups"] += int(called)
                if cat:
                    coin.category = cat
                elif definitive:
                    coin.category = ""

            # The DefiLlama daily revenue number (step 4); None if UNKNOWN.
            rev_num = revenue.get("daily_revenue")

            # --- Insert or update today's snapshot --------------------------
            # One snapshot per coin per day: running again the same day updates
            # the existing row instead of creating a duplicate.
            snapshot = (
                session.query(DailySnapshot)
                .filter_by(coin_id=coin.id, date=today)
                .first()
            )
            if snapshot is None:
                snapshot = DailySnapshot(
                    coin_id=coin.id,
                    date=today,
                    volume=volume_24h,
                    liquidity=liquidity,
                    mcap=mcap,
                    revenue=rev_num,      # DefiLlama daily revenue (None if unknown)
                    price_usd=price,      # USD price that day
                    holders=holder_num,   # CMC DEX holder count (None if unknown)
                    # score is filled in later by the scoring step.
                )
                session.add(snapshot)
                stats["snapshots_inserted"] += 1
            else:
                snapshot.volume = volume_24h
                snapshot.liquidity = liquidity
                snapshot.mcap = mcap
                snapshot.revenue = rev_num
                snapshot.price_usd = price
                snapshot.holders = holder_num
                stats["snapshots_updated"] += 1

        # Every coin that had its turn this run goes to the back of the queue
        # (whatever its result). Same transaction as the saves: if the run
        # fails, nothing is marked and those coins keep their place.
        mark_checked(session, picked)

        # Save everything at once. If anything above had failed, we'd roll back.
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return stats


def print_summary(stats):
    """Print a clear, human-readable summary of what happened."""
    from discovery import LAST_COUNTS
    dc = LAST_COUNTS
    line = "=" * 56
    sub = "  " + "-" * 52
    print(line)
    print("  FILTER FUNNEL (7 steps)")
    print(line)
    print(f"  1. Discovery (CG+CMC, $1M-$500M) - unique .. {dc.get('discovery', '?'):>6}")
    print(f"  2. Meme + stablecoin filter - passed ....... {dc.get('after_excl', '?'):>6}")
    print(f"       - dropped as meme ..................... {dc.get('meme_dropped', '?'):>6}")
    print(f"       - dropped as stablecoin ............... {dc.get('stable_dropped', '?'):>6}")
    print(f"       (not a filter: with DexScreener data -> {stats['discovered']})")
    print(sub)
    print(f"  3. Age >= 10 days - passed ................. {stats['passed_age']:>6}")
    print(f"       - dropped (too new, <10d) ............. {stats['skipped_too_new']:>6}")
    print(f"       - dropped (age unknown) ............... {stats['skipped_no_age']:>6}")
    print(sub)
    print(f"  ROTATION QUEUE (budget {ROTATION_BUDGET}/run) - not a filter:")
    print(f"       age-passers: new {stats['queue_new']} + known {stats['queue_known']}")
    print(f"       picked this run (waited longest) ...... {stats['queue_picked']:>6}")
    print(f"       waiting for a later run ............... {stats['queue_waiting']:>6}")
    print(sub)
    print(f"  4. Revenue (DefiLlama) - passed ............ {stats['passed_revenue']:>6}")
    print(f"       - dropped (NO_REVENUE) ................ {stats['skipped_no_revenue']:>6}")
    print(f"       - passed as unknown ................... {stats['revenue_unknown']:>6}")
    import volume_check
    print(f"  5. Volume rising (CoinGecko) - passed ...... {stats['passed_volume']:>6}"
          f"   ({volume_check.CALLS} CoinGecko calls)")
    print(f"       - dropped (falling) ................... {stats['skipped_vol_falling']:>6}")
    print(f"       - dropped (no CG history) ............. {stats['skipped_vol_unknown']:>6}")
    print(f"  6. Honeypot (GoPlus) - passed .............. {stats['passed_security']:>6}")
    print(f"       - dropped (honeypot/unsafe) ........... {stats['skipped_unsafe']:>6}")
    print(f"       - passed as unknown ................... {stats['security_unknown']:>6}")
    print(f"  7. Liquidity trend (Codex) - passed ........ {stats['passed_liquidity']:>6}")
    print(f"       - dropped (down > 5%) ................. {stats['skipped_liq_falling']:>6}")
    print(f"       - passed as unknown ................... {stats['liquidity_unknown']:>6}")
    print(sub)
    print(f"  SAVED -> NEW coins ......................... {stats['new_coins']:>6}")
    print(f"         + already existed (re-saved) ........ {stats['existing_coins']:>6}")
    print(f"  Snapshots inserted / updated today ......... "
          f"{stats['snapshots_inserted']:>3} / {stats['snapshots_updated']}")
    print(f"  Category lookups (CoinGecko calls) ......... "
          f"{stats['category_lookups']:>3} / {CATEGORY_BUDGET} budget")
    print(line)


def main():
    # Make sure the tables exist (safe to call every run).
    init_db()

    print("Discovering + fetching tokens...\n")
    tokens = discover_tokens()
    print()

    if not tokens:
        print("No tokens fetched - nothing to save.")
        return

    stats = save_tokens(tokens)
    print_summary(stats)


if __name__ == "__main__":
    main()
