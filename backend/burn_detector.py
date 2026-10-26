"""
burn_detector.py
----------------
Burned supply for EVM coins (detail page / Check a Coin "Burned" row).

Burned = the token balance of the 3 burn wallets (0x...dEaD, 0x...0000,
0x...0001). If all three hold 0, incoming transfers to 0x...0000 still count:
OpenZeppelin _burn() emits Transfer(to=0x0) and lowers totalSupply instead.
Mints are Transfer(from=0x0), so only INCOMING transfers (to == burn wallet)
are ever counted.

Two free sources, by chain (probed 2026-10-26):
  * Etherscan V2 (ETHERSCAN_API_KEY): ethereum, arbitrum, polygon, hyperevm,
    blast - tokenbalance / tokensupply / tokentx, max ~4 calls/s.
  * Public JSON-RPC: bsc, base, optimism, avalanche (Etherscan's free tier
    refuses them), zksync, cronos (not in Etherscan V2), linea - one batched
    request for balanceOf x3 / totalSupply / decimals, then eth_getLogs for
    burn transfers. Public RPCs only keep a short log window (BSC / OP ~1 h,
    Base / Avalanche ~1 day), so an older burn there reads STATIC_PRE_BURN.

Classification from the last 10 incoming burn transfers:
  BUYBACK_AND_BURN  a burn came from the DEX pair or the token contract
  STATIC_PRE_BURN   1-2 burns, all within 48 h of the pair's creation, or the
                    last burn > 30 days ago (ONE_TIME)
  ACTIVE_BURN       repeated burns, the last within 14 days (or 15-30 days
                    when they recur at least monthly)
  NO_BURN           nothing burned, or < 0.001% of supply (dust sent by accident)
Daily job: run_burn_update() stores the result on active EVM coins.
"""

import asyncio
import os
import statistics
import threading
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

ES_URL = "https://api.etherscan.io/v2/api"
ES_GAP = 0.24                        # seconds between Etherscan calls (free tier: 5/s)
DUST_PCT = 0.001                     # burned < 0.001% of supply = NO_BURN (accidental sends)
BURN_WALLETS = ["0x000000000000000000000000000000000000dEaD",
                "0x0000000000000000000000000000000000000000",
                "0x0000000000000000000000000000000000000001"]
ZERO = BURN_WALLETS[1]
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

CHAIN_IDS = {"ethereum": 1, "eth": 1, "bsc": 56, "bnb": 56, "base": 8453, "arbitrum": 42161,
             "arbitrum-one": 42161, "polygon": 137, "matic": 137, "optimism": 10, "op": 10,
             "avalanche": 43114, "avax": 43114, "linea": 59144, "blast": 81457, "zksync": 324,
             "hyperevm": 999, "cronos": 25}
ES_FREE = {1, 42161, 137, 999, 81457}
# chain id -> (public RPC, eth_getLogs block window it accepts)
RPCS = {56: ("https://bsc-rpc.publicnode.com", 5000),
        8453: ("https://base-rpc.publicnode.com", 50000),
        10: ("https://optimism-rpc.publicnode.com", 5000),
        43114: ("https://avalanche-c-chain-rpc.publicnode.com", 50000),
        324: ("https://mainnet.era.zksync.io", 500000),
        25: ("https://evm.cronos.org", 2000),
        59144: ("https://rpc.linea.build", 5000)}


class BurnError(Exception):
    pass


def chain_id(chain):
    return CHAIN_IDS.get((chain or "").lower())


# ---------------------------------------------------------------- Etherscan
_es_lock, _es_last = threading.Lock(), [0.0]


def _es_get(cid, **params):
    """One Etherscan V2 call, spaced ES_GAP apart across all threads, retried
    on rate limits. Errors never carry the URL (it holds the API key)."""
    key = os.getenv("ETHERSCAN_API_KEY")
    if not key:
        raise BurnError("ETHERSCAN_API_KEY not set")
    for attempt in range(4):
        with _es_lock:
            wait = _es_last[0] + ES_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _es_last[0] = time.monotonic()
        try:
            d = requests.get(ES_URL, params={"chainid": cid, **params, "apikey": key}, timeout=20).json()
        except Exception as e:
            raise BurnError(f"Etherscan request failed ({type(e).__name__})") from None
        res = d.get("result")
        if d.get("status") == "1" or "jsonrpc" in d or str(d.get("message", "")).startswith("No transactions"):
            return res
        if isinstance(res, str) and "rate limit" in res.lower():
            time.sleep(1.0 * (attempt + 1))
            continue
        raise BurnError(f"Etherscan: {str(res or d.get('message'))[:80]}")
    raise BurnError("Etherscan: rate limited")


