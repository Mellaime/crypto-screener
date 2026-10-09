"""
socials.py
----------
A coin's X / Twitter link, shown on the detail page and in Check a Coin.
DISPLAY ONLY - the filter and drop_logic never use it.

Source: CoinGecko coin data (by contract), field links.twitter_screen_name.
FALLBACK: if CoinGecko has no handle, CoinMarketCap /v2/cryptocurrency/info
(by contract; urls.twitter) - only for those coins, matched on the same
contract AND chain. The URL is https://x.com/<handle>. No handle on either
-> the page shows "Unknown" (plain text, not a link).

Where the handle comes from, cheapest first:
  1. remember(contract, data) - called wherever the daily run ALREADY fetches
     a coin's full CoinGecko data (narrative_check.lookup_category for new
     coins / category catch-up, rank_check.get_coingecko_rank). No extra call.
  2. refresh_twitter(session, coins) - the daily tracking step: every tracked
     coin whose handle was never checked (or was checked more than
     RECHECK_DAYS ago) takes it from (1) if this run saw it, else gets ONE
     CoinGecko call - at most TWITTER_BUDGET per run. If CoinGecko has no
     handle, ONE CoinMarketCap info call (1 credit) - at most CMC_BUDGET.
Stored on the coin: coins.twitter_handle (None = no handle) and
coins.twitter_checked_at (None = never checked).
"""

import re
import time
from datetime import timedelta

import requests

TWITTER_BUDGET = 20        # live CoinGecko calls per daily run for handles
CMC_BUDGET = 20            # CoinMarketCap fallback calls per daily run
RECHECK_DAYS = 30          # a stored handle (or "none") is re-checked after this
_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")      # what X allows
_X_URL = re.compile(r"^https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/@?([^/?#]+)/?(?:[?#].*)?$", re.I)
# Our chain -> words in CoinMarketCap's platform name ("BNB Smart Chain (BEP20)").
CMC_CHAIN_WORDS = {
    "ethereum": "ethereum", "bsc": "bnb smart chain", "base": "base", "solana": "solana",
    "polygon": "polygon", "arbitrum": "arbitrum", "optimism": "optimism",
    "avalanche": "avalanche", "sui": "sui", "aptos": "aptos", "tron": "tron",
    "fantom": "fantom", "cronos": "cronos", "linea": "linea", "scroll": "scroll",
    "mantle": "mantle", "blast": "blast", "zksync": "zksync", "ton": "toncoin",
}

# contract_lower -> handle or None, from CoinGecko data seen in this process.
SEEN = {}


def handle_from(data):
    """The valid X handle in a CoinGecko coin response, or None."""
    h = (((data or {}).get("links") or {}).get("twitter_screen_name") or "").strip().lstrip("@")
    return h if _HANDLE.match(h) else None


def handle_from_url(url):
    """The valid X handle in a twitter.com / x.com profile URL, or None."""
    m = _X_URL.match((url or "").strip())
    h = m.group(1) if m else None
    return h if h and _HANDLE.match(h) else None


def _cmc_entries(info):
    """CMC info `data` (dict of id -> entry or [entries]) or a single entry -> entries."""
    if isinstance(info, dict) and "id" in info:
        return [info]
    out = []
    for v in (info or {}).values():
        out.extend(v if isinstance(v, list) else [v])
    return [e for e in out if isinstance(e, dict)]


def handle_from_cmc(info, contract, chain):
    """The X handle from CoinMarketCap info data - only from an entry that
    lists THIS contract on THIS chain (contract + chain match)."""
    word = CMC_CHAIN_WORDS.get((chain or "").lower())
    for e in _cmc_entries(info):
        listed = [(((p.get("platform") or {}).get("name") or "").lower(), (p.get("contract_address") or "").lower())
                  for p in (e.get("contract_address") or [])]
        if not any(addr == (contract or "").lower() and (word is None or word in name)
                   for name, addr in listed):
            continue
        for url in ((e.get("urls") or {}).get("twitter") or []):
            h = handle_from_url(url)
            if h:
                return h
    return None


