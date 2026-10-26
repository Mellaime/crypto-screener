"""
burn_detector.py
----------------
Burned supply for EVM and Solana coins (detail page / Check a Coin "Burned" row).

Solana (Helius, HELIUS_API_KEY, max 2 calls per token): getAsset (supply,
decimals, mint / freeze authority) + ONE page of type=BURN transactions.
Helius only searches a recent window, so the page is a recent sample: the
total comes from max_supply - current_supply when the mint authority is
revoked (A/B), else the sampled burns (A), else "Unknown" when the page is
full (C - the sample is stored, % stays NULL).

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
DUST_PCT = 0.01                      # burned < 0.01% of supply = NO_BURN (accidental sends / account closes)
MICRO_BURN_PCT = 0.0001              # Solana: a single burn < 0.0001% of supply is ignored for classification
HELIUS_RPC = "https://mainnet.helius-rpc.com/"
HELIUS_TXS = "https://api.helius.xyz/v0/addresses/{mint}/transactions"
HELIUS_GAP = 0.25                    # seconds between Helius calls
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
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
_es_slot, _helius_slot = [threading.Lock(), 0.0], [threading.Lock(), 0.0]


def _space(slot, gap):
    """Wait so calls sharing `slot` are at least `gap` seconds apart (all threads)."""
    with slot[0]:
        wait = slot[1] + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        slot[1] = time.monotonic()


def _es_get(cid, **params):
    """One Etherscan V2 call, spaced ES_GAP apart across all threads, retried
    on rate limits. Errors never carry the URL (it holds the API key)."""
    key = os.getenv("ETHERSCAN_API_KEY")
    if not key:
        raise BurnError("ETHERSCAN_API_KEY not set")
    for attempt in range(4):
        _space(_es_slot, ES_GAP)
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


def classify(transfers, burned, contract, pair_address=None, pair_created_at=None, now=None, buyback=None):
    """(burn_status, burn_frequency) from incoming burn transfers (newest
    first). buyback=None: a burn from the pair / token contract is a buyback."""
    if not transfers:
        return ("STATIC_PRE_BURN", "ONE_TIME") if burned else ("NO_BURN", "NONE")
    now = now or time.time()
    times = sorted((t["ts"] for t in transfers), reverse=True)
    freq, days = _frequency(times), (now - times[0]) / 86400
    if buyback is None:
        buyback = bool({t["from"] for t in transfers} & {(pair_address or "").lower(), contract.lower()} - {""})
    if buyback:
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


# ---------------------------------------------------------------- Solana (Helius, 2 calls per token)
def _helius(method, url, **kw):
    """One Helius call, spaced HELIUS_GAP apart, retried on 429. A 404 (no
    events in Helius's search window) returns None. Errors never carry the URL."""
    key = os.getenv("HELIUS_API_KEY")
    if not key:
        raise BurnError("HELIUS_API_KEY not set")
    params = {"api-key": key, **kw.pop("params", {})}
    for attempt in range(4):
        _space(_helius_slot, HELIUS_GAP)
        try:
            r = requests.request(method, url, params=params, timeout=30, **kw)
        except Exception as e:
            raise BurnError(f"Helius request failed ({type(e).__name__})") from None
        if r.status_code == 429:
            time.sleep(1.0 * (attempt + 1))
            continue
        if r.status_code == 404:
            return None
        try:
            return r.json()
        except ValueError:
            raise BurnError(f"Helius: HTTP {r.status_code}") from None
    raise BurnError("Helius: rate limited")


def _solana_burns(mint, txs):
    """Burns of `mint` in Helius BURN transactions (other mints / LP tokens in
    the same tx ignored): [{"ts", "value", "buyback"}], newest first."""
    out = []
    for t in txs:
        if t.get("transactionError"):
            continue
        tt = t.get("tokenTransfers") or []
        amount = sum(x.get("tokenAmount") or 0 for x in tt if x.get("mint") == mint
                     and (not x.get("toTokenAccount") or x.get("toUserAccount") == INCINERATOR))
        if amount <= 0:
            continue
        payer = t.get("feePayer")
        # buyback: a swap event, or the burner received the token from someone else in the same tx
        bought = bool((t.get("events") or {}).get("swap")) or any(
            x.get("mint") == mint and x.get("toUserAccount") == payer and x.get("fromUserAccount") not in (payer, "", None)
            for x in tt)
        out.append({"ts": t["timestamp"], "value": amount, "buyback": bought})
    return out


def _solana_summary(mint, current, burns, raw_count, max_supply, mint_off, freeze_off, pair_created_at, now):
    """Pure: the Solana result dict (cases A / B / C, dust rule, classification)."""
    sampled = sum(b["value"] for b in burns)
    base = {"network": "Solana", "contract": mint, "error": None, "current_supply": current,
            "recent_sampled_burned_amount": sampled or None, "mint_authority_disabled": mint_off,
            "freeze_authority_disabled": freeze_off, "is_safe_from_manipulation": mint_off and freeze_off,
            "last_burn_date": None, "source": "helius"}
    none = {**base, "is_burnable": False, "burn_status": "NO_BURN", "burn_frequency": "NONE",
            "total_burned_amount": 0.0, "burned_percentage": 0.0}
    truncated = raw_count >= 100                       # one page only: older burns may be missing
    # max - current is burned only when nothing more can be minted (else it's unminted supply)
    gap = max_supply - current if max_supply and mint_off and max_supply > current else 0
    if gap and (truncated or gap > sampled):           # case B (or A with an older burn outside the window)
        total, pct = gap, gap / max_supply * 100
    elif truncated:                                    # case C: don't guess the full history
        if sampled / (current + sampled) * 100 < DUST_PCT:
            return none                                # 100 account-close dust burns, no real burn seen
        total = pct = "Unknown"
    elif sampled:                                      # case A
        total, pct = sampled, sampled / (current + sampled) * 100
    else:
        return none
    if pct != "Unknown" and pct < DUST_PCT:
        return none
    big = [b for b in burns if b["value"] / (current + sampled) * 100 >= MICRO_BURN_PCT]
    status, freq = classify([{"from": "", "ts": b["ts"], "value": b["value"]} for b in big], 1, mint,
                            pair_created_at=pair_created_at, now=now, buyback=any(b["buyback"] for b in big))
    last = max((b["ts"] for b in big), default=None)
    return {**base, "is_burnable": True, "burn_status": status, "burn_frequency": freq,
            "total_burned_amount": total, "burned_percentage": pct if pct == "Unknown" else round(min(100.0, pct), 4),
            "last_burn_date": datetime.fromtimestamp(last, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if last else None}


def _solana_sync(mint, max_supply, pair_created_at):
    a = _helius("POST", HELIUS_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": {"id": mint}})
    ti = ((a or {}).get("result") or {}).get("token_info") or {}
    if ti.get("supply") is None or ti.get("decimals") is None:
        raise BurnError("Helius: no SPL token info for this mint")
    current = ti["supply"] / 10 ** ti["decimals"]
    txs = _helius("GET", HELIUS_TXS.format(mint=mint), params={"type": "BURN", "limit": 100}) or []
    if not isinstance(txs, list):
        raise BurnError(f"Helius: {str(txs.get('error') if isinstance(txs, dict) else txs)[:80]}")
    return _solana_summary(mint, current, _solana_burns(mint, txs), len(txs), max_supply,
                           ti.get("mint_authority") is None, ti.get("freeze_authority") is None,
                           pair_created_at, time.time())


async def detect_solana_burn(mint, max_supply=None, total_supply=None, pair_created_at=None):
    """Burn data for one SPL token: Helius getAsset + one page of BURN
    transactions (max 2 calls). total_supply is unused (on-chain supply wins)."""
    try:
        return await asyncio.to_thread(_solana_sync, mint, max_supply, pair_created_at)
    except (BurnError, KeyError, ValueError, TypeError, AttributeError, ZeroDivisionError) as e:
        return {"is_burnable": None, "burn_status": None, "burn_frequency": None, "total_burned_amount": None,
                "burned_percentage": None, "recent_sampled_burned_amount": None, "last_burn_date": None,
                "mint_authority_disabled": None, "freeze_authority_disabled": None, "is_safe_from_manipulation": None,
                "current_supply": None, "network": "Solana", "contract": mint, "source": "helius",
                "error": str(e)[:120] if isinstance(e, BurnError) else f"bad response ({type(e).__name__})"}


def supported(chain):
    return chain_id(chain) is not None or (chain or "").lower() == "solana"


async def detect(contract, chain, pair_address=None, pair_created_at=None, max_supply=None):
    """EVM or Solana, by chain."""
    if (chain or "").lower() == "solana":
        return await detect_solana_burn(contract, max_supply, pair_created_at=pair_created_at)
    return await detect_token_burn(contract, chain_id(chain), pair_address=pair_address, pair_created_at=pair_created_at)


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
    unknown = r["total_burned_amount"] == "Unknown"    # Solana case C: store the sample, % stays NULL
    coin.burned_amount = r["recent_sampled_burned_amount"] if unknown else r["total_burned_amount"]
    coin.burned_pct = None if unknown else r["burned_percentage"]
    coin.burn_status, coin.burn_frequency = r["burn_status"], r["burn_frequency"]
    coin.last_burn_at = (datetime.strptime(r["last_burn_date"], "%Y-%m-%dT%H:%M:%SZ")
                         if r["last_burn_date"] else None)
    coin.burn_checked_at = now
    return True


def display(amount, pct, status, frequency, last_burn_at, checked_at, source=None):
    """The Supply section's `burned` value (None = not checked / unsupported
    chain). pct None with a burn status = only a recent sample is known."""
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


def check(contract, chain, pairs, max_supply=None):
    """Check a Coin: live burn data for an EVM or Solana token (None for other chains)."""
    if not supported(chain):
        return None
    pair, created = main_pair(pairs, chain)
    r = asyncio.run(detect(contract, chain, pair, created, max_supply))
    if r["error"]:
        return None
    unknown = r["total_burned_amount"] == "Unknown"
    return display(r["recent_sampled_burned_amount"] if unknown else r["total_burned_amount"],
                   None if unknown else r["burned_percentage"], r["burn_status"], r["burn_frequency"],
                   r["last_burn_date"], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), r["source"])


def run_burn_update(session=None, statuses=("active",), concurrency=3):
    """Daily job / backfill: refresh burn data for every EVM / Solana coin
    with one of `statuses`. Returns {"checked", "stored", "errors", "burning"}."""
    from database import SessionLocal, utcnow
    from models import Coin
    from fetch_dexscreener import fetch_pairs_for_addresses
    own = session is None
    session = session or SessionLocal()
    stats = {"checked": 0, "stored": 0, "errors": 0, "burning": 0}
    try:
        coins = [c for c in session.query(Coin).filter(Coin.status.in_(statuses)).all() if supported(c.chain)]
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
                return c, await detect(c.contract, c.chain, pair, created, c.max_supply)

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