async def _es(cid, **params):
    return await asyncio.to_thread(_es_get, cid, **params)


async def _from_etherscan(contract, cid):
    bals = {w: int(await _es(cid, module="account", action="tokenbalance", contractaddress=contract,
                             address=w, tag="latest") or 0) for w in BURN_WALLETS}
    top = max(BURN_WALLETS, key=lambda w: bals[w]) if any(bals.values()) else ZERO
    txs = await _es(cid, module="account", action="tokentx", contractaddress=contract, address=top,
                    page=1, offset=25, sort="desc") or []
    incoming = [t for t in txs if t["to"].lower() == top.lower()][:10]          # from 0x0 = mint, never a burn
    if not any(bals.values()) and not incoming:
        return bals, None, None, []
    supply = int(await _es(cid, module="stats", action="tokensupply", contractaddress=contract) or 0)
    if txs:
        decimals = int(txs[0]["tokenDecimal"])
    else:
        r = await _es(cid, module="proxy", action="eth_call", to=contract, data="0x313ce567", tag="latest")
        decimals = int(r, 16) if r and r != "0x" else None
    return bals, supply, decimals, [{"from": t["from"].lower(), "ts": int(t["timeStamp"]), "value": int(t["value"])}
                                    for t in incoming]


# ---------------------------------------------------------------- public RPC
def _rpc_post(url, batch):
    try:
        r = requests.post(url, json=batch, timeout=25).json()
    except Exception as e:
        raise BurnError(f"RPC request failed ({type(e).__name__})") from None
    if not isinstance(r, list):
        raise BurnError(f"RPC: {str(r.get('error') if isinstance(r, dict) else r)[:80]}")
    return {x.get("id"): x for x in r}


def _hex(x):
    v = (x or {}).get("result")
    return int(v, 16) if isinstance(v, str) and v not in ("0x", "") else None


def _pad(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def _from_rpc_sync(contract, cid):
    url, span = RPCS[cid]
    call = lambda i, data: {"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": contract, "data": data}, "latest"]}
    batch = [call(i, "0x70a08231" + _pad(w)[2:]) for i, w in enumerate(BURN_WALLETS)]
    batch += [call(3, "0x18160ddd"), call(4, "0x313ce567"),
              {"jsonrpc": "2.0", "id": 5, "method": "eth_blockNumber", "params": []}]
    res = _rpc_post(url, batch)
    if _hex(res.get(3)) is None:
        raise BurnError("RPC: totalSupply() failed - not an ERC-20 here?")
    bals = {w: _hex(res.get(i)) or 0 for i, w in enumerate(BURN_WALLETS)}
    supply, decimals, head = _hex(res[3]), _hex(res.get(4)), _hex(res.get(5))
    top = max(BURN_WALLETS, key=lambda w: bals[w]) if any(bals.values()) else ZERO
    transfers = []
    if head:
        # shortcut: public RPCs keep only `span` blocks of logs; a paid archive RPC would see every burn
        logs = _rpc_post(url, [{"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": [{
            "address": contract, "fromBlock": hex(max(0, head - span)), "toBlock": hex(head),
            "topics": [TRANSFER_TOPIC, None, _pad(top)]}]}]).get(1, {}).get("result") or []
        logs = [l for l in logs if len(l.get("topics") or []) == 3][-10:][::-1]     # ERC-20 only, newest first
        need = sorted({l["blockNumber"] for l in logs if not l.get("blockTimestamp")})
        times = {}
        if need:
            got = _rpc_post(url, [{"jsonrpc": "2.0", "id": i, "method": "eth_getBlockByNumber", "params": [b, False]}
                                  for i, b in enumerate(need)])
            times = {b: int(got[i]["result"]["timestamp"], 16) for i, b in enumerate(need)
                     if (got.get(i) or {}).get("result")}
        for l in logs:
            ts = int(l["blockTimestamp"], 16) if l.get("blockTimestamp") else times.get(l["blockNumber"])
            if ts:
                transfers.append({"from": "0x" + l["topics"][1][-40:], "ts": ts, "value": int(l["data"], 16)})
    return bals, supply, decimals, transfers


