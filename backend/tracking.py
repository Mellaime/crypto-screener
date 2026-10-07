"""
tracking.py
-----------
DAILY TRACKING of every coin in the ACTIVE and DROPPED lists. Runs each day
after the filter and BEFORE drop_logic (which reads what this records).

The filter is a one-time entry gate (it never re-analyzes a coin), so this is
where tracked coins get their fresh daily numbers - with NO CoinGecko calls:

  1. Codex - trend history for drop_logic. ONE bulk filterTokens request
     covers 200 coins' CURRENT liquidity + 24h volume, so ~3 requests/day even
     for 500+ coins. Stored per day on the coin's snapshot (codex_liquidity,
     codex_volume24); drop_logic computes the volume and liquidity TRENDS from
     this stored history.
  2. DexScreener - what the dashboard shows: price, 24h volume, liquidity
     (main pool; the fast batched fetch).
  3. Market cap - the coin's GLOBAL market cap from CoinGecko / CMC, taken
     from this run's discovery (discovery.LAST_MCAPS, no extra calls). Not
     from DexScreener: one pool's market cap is tiny for a bridged copy. A
     coin discovery didn't see today (e.g. fell below $5M) gets yesterday's
     market cap moved by today's price change.
  4. Category catch-up - tracked coins that have no CoinGecko category yet
     (the save step only looks up CATEGORY_BUDGET new coins per run), or
     still show a launch / backer label, get one lookup each, at most
     CATCHUP_BUDGET (10) per run.

Every tracked coin gets a snapshot for TODAY (created if missing). Anything a
source didn't return today is carried forward from the coin's previous
snapshot, so the dashboard and scores never go blank - and the daily holders
update then writes each day's holder count into that day's own snapshot.

Run it standalone:
    python tracking.py
"""

import os
import time
from datetime import date

import requests
from dotenv import load_dotenv

from database import SessionLocal, init_db, utcnow
from models import Coin, DailySnapshot

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
CODEX_KEY = os.getenv("CODEX_API_KEY")
CODEX_URL = "https://graph.codex.io/graphql"

CODEX_BATCH = 200             # coins per filterTokens request
RETRY_WAITS = [5, 15, 30]

BULK_QUERY = """
query ($tokens: [String]) {
  filterTokens(tokens: $tokens, limit: %d) {
    results { liquidity volume24 token { address networkId } }
  }
}""" % CODEX_BATCH


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def codex_bulk_readings(coins, stats):
    """
    {contract_lower: (liquidity, volume24)} for the given coins, via bulk
    filterTokens requests (200 coins each). Coins on chains Codex doesn't
    cover get no reading. Never raises.
    """
    from liquidity_check import CHAIN_TO_NETWORK

    ids = []
    for c in coins:
        net = CHAIN_TO_NETWORK.get((c.chain or "").lower())
        if net and c.contract:
            ids.append(f"{c.contract}:{net}")
    stats["codex_ids"] = len(ids)
    if not CODEX_KEY:
        return {}

    out = {}
    for i in range(0, len(ids), CODEX_BATCH):
        batch = ids[i:i + CODEX_BATCH]
        data = None
        for wait in [0] + RETRY_WAITS:
            if wait:
                time.sleep(wait)
            stats["codex_requests"] += 1
            try:
                body = requests.post(CODEX_URL, json={"query": BULK_QUERY,
                                                      "variables": {"tokens": batch}},
                                     headers={"Authorization": CODEX_KEY},
                                     timeout=60).json()
            except (requests.exceptions.RequestException, ValueError):
                continue
            if body.get("errors"):
                print(f"[WARN] Codex filterTokens: {body['errors'][0].get('message', '')[:100]}")
                continue
            data = ((body.get("data") or {}).get("filterTokens") or {}).get("results") or []
            break
        if data is None:
            stats["codex_failed_batches"] += 1
            print(f"[WARN] Codex bulk batch of {len(batch)} failed after retries "
                  f"- no reading for those coins today")
            continue
        for r in data:
            addr = ((r.get("token") or {}).get("address") or "").lower()
            if addr:
                out[addr] = (_to_float(r.get("liquidity")), _to_float(r.get("volume24")))
    return out


def _catch_up_categories(coins):
    """Look up a CoinGecko category for tracked coins that still have none
    (or a launch / backer label). At most CATCHUP_BUDGET live calls."""
    from narrative_check import lookup_category, is_skipped_label, CATCHUP_BUDGET

    calls = 0
    for coin in coins:
        if calls >= CATCHUP_BUDGET:
            break
        if coin.category == "" or (coin.category and not is_skipped_label(coin.category)):
            continue                     # final answer already
        cat, definitive, called = lookup_category(coin.contract, coin.chain)
        calls += int(called)
        if cat:
            coin.category = cat
        elif definitive:
            coin.category = ""           # CoinGecko has no usable category
    return calls


