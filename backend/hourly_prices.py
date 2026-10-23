"""
hourly_prices.py
----------------
Hourly LIVE price for every active coin (.github/workflows/hourly-prices.yml,
every hour on the hour). Writes ONLY coins.live_price + coins.live_price_at;
the daily price (coins.price_usd), snapshots, highest / lowest, scores and
every aggregate (Overview, Portfolio Performance) stay daily.

Source: DexScreener /tokens/v1/{chain}/{addresses} (30 coins per request,
free, no key) - the SAME source as the daily price, so a coin's live P&L is
measured against an entry price from the same feed. Each coin takes its
highest-liquidity pool. (CoinGecko's contract-price endpoint takes one chain
per call: ~13 calls an hour = ~9,400 a month of the 10K budget.)

Safety:
  * a coin missing from the response keeps its previous live price
  * a price more than SANITY_X times away from the coin's daily price is
    skipped (a dust / wrong pool), the previous live price is kept
  * nothing is ever set to NULL
  * Telegram alert only if the run fails or fewer than ALERT_SHARE of the
    active coins were updated; the script always exits 0.

The API uses a live price only while it is fresher than MAX_AGE_MIN
(effective_price()), otherwise the daily price.
"""

import sys
import logging
from datetime import timedelta

log = logging.getLogger("hourly_prices")

MAX_AGE_MIN = 90           # API: live price used only while younger than this
SANITY_X = 3.0             # skip a live price > 3x away from the daily price
ALERT_SHARE = 0.5          # alert if fewer than 50% of active coins updated


def effective_price(coin, max_age_min=MAX_AGE_MIN, now=None):
    """(price, is_live, live_price_at or None): the hourly live price while
    it is fresher than max_age_min, else the daily price_usd."""
    from database import utcnow
    now = now or utcnow()
    lp, at = getattr(coin, "live_price", None), getattr(coin, "live_price_at", None)
    if lp and at is not None and now - at < timedelta(minutes=max_age_min):
        return lp, True, at
    return coin.price_usd, False, None


def _best_prices(pairs):
    """{base address lower: (price, liquidity)} - highest-liquidity pool each."""
    best = {}
    for p in pairs or []:
        addr = ((p.get("baseToken") or {}).get("address") or "").lower()
        try:
            price = float(p.get("priceUsd"))
        except (TypeError, ValueError):
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if addr and price > 0 and (addr not in best or liq > best[addr][1]):
            best[addr] = (price, liq)
    return best


def fetch_live_prices(coins):
    """{contract lower: price} for (chain, contract) pairs. Never raises."""
    import requests
    from fetch_dexscreener import _fetch_v1, V1_CHAINS, V1_BATCH, TOKENS_URL
    stats = {"v1_requests": 0, "v1_failed": 0}
    by_chain, other = {}, []
    for chain, contract in coins:
        (by_chain.setdefault(chain, []) if chain in V1_CHAINS else other).append(contract)
    out = {}
    for chain, addrs in by_chain.items():
        for i in range(0, len(addrs), V1_BATCH):
            out.update({a: v[0] for a, v in _best_prices(_fetch_v1(chain, addrs[i:i + V1_BATCH], stats)).items()})
    for addr in other:                       # chains the v1 endpoint doesn't know: one call each
        try:
            r = requests.get(TOKENS_URL.format(addresses=addr), timeout=15)
            got = _best_prices(r.json().get("pairs") if r.status_code == 200 else [])
            if addr.lower() in got:
                out[addr.lower()] = got[addr.lower()][0]
        except Exception:
            pass
    return out, stats["v1_requests"] + len(other), stats["v1_failed"]


def run():
    """One hourly update. Returns stats; never raises."""
    from database import SessionLocal, utcnow
    from models import Coin
    stats = {"active": 0, "updated": 0, "missing": 0, "insane": 0, "requests": 0, "failed_requests": 0, "error": None}
    session = SessionLocal()
    try:
        coins = (session.query(Coin.id, Coin.chain, Coin.contract, Coin.price_usd, Coin.name)
                 .filter(Coin.status == "active").all())
        stats["active"] = len(coins)
        prices, stats["requests"], stats["failed_requests"] = fetch_live_prices(
            [((c.chain or "").lower(), (c.contract or "").lower()) for c in coins])
        now = utcnow()
        updates = []
        for c in coins:
            p = prices.get((c.contract or "").lower())
            if p is None:
                stats["missing"] += 1
                continue
            if c.price_usd and not (1 / SANITY_X <= p / c.price_usd <= SANITY_X):
                stats["insane"] += 1
                log.info("skip %s: live %.6g vs daily %.6g", c.name, p, c.price_usd)
                continue
            updates.append({"id": c.id, "live_price": p, "live_price_at": now})
        if updates:
            session.bulk_update_mappings(Coin, updates)
            session.commit()
        stats["updated"] = len(updates)
    except Exception as e:
        session.rollback()
        stats["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        log.exception("Hourly price update failed")
    finally:
        session.close()
    return stats


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    st = run()
    print(f"Hourly prices: {st['updated']}/{st['active']} active coins updated | missing {st['missing']} | "
          f"skipped (>{SANITY_X:g}x off daily) {st['insane']} | DexScreener requests {st['requests']} "
          f"(failed {st['failed_requests']})" + (f" | ERROR {st['error']}" if st["error"] else ""))
    if st["error"] or (st["active"] and st["updated"] < ALERT_SHARE * st["active"]):
        try:
            from daily_report import send_message
            send_message("⏱ Hourly prices: " + (f"run failed — {st['error']}" if st["error"] else
                         f"only {st['updated']} of {st['active']} active coins updated (missing {st['missing']}, "
                         f"skipped {st['insane']}). Live prices fall back to the daily price after 90 min."))
        except Exception as e:
            print(f"Telegram alert not sent ({type(e).__name__})")
    return 0                                  # never fail the workflow (no Actions spam)


if __name__ == "__main__":
    sys.exit(main())
