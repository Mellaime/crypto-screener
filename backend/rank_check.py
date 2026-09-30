"""
rank_check.py
-------------
RANK CHANGE - tracks each coin's market rank from BOTH CoinGecko and
CoinMarketCap, stores it weekly, and measures how it moved vs last week.

Idea:
  * Lower rank number = better (rank #1 is the biggest coin).
  * "Change" = last week's rank - this week's rank. Positive = moved UP.
      e.g. #280 -> #150  ==  +130 places up.
  * We combine the two sources into a signal:
      both up  -> "strong_up"
      both down-> "down"
      disagree -> "neutral" (weak)
      one source only -> up/down/neutral by that source.

History note:
  Rank change needs a PREVIOUS week to compare against. On the very first
  run we just store this week's ranks (no comparison). Comparison kicks in
  once a prior week exists.

Keys come from backend/.env (CoinGecko + CoinMarketCap). Rate limits are
respected with a delay before each live call + an in-run memo cache.

Run it (tests the two sources + the change logic):
    python rank_check.py
"""

import os
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

from narrative_check import CHAIN_TO_PLATFORM  # reuse the chain->platform map

load_dotenv(Path(__file__).resolve().parent / ".env")
CG_KEY = os.getenv("COINGECKO_API_KEY")
CMC_KEY = os.getenv("COINMARKETCAP_API_KEY")

CG_BASE = "https://api.coingecko.com/api/v3"
CMC_BASE = "https://pro-api.coinmarketcap.com"

REQUEST_DELAY = 2.5  # seconds before each LIVE call (free tiers are limited)

# CoinGecko calls budget. A coin's CoinGecko rank comes, in order, from:
#   1. this run's discovery pages (discovery.LAST_CG_RANKS) - free
#   2. the rank already stored for it THIS ISO week      - free (rank change
#      is week-over-week, so one reading per week is enough)
#   3. an individual lookup - 1 CoinGecko call, at most this many per run
RANK_LOOKUP_BUDGET = 10

# Per-process memo so we never fetch the same coin twice in one run.
_memo = {}


def _iso_week(d=None):
    d = d or datetime.utcnow().date()
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


# ---------------------------------------------------------------------------
# Source 1: CoinGecko market-cap rank (by contract)
# ---------------------------------------------------------------------------
def get_coingecko_rank(contract, chain):
    platform = CHAIN_TO_PLATFORM.get((chain or "").lower())
    if not platform or not contract:
        return None
    memo_key = ("cg", platform, contract.lower())
    if memo_key in _memo:
        return _memo[memo_key]
    if not CG_KEY:
        return None

    time.sleep(REQUEST_DELAY)
    try:
        r = requests.get(
            f"{CG_BASE}/coins/{platform}/contract/{contract}",
            headers={"x-cg-demo-api-key": CG_KEY, "User-Agent": "crypto-screener/0.1"},
            params={"localization": "false", "tickers": "false",
                    "community_data": "false", "developer_data": "false", "sparkline": "false"},
            timeout=20,
        )
    except requests.exceptions.RequestException as e:
        print(f"[WARN] CoinGecko rank request failed: {e}")
        return None

    if r.status_code == 404:
        _memo[memo_key] = None
        return None
    if r.status_code != 200:
        print(f"[WARN] CoinGecko rank status {r.status_code}")
        return None
    try:
        rank = r.json().get("market_cap_rank")
    except ValueError:
        return None
    _memo[memo_key] = rank
    return rank


# ---------------------------------------------------------------------------
# Source 2: CoinMarketCap rank (via the map: contract address -> rank)
# ---------------------------------------------------------------------------
# CMC's free tier can't look up a token by contract address directly, but its
# /cryptocurrency/map lists EVERY coin with its contract address AND rank. We
# fetch that ONCE (one credit), index it by address, then look up instantly.
_CMC_MAP = None  # {"exact": {addr: rank}, "lower": {addr_lower: rank}}