def run_daily_tracking():
    """Record today's Codex readings + dashboard numbers for active + dropped."""
    from fetch_dexscreener import fetch_pairs_for_candidates, build_token_records
    from discovery import LAST_MCAPS, LAST_SUPPLY

    now = utcnow()
    today = date.today()
    stats = {"coins": 0, "codex_ids": 0, "codex_requests": 0, "codex_failed_batches": 0,
             "codex_readings": 0, "dex_data": 0, "snapshots_created": 0, "price_updated": 0}

    session = SessionLocal()
    try:
        coins = (session.query(Coin)
                 .filter(Coin.status.in_(["active", "dropped"])).all())
        stats["coins"] = len(coins)
        stats["mcap_discovery"] = stats["mcap_scaled"] = stats["supply"] = 0
        if not coins:
            print("No active or dropped coins - nothing to track.")
            return stats

        # 1. Codex: today's liquidity + 24h volume (trend history).
        codex = codex_bulk_readings(coins, stats)

        # 2. DexScreener: today's dashboard numbers (price, mcap, vol, liq).
        cands = [{"contract": c.contract, "chain": c.chain} for c in coins]
        records = build_token_records(fetch_pairs_for_candidates(cands),
                                      keep_addresses={c.contract.lower() for c in coins})
        dex = {r["contract"].lower(): r for r in records}

        for coin in coins:
            key = coin.contract.lower()
            snap = (session.query(DailySnapshot)
                    .filter_by(coin_id=coin.id, date=today).first())
            if snap is None:
                prev = (session.query(DailySnapshot)
                        .filter(DailySnapshot.coin_id == coin.id,
                                DailySnapshot.date < today)
                        .order_by(DailySnapshot.date.desc()).first())
                snap = DailySnapshot(
                    coin_id=coin.id, date=today,
                    # carried forward; replaced below by today's data if any
                    volume=prev.volume if prev else None,
                    liquidity=prev.liquidity if prev else None,
                    mcap=prev.mcap if prev else None,
                    revenue=prev.revenue if prev else None,
                    price_usd=prev.price_usd if prev else None,
                    holders=prev.holders if prev else None,
                )
                session.add(snap)
                stats["snapshots_created"] += 1

            reading = codex.get(key)
            if reading and (reading[0] is not None or reading[1] is not None):
                snap.codex_liquidity, snap.codex_volume24 = reading
                stats["codex_readings"] += 1

            rec = dex.get(key)
            if rec is not None:
                stats["dex_data"] += 1
                price = _to_float(rec.get("price_usd"))
                snap.volume = _to_float(rec.get("volume_24h"))
                snap.liquidity = _to_float(rec.get("liquidity_usd"))
                if price is not None:
                    snap.price_usd = price
                    coin.price_usd = price
                    coin.price_updated_at = now
                    stats["price_updated"] += 1

            # Market cap: CoinGecko / CMC (global), never a DexScreener pool.
            prev = (session.query(DailySnapshot)
                    .filter(DailySnapshot.coin_id == coin.id, DailySnapshot.date < today)
                    .order_by(DailySnapshot.date.desc()).first())
            if LAST_MCAPS.get(key):
                snap.mcap = LAST_MCAPS[key]
                stats["mcap_discovery"] += 1
            elif prev and prev.mcap and prev.price_usd and snap.price_usd:
                snap.mcap = prev.mcap * snap.price_usd / prev.price_usd
                stats["mcap_scaled"] += 1

            # Supply (display only): from this run's discovery, when it saw it.
            sup = LAST_SUPPLY.get(key)
            if sup and any(sup.get(k) is not None for k in ("total", "circulating", "max")):
                coin.total_supply = sup.get("total")
                coin.circulating_supply = sup.get("circulating")
                coin.max_supply = sup.get("max")
                coin.supply_uncapped = sup.get("uncapped")
                coin.supply_updated_at = now
                stats["supply"] += 1

        stats["category_lookups"] = _catch_up_categories(coins)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    line = "=" * 52
    print(line)
    print("  DAILY TRACKING (active + dropped)")
    print(line)
    print(f"  coins tracked ................... {stats['coins']:>5}")
    print(f"  Codex readings (liq + vol24) .... {stats['codex_readings']:>5}"
          f"   ({stats['codex_requests']} bulk Codex requests for "
          f"{stats['codex_ids']} coins on Codex chains)")
    print(f"  DexScreener data (price etc.) ... {stats['dex_data']:>5}")
    print(f"  price refreshed ................. {stats['price_updated']:>5}")
    print(f"  market cap from CoinGecko/CMC ... {stats.get('mcap_discovery', 0):>5}"
          f"   (moved with price: {stats.get('mcap_scaled', 0)})")
    print(f"  today's snapshots created ....... {stats['snapshots_created']:>5}")
    print(f"  category catch-up (CG calls) .... {stats.get('category_lookups', 0):>5}")
    print(f"  supply refreshed (CG / CMC) ..... {stats.get('supply', 0):>5}")
    if stats["codex_failed_batches"]:
        print(f"  Codex batches FAILED ............ {stats['codex_failed_batches']:>5}")
    print(line)
    return stats


if __name__ == "__main__":
    init_db()
    run_daily_tracking()
