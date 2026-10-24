"""
price_sources.py
----------------
Batched price lookups for the hourly live price and the daily sanity check.

  CoinMarketCap - the hourly LIVE price. /v1/cryptocurrency/quotes/latest by
      CMC id, 100 ids per call (1 credit per call). Ids come from coins.cmc_id,
      filled by the daily job from /v1/cryptocurrency/map (contract -> id; the
      same call rank_check already makes, 1 credit).
  CoinGecko - the daily SANITY price. /simple/token_price/{platform}, one
      call per blockchain per 100 contracts (~13 calls a day).

The daily job (BEFORE the drop logic) fetches fresh CMC + CoinGecko prices
for every active and dropped coin (price_verify.FRESH). price_verify then
REMOVES a coin whose CMC / CoinGecko / daily prices disagree by more than 2x
(applied in drop_logic); coins whose worst pair is between 1.5x and 2x are
only warned about in the Telegram report.
"""

import os
import time
import logging

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
log = logging.getLogger("price_sources")

CMC_BASE = "https://pro-api.coinmarketcap.com"
CG_BASE = "https://api.coingecko.com/api/v3"
CMC_BATCH = 100
CG_BATCH = 100
CMC_INFO_BUDGET = 40       # per daily run: /v2/info lookups (1 credit each) for coins the map misses


def _cmc_key():
    return os.getenv("COINMARKETCAP_API_KEY")


# ---------------------------------------------------------------- CoinMarketCap
def cmc_id_map():
    """{"exact": {address: cmc id}, "lower": {address lower: cmc id}} from
    /v1/cryptocurrency/map (active listings, primary-platform address).
    Empty on failure. 1 credit."""
    out = {"exact": {}, "lower": {}}
    if not _cmc_key():
        return out
    try:
        r = requests.get(f"{CMC_BASE}/v1/cryptocurrency/map", params={"listing_status": "active", "aux": "platform"},
                         headers={"X-CMC_PRO_API_KEY": _cmc_key(), "Accept": "application/json"}, timeout=60)
        data = (r.json().get("data") or []) if r.status_code == 200 else []
    except Exception as e:
        log.warning("CMC map failed (%s)", type(e).__name__)
        return out
    for e in data:
        addr = (e.get("platform") or {}).get("token_address")
        if addr and e.get("id"):
            out["exact"][addr] = e["id"]
            out["lower"][addr.lower()] = e["id"]
    return out


def cmc_id_for(contract, m):
    """A contract's CMC id from cmc_id_map() (exact first: Solana is case-sensitive)."""
    if not contract:
        return None
    return m["exact"].get(contract) or m["lower"].get(contract.lower())


def cmc_id_by_address(contract, chain):
    """CMC id for any of a coin's contract addresses (/v2/cryptocurrency/info,
    1 credit; the map only lists each coin's main-chain address). None if not
    on CMC / on error."""
    if not contract or not _cmc_key():
        return None
    tries = [contract] if (chain or "").lower() == "solana" else [contract.lower()]
    for addr in tries:
        try:
            r = requests.get(f"{CMC_BASE}/v2/cryptocurrency/info", params={"address": addr},
                             headers={"X-CMC_PRO_API_KEY": _cmc_key(), "Accept": "application/json"}, timeout=20)
            d = (r.json().get("data") or {}) if r.status_code == 200 else {}
            if d:
                return int(next(iter(d)))
        except Exception:
            pass
    return None


CHANGES_24H = {}           # {cmc id: 24h price change %} from the last cmc_quotes call


def cmc_quotes(ids):
    """({cmc id: USD price}, calls, all_failed) for CMC ids, 100 per call.
    Also fills CHANGES_24H with each coin's 24h price change (%)."""
    CHANGES_24H.clear()
    ids = sorted({int(i) for i in ids if i})
    if not ids or not _cmc_key():
        return {}, 0, False
    out, calls, failed = {}, 0, 0
    for i in range(0, len(ids), CMC_BATCH):
        calls += 1
        try:
            r = requests.get(f"{CMC_BASE}/v1/cryptocurrency/quotes/latest",
                             params={"id": ",".join(str(x) for x in ids[i:i + CMC_BATCH]), "convert": "USD"},
                             headers={"X-CMC_PRO_API_KEY": _cmc_key(), "Accept": "application/json"}, timeout=30)
            data = r.json().get("data") if r.status_code == 200 else None
            if data is None:
                raise ValueError(f"HTTP {r.status_code}")
        except Exception as e:
            failed += 1
            log.warning("CMC quotes failed (%s)", type(e).__name__)
            continue
        for cid, d in data.items():
            try:
                p = float(((d.get("quote") or {}).get("USD") or {}).get("price"))
            except (TypeError, ValueError):
                continue
            if p > 0:
                out[int(cid)] = p
                chg = ((d.get("quote") or {}).get("USD") or {}).get("percent_change_24h")
                if isinstance(chg, (int, float)):
                    CHANGES_24H[int(cid)] = float(chg)
    return out, calls, bool(calls) and failed == calls