# ---------------------------------------------------------------- classification
def _frequency(times):
    if len(times) < 2:
        return "ONE_TIME"
    avg = statistics.mean(a - b for a, b in zip(times, times[1:])) / 86400
    return "DAILY" if avg <= 2 else "WEEKLY" if avg <= 10 else "MONTHLY" if avg <= 35 else "IRREGULAR"


def classify(transfers, burned, contract, pair_address=None, pair_created_at=None, now=None):
    """(burn_status, burn_frequency) from incoming burn transfers (newest first)."""
    if not transfers:
        return ("STATIC_PRE_BURN", "ONE_TIME") if burned else ("NO_BURN", "NONE")
    now = now or time.time()
    times = sorted((t["ts"] for t in transfers), reverse=True)
    freq, days = _frequency(times), (now - times[0]) / 86400
    if {t["from"] for t in transfers} & {(pair_address or "").lower(), contract.lower()} - {""}:
        return "BUYBACK_AND_BURN", freq
    if (len(times) <= 2 or days > 30
            or (pair_created_at and all(0 <= t - pair_created_at <= 48 * 3600 for t in times))):
        return "STATIC_PRE_BURN", "ONE_TIME"
    if days <= 14 or freq in ("DAILY", "WEEKLY", "MONTHLY"):
        return "ACTIVE_BURN", freq
    return "STATIC_PRE_BURN", "ONE_TIME"


