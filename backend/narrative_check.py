"""
narrative_check.py
------------------
Gets a coin's REAL category/narrative from CoinGecko (e.g. "Meme", "DeFi",
"DePIN", "Launchpad", "Real World Assets (RWA)", "GameFi") using the token's
contract address.

The API key is read from backend/.env (never hard-coded) via python-dotenv.

Behaviour:
  * Look the coin up by contract address + platform.
  * Return the single most relevant CATEGORY as a short tag.
  * If CoinGecko doesn't list it, has no usable category, the chain isn't
    supported, or anything fails -> return None (the caller then falls back
    to the generic narrative sentence). Never raises.
  * Respects rate limits: a small delay before each live call + an on-disk
    cache so we never look the same token up twice.

Run it (tests a few coins):
    python narrative_check.py
"""

import os
import json
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

# Load the key from backend/.env (this file's own folder).
load_dotenv(Path(__file__).resolve().parent / ".env")
API_KEY = os.getenv("COINGECKO_API_KEY")

# Free/demo keys use the public host + the demo header.
BASE_URL = "https://api.coingecko.com/api/v3"

# Map our DexScreener chain names -> CoinGecko "asset platform" ids.
CHAIN_TO_PLATFORM = {
    "ethereum": "ethereum",
    "bsc": "binance-smart-chain",
    "base": "base",
    "solana": "solana",
    "polygon": "polygon-pos",
    "arbitrum": "arbitrum-one",
    "optimism": "optimistic-ethereum",
    "avalanche": "avalanche",
    "fantom": "fantom",
    "cronos": "cronos",
    "zksync": "zksync",
    "linea": "linea",
    "scroll": "scroll",
    "mantle": "mantle",
    "blast": "blast",
    "sui": "sui",
    "aptos": "aptos",
    "ton": "the-open-network",
    "tron": "tron",
}

# When a coin has several categories, prefer a real "narrative" over generic
# ones. First match (case-insensitive substring) wins.
PREFERRED = [
    "Launchpad",
    "Real World Assets (RWA)",
    "DePIN",
    "Artificial Intelligence (AI)",
    "GameFi",
    "Gaming",
    "Meme",
    "Decentralized Exchange (DEX)",
    "Liquid Staking",
    "Lending/Borrowing",
    "Oracle",
    "Bridge",
    "Decentralized Finance (DeFi)",
    "DeFi",
    "NFT",
    "Stablecoin",
]

# Seconds to wait before each LIVE call (cached lookups don't wait).
REQUEST_DELAY = 2.5

# Max live CoinGecko category lookups per daily run (display-only info; a coin
# not looked up yet shows the generic narrative and gets its turn later).
CATEGORY_BUDGET = 20

CACHE_FILE = Path(__file__).resolve().parent / "coingecko_cache.json"


def _load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


_CACHE = _load_cache()


def _save_cache():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_CACHE, f)
    except OSError:
        pass  # caching is best-effort; never break over it


def _pick_category(categories):
    """Choose the most relevant category from CoinGecko's list, or None."""
    # Drop blanks and chain/ecosystem tags ("Solana Ecosystem", etc.).
    cats = [c for c in categories if c and "ecosystem" not in c.lower()]
    if not cats:
        return None
    for pref in PREFERRED:
        for c in cats:
            if pref.lower() in c.lower():
                return c
    return cats[0]  # nothing preferred matched -> just take the first


def get_category(contract, chain):
    """
    Return a short category tag for the coin, or None to signal 'use the
    generic narrative instead'. Never raises.
    """
    return lookup_category(contract, chain)[0]


def lookup_category(contract, chain):
    """
    Like get_category, but also says whether the answer is FINAL. Returns
    (category_or_None, definitive, made_call):
      * definitive=True  -> CoinGecko really has this answer (a tag, or no
        usable category / not listed / chain not on CoinGecko) - safe to
        remember forever, never look up again.
      * definitive=False -> a transient failure (network, 429, bad status):
        try again on a later run.
      * made_call        -> a live CoinGecko request was made (for budgeting).
    Never raises.
    """
    platform = CHAIN_TO_PLATFORM.get((chain or "").lower())
    if not platform or not contract:
        return None, True, False  # chain not supported by CoinGecko -> final

    key = f"{platform}:{contract.lower()}"
    if key in _CACHE:
        return _CACHE[key], True, False  # cached definitive result (may be None)

    if not API_KEY:
        print("[WARN] No COINGECKO_API_KEY found in .env - skipping narrative.")
        return None, False, False

    # Be polite to the rate limiter before a real network call.
    time.sleep(REQUEST_DELAY)
    url = f"{BASE_URL}/coins/{platform}/contract/{contract}"
    try:
        r = requests.get(
            url,
            headers={"x-cg-demo-api-key": API_KEY, "User-Agent": "crypto-screener/0.1"},
            timeout=20,
        )
    except requests.exceptions.RequestException as e:
        print(f"[WARN] CoinGecko request failed: {e}")
        return None, False, True  # don't cache transient failures

    if r.status_code == 404:
        _CACHE[key] = None  # definitively not on CoinGecko
        _save_cache()
        return None, True, True
    if r.status_code == 429:
        print("[WARN] CoinGecko rate limit (429) - falling back this time.")
        return None, False, True  # don't cache; try again later
    if r.status_code != 200:
        print(f"[WARN] CoinGecko returned status {r.status_code}")
        return None, False, True

    try:
        data = r.json()
    except ValueError:
        return None, False, True

    category = _pick_category(data.get("categories") or [])
    _CACHE[key] = category  # cache the definitive result (tag or None)
    _save_cache()
    return category, True, True


# ---------------------------------------------------------------------------
# Manual test
# ---------------------------------------------------------------------------
def main():
    print(f"CoinGecko API key loaded: {bool(API_KEY)}\n")

    test_coins = [
        ("Uniswap", "ethereum", "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984"),
        ("Pepe", "ethereum", "0x6982508145454Ce325dDbE47a25d4ec3d2311933"),
        ("Jupiter", "solana", "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"),
        ("Render", "ethereum", "0x6De037ef9aD2725EB40118Bb1702EBb27e4Aeb24"),
        ("YesBlob (our coin)", "solana", "VzqFToxrHqs7BFtfWvwmGAn6nv7p9W3zdmW95iTpump"),
        ("When (robinhood - unsupported)", "robinhood", "0x0000000000000000000000000000000000000000"),
    ]

    print("=" * 70)
    print("  COINGECKO NARRATIVE / CATEGORY CHECK")
    print("=" * 70)
    for label, chain, contract in test_coins:
        cat = get_category(contract, chain)
        shown = cat if cat else "(none -> fall back to generic sentence)"
        print(f"{label:<34} [{chain}] -> {shown}")


if __name__ == "__main__":
    main()
