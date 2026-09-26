"""
age_check.py
------------
Filter 2 - AGE, matched strictly by CONTRACT + CHAIN (never by name).

A coin must be at least MIN_AGE_DAYS old; brand-new coins are too risky
(likely scams / rugs). This check runs FIRST - before every other filter - so
anything too new, or whose age can't be established anywhere, is dropped early.

Two steps:

  Step 1 - DexScreener `pairCreatedAt` (already enriched onto the token as
  `age_days` by fetch_dexscreener.build_token_records):
      age <  MIN_AGE_DAYS   -> DROP
      age >= MIN_AGE_DAYS    -> KEEP  (done)
      unknown (no pair timestamp) -> go to Step 2

  Step 2 - fallback listing/genesis date, ONLY when DexScreener had none.
  CoinMarketCap `date_added` (tried first - one fast call), else CoinGecko
  `genesis_date`:
      age <  MIN_AGE_DAYS   -> DROP
      age >= MIN_AGE_DAYS    -> KEEP
      unknown on BOTH        -> DROP

decide_age(token) -> (keep: bool, age_days: int | None, source: str)
    source is "dexscreener", "fallback", or "unknown".
Never raises; any network / parse error degrades to "unknown".
"""

import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

CG_KEY = os.getenv("COINGECKO_API_KEY")
CMC_KEY = os.getenv("COINMARKETCAP_API_KEY")

# A coin must be at least this many days old to pass.
MIN_AGE_DAYS = 10

CG_CONTRACT_URL = "https://api.coingecko.com/api/v3/coins/{platform}/contract/{contract}"
CMC_INFO_URL = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/info"

CG_DELAY = 2.5   # be gentle with CoinGecko's demo rate limit

# Our chain name -> CoinGecko asset-platform id (reverse of discovery's map).
CHAIN_TO_CG_PLATFORM = {
    "ethereum": "ethereum", "solana": "solana", "base": "base",
    "bsc": "binance-smart-chain", "polygon": "polygon-pos",
    "arbitrum": "arbitrum-one", "optimism": "optimistic-ethereum",
    "avalanche": "avalanche", "fantom": "fantom", "cronos": "cronos",
    "zksync": "zksync", "linea": "linea", "scroll": "scroll",
    "mantle": "mantle", "blast": "blast", "sui": "sui", "aptos": "aptos",
    "ton": "the-open-network", "tron": "tron",
}

# Per-process cache: look up each coin's fallback age at most once per run.
_memo = {}


def _age_days_from(dt):
    """Whole days between `dt` and now (UTC), or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def _parse_iso(s):
    """Parse an ISO8601 timestamp or bare 'YYYY-MM-DD' into a UTC datetime."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _cmc_listing_date(contract):
    """CoinMarketCap `date_added` for a contract, as a datetime, or None."""
    if not contract or not CMC_KEY:
        return None
    try:
        r = requests.get(
            CMC_INFO_URL,
            # CMC's info endpoint 400s on a checksum-cased address ("slug null")
            # but accepts a lowercased one - so always send it lowercase.
            params={"address": contract.lower()},
            headers={"X-CMC_PRO_API_KEY": CMC_KEY, "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        data = (r.json() or {}).get("data") or {}
    except (requests.exceptions.RequestException, ValueError):
        return None
    # `data` is keyed by CMC id; each value may be an object or a list of them.
    dates = []
    for value in data.values():
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            dt = _parse_iso((entry or {}).get("date_added"))
            if dt:
                dates.append(dt)
    return min(dates) if dates else None   # earliest listing


def _cg_genesis_date(contract, chain):
    """CoinGecko `genesis_date` for a contract, as a datetime, or None."""
    platform = CHAIN_TO_CG_PLATFORM.get((chain or "").lower())
    if not platform or not contract or not CG_KEY:
        return None
    time.sleep(CG_DELAY)
    url = CG_CONTRACT_URL.format(platform=platform, contract=contract)
    try:
        r = requests.get(url, headers={"x-cg-demo-api-key": CG_KEY}, timeout=20)
        if r.status_code != 200:
            return None
        return _parse_iso(r.json().get("genesis_date"))
    except (requests.exceptions.RequestException, ValueError):
        return None


def _fallback_age(contract, chain):
    """
    Age (days) from the listing/genesis date, or None if unknown.
    CoinMarketCap `date_added` is tried first (one fast call); only if it has
    nothing do we consult CoinGecko `genesis_date` (which is rate-limited and
    usually null for tokens anyway).
    """
    key = ((chain or "").lower(), (contract or "").lower())
    if key in _memo:
        return _memo[key]
    dt = _cmc_listing_date(contract)
    if dt is None:
        dt = _cg_genesis_date(contract, chain)
    age = _age_days_from(dt)
    _memo[key] = age
    return age


def decide_age(token):
    """
    Two-step age decision for one enriched token record.

    Returns (keep, age_days, source):
      * keep      - True if age >= MIN_AGE_DAYS (else False, i.e. drop).
      * age_days  - the age used for the decision, or None if unknown.
      * source    - "dexscreener", "fallback" (CoinGecko/CMC), or "unknown".
    """
    # Step 1 - DexScreener pairCreatedAt (enriched as age_days).
    age = token.get("age_days")
    if age is not None:
        return (age >= MIN_AGE_DAYS, age, "dexscreener")

    # Step 2 - CoinMarketCap / CoinGecko listing date (only if Step 1 unknown).
    age = _fallback_age(token.get("contract"), token.get("chain"))
    if age is not None:
        return (age >= MIN_AGE_DAYS, age, "fallback")

    # Unknown on every source -> drop.
    return (False, None, "unknown")


# ---------------------------------------------------------------------------
# Manual test: prove both steps on known coins.
# ---------------------------------------------------------------------------
def main():
    samples = [
        # (label, token dict) - age_days=None forces the CG/CMC fallback.
        ("Pepe (old, via fallback)",
         {"contract": "0x6982508145454Ce325dDbE47a25d4ec3d2311933",
          "chain": "ethereum", "age_days": None}),
        ("Uniswap (old, via fallback)",
         {"contract": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
          "chain": "ethereum", "age_days": None}),
        ("DexScreener says 3d (too new)",
         {"contract": "0xabc", "chain": "ethereum", "age_days": 3}),
        ("DexScreener says 42d (old enough)",
         {"contract": "0xdef", "chain": "ethereum", "age_days": 42}),
    ]
    print("=" * 62)
    print("  AGE CHECK (two-step: DexScreener -> CoinMarketCap/CoinGecko)")
    print("=" * 62)
    for label, tok in samples:
        keep, age, source = decide_age(tok)
        verdict = "KEEP" if keep else "DROP"
        age_str = "unknown" if age is None else f"{age}d"
        print(f"  {label:<34} -> {verdict:<4} ({age_str}, {source})")
    print("=" * 62)


if __name__ == "__main__":
    main()
