"""
save_to_db.py
-------------
Phase 2 - Step (b): run the filter chain on discovered tokens and save the
survivors into the database.

THE FILTER - exactly these 7 steps, in this order:
    1. Discovery  - CoinGecko + CoinMarketCap, MCap $1M-$400M (discovery.py).
                    MCap is enforced HERE only - there is no later mcap step.
    2. Meme +     - memes AND stablecoins removed right after discovery
       Stablecoin   (meme_filter.py).
    3. Age        - >= 10 days; DexScreener pairCreatedAt, else CMC listing
                    date; too new or unknown -> DROP (age_check.py).
    -- ROTATION QUEUE (rotation.py): steps 4-7 run only on ROTATION_BUDGET
       age-passers per run, first-come-first-served; the rest wait. --
    4. Revenue    - DefiLlama; DROP only on confirmed NO_REVENUE.
    5. Volume     - CoinGecko 5 complete days, day5 >= day1 x 1.35 (+35%); less or
                    unknown -> DROP (volume_check.py).
    6. Honeypot   - GoPlus; DROP only on confirmed unsafe. After volume
                    because free GoPlus is rate-limited (~20-30 calls/min).
    7. Liquidity  - Codex daily liquidity over min(30d, age); down > 5% ->
                    DROP; unknown -> PASS (liquidity_check.py). LAST step.
                    Then the 3-source minimum (DexScreener + Codex + a live
                    Birdeye lookup, liquidity_verify.evaluate): only a
                    "healthy" verdict enters (a failed request -> passes).
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

import re
from datetime import date

from database import SessionLocal, init_db, utcnow
from models import Coin, DailySnapshot
from discovery import discover_tokens   # steps 1-2: discovery + meme/stablecoin
                                        # (DexScreener only enriches by contract)
from age_check import decide_age                    # step 3
from queue_view import remember_queue_display
from revenue_check import check_revenue             # step 4
from volume_check import check_volume_rising       # step 5
from rotation import (pick_batch, mark_checked, analyzed_keys, is_analyzed,
                      ROTATION_BUDGET)              # entry queue for steps 4-7
from security_check import check_token_security, rejects, store as store_security     # step 6 (honeypot)
from liquidity_check import check_liquidity_trend   # step 7 (LAST)
import birdeye                                                             # step 7's 3rd source
from liquidity_verify import evaluate, describe, HEALTHY, UNKNOWN
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


# Re-home threshold: a tracked coin moves to another chain only when that
# chain's liquidity is at least REHOME_MIN_RATIO x AND REHOME_MIN_GAIN higher
# - so a coin doesn't flip between two similar chains day to day.
REHOME_MIN_RATIO = 2.0
REHOME_MIN_GAIN = 10_000


def rehome_coin(session, coin, tok, now=None):
    """Move a tracked coin to another chain's copy of itself (its real home).
    The old chain's numbers describe a different pool set, so its chain-
    specific history is cleared (Codex readings, DexScreener volume /
    liquidity, holder counts) and the coin gets a fresh grace period while
    the new chain's history builds up. Its entry-queue row is added so the
    new copy is never re-analyzed. Returns a one-line description."""
    from models import CoinQueue
    from holders_check import get_holder_count

    now = now or utcnow()
    old = f"{coin.chain} {coin.contract}"
    coin.chain, coin.contract = tok["chain"], tok["contract"]
    # Keep the coin's name (the new chain's listing may carry an old brand,
    # e.g. SuperVerse's Ethereum token is still "SuperFarm"); only drop a
    # bridged-copy suffix like " (PoS)".
    coin.name = re.sub(r"\s*\((PoS|Bridged|Wormhole)\)\s*$", "", coin.name or "", flags=re.I) or coin.name
    for snap in session.query(DailySnapshot).filter_by(coin_id=coin.id):
        snap.codex_liquidity = snap.codex_volume24 = None
        snap.volume = snap.liquidity = snap.holders = None
    h = get_holder_count(coin.contract, coin.chain)
    coin.holders_at_entry = h if isinstance(h, int) else None
    coin.active_since = now                       # fresh grace on the new chain
    key = (coin.chain.lower(), coin.contract.lower())
    row = session.query(CoinQueue).filter_by(chain=key[0], contract=key[1]).first()
    if row is None:
        row = CoinQueue(chain=key[0], contract=key[1], first_seen=now)
        session.add(row)
    row.last_checked, row.outcome, row.attempts = now, "saved", max(row.attempts or 0, 1)
    return f"{old} -> {coin.chain} {coin.contract}"


def dedupe_by_coin(session, tokens, rehomed=None):
    """
    One token per COIN: the same coin discovered on several chains (bridged
    copies) shares a coin_key. The coin is tracked on its HIGHEST-liquidity
    chain (DexScreener liquidity of its pools there) - its real home:
      * not tracked yet -> the most liquid copy (ties: one already analyzed,
        then discovery order)
      * already tracked -> stays on its chain, unless another copy has at
        least REHOME_MIN_RATIO x and REHOME_MIN_GAIN more liquidity: then the
        coin is RE-HOMED there (rehome_coin) - never saved twice.
    `rehomed` (a list) collects (name, from, from_liq, to, to_liq) for reports.
    Returns (kept_tokens, dropped_count).
    """
    from models import CoinQueue

    coins_by_addr = {(c.contract or "").lower(): c for c in session.query(Coin)}
    analyzed = {r.contract for r in session.query(CoinQueue.contract)
                .filter(CoinQueue.last_checked.isnot(None))}

    def liq(t):
        return to_float(t.get("liquidity_usd")) or 0

    groups = {}
    for i, t in enumerate(tokens):
        key = t.get("coin_key") or ("addr:" + (t["contract"] or "").lower())
        groups.setdefault(key, []).append((i, t))
    kept = []
    for members in groups.values():
        best = max(members, key=lambda m: (liq(m[1]), (m[1]["contract"] or "").lower() in analyzed, -m[0]))
        tracked = [m for m in members if (m[1]["contract"] or "").lower() in coins_by_addr]
        if tracked:
            cur = max(tracked, key=lambda m: liq(m[1]))
            coin = coins_by_addr[(cur[1]["contract"] or "").lower()]
            new_liq, old_liq = liq(best[1]), liq(cur[1])
            if (best is not cur and (best[1]["contract"] or "").lower() not in coins_by_addr
                    and new_liq >= REHOME_MIN_RATIO * old_liq and new_liq - old_liq >= REHOME_MIN_GAIN):
                old_chain = coin.chain
                rehome_coin(session, coin, best[1])
                if rehomed is not None:
                    rehomed.append((coin.name, old_chain, old_liq, coin.chain, new_liq))
                kept.append(best)
            else:
                kept.append(cur)
        else:
            kept.append(best)
    kept.sort(key=lambda m: m[0])
    return [t for _, t in kept], len(tokens) - len(kept)


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
        "age_deferred": 0,       # fallback lookup budget used up -> next run
        "dup_chains_dropped": 0, # same coin on another chain -> one kept
        "rehomed": 0,            # tracked coins moved to a much stronger chain
        "queue_rechecks": 0,     # 15-day second chances picked this run
        "passed_volume": 0,          # volume rising (CoinGecko 5-day)
        "skipped_vol_falling": 0,
        "skipped_vol_unknown": 0,    # no CoinGecko history / chain not on CG
        "already_analyzed": 0,       # analyzed on an earlier run -> skipped
        "queue_new": 0,              # age-passers seen for the first time
        "queue_picked": 0,           # got the expensive steps this run
        "queue_waiting": 0,          # waiting for a later run
        "vol_retry_next_run": 0,     # CoinGecko failed transiently -> retried
        "passed_liquidity": 0,       # Codex liquidity trend (step 7)
        "skipped_liq_falling": 0,    # down more than 5% over the window
        "liquidity_unknown": 0,      # no Codex data -> passed, not dropped
        "skipped_thin": 0,           # failed the 3-source liquidity minimum
        "passed_revenue": 0,
        "skipped_no_revenue": 0,
        "revenue_has": 0,
        "revenue_unknown": 0,
        "passed_security": 0,
        "skipped_unsafe": 0,
        "skipped_unverified": 0,
        "security_unknown": 0,
        "new_coins": 0,
        "existing_coins": 0,
        "snapshots_inserted": 0,
        "snapshots_updated": 0,
        "category_lookups": 0,       # live CoinGecko category calls this run
    }

    session = SessionLocal()
    try:
        # --- One token per coin (bridged copies on other chains dropped) ----
        rehomed = []
        tokens, stats["dup_chains_dropped"] = dedupe_by_coin(session, tokens, rehomed)
        stats["rehomed"] = len(rehomed)
        for name, a, al, b, bl in rehomed:
            print(f"[INFO] re-homed {name}: {a} (${al:,.0f}) -> {b} (${bl:,.0f})")

        # --- Step 3: AGE (two-step, by contract + chain) --------------------
        # Run over every token first, so we know exactly who reaches step 4.
        # DexScreener pairCreatedAt, else CoinMarketCap listing date
        # (see age_check.decide_age). Younger than 10 days, OR of unknowable
        # age, is dropped here.
        # The filter is a ONE-TIME entry gate: coins already analyzed on an
        # earlier run are skipped right here - never re-analyzed, and no age
        # fallback calls spent on them. (Coins in active/dropped are tracked
        # daily by Codex instead: tracking.py + drop_logic.py.)
        analyzed = analyzed_keys(session)
        fresh_tokens = [t for t in tokens if not is_analyzed(t, analyzed)]
        stats["already_analyzed"] = len(tokens) - len(fresh_tokens)

        age_passed = []
        for t in fresh_tokens:
            keep_age, age_days, age_source = decide_age(t, session)
            if age_source == "fallback":
                stats["age_via_fallback"] += 1
            if age_source == "deferred":
                stats["age_deferred"] += 1           # fallback lookup next run
                continue
            if not keep_age:
                if age_days is None:
                    stats["skipped_no_age"] += 1     # unknown on DS + CG + CMC
                else:
                    stats["skipped_too_new"] += 1    # younger than 10 days
                continue
            stats["passed_age"] += 1
            age_passed.append(t)

        # --- ENTRY QUEUE (rotation.py) --------------------------------------
        # The expensive steps 4-7 run only on ROTATION_BUDGET never-analyzed
        # age-passers per run, in arrival order. The rest wait for a later run
        # - skipped this run, neither saved nor dropped.
        picked, queue = pick_batch(session, age_passed)
        remember_queue_display(session, age_passed)   # dashboard only, no calls
        stats["queue_new"] = queue["new"]
        stats["queue_picked"] = len(picked)
        stats["queue_rechecks"] = queue["rechecks"]
        stats["queue_waiting"] = queue["queued"] - len(picked)
        retry_next_run = []   # CoinGecko failed transiently -> not analyzed yet
        saved = []            # passed all 7 steps (queue outcome "saved")

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
            # This is the coin's ONE CoinGecko check. If it failed only because
            # of a network error / rate limit, don't count the coin as analyzed
            # - it gets another try next run instead of being shut out forever.
            vol = check_volume_rising(t["contract"], t["chain"])
            if vol.get("transient"):
                retry_next_run.append(t)
                stats["vol_retry_next_run"] += 1
                continue
            if vol["verdict"] != "rising":
                if vol["verdict"] == "falling":
                    stats["skipped_vol_falling"] += 1
                else:
                    stats["skipped_vol_unknown"] += 1
                continue
            stats["passed_volume"] += 1

            # --- Step 6: honeypot / security check (GoPlus / RugCheck) -----
            # Placed AFTER volume: free GoPlus is rate-limited, so it runs on
            # the small volume-passing set. Rejects an unsafe coin AND an
            # unverified one ("unknown") on a chain GoPlus / RugCheck cover;
            # "unknown" passes only on an unsupported chain (sui, hyperevm...).
            security = check_token_security(t["contract"], t["chain"])
            if security["verdict"] == "unsafe":
                stats["skipped_unsafe"] += 1
                continue
            if rejects(security):
                stats["skipped_unverified"] += 1
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
            # ...and the 3-source minimum (DexScreener + Codex + Birdeye, the
            # same rule drop_logic applies daily): 2+ sources with a reading
            # and 2+ at $5K or more. One live Birdeye lookup (3 CU) per coin
            # reaching this point (a handful a day).
            be = birdeye.lookup(t["chain"], t["contract"])
            ev = evaluate({"dexscreener": to_float(t.get("liquidity_usd")), "codex": liq.get("today_liq"),
                           "birdeye": be["liquidity"]}, be["updated_at"],
                          {"birdeye"} if be["status"] == "error" else ())
            if ev["verdict"] not in (HEALTHY, UNKNOWN):
                stats["skipped_thin"] += 1
                print(f"[INFO] {t.get('name') or t['contract']}: not saved - {ev['reason']} ({describe(ev)})")
                continue
            t["_birdeye"] = be
            stats["passed_liquidity"] += 1  # passed ALL 7 steps -> save it
            saved.append(t)

            # --- Values STORED on the snapshot (display / scoring only) ----
            # None of these keep or drop a coin any more.
            volume_24h = to_float(t.get("volume_24h")) or 0   # DexScreener 24h
            liquidity = to_float(t.get("liquidity_usd")) or 0  # DexScreener now
            # The coin's real market cap from CoinGecko / CMC (discovery) - NOT
            # the DexScreener pool's, which is tiny for bridged copies.
            mcap = to_float(t.get("discovery_mcap")) or to_float(t.get("market_cap"))
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
            store_security(coin, security)

            # Current price for the dashboard. tracking.py refreshes it
            # again later in every run, for active + dropped coins.
            if price is not None:
                coin.price_usd = price
                coin.price_updated_at = utcnow()

            # Birdeye reading from step 7 (the daily verification reuses it).
            be = t.get("_birdeye") or {}
            if be.get("status") in ("ok", "no_data", "unsupported"):
                coin.birdeye_liquidity = be.get("liquidity")
                coin.birdeye_updated_at = be.get("updated_at")
                coin.birdeye_checked_at = utcnow()

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

        # Every coin that had its turn this run is marked analyzed, with its
        # outcome: "saved", or "rejected" (eligible for ONE second chance in
        # 15 days - rotation.py). Same transaction as the saves: if the run
        # fails, nothing is marked and those coins keep their place.
        skip = {id(t) for t in retry_next_run}
        saved_ids = {id(t) for t in saved}
        mark_checked(session, saved, "saved")
        mark_checked(session, [t for t in picked
                               if id(t) not in skip and id(t) not in saved_ids], "rejected")

        # This run's funnel, for the Coming Up page's "Last run" summary.
        from models import RunLog
        from discovery import LAST_COUNTS
        session.add(RunLog(
            run_at=utcnow(), discovered=LAST_COUNTS.get("discovery"),
            passed_age=stats["passed_age"], analyzed=len(picked) - len(retry_next_run),
            saved=len(saved), rejected=len(picked) - len(retry_next_run) - len(saved),
            retry=len(retry_next_run), waiting=stats["queue_waiting"]))

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
    print(f"  1. Discovery (CG+CMC, $1M-$400M) - unique .. {dc.get('discovery', '?'):>6}")
    print(f"  2. Meme/stable/tokenized/wrapped - passed .. {dc.get('after_excl', '?'):>6}")
    print(f"       - dropped as meme ..................... {dc.get('meme_dropped', '?'):>6}")
    print(f"       - dropped as stablecoin ............... {dc.get('stable_dropped', '?'):>6}")
    print(f"       - dropped as tokenized stock/fund ..... {dc.get('tokenized_dropped', '?'):>6}")
    print(f"       - dropped as wrapped / staked ......... {dc.get('wrapped_dropped', '?'):>6}")
    print(f"       (not a filter: with DexScreener data -> {dc.get('enriched', '?')}, "
          f"without -> {dc.get('no_dexscreener', '?')}; both go on to age)")
    print(f"       (same coin on another chain -> one kept, dropped: "
          f"{stats['dup_chains_dropped']}; tracked coins re-homed: {stats['rehomed']})")
    print(f"       (already analyzed on an earlier run -> skipped: "
          f"{stats['already_analyzed']})")
    print(sub)
    print(f"  3. Age >= 10 days - passed ................. {stats['passed_age']:>6}")
    print(f"       - dropped (too new, <10d) ............. {stats['skipped_too_new']:>6}")
    print(f"       - dropped (age unknown) ............... {stats['skipped_no_age']:>6}")
    print(f"       - age lookup postponed to next run .... {stats['age_deferred']:>6}")
    print(sub)
    print(f"  ENTRY QUEUE (never-analyzed coins, budget {ROTATION_BUDGET}/run):")
    print(f"       first seen this run ................... {stats['queue_new']:>6}")
    print(f"       picked this run (arrival order) ....... {stats['queue_picked']:>6}")
    print(f"         of which 15-day second chances ...... {stats['queue_rechecks']:>6}")
    print(f"       waiting for a later run ............... {stats['queue_waiting']:>6}")
    print(sub)
    print(f"  4. Revenue (DefiLlama) - passed ............ {stats['passed_revenue']:>6}")
    print(f"       - dropped (NO_REVENUE) ................ {stats['skipped_no_revenue']:>6}")
    print(f"       - passed as unknown ................... {stats['revenue_unknown']:>6}")
    import volume_check
    print(f"  5. Volume +35% (CoinGecko) - passed ........ {stats['passed_volume']:>6}"
          f"   ({volume_check.CALLS} CoinGecko calls)")
    print(f"       - dropped (falling) ................... {stats['skipped_vol_falling']:>6}")
    print(f"       - dropped (no CG history) ............. {stats['skipped_vol_unknown']:>6}")
    print(f"       - CoinGecko failed, retry next run .... {stats['vol_retry_next_run']:>6}")
    print(f"  6. Honeypot (GoPlus) - passed .............. {stats['passed_security']:>6}")
    print(f"       - dropped (honeypot/unsafe) ........... {stats['skipped_unsafe']:>6}")
    print(f"       - dropped (unverified, supported chain) {stats['skipped_unverified']:>6}")
    print(f"       - passed as unknown (unsupported chain) {stats['security_unknown']:>6}")
    print(f"  7. Liquidity trend (Codex) - passed ........ {stats['passed_liquidity']:>6}")
    print(f"       - dropped (down > 5%) ................. {stats['skipped_liq_falling']:>6}")
    print(f"       - passed as unknown ................... {stats['liquidity_unknown']:>6}")
    print(f"       - dropped (3-source liquidity minimum) . {stats['skipped_thin']:>6}")
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
