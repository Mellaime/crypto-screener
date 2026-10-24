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

The daily job compares the latest CMC live price with CoinGecko's price and
puts coins more than MISMATCH_X apart in the Telegram report - ALERT ONLY:
nothing is dropped, no price or status is changed.
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
MISMATCH_X = 3.0           # CMC vs CoinGecko more than 3x apart -> Telegram alert
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
    """Daily job step: refresh coins.cmc_id (CMC map, 1 credit), store
    coins.coingecko_daily_price (CoinGecko, ~13 calls), and list active coins
    whose latest CMC live price and CoinGecko price are more than MISMATCH_X
    apart. ALERT ONLY - nothing else changes. Returns stats with
    "mismatches" = [(name, cmc, coingecko, ratio)]."""
    from database import SessionLocal
    from models import Coin
    stats = {"active": 0, "cmc_mapped": 0, "coingecko_priced": 0, "compared": 0, "mismatches": [],
             "cg_calls": 0, "cmc_info_calls": 0}
    session = SessionLocal()
    try:
        coins = session.query(Coin).filter(Coin.status == "active").all()
        stats["active"] = len(coins)
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
        for c in coins:
            p = cg.get((c.contract or "").lower())
            if p is not None:
                c.coingecko_daily_price = p
                stats["coingecko_priced"] += 1
            r = ratio(c.cmc_live_price, p)
            if r is not None:
                stats["compared"] += 1
                if r > MISMATCH_X:
                    stats["mismatches"].append((c.name, c.cmc_live_price, p, round(r, 1)))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    stats["mismatches"].sort(key=lambda x: -x[3])
    log.info("Price sanity: %d active, %d with a CMC id, %d priced by CoinGecko (%d calls), %d compared, %d mismatches > %gx.",
             stats["active"], stats["cmc_mapped"], stats["coingecko_priced"], stats["cg_calls"], stats["compared"],
             len(stats["mismatches"]), MISMATCH_X)
    return stats