def _summarize(contract, cid, source, bals, supply, decimals, transfers, pair_address, pair_created_at, now):
    base = {"chain_id": cid, "contract": contract, "source": source, "error": None}
    burned = sum(bals.values())
    real_burn = burned == 0 and bool(transfers)       # _burn(): already subtracted from totalSupply
    if real_burn:
        burned = sum(t["value"] for t in transfers)   # shortcut: only the last 10 transfers are summed
    denom = (supply or 0) + burned if real_burn else supply
    if not burned or (denom and burned / denom * 100 < DUST_PCT):     # dust sent to a burn wallet isn't a burn
        return {**base, "is_burnable": False, "burn_status": "NO_BURN", "burn_frequency": "NONE",
                "total_burned_amount": 0.0, "burned_percentage": 0.0, "last_burn_date": None,
                "days_since_last_burn": None, "burn_addresses_with_balance": []}
    status, freq = classify(transfers, burned, contract, pair_address, pair_created_at, now)
    last = max((t["ts"] for t in transfers), default=None)
    scale = 10 ** decimals
    return {**base, "is_burnable": True, "burn_status": status, "burn_frequency": freq,
            "total_burned_amount": burned / scale,
            "burned_percentage": round(min(100.0, max(0.0, burned / denom * 100)), 4) if denom else None,
            "last_burn_date": datetime.fromtimestamp(last, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if last else None,
            "days_since_last_burn": int((now - last) // 86400) if last else None,
            "burn_addresses_with_balance": [{"address": w, "amount": b / scale} for w, b in bals.items() if b]}


async def detect_token_burn(contract_address, chain_id, api_key=None, token_decimals=18,
                            pair_address=None, pair_created_at=None):
    """Burn data for one token (see module docstring). api_key defaults to
    ETHERSCAN_API_KEY; token_decimals is only a fallback (read on-chain)."""
    if api_key:
        os.environ.setdefault("ETHERSCAN_API_KEY", api_key)
    empty = {"is_burnable": None, "burn_status": None, "burn_frequency": None, "total_burned_amount": None,
             "burned_percentage": None, "last_burn_date": None, "days_since_last_burn": None,
             "burn_addresses_with_balance": [], "chain_id": chain_id, "contract": contract_address,
             "source": None, "error": None}
    try:
        if chain_id in ES_FREE:
            source, (bals, supply, dec, tr) = "etherscan", await _from_etherscan(contract_address, chain_id)
        elif chain_id in RPCS:
            source, (bals, supply, dec, tr) = "rpc", await asyncio.to_thread(_from_rpc_sync, contract_address, chain_id)
        else:
            return {**empty, "error": "unsupported chain"}
        return _summarize(contract_address, chain_id, source, bals, supply, token_decimals if dec is None else dec, tr,
                          pair_address, pair_created_at, time.time())
    except (BurnError, KeyError, ValueError, TypeError, AttributeError) as e:
        return {**empty, "error": str(e)[:120] if isinstance(e, BurnError) else f"bad response ({type(e).__name__})"}


# ---------------------------------------------------------------- storage / display
def main_pair(pairs, chain):
    """(pair address, created-at seconds) of the most liquid DexScreener pair on `chain`."""
    pairs = [p for p in pairs or [] if p.get("chainId") == chain]
    if not pairs:
        return None, None
    p = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    return p.get("pairAddress"), (p["pairCreatedAt"] // 1000 if p.get("pairCreatedAt") else None)


def store(coin, r, now):
    """Save a detect_token_burn result on the coin (an error leaves the old values)."""
    if r.get("error"):
        return False
    coin.burned_amount, coin.burned_pct = r["total_burned_amount"], r["burned_percentage"]
    coin.burn_status, coin.burn_frequency = r["burn_status"], r["burn_frequency"]
    coin.last_burn_at = (datetime.strptime(r["last_burn_date"], "%Y-%m-%dT%H:%M:%SZ")
                         if r["last_burn_date"] else None)
    coin.burn_checked_at = now
    return True


def display(amount, pct, status, frequency, last_burn_at, checked_at, source=None):
    """The Supply section's `burned` value (None = not checked / not EVM)."""
    if status is None:
        return None
    last = last_burn_at if isinstance(last_burn_at, datetime) or last_burn_at is None else \
        datetime.strptime(last_burn_at, "%Y-%m-%dT%H:%M:%SZ")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return {"amount": amount, "pct": pct, "status": status, "frequency": frequency,
            "last_burn_at": last.strftime("%Y-%m-%dT%H:%M:%SZ") if last else None,
            "days_since_last_burn": (now - last).days if last else None,
            "checked_at": checked_at.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(checked_at, datetime) else checked_at,
            "source": source}


def for_coin(coin):
    return display(coin.burned_amount, coin.burned_pct, coin.burn_status, coin.burn_frequency,
                   coin.last_burn_at, coin.burn_checked_at)


def check(contract, chain, pairs):
    """Check a Coin: live burn data for an EVM contract (None for other chains)."""
    cid = chain_id(chain)
    if cid is None:
        return None
    pair, created = main_pair(pairs, chain)
    r = asyncio.run(detect_token_burn(contract, cid, pair_address=pair, pair_created_at=created))
    if r["error"]:
        return None
    return display(r["total_burned_amount"], r["burned_percentage"], r["burn_status"], r["burn_frequency"],
                   r["last_burn_date"], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), r["source"])


def run_burn_update(session=None, statuses=("active",), concurrency=3):
    """Daily job / backfill: refresh burn data for every EVM coin with one
    of `statuses`. Returns {"checked", "stored", "errors", "burning"}."""
    from database import SessionLocal, utcnow
    from models import Coin
    from fetch_dexscreener import fetch_pairs_for_addresses
    own = session is None
    session = session or SessionLocal()
    stats = {"checked": 0, "stored": 0, "errors": 0, "burning": 0}
    try:
        coins = [c for c in session.query(Coin).filter(Coin.status.in_(statuses)).all() if chain_id(c.chain)]
        try:
            pairs = fetch_pairs_for_addresses([c.contract for c in coins])
        except Exception:
            pairs = []                                  # no pair -> buyback from the pool isn't detected
        by_token = {}
        for p in pairs:
            by_token.setdefault(((p.get("baseToken") or {}).get("address") or "").lower(), []).append(p)

        async def one(sem, c):
            async with sem:
                pair, created = main_pair(by_token.get(c.contract.lower()), c.chain)
                return c, await detect_token_burn(c.contract, chain_id(c.chain), pair_address=pair,
                                                  pair_created_at=created)

        async def all_coins():
            sem = asyncio.Semaphore(concurrency)
            return await asyncio.gather(*(one(sem, c) for c in coins))

        now = utcnow()
        for c, r in asyncio.run(all_coins()):
            stats["checked"] += 1
            if store(c, r, now):
                stats["stored"] += 1
                stats["burning"] += bool(r["is_burnable"])
            else:
                stats["errors"] += 1
                print(f"  burn check failed for {c.name} ({c.chain}): {r['error']}")
        session.commit()
    finally:
        if own:
            session.close()
    print(f"  burn data: {stats['stored']}/{stats['checked']} coins stored, "
          f"{stats['burning']} with burned supply, {stats['errors']} errors")
    return stats


if __name__ == "__main__":
    # One-time backfill: python burn_detector.py
    run_burn_update()
