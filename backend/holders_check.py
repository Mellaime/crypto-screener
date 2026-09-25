"""
holders_check.py
----------------
Holder counts from CoinMarketCap's KEYLESS DEX endpoint:
    https://pro-api.coinmarketcap.com/public-api/v1/dex/holders/count
Params: platform (chain slug) + tokenAddress (camelCase contract).

We only use /count (the paid /trend and /tag_count endpoints are 403 on our
tier - see the earlier test). We build holder-growth history ourselves from
daily snapshots, just like we do for volume and rank.

Behaviour:
  * get_holder_count(contract, chain) -> int, or the string "unknown".
      "unknown" = unsupported chain (robinhood/arc) or any error. We NEVER
      reject a coin for "unknown" - same rule as the honeypot check.
  * Never raises.
  * is_holders_rising(coin) -> ready for when we have a few days of history.
  * run_holders_update() -> refresh holder counts for all ACTIVE coins.
"""

import time

import requests

HOLDERS_URL = "https://pro-api.coinmarketcap.com/public-api/v1/dex/holders/count"

# Map our DexScreener chain names -> CoinMarketCap DEX platform slugs.
CHAIN_TO_PLATFORM = {
    "ethereum": "ethereum",
    "solana": "solana",
    "base": "base",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "polygon": "polygon",
    "avalanche": "avalanche",
    "zksync": "zksync",
    "scroll": "scroll",
    "linea": "linea",
    "monad": "monad",
    "tron": "tron",
}

REQUEST_DELAY = 1.5  # seconds before each live call

# Per-process memo so we fetch each coin at most once per run.
_memo = {}


def get_holder_count(contract, chain):
    """Return an int holder count, or the string 'unknown'. Never raises."""
    platform = CHAIN_TO_PLATFORM.get((chain or "").lower())
    if not platform or not contract:
        return "unknown"  # chain not covered by CMC DEX -> don't reject

    memo_key = (platform, contract)
    if memo_key in _memo:
        return _memo[memo_key]

    time.sleep(REQUEST_DELAY)
    try:
        r = requests.get(
            HOLDERS_URL,
            params={"platform": platform, "tokenAddress": contract},
            headers={"Accept": "application/json"},
            timeout=20,
        )
    except requests.exceptions.RequestException as e:
        print(f"[WARN] CMC holders request failed: {e}")
        return "unknown"  # transient error -> unknown (don't cache)

    if r.status_code != 200:
        # 400 (bad platform / not found), 429 (rate limit), etc. -> unknown
        _memo[memo_key] = "unknown"
        return "unknown"
    try:
        count = (r.json().get("data") or {}).get("count")
    except ValueError:
        return "unknown"
    if count is None:
        _memo[memo_key] = "unknown"
        return "unknown"
    try:
        result = int(count)
    except (TypeError, ValueError):
        result = "unknown"
    _memo[memo_key] = result
    return result


def is_holders_rising(coin, days=3):
    """
    Return True only if the coin's holder count is trending UP over the last
    `days` daily snapshots.

    *** NOT USED YET as a filter/score - needs a few days of history first. ***
    Same pattern as is_volume_rising(): read the latest `days` snapshots
    (newest last), require all have a holder count, never drop day-to-day, and
    end higher than they started. Swap this into Filter #Holders and the
    holder-growth score once history exists.
    """
    from database import SessionLocal
    from models import DailySnapshot

    session = SessionLocal()
    try:
        snaps = (
            session.query(DailySnapshot)
            .filter(DailySnapshot.coin_id == coin.id)
            .order_by(DailySnapshot.date.desc())
            .limit(days)
            .all()
        )
    finally:
        session.close()

    if len(snaps) < days:
        return False
    vals = [s.holders for s in reversed(snaps)]
    if any(v is None for v in vals):
        return False
    never_drops = all(vals[i] >= vals[i - 1] for i in range(1, len(vals)))
    net_increase = vals[-1] > vals[0]
    return never_drops and net_increase


def run_holders_update():
    """
    Refresh holder counts for every ACTIVE coin into its latest snapshot.

    On a transient "unknown" (CMC didn't return a count this run) we do NOT
    wipe a previously known count. If today's snapshot already has a count we
    keep it; if it doesn't, we carry forward the most recent KNOWN count from
    history - so the dashboard/score don't flip to "-" over a temporary miss.
    """
    from database import SessionLocal
    from models import Coin, DailySnapshot

    stats = {"coins": 0, "with_count": 0, "kept_last": 0, "unknown": 0}
    details = []

    session = SessionLocal()
    try:
        coins = session.query(Coin).filter_by(status="active").all()
        for coin in coins:
            res = get_holder_count(coin.contract, coin.chain)
            snap = (
                session.query(DailySnapshot)
                .filter_by(coin_id=coin.id)
                .order_by(DailySnapshot.date.desc(), DailySnapshot.id.desc())
                .first()
            )

            if isinstance(res, int):
                # Fresh count -> store it.
                if snap is not None:
                    snap.holders = res
                stats["with_count"] += 1
                shown = f"{res:,}"
            else:
                # Transient unknown -> keep / carry forward the last known count.
                kept = None
                if snap is not None and snap.holders is not None:
                    kept = snap.holders                       # already has one; leave it
                elif snap is not None:
                    prev = (
                        session.query(DailySnapshot.holders)
                        .filter(DailySnapshot.coin_id == coin.id,
                                DailySnapshot.holders.isnot(None))
                        .order_by(DailySnapshot.date.desc(), DailySnapshot.id.desc())
                        .first()
                    )
                    if prev is not None:
                        snap.holders = prev[0]                # carry forward into today
                        kept = prev[0]

                if kept is not None:
                    stats["kept_last"] += 1
                    shown = f"{kept:,} (kept)"
                else:
                    stats["unknown"] += 1
                    shown = "unknown"

            stats["coins"] += 1
            details.append((coin.name, shown))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    line = "=" * 46
    print(line)
    print("  HOLDERS UPDATE")
    print(line)
    print(f"  active coins ......... {stats['coins']:>4}")
    print(f"  with fresh count ..... {stats['with_count']:>4}")
    print(f"  kept last known ...... {stats['kept_last']:>4}")
    print(f"  still unknown ........ {stats['unknown']:>4}")
    for name, shown in details:
        print(f"      - {(name or '')[:18]:<18} {shown}")
    print(line)
    return stats
    return stats


# ---------------------------------------------------------------------------
# Manual test
# ---------------------------------------------------------------------------
def main():
    test_coins = [
        ("YesBlob", "solana", "VzqFToxrHqs7BFtfWvwmGAn6nv7p9W3zdmW95iTpump"),
        ("Strike Robot", "base", "0x10c56F005a379f8eAfc88ff5c3f40d30F0031AC9"),
        ("Uniswap", "ethereum", "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984"),
        ("Pepe", "ethereum", "0x6982508145454Ce325dDbE47a25d4ec3d2311933"),
        ("Good In The Hood", "robinhood", "0x0000000000000000000000000000000000000000"),
    ]
    print("=" * 60)
    print("  CMC DEX HOLDER COUNTS")
    print("=" * 60)
    for name, chain, contract in test_coins:
        res = get_holder_count(contract, chain)
        shown = f"{res:,} holders" if isinstance(res, int) else "unknown (not rejected)"
        print(f"  {name:<18} [{chain:<9}] -> {shown}")


if __name__ == "__main__":
    main()
