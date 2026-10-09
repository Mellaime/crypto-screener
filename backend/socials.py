"""
socials.py
----------
A coin's X / Twitter link, shown on the detail page and in Check a Coin.
DISPLAY ONLY - the filter and drop_logic never use it.

Source: CoinGecko coin data (by contract), field links.twitter_screen_name.
The URL is https://x.com/<handle>. No handle on CoinGecko -> the page shows
"Unknown" (plain text, not a link).

Where the handle comes from, cheapest first:
  1. remember(contract, data) - called wherever the daily run ALREADY fetches
     a coin's full CoinGecko data (narrative_check.lookup_category for new
     coins / category catch-up, rank_check.get_coingecko_rank). No extra call.
  2. refresh_twitter(session, coins) - the daily tracking step: every tracked
     coin whose handle was never checked (or was checked more than
     RECHECK_DAYS ago) takes it from (1) if this run saw it, else gets ONE
     CoinGecko call - at most TWITTER_BUDGET per run.
Stored on the coin: coins.twitter_handle (None = no handle) and
coins.twitter_checked_at (None = never checked).
"""

import re
import time
from datetime import timedelta

import requests

TWITTER_BUDGET = 20        # live CoinGecko calls per daily run for handles
RECHECK_DAYS = 30          # a stored handle (or "none") is re-checked after this
_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")      # what X allows

# contract_lower -> handle or None, from CoinGecko data seen in this process.
SEEN = {}


def handle_from(data):
    """The valid X handle in a CoinGecko coin response, or None."""
    h = (((data or {}).get("links") or {}).get("twitter_screen_name") or "").strip().lstrip("@")
    return h if _HANDLE.match(h) else None


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


def refresh_twitter(session, coins, now, budget=TWITTER_BUDGET):
    """Fill / refresh coins.twitter_handle. Returns live CoinGecko calls made."""
    calls = 0
    for coin in coins:
        checked = coin.twitter_checked_at
        if checked is not None and checked > now - timedelta(days=RECHECK_DAYS):
            continue
        key = (coin.contract or "").lower()
        if key in SEEN:                          # seen in this run's CoinGecko data
            coin.twitter_handle, coin.twitter_checked_at = SEEN[key], now
            continue
        if calls >= budget:
            continue
        calls += 1
        handle, definitive = fetch_handle(coin.contract, coin.chain)
        if definitive:
            coin.twitter_handle, coin.twitter_checked_at = handle, now
    return calls
