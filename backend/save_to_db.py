"""
save_to_db.py
-------------
Phase 2 - Step (b): discover tokens, apply our market-cap filter, and save
the survivors into the database.

FILTER RULES (the core of the project) - a coin must pass every gate:
    2. Age >= 10 days - RUNS FIRST. Two steps (see age_check.py): DexScreener
       pairCreatedAt, and if that's unknown, CoinMarketCap/CoinGecko listing
       date. Anything younger than 10 days, OR of unknowable age, is dropped
       here before any other filter runs.
    1. Market cap between $1M and $500M (inclusive).
    3. 24h volume greater than $50,000 (TEMPORARY simple rule - see note below).
    4. Liquidity greater than $30,000 (too-thin liquidity = can't exit safely).
    (Then holders > 0, revenue exists, and the honeypot check - see below.)
    Skip anything that fails any rule (or is missing the data to check).

What gets saved:
    coins            -> contract, chain, name, first_seen, status="active"
                        (contract is UNIQUE, so we never insert duplicates)
    daily_snapshots  -> coin_id, date, volume, liquidity, mcap
                        (holders / revenue / score stay NULL for now)

Run it:
    python save_to_db.py
"""

from datetime import date

from database import SessionLocal, init_db
from models import Coin, DailySnapshot
from discovery import discover_tokens   # NEW source: CoinGecko + CMC + Bitquery
                                        # (DexScreener no longer discovers; it only
                                        # enriches by contract inside discover_tokens)
from revenue_check import check_revenue
from security_check import check_token_security
from narrative_check import get_category
from holders_check import get_holder_count
from age_check import decide_age   # Filter 2 (runs FIRST): two-step age gate
                                   # (DexScreener pairCreatedAt, else CG/CMC date)

# Filter 1: target market-cap window.
MIN_MARKET_CAP = 1_000_000        # $1M
MAX_MARKET_CAP = 500_000_000      # $500M

# Filter 2: minimum age lives in age_check.py (MIN_AGE_DAYS there). It runs
# FIRST in the loop below, so anything too new never reaches the other filters.

# Filter 3: minimum 24h volume (TEMPORARY - see the note in the save loop).
MIN_VOLUME_24H = 50_000           # $50K