def _load_cmc_map():
    global _CMC_MAP
    if _CMC_MAP is not None:
        return _CMC_MAP
    _CMC_MAP = {"exact": {}, "lower": {}}
    if not CMC_KEY:
        return _CMC_MAP

    time.sleep(REQUEST_DELAY)
    try:
        r = requests.get(
            f"{CMC_BASE}/v1/cryptocurrency/map",
            headers={"X-CMC_PRO_API_KEY": CMC_KEY, "Accept": "application/json"},
            params={"listing_status": "active", "aux": "platform"},
            timeout=60,
        )
    except requests.exceptions.RequestException as e:
        print(f"[WARN] CMC map request failed: {e}")
        return _CMC_MAP
    if r.status_code != 200:
        print(f"[WARN] CMC map status {r.status_code}")
        return _CMC_MAP
    try:
        data = r.json().get("data") or []
    except ValueError:
        return _CMC_MAP

    for e in data:
        addr = (e.get("platform") or {}).get("token_address")
        rank = e.get("rank")
        if addr and rank is not None:
            # EVM addresses are stored lowercased; Solana (base58) keep case.
            _CMC_MAP["exact"][addr] = rank
            _CMC_MAP["lower"][addr.lower()] = rank
    return _CMC_MAP


def get_cmc_rank(contract, chain=None):
    """CoinMarketCap rank for a token by its contract address, or None."""
    if not contract:
        return None
    m = _load_cmc_map()
    # Exact match first (correct for case-sensitive Solana addresses), then
    # lowercased (correct for checksummed EVM addresses).
    if contract in m["exact"]:
        return m["exact"][contract]
    return m["lower"].get(contract.lower())


# ---------------------------------------------------------------------------
# Change + signal logic (pure functions - easy to test)
# ---------------------------------------------------------------------------
def compute_change(prev_rank, cur_rank):
    """Places moved since last week. Positive = moved UP (improved)."""
    if prev_rank is None or cur_rank is None:
        return None
    return prev_rank - cur_rank


def combine_signal(cg_change, cmc_change):
    """Return (avg_change, signal) from the two sources' changes."""
    changes = [c for c in (cg_change, cmc_change) if c is not None]
    if not changes:
        return None, None

    avg = round(sum(changes) / len(changes))

    if cg_change is not None and cmc_change is not None:
        if cg_change > 0 and cmc_change > 0:
            signal = "strong_up"       # both sources agree: up
        elif cg_change < 0 and cmc_change < 0:
            signal = "down"            # both agree: down
        else:
            signal = "neutral"         # disagree or flat -> weak
    else:
        c = changes[0]                 # only one source available
        signal = "up" if c > 0 else ("down" if c < 0 else "neutral")
    return avg, signal


# ---------------------------------------------------------------------------
# The daily pipeline step: fetch ranks, store weekly, compute change.
# ---------------------------------------------------------------------------
def _upsert_and_change(session, coin_id, source, week, rank):
    """Store this week's rank (upsert) and return the change vs last week."""
    from models import RankHistory

    prev = (
        session.query(RankHistory)
        .filter(RankHistory.coin_id == coin_id,
                RankHistory.source == source,
                RankHistory.week < week)
        .order_by(RankHistory.week.desc())
        .first()
    )
    prev_rank = prev.rank if prev else None

    if rank is not None:
        row = (session.query(RankHistory)
               .filter_by(coin_id=coin_id, source=source, week=week).first())
        if row is None:
            session.add(RankHistory(coin_id=coin_id, source=source, week=week, rank=rank))
        else:
            row.rank = rank

    return compute_change(prev_rank, rank)


def _cg_rank_this_run(session, coin, week, stats):
    """CoinGecko rank with as few CoinGecko calls as possible (see
    RANK_LOOKUP_BUDGET). Returns the rank or None."""
    from discovery import LAST_CG_RANKS
    from models import RankHistory

    rank = LAST_CG_RANKS.get(coin.contract.lower())
    if rank is not None:
        stats["cg_from_discovery"] += 1
        return rank
    stored = (session.query(RankHistory)
              .filter_by(coin_id=coin.id, source="coingecko", week=week).first())
    if stored is not None and stored.rank is not None:
        stats["cg_from_this_week"] += 1
        return stored.rank
    if stats["cg_looked_up"] < RANK_LOOKUP_BUDGET:
        stats["cg_looked_up"] += 1
        return get_coingecko_rank(coin.contract, coin.chain)
    stats["cg_over_budget"] += 1
    return None