# ---------------------------------------------------------------- CoinGecko
def coingecko_token_prices(coins):
    """({contract lower: USD price}, calls) for [(chain, contract)] via
    /simple/token_price/{platform}, 100 contracts per call. Never raises."""
    from age_check import CHAIN_TO_CG_PLATFORM
    key = os.getenv("COINGECKO_API_KEY")
    by_platform = {}
    for chain, contract in coins:
        plat = CHAIN_TO_CG_PLATFORM.get((chain or "").lower())
        if plat and contract:
            by_platform.setdefault(plat, []).append(contract)
    out, calls = {}, 0
    for plat, addrs in by_platform.items():
        for i in range(0, len(addrs), CG_BATCH):
            calls += 1
            try:
                r = requests.get(f"{CG_BASE}/simple/token_price/{plat}",
                                 params={"contract_addresses": ",".join(addrs[i:i + CG_BATCH]), "vs_currencies": "usd"},
                                 headers={"x-cg-demo-api-key": key} if key else {}, timeout=30)
                data = r.json() if r.status_code == 200 else {}
            except Exception as e:
                log.warning("CoinGecko token_price %s failed (%s)", plat, type(e).__name__)
                data = {}
            for addr, v in (data or {}).items():
                p = (v or {}).get("usd")
                if isinstance(p, (int, float)) and p > 0:
                    out[addr.lower()] = float(p)
            time.sleep(1.5)                                   # demo plan: ~30 calls / minute
    return out, calls


def ratio(a, b):
    """max/min of two positive prices, or None."""
    return max(a, b) / min(a, b) if a and b else None


# ---------------------------------------------------------------- daily sanity step
def run_price_sanity():
    """Daily job step (before drop_logic): refresh coins.cmc_id (CMC map, 1
    credit + up to CMC_INFO_BUDGET /v2/info lookups), then FRESH CMC quotes
    (~3 credits) and CoinGecko prices (~15 calls) for every active and dropped
    coin -> price_verify.FRESH (+ coins.cmc_live_price / coingecko_daily_price).
    Returns stats; "mismatches" = the WARNINGS: active coins (and dropped coins
    due their 3-day re-check) whose worst price pair is between
    price_verify.WARN_X and REMOVE_X - [(name, source_a, a, source_b, b, ratio)].
    Over REMOVE_X is drop_logic's removal, not a warning."""
    from database import SessionLocal, utcnow
    from models import Coin
    import price_verify
    from liquidity_verify import DROPPED_RECHECK_DAYS
    stats = {"active": 0, "dropped": 0, "cmc_mapped": 0, "coingecko_priced": 0, "cmc_priced": 0, "compared": 0,
             "mismatches": [], "to_remove": 0, "cg_calls": 0, "cmc_info_calls": 0, "cmc_calls": 0}
    price_verify.FRESH.clear()
    today = utcnow().date()
    session = SessionLocal()
    try:
        coins = session.query(Coin).filter(Coin.status.in_(["active", "dropped"])).all()
        stats["active"] = sum(c.status == "active" for c in coins)
        stats["dropped"] = len(coins) - stats["active"]
        m = cmc_id_map()
        cg, stats["cg_calls"] = coingecko_token_prices([(c.chain, c.contract) for c in coins])
        info_left = CMC_INFO_BUDGET
        for c in coins:
            if m["lower"]:
                cid = cmc_id_for(c.contract, m)
                if cid:
                    c.cmc_id = cid
            if c.cmc_id is None and info_left > 0:            # not in the map: ask by address once
                info_left -= 1
                c.cmc_id = cmc_id_by_address(c.contract, c.chain) or 0    # 0 = not on CMC
                time.sleep(1.3)                               # CMC: 50 requests / minute
            stats["cmc_mapped"] += bool(c.cmc_id)
        stats["cmc_info_calls"] = CMC_INFO_BUDGET - info_left
        q, stats["cmc_calls"], cmc_failed = cmc_quotes([c.cmc_id for c in coins if c.cmc_id])
        if cmc_failed:
            raise RuntimeError("CMC quotes failed - no price mismatch decisions this run")
        for c in coins:
            p_cg = cg.get((c.contract or "").lower())
            p_cmc = q.get(c.cmc_id) if c.cmc_id else None
            if p_cg is not None:
                c.coingecko_daily_price = p_cg
                stats["coingecko_priced"] += 1
            if p_cmc is not None:
                c.cmc_live_price = p_cmc
                stats["cmc_priced"] += 1
            price_verify.FRESH[c.id] = {"CMC": p_cmc, "CoinGecko": p_cg}
            checked = c.birdeye_checked_at.date() if c.birdeye_checked_at else None
            due = c.status == "active" or checked is None or (today - checked).days >= DROPPED_RECHECK_DAYS
            wp = price_verify.worst_pair(price_verify.readings(c))
            if wp is None or not due:
                continue
            stats["compared"] += 1
            if wp[4] > price_verify.REMOVE_X:
                stats["to_remove"] += 1
            elif wp[4] > price_verify.WARN_X:
                stats["mismatches"].append((c.name, wp[0], wp[1], wp[2], wp[3], round(wp[4], 2)))
        session.commit()
    except Exception:
        session.rollback()
        price_verify.FRESH.clear()                            # nothing decided on partial data
        raise
    finally:
        session.close()
    stats["mismatches"].sort(key=lambda x: -x[5])
    log.info("Price check: %d active + %d dropped | CMC id %d, CMC priced %d, CoinGecko priced %d (%d calls) | "
             "%d compared, %d to remove (> %gx), %d warnings (%g-%gx)", stats["active"], stats["dropped"],
             stats["cmc_mapped"], stats["cmc_priced"], stats["coingecko_priced"], stats["cg_calls"], stats["compared"],
             stats["to_remove"], price_verify.REMOVE_X, len(stats["mismatches"]), price_verify.WARN_X,
             price_verify.REMOVE_X)
    return stats
