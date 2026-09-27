"""
meme_filter.py
--------------
Early MEME filter for the DISCOVERY step. Meme coins are removed right after
discovery and BEFORE the Age / Volume / ... filter chain, so we never spend
per-coin API calls on coins we don't want.

Two signals (checked by CONTRACT + CHAIN). If ANY fires -> DROP; none -> KEEP:
  1. CATEGORY  - the coin is in CoinGecko's "meme" category. We fetch that
     whole category ONCE per run in BULK (fetch_cg_meme_keys) and build a set
     of {(chain, contract)} - so the category check is a free set lookup, not
     a per-coin API call.
  2. TAG       - a CoinMarketCap tag contains "meme" (e.g. "memes",
     "animal-memes"). CMC tags come from the listings data discovery already
     fetches - no extra call.

(These two signals caught 100% of test memes with zero false positives, so the
earlier free-text "description" signal was dropped as redundant.)

Unknown / no data never drops a coin (same "only drop on a positive signal"
policy used by the revenue and security checks).

Public API:
  fetch_cg_meme_keys() -> set of (chain, contract_lower)   [call once per run]
  is_meme(contract, chain, cg_meme_keys, cmc_tags=None) -> dict
Never raises.
"""

import os
import time

from dotenv import load_dotenv

# Reuse discovery's CoinGecko helpers + maps (no duplication, no extra config).
from discovery import _get, CG_MARKETS_URL, CG_PLATFORM_TO_CHAIN, _cg_id_to_platforms

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
CG_KEY = os.getenv("COINGECKO_API_KEY")

CG_DELAY = 2.5
MIN_MCAP = 1_000_000        # match discovery's window: memes below this can't
                            # be candidates anyway, so we stop the bulk pull here.

# CoinGecko's umbrella meme category slug (its /coins/markets `category` filter).
CG_MEME_CATEGORY = "meme-token"

# Meme keywords - plain, easy to extend. Matched case-insensitively as
# substrings, so "meme" already covers "Memes", "animal-memes", "Meme Coin".
MEME_KEYWORDS = ["meme", "memecoin", "meme coin", "meme coins", "memes"]


def _text_has_meme(text):
    """Return the meme keyword found in `text`, or None."""
    if not text:
        return None
    low = str(text).lower()
    for kw in MEME_KEYWORDS:
        if kw in low:
            return kw
    return None


def _list_has_meme(items):
    """Return the first list entry that contains a meme keyword, or None."""
    for item in items or []:
        if _text_has_meme(item):
            return str(item)
    return None


def fetch_cg_meme_keys(id_platforms=None, min_mcap=MIN_MCAP, max_pages=8):
    """
    BULK: one pull of CoinGecko's meme category -> a set of (chain, contract)
    keys for every meme coin at/above min_mcap. Reuses discovery's id->platforms
    map (pass it in to avoid re-fetching). Returns (keys_set, meme_id_count).
    """
    if not CG_KEY:
        return set(), 0
    if id_platforms is None:
        id_platforms = _cg_id_to_platforms()      # 1 call (also used by discovery)

    meme_ids = []
    for page in range(1, max_pages + 1):
        time.sleep(CG_DELAY)
        rows = _get(CG_MARKETS_URL,
                    {"vs_currency": "usd", "category": CG_MEME_CATEGORY,
                     "order": "market_cap_desc", "per_page": 250, "page": page},
                    {"x-cg-demo-api-key": CG_KEY})
        if not rows:
            break
        for coin in rows:
            mc = coin.get("market_cap")
            if mc is None or mc >= min_mcap:
                meme_ids.append(coin["id"])
        last_mc = rows[-1].get("market_cap")
        if (last_mc is not None and last_mc < min_mcap) or len(rows) < 250:
            break                                  # past our mcap window -> stop

    keys = set()
    for cid in meme_ids:
        for platform, addr in (id_platforms.get(cid) or {}).items():
            chain = CG_PLATFORM_TO_CHAIN.get(platform, platform)
            keys.add((chain, addr.lower()))
    return keys, len(meme_ids)


def is_meme(contract, chain, cg_meme_keys, cmc_tags=None):
    """
    Decide whether a candidate is a meme. Pure (no network I/O): the caller
    supplies the bulk meme-key set plus any CMC tags it already has.
    Returns {"is_meme", "signals" {..}, "matched"}.
    """
    signals = {}

    key = ((chain or "").lower(), (contract or "").lower())
    if key in cg_meme_keys:
        signals["category"] = "CoinGecko meme category"

    tag_hit = _list_has_meme(cmc_tags)
    if tag_hit:
        signals["tag"] = f"CMC tag '{tag_hit}'"

    return {
        "is_meme": bool(signals),
        "signals": signals,
        "matched": "; ".join(signals.values()) if signals else None,
    }


# ---------------------------------------------------------------------------
# Per-coin CMC tags (used for testing the tag signal). In production, tags come
# from discovery's listings pull - no extra call.
# ---------------------------------------------------------------------------
def fetch_cmc_tags(contract):
    """CoinMarketCap tag slugs for a contract, or []. Never raises."""
    import requests
    key = os.getenv("COINMARKETCAP_API_KEY")
    if not contract or not key:
        return []
    try:
        r = requests.get(
            "https://pro-api.coinmarketcap.com/v2/cryptocurrency/info",
            params={"address": contract.lower()},
            headers={"X-CMC_PRO_API_KEY": key, "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code != 200:
            return []
        data = (r.json() or {}).get("data") or {}
    except (requests.exceptions.RequestException, ValueError):
        return []
    for value in data.values():
        entry = value[0] if isinstance(value, list) else value
        if not entry:
            continue
        tags = []
        for t in entry.get("tags") or []:
            tags.append(t.get("name") or t.get("slug") if isinstance(t, dict) else str(t))
        return tags
    return []


if __name__ == "__main__":
    keys, n = fetch_cg_meme_keys()
    print(f"CoinGecko meme category: {n} meme ids -> {len(keys)} (chain,contract) keys")