# Filter 4: minimum liquidity.
MIN_LIQUIDITY = 30_000            # $30K


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
        "passed_mcap": 0,
        "skipped_below": 0,
        "skipped_above": 0,
        "skipped_no_mcap": 0,
        "passed_age": 0,
        "skipped_too_new": 0,
        "skipped_no_age": 0,
        "age_via_fallback": 0,   # Step 2 (CG/CMC) was used for the age decision
        "passed_volume": 0,
        "skipped_low_volume": 0,
        "passed_liquidity": 0,
        "skipped_low_liquidity": 0,
        "passed_holders": 0,
        "skipped_no_holders": 0,
        "holders_has": 0,
        "holders_unknown": 0,
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
    }

    session = SessionLocal()
    try:
        for t in tokens:
            # --- Filter 2: AGE - RUNS FIRST (two-step, by contract + chain) -
            # DexScreener pairCreatedAt, else CoinMarketCap/CoinGecko listing
            # date (see age_check.decide_age). Anything younger than 10 days,
            # OR of unknowable age, is dropped HERE before any other filter.
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

            # --- Filter 1: market cap must be inside our window -------------
            mcap = to_float(t.get("market_cap"))
            if mcap is None:
                stats["skipped_no_mcap"] += 1
                continue
            if mcap < MIN_MARKET_CAP:
                stats["skipped_below"] += 1
                continue
            if mcap > MAX_MARKET_CAP:
                stats["skipped_above"] += 1
                continue
            stats["passed_mcap"] += 1  # passed age + market cap

            # --- Filter 3: 24h volume must be alive, not dead ---------------
            # TEMPORARY - replace with rolling 5-day rising check once 5 days
            # of snapshots exist (see is_volume_rising() below). We can't
            # measure a trend yet with only 1 day of history, so for now we
            # simply require 24h volume > $50,000. Every run still SAVES today's
            # volume into daily_snapshots, so the 5-day history builds itself.
            volume_24h = to_float(t.get("volume_24h")) or 0
            if volume_24h <= MIN_VOLUME_24H:
                stats["skipped_low_volume"] += 1
                continue
            stats["passed_volume"] += 1  # passed filters 1-3

            # --- Filter 4: liquidity must be deep enough to exit safely -----
            liquidity = to_float(t.get("liquidity_usd")) or 0
            if liquidity <= MIN_LIQUIDITY:
                stats["skipped_low_liquidity"] += 1
                continue
            stats["passed_liquidity"] += 1  # passed filters 1-4

            # --- Filter 4b: HOLDERS (CMC DEX) ------------------------------
            # TEMPORARY - with only 1 day of history we can't measure holder
            # GROWTH yet, so for now we just require a positive holder count.
            # Switch to holders_check.is_holders_rising() once a few days of
            # snapshots exist. "unknown" (unsupported chain / API issue) does
            # NOT reject - same rule as the honeypot check. Every run also
            # STORES the count, so the growth history builds itself.
            holders_result = get_holder_count(t["contract"], t["chain"])
            holder_num = holders_result if isinstance(holders_result, int) else None
            if isinstance(holders_result, int) and holders_result <= 0:
                stats["skipped_no_holders"] += 1
                continue
            if holders_result == "unknown":
                stats["holders_unknown"] += 1
            else:
                stats["holders_has"] += 1
            stats["passed_holders"] += 1

            # --- Filter 6: revenue EXISTENCE (DefiLlama) -------------------
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

            # --- Filter 7: security / honeypot check (GoPlus) --------------
            # Only a CONFIRMED honeypot / cannot-sell rejects the coin.
            # "unknown" (API down, unsupported chain, no data) does NOT reject,
            # because we don't want to lose good coins over a missing check.
            security = check_token_security(t["contract"], t["chain"])
            if security["verdict"] == "unsafe":
                stats["skipped_unsafe"] += 1
                continue
            if security["verdict"] == "unknown":
                stats["security_unknown"] += 1
            stats["passed_security"] += 1  # passed ALL gates -> save it

            # --- Find or create the coin (unique by contract) ---------------
            coin = session.query(Coin).filter_by(contract=t["contract"]).first()
            if coin is None:
                coin = Coin(
                    contract=t["contract"],
                    chain=t["chain"],
                    name=t.get("name") or t.get("symbol") or "unknown",
                    status="active",
                    ever_qualified=True,  # it passed all 4 filters to get here
                    # first_seen is filled in automatically (default = now).
                )
                session.add(coin)
                session.flush()  # flush so the new coin gets its id now
                stats["new_coins"] += 1
            else:
                # It qualified again -> mark it as a legitimate opportunity.
                coin.ever_qualified = True
                stats["existing_coins"] += 1

            # Store the security info (filter 7) on the coin so the dashboard
            # can show it. Applies to both new and existing coins.
            coin.security_verdict = security["verdict"]
            coin.verified = security["verified"]
            coin.renounced = security["renounced"]
            coin.mint_disabled = security["mint_disabled"]

            # Real category/narrative from CoinGecko. Only look it up once per
            # coin (cached); None -> the dashboard uses the generic sentence.
            if not coin.category:
                coin.category = get_category(t["contract"], t["chain"])

            # The DefiLlama daily revenue number (filter 6); None if UNKNOWN.
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
                snapshot.holders = holder_num
                stats["snapshots_updated"] += 1

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
    line = "=" * 50
    print(line)
    print("  SAVE SUMMARY")
    print(line)
    print(f"  Discovered tokens ................. {stats['discovered']:>5}")
    print("  " + "-" * 46)
    print("  FILTER 2 - age >= 10 days  (RUNS FIRST)")
    print(f"    Passed age ...................... {stats['passed_age']:>5}")
    print(f"      - skipped (too new, <10d) ..... {stats['skipped_too_new']:>5}")
    print(f"      - skipped (age unknown) ....... {stats['skipped_no_age']:>5}")
    print(f"      - via CG/CMC fallback ......... {stats['age_via_fallback']:>5}")
    print("  " + "-" * 46)
    print("  FILTER 1 - market cap $1M-$500M")
    print(f"    ALSO passed market cap .......... {stats['passed_mcap']:>5}")
    print(f"      - skipped (below $1M) ......... {stats['skipped_below']:>5}")
    print(f"      - skipped (above $500M) ....... {stats['skipped_above']:>5}")
    print(f"      - skipped (no market cap) ..... {stats['skipped_no_mcap']:>5}")
    print("  " + "-" * 46)
    print("  FILTER 3 - 24h volume > $50K (temporary)")
    print(f"    ALSO passed volume ............. {stats['passed_volume']:>5}")
    print(f"      - skipped (volume <= $50K) .... {stats['skipped_low_volume']:>5}")
    print("  " + "-" * 46)
    print("  FILTER 4 - liquidity > $30K")
    print(f"    ALSO passed liquidity .......... {stats['passed_liquidity']:>5}")
    print(f"      - skipped (liq <= $30K) ....... {stats['skipped_low_liquidity']:>5}")
    print("  " + "-" * 46)
    print("  FILTER 4b - holders > 0 (temporary)")
    print(f"    ALSO passed holders ........... {stats['passed_holders']:>5}")
    print(f"      - skipped (0 holders) ........ {stats['skipped_no_holders']:>5}")
    print(f"      - has count .................. {stats['holders_has']:>5}")
    print(f"      - unknown (chain n/a) ........ {stats['holders_unknown']:>5}")
    print("  " + "-" * 46)
    print("  FILTER 6 - revenue exists (DefiLlama)")
    print(f"    ALSO passed revenue ........... {stats['passed_revenue']:>5}")
    print(f"      - skipped (NO_REVENUE) ....... {stats['skipped_no_revenue']:>5}")
    print(f"      - has revenue ................ {stats['revenue_has']:>5}")
    print(f"      - unknown (not listed) ....... {stats['revenue_unknown']:>5}")
    print("  " + "-" * 46)
    print("  FILTER 7 - security (GoPlus honeypot)")
    print(f"    ALSO passed security (ALL) ..... {stats['passed_security']:>5}")
    print(f"      - skipped (honeypot/unsafe) .. {stats['skipped_unsafe']:>5}")
    print(f"      - passed but unknown ......... {stats['security_unknown']:>5}")
    print("  " + "-" * 46)
    print(f"  Saved as NEW coins ................ {stats['new_coins']:>5}")
    print(f"  Already existed ................... {stats['existing_coins']:>5}")
    print(f"  Snapshots inserted (today) ....... {stats['snapshots_inserted']:>5}")
    print(f"  Snapshots updated (today) ........ {stats['snapshots_updated']:>5}")
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
