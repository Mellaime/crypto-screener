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

  Step 2 - fallback listing date, ONLY when DexScreener had none
  (no pool timestamp, OR the coin has no DexScreener listing at all - such
  coins still reach this step; a missing DexScreener listing is NOT a drop):
    a) CoinMarketCap `date_added` from the listings call discovery already
       made (token["listed_at"]) - free, no extra call
    b) else CoinMarketCap /info `date_added` (1 call). The answer is stored in the age_lookups table, so a coin
       costs these calls ONCE (a "no date anywhere" answer is re-asked after
       NO_DATE_RETRY_DAYS). At most FALLBACK_BUDGET coins per run use (b);
       the rest wait for the next run (counted as "deferred", not dropped).
      age <  MIN_AGE_DAYS   -> DROP
      age >= MIN_AGE_DAYS    -> KEEP
      unknown on all three   -> DROP
  (CoinGecko genesis_date is NOT used: it had a date for only ~2% of these
  coins and cost a CoinGecko call each.)

decide_age(token, session=None) -> (keep: bool, age_days: int | None, source: str)
    source is "dexscreener", "fallback", "deferred" or "unknown".
Never raises; any network / parse error degrades to "unknown" (and a
transient error is not cached, so it's retried next run).
"""

import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

CMC_KEY = os.getenv("COINMARKETCAP_API_KEY")

# A coin must be at least this many days old to pass.
MIN_AGE_DAYS = 10

CMC_INFO_URL = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/info"


# Our chain name -> CoinGecko asset-platform id (reverse of discovery's map).
# Not used by the age check any more - volume_check imports it from here.
CHAIN_TO_CG_PLATFORM = {
    "ethereum": "ethereum", "solana": "solana", "base": "base",
    "bsc": "binance-smart-chain", "polygon": "polygon-pos",
    "arbitrum": "arbitrum-one", "optimism": "optimistic-ethereum",
    "avalanche": "avalanche", "fantom": "fantom", "cronos": "cronos",
    "zksync": "zksync", "linea": "linea", "scroll": "scroll",
    "mantle": "mantle", "blast": "blast", "sui": "sui", "aptos": "aptos",
    "ton": "the-open-network", "tron": "tron",
}

# Coins per run that may spend a live fallback call (CMC /info).
# Answers are cached in age_lookups, so this only paces the backlog.
FALLBACK_BUDGET = 150
# A cached "no date anywhere" answer is asked again after this many days.
NO_DATE_RETRY_DAYS = 30

# Per-process cache: look up each coin's fallback date at most once per run.
_memo = {}
# Live fallback lookups made this run (counts against FALLBACK_BUDGET).
LOOKUPS = 0


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
    """(CoinMarketCap `date_added` as a datetime or None, definitive). Not
    definitive = network error / rate limit / server error -> ask again."""
    if not contract or not CMC_KEY:
        return None, True
    try:
        r = requests.get(
            CMC_INFO_URL,
            # CMC's info endpoint 400s on a checksum-cased address ("slug null")
            # but accepts a lowercased one - so always send it lowercase.
            params={"address": contract.lower()},
            headers={"X-CMC_PRO_API_KEY": CMC_KEY, "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code == 429 or r.status_code >= 500:
            return None, False
        if r.status_code != 200:
            return None, True               # CMC doesn't know this contract
        data = (r.json() or {}).get("data") or {}
    except (requests.exceptions.RequestException, ValueError):
        return None, False
    # `data` is keyed by CMC id; each value may be an object or a list of them.
    dates = []
    for value in data.values():
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            dt = _parse_iso((entry or {}).get("date_added"))
            if dt:
                dates.append(dt)
    return (min(dates) if dates else None), True   # earliest listing


def _naive_utc(dt):
    if dt is not None and dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _fallback_date(contract, chain, session=None):
    """
    Listing date from CMC /info - via the age_lookups
    cache when a DB session is given. Returns (datetime | None, status):
    status "ok" (a date, or a definitive "CMC has none") or "deferred"
    (budget used up this run, or a transient error -> try again next run).
    """
    global LOOKUPS
    key = ((chain or "").lower(), (contract or "").lower())
    if key in _memo:
        return _memo[key]

    row = None
    if session is not None:
        from models import AgeLookup
        row = session.query(AgeLookup).filter_by(chain=key[0], contract=key[1]).first()
        if row is not None:
            fresh = row.checked_at >= _naive_utc(datetime.now(timezone.utc)) - timedelta(
                days=NO_DATE_RETRY_DAYS)
            if row.listed_at is not None or fresh:
                _memo[key] = (row.listed_at, "ok")       # cached - no calls
                return _memo[key]

    if LOOKUPS >= FALLBACK_BUDGET:
        return None, "deferred"
    LOOKUPS += 1
    dt, ok = _cmc_listing_date(contract)
    if dt is None and not ok:
        return None, "deferred"                          # transient -> not cached

    dt = _naive_utc(dt)
    if session is not None:
        from models import AgeLookup
        if row is None:
            row = AgeLookup(chain=key[0], contract=key[1])
            session.add(row)
        row.listed_at = dt
        row.checked_at = _naive_utc(datetime.now(timezone.utc))
    _memo[key] = (dt, "ok")
    return _memo[key]


def _fallback_age(contract, chain, session=None):
    """Age (days) from the CMC listing date, or None if unknown."""
    return _age_days_from(_fallback_date(contract, chain, session)[0])


def decide_age(token, session=None):
    """
    Two-step age decision for one token record (with or without DexScreener
    data).

    Returns (keep, age_days, source):
      * keep      - True if age >= MIN_AGE_DAYS (else False, i.e. drop).
      * age_days  - the age used for the decision, or None if unknown.
      * source    - "dexscreener", "fallback" (CMC), "deferred"
                    (fallback lookup postponed to the next run) or "unknown".
    """
    # Step 1 - DexScreener pairCreatedAt (enriched as age_days).
    age = token.get("age_days")
    if age is not None:
        return (age >= MIN_AGE_DAYS, age, "dexscreener")

    # Step 2a - CMC date_added from discovery's listings call (free).
    age = _age_days_from(token.get("listed_at"))
    if age is not None:
        return (age >= MIN_AGE_DAYS, age, "fallback")

    # Step 2b - CMC /info date_added (cached in age_lookups).
    dt, status = _fallback_date(token.get("contract"), token.get("chain"), session)
    if status == "deferred":
        return (False, None, "deferred")
    age = _age_days_from(dt)
    if age is not None:
        return (age >= MIN_AGE_DAYS, age, "fallback")

    # Unknown on all three sources -> drop.
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
    print("  AGE CHECK (DexScreener -> CMC listing date -> CMC /info)")
    print("=" * 62)
    for label, tok in samples:
        keep, age, source = decide_age(tok)
        verdict = "KEEP" if keep else "DROP"
        age_str = "unknown" if age is None else f"{age}d"
        print(f"  {label:<34} -> {verdict:<4} ({age_str}, {source})")
    print("=" * 62)


if __name__ == "__main__":
    main()
