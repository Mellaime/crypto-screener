"""
hourly_prices.py
----------------
Hourly LIVE price for every active coin (.github/workflows/hourly-prices.yml,
every hour on the hour). Writes ONLY coins.live_price + coins.live_price_at;
the daily price (coins.price_usd), snapshots, highest / lowest, scores and
every aggregate (Overview, Portfolio Performance) stay daily.

Cascade (price_sources.py for CMC):
  1. CoinMarketCap quotes/latest by coins.cmc_id - 100 per call (~2 credits a run)
  2. coins CMC can't price -> DexScreener (below)
  3. still missing -> Codex (below)
  4. nothing -> the previous live price is kept (never nulled)
The raw CMC price is also stored in coins.cmc_live_price (the daily
CMC-vs-CoinGecko sanity check compares it).

DexScreener: /tokens/v1/{chain}/{addresses} (30 coins per request,
free, no key) - the SAME source as the daily price, so a coin's live P&L is
measured against an entry price from the same feed. Each coin takes its
highest-liquidity pool. (CoinGecko's contract-price endpoint takes one chain
per call: ~13 calls an hour = ~9,400 a month of the 10K budget.)

Fallback: coins DexScreener couldn't price this run (no pool, or rejected
by the safety check) are asked from Codex getTokenPrices - bulk, 25 tokens
per request (~1 request a run, ~720 a month), networkId from
liquidity_check.CHAIN_TO_NETWORK - with the same safety check.
coins.live_source = "dexscreener" | "codex" (where the live price came from).

Safety:
  * a coin missing from both keeps its previous live price
  * a price more than SANITY_X times away from the coin's daily price is
    skipped (a dust / wrong pool), the previous live price is kept
  * nothing is ever set to NULL
  * Telegram alert only if the run fails, fewer than ALERT_SHARE of the
    active coins were updated, or the whole Codex fallback call failed;
    the script always exits 0.

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
GUARD_MIN = 50             # skip a run if live prices were updated less than this ago (duplicate run)
CODEX_BATCH = 25           # tokens per Codex getTokenPrices request
CODEX_PRICES_QUERY = """query ($inputs: [GetPriceInput]) {
  getTokenPrices(inputs: $inputs) { address networkId priceUsd } }"""


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


def fetch_codex_prices(coins):
    """Codex fallback. coins = [(chain, contract as stored)] (original case:
    Solana addresses are case-sensitive). Returns ({contract lower: price},
    requests, all_failed). Never raises."""
    import requests
    from liquidity_check import CHAIN_TO_NETWORK, CODEX_KEY, CODEX_URL
    inputs = [{"address": ct, "networkId": CHAIN_TO_NETWORK[ch]} for ch, ct in coins if ch in CHAIN_TO_NETWORK and ct]
    if not inputs or not CODEX_KEY:
        return {}, 0, False
    out, calls, failed = {}, 0, 0
    for i in range(0, len(inputs), CODEX_BATCH):
        calls += 1
        try:
            body = requests.post(CODEX_URL, json={"query": CODEX_PRICES_QUERY, "variables": {"inputs": inputs[i:i + CODEX_BATCH]}},
                                 headers={"Authorization": CODEX_KEY}, timeout=30).json()
            rows = (body.get("data") or {}).get("getTokenPrices")
            if rows is None:
                raise ValueError(str(body.get("errors"))[:200])
        except Exception as e:
            failed += 1
            log.warning("Codex getTokenPrices failed (%s)", type(e).__name__)
            continue
        for r in rows or []:
            if not isinstance(r, dict):                       # Codex returns null for tokens it doesn't know
                continue
            try:
                p = float(r.get("priceUsd"))
            except (TypeError, ValueError):
                continue
            if p > 0 and r.get("address"):
                out[r["address"].lower()] = p
    return out, calls, failed == calls


def run():
    """One hourly update. Returns stats; never raises."""
    from database import SessionLocal, utcnow
    from models import Coin
    stats = {"active": 0, "updated": 0, "missing": 0, "insane": 0, "requests": 0, "failed_requests": 0, "error": None,
             "dexscreener": 0, "codex": 0, "codex_requests": 0, "codex_failed": False,
             "cmc": 0, "cmc_requests": 0, "cmc_failed": False}
    session = SessionLocal()
    try:
        coins = (session.query(Coin.id, Coin.chain, Coin.contract, Coin.price_usd, Coin.name, Coin.cmc_id)
                 .filter(Coin.status == "active").all())
        stats["active"] = len(coins)
        now = utcnow()

        def sane(c, p):
            return not c.price_usd or 1 / SANITY_X <= p / c.price_usd <= SANITY_X

        updates, raw_cmc, todo = [], [], []
        try:                                                  # 1. CoinMarketCap
            from price_sources import cmc_quotes
            q, stats["cmc_requests"], stats["cmc_failed"] = cmc_quotes([c.cmc_id for c in coins if c.cmc_id])
        except Exception as e:
            log.warning("CMC stage failed (%s) - DexScreener / Codex only this run", type(e).__name__)
            q, stats["cmc_failed"] = {}, True
        for c in coins:
            p = q.get(c.cmc_id) if c.cmc_id else None
            if p is not None:
                raw_cmc.append({"id": c.id, "cmc_live_price": p})
            if p is not None and sane(c, p):
                updates.append({"id": c.id, "live_price": p, "live_price_at": now, "live_source": "cmc"})
                stats["cmc"] += 1
                log.info("%s: cmc %.6g", c.name, p)
            else:
                if p is not None:
                    log.info("%s: cmc %.6g rejected (daily %.6g)", c.name, p, c.price_usd)
                todo.append(c)
        coins_for_dex = todo
        prices, stats["requests"], stats["failed_requests"] = fetch_live_prices(
            [((c.chain or "").lower(), (c.contract or "").lower()) for c in coins_for_dex])

        missed = []
        for c in coins_for_dex:                               # 2. DexScreener
            p = prices.get((c.contract or "").lower())
            if p is not None and sane(c, p):
                updates.append({"id": c.id, "live_price": p, "live_price_at": now, "live_source": "dexscreener"})
                stats["dexscreener"] += 1
                log.info("%s: dexscreener %.6g", c.name, p)
            else:
                if p is not None:
                    log.info("%s: dexscreener %.6g rejected (daily %.6g)", c.name, p, c.price_usd)
                missed.append(c)
        if missed:                                            # 3. Codex, only for the missed coins
            try:
                cx, stats["codex_requests"], stats["codex_failed"] = fetch_codex_prices(
                    [((c.chain or "").lower(), c.contract) for c in missed])
            except Exception as e:                            # never lose the DexScreener results
                log.warning("Codex fallback failed (%s) - DexScreener prices only this run", type(e).__name__)
                cx, stats["codex_failed"] = {}, True
            for c in missed:
                p = cx.get((c.contract or "").lower())
                if p is not None and sane(c, p):
                    updates.append({"id": c.id, "live_price": p, "live_price_at": now, "live_source": "codex"})
                    stats["codex"] += 1
                    log.info("%s: codex %.6g", c.name, p)
                else:
                    stats["insane" if p is not None else "missing"] += 1
                    log.info("%s: skipped (%s)", c.name, f"codex {p:.6g} vs daily {c.price_usd:.6g}" if p is not None else "no price")
        if updates or raw_cmc:
            if updates:
                session.bulk_update_mappings(Coin, updates)
            if raw_cmc:
                session.bulk_update_mappings(Coin, raw_cmc)
            session.commit()
        stats["updated"] = len(updates)
    except Exception as e:
        session.rollback()
        stats["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        log.exception("Hourly price update failed")
    finally:
        session.close()
    return stats


def minutes_since_last_update():
    """Minutes since the newest coins.live_price_at, or None (never / error)."""
    try:
        from sqlalchemy import func
        from database import SessionLocal, utcnow
        from models import Coin
        session = SessionLocal()
        try:
            last = session.query(func.max(Coin.live_price_at)).scalar()
        finally:
            session.close()
        return None if last is None else (utcnow() - last).total_seconds() / 60
    except Exception:
        return None


def _set_output(key, value):
    """GitHub Actions step output (no-op outside Actions)."""
    import os
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")


def main():
    import os
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    # Duplicate-run guard (self-chaining + cron backup): prices updated less
    # than GUARD_MIN minutes ago -> another run just did it; exit cleanly.
    if os.getenv("FORCE_RUN", "").lower() != "true":
        ago = minutes_since_last_update()
        if ago is not None and ago < GUARD_MIN:
            print(f"Hourly prices: skipped - live prices were updated {ago:.0f} min ago (< {GUARD_MIN} min).")
            _set_output("skipped", "true")
            return 0
    _set_output("skipped", "false")
    st = run()
    print(f"Hourly prices: {st['updated']}/{st['active']} active coins updated (cmc {st['cmc']}, dexscreener "
          f"{st['dexscreener']}, codex {st['codex']}) | skipped: no price {st['missing']}, >{SANITY_X:g}x off daily "
          f"{st['insane']} | CMC requests {st['cmc_requests']}" + (" (ALL FAILED)" if st["cmc_failed"] else "")
          + f" | DexScreener requests {st['requests']} (failed {st['failed_requests']}) | Codex requests {st['codex_requests']}"
          + (" (ALL FAILED)" if st["codex_failed"] else "") + (f" | ERROR {st['error']}" if st["error"] else ""))
    if st["codex_failed"] and not st["error"]:
        try:
            from daily_report import send_message
            send_message(f"⏱ Hourly prices: the Codex fallback failed ({st['codex_requests']} request(s)) — "
                         f"{st['dexscreener']} coins still updated from DexScreener; the rest keep their previous price.")
        except Exception as e:
            print(f"Telegram alert not sent ({type(e).__name__})")
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