def run_rank_update():
    """Fetch + store ranks for every ACTIVE coin and compute the change."""
    from database import SessionLocal
    from models import Coin

    week = _iso_week()
    stats = {"coins": 0, "cg_found": 0, "cmc_found": 0, "with_change": 0,
             "cg_from_discovery": 0, "cg_from_this_week": 0, "cg_looked_up": 0,
             "cg_over_budget": 0}
    details = []

    session = SessionLocal()
    try:
        coins = session.query(Coin).filter_by(status="active").all()
        for coin in coins:
            cg = _cg_rank_this_run(session, coin, week, stats)
            cmc = get_cmc_rank(coin.contract, coin.chain)
            if cg is not None:
                stats["cg_found"] += 1
            if cmc is not None:
                stats["cmc_found"] += 1

            cg_change = _upsert_and_change(session, coin.id, "coingecko", week, cg)
            cmc_change = _upsert_and_change(session, coin.id, "cmc", week, cmc)
            avg, signal = combine_signal(cg_change, cmc_change)

            # No rank this run -> keep the last known one for display.
            if cg is not None:
                coin.cg_rank = cg
            if cmc is not None:
                coin.cmc_rank = cmc
            coin.rank_change = avg
            coin.rank_signal = signal

            if avg is not None:
                stats["with_change"] += 1
            stats["coins"] += 1
            details.append((coin.name, cg, cmc, avg, signal))

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _print_summary(stats, details, week)
    return stats


def _print_summary(stats, details, week):
    line = "=" * 60
    print(line)
    print(f"  RANK UPDATE ({week})")
    print(line)
    print(f"  active coins ......... {stats['coins']:>4}")
    print(f"  CoinGecko ranks ...... {stats['cg_found']:>4}"
          f"   (from discovery pages {stats.get('cg_from_discovery', 0)}, "
          f"stored this week {stats.get('cg_from_this_week', 0)}, "
          f"looked up {stats.get('cg_looked_up', 0)}/{RANK_LOOKUP_BUDGET} CoinGecko "
          f"calls, over budget {stats.get('cg_over_budget', 0)})")
    print(f"  CoinMarketCap ranks .. {stats['cmc_found']:>4}")
    print(f"  with change vs last wk {stats['with_change']:>4}")
    print("  " + "-" * 56)
    print(f"  {'COIN':<16} {'CG':>6} {'CMC':>6} {'CHANGE':>8}  SIGNAL")
    for name, cg, cmc, avg, signal in details:
        cg_s = f"#{cg}" if cg else "—"
        cmc_s = f"#{cmc}" if cmc else "—"
        ch = "(first run)" if avg is None else (f"+{avg}" if avg > 0 else str(avg))
        print(f"  {(name or '')[:16]:<16} {cg_s:>6} {cmc_s:>6} {ch:>8}  {signal or '-'}")
    print(line)


# ---------------------------------------------------------------------------
# Manual test
# ---------------------------------------------------------------------------
def main():
    print(f"CoinGecko key: {bool(CG_KEY)} | CoinMarketCap key: {bool(CMC_KEY)}\n")

    coins = [
        ("Uniswap", "ethereum", "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984"),
        ("Pepe", "ethereum", "0x6982508145454Ce325dDbE47a25d4ec3d2311933"),
        ("YesBlob (our coin)", "solana", "VzqFToxrHqs7BFtfWvwmGAn6nv7p9W3zdmW95iTpump"),
    ]
    print("=" * 60)
    print("  LIVE RANKS (CoinGecko + CoinMarketCap)")
    print("=" * 60)
    for label, chain, contract in coins:
        cg = get_coingecko_rank(contract, chain)
        cmc = get_cmc_rank(contract, chain)
        print(f"  {label:<22} CoinGecko={('#'+str(cg)) if cg else '—':<8} "
              f"CoinMarketCap={('#'+str(cmc)) if cmc else '—'}")

    print("\n" + "=" * 60)
    print("  CHANGE LOGIC self-test (simulated last week -> this week)")
    print("=" * 60)
    cases = [
        ("both move up",   280, 150, 300, 180),   # cg 280->150, cmc 300->180
        ("disagree",       150, 120, 100, 160),   # cg up, cmc down
        ("both move down", 120, 200, 130, 190),   # both worse
        ("one source only", 280, 150, None, None),
    ]
    for label, cg_prev, cg_cur, cmc_prev, cmc_cur in cases:
        cg_ch = compute_change(cg_prev, cg_cur)
        cmc_ch = compute_change(cmc_prev, cmc_cur)
        avg, signal = combine_signal(cg_ch, cmc_ch)
        print(f"  {label:<16} cg_change={cg_ch} cmc_change={cmc_ch} "
              f"-> avg={avg} signal={signal}")


if __name__ == "__main__":
    main()