def fetch_cmc_handle(contract, chain):
    """(handle_or_None, definitive). One CoinMarketCap info call (1 credit)."""
    from age_check import CMC_KEY, CMC_INFO_URL
    if not contract or not CMC_KEY:
        return None, False
    try:
        r = requests.get(CMC_INFO_URL,
                         params={"address": contract.lower() if contract.startswith("0x") else contract},
                         headers={"X-CMC_PRO_API_KEY": CMC_KEY, "Accept": "application/json"}, timeout=20)
    except requests.exceptions.RequestException:
        return None, False
    if r.status_code == 429 or r.status_code >= 500:
        return None, False
    if r.status_code != 200:
        return None, True                        # CMC doesn't know this contract
    try:
        return handle_from_cmc((r.json() or {}).get("data"), contract, chain), True
    except ValueError:
        return None, False


def x_url(handle):
    return f"https://x.com/{handle}" if handle and _HANDLE.match(handle) else None


def remember(contract, data):
    """Note the handle from CoinGecko data the run fetched anyway (free)."""
    if contract and isinstance(data, dict):
        SEEN[contract.lower()] = handle_from(data)


def fetch_handle(contract, chain):
    """(handle_or_None, definitive). One CoinGecko call. definitive=False on a
    network error / rate limit (then nothing is stored - try next run)."""
    from age_check import CHAIN_TO_CG_PLATFORM
    from discovery import CG_KEY
    platform = CHAIN_TO_CG_PLATFORM.get((chain or "").lower())
    if not platform or not contract or not CG_KEY:
        return None, bool(not platform)          # chain not on CoinGecko -> none, final
    time.sleep(2.5)
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/coins/{platform}/contract/{contract}",
                         params={"localization": "false", "tickers": "false", "market_data": "false",
                                 "community_data": "false", "developer_data": "false", "sparkline": "false"},
                         headers={"x-cg-demo-api-key": CG_KEY}, timeout=20)
    except requests.exceptions.RequestException:
        return None, False
    if r.status_code == 404:
        return None, True                        # not on CoinGecko -> no handle
    if r.status_code != 200:
        return None, False
    try:
        data = r.json()
    except ValueError:
        return None, False
    remember(contract, data)
    return handle_from(data), True


def refresh_twitter(session, coins, now, budget=TWITTER_BUDGET, cmc_budget=CMC_BUDGET, counts=None):
    """Fill / refresh coins.twitter_handle: CoinGecko first, CoinMarketCap only
    when CoinGecko has none. Returns live CoinGecko calls made; `counts` (a
    dict) also gets "cmc_calls" and "from_cmc"."""
    counts = counts if counts is not None else {}
    counts.setdefault("cmc_calls", 0)
    counts.setdefault("from_cmc", 0)
    calls = 0
    for coin in coins:
        checked = coin.twitter_checked_at
        if checked is not None and checked > now - timedelta(days=RECHECK_DAYS):
            continue
        key = (coin.contract or "").lower()
        if key in SEEN:                          # seen in this run's CoinGecko data
            handle, definitive = SEEN[key], True
        elif calls < budget:
            calls += 1
            handle, definitive = fetch_handle(coin.contract, coin.chain)
        else:
            continue
        if not definitive:
            continue                             # CoinGecko hiccup -> next run
        if handle is None:                       # CoinGecko has none -> CoinMarketCap
            if counts["cmc_calls"] >= cmc_budget:
                continue                         # try again next run
            counts["cmc_calls"] += 1
            handle, definitive = fetch_cmc_handle(coin.contract, coin.chain)
            if not definitive:
                continue
            counts["from_cmc"] += bool(handle)
        coin.twitter_handle, coin.twitter_checked_at = handle, now
    return calls
