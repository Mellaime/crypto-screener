"""
meme_filter.py
--------------
Early MEME + STABLECOIN filter for the DISCOVERY step (filter step 2). These
coins are removed right after discovery, BEFORE the age / revenue / honeypot /
volume / liquidity chain, so we never spend per-coin API calls on coins we
don't want:
  * memes       - not screening targets
  * stablecoins - they don't move (USDC, USDT, DAI, ...), so not targets either

A coin is DROPPED if it is a meme OR a stablecoin. Each kind uses the same two
signals (checked by CONTRACT + CHAIN):
  1. CATEGORY - the coin is in a CoinGecko category for that kind. Each
     category is fetched ONCE per run in BULK and turned into a set of
     {(chain, contract)}, so the check is a free set lookup, not a per-coin
     API call.
       memes       -> "meme-token"
       stablecoins -> "stablecoins" + "bridged-stablecoins" (bridged copies
                      like USDC.e live on other chains with their own contract)
  2. TAG - a CoinMarketCap tag contains the kind's keyword ("meme",
     "stablecoin"). CMC tags come from the listings data discovery already
     fetches - no extra call.

Unknown / no data never drops a coin (same "only drop on a positive signal"
policy used by the revenue and security checks).

Public API:
  fetch_cg_meme_keys()       -> (set of (chain, contract_lower), id_count)
  fetch_cg_stablecoin_keys() -> (set of (chain, contract_lower), id_count)
  is_meme(contract, chain, cg_meme_keys, cmc_tags=None)          -> dict
  is_stablecoin(contract, chain, cg_stable_keys, cmc_tags=None)  -> dict
  classify_excluded(contract, chain, meme_keys, stable_keys, cmc_tags=None)
      -> "meme" | "stablecoin" | None
Never raises.
"""

import os
import time

from dotenv import load_dotenv

# Reuse discovery's CoinGecko helpers + maps (no duplication, no extra config).
from discovery import (_get_retry, CG_MARKETS_URL, CG_PLATFORM_TO_CHAIN,
                       _cg_id_to_platforms, DISCOVERY_ISSUES,
                       MAX_CONSECUTIVE_FAILED_PAGES)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
CG_KEY = os.getenv("COINGECKO_API_KEY")

CG_DELAY = 2.5
MIN_MCAP = 5_000_000        # match discovery's window: coins below this can't be
                            # candidates anyway, so each bulk pull stops here.

# CoinGecko category slugs (their /coins/markets `category` filter).
CG_MEME_CATEGORIES = ["meme-token"]
CG_STABLECOIN_CATEGORIES = ["stablecoins", "bridged-stablecoins"]

# Keywords - plain, easy to extend. Matched case-insensitively as substrings,
# so "meme" covers "Memes"/"animal-memes" and "stablecoin" covers
# "fiat-stablecoin"/"usd-stablecoin"/"Stablecoins".
MEME_KEYWORDS = ["meme", "memecoin", "meme coin", "meme coins", "memes"]
STABLECOIN_KEYWORDS = ["stablecoin", "stablecoins"]


def _match(text, keywords):
    """Return the keyword found in `text` (case-insensitive), or None."""
    if not text:
        return None
    low = str(text).lower()
    for kw in keywords:
        if kw in low:
            return kw
    return None


def _list_match(items, keywords):
    """Return the first list entry that contains one of `keywords`, or None."""
    for item in items or []:
        if _match(item, keywords):
            return str(item)
    return None


def _fetch_cg_category_keys(categories, id_platforms=None, min_mcap=MIN_MCAP,
                            max_pages=8):
    """
    BULK: pull each CoinGecko category (mcap-desc, stopping below min_mcap) and
    map its coin ids to {(chain, contract)} keys via discovery's cached
    id->platforms map. Returns (keys_set, coin_id_count).
    """
    if not CG_KEY:
        return set(), 0
    if id_platforms is None:
        id_platforms = _cg_id_to_platforms()      # cached - shared with discovery

    ids = set()
    for category in categories:
        consecutive = 0
        for page in range(1, max_pages + 1):
            time.sleep(CG_DELAY)
            rows = _get_retry(CG_MARKETS_URL,
                              {"vs_currency": "usd", "category": category,
                               "order": "market_cap_desc", "per_page": 250, "page": page},
                              {"x-cg-demo-api-key": CG_KEY})
            if rows is None:
                # Failed even after retries: skip just this page, keep going.
                consecutive += 1
                print(f"[WARN] CoinGecko '{category}' page {page} failed after "
                      f"retries - skipped (some of these coins may not be excluded)")
                DISCOVERY_ISSUES.setdefault("category_failed_pages", []).append(
                    f"{category} p{page}")
                if consecutive >= MAX_CONSECUTIVE_FAILED_PAGES:
                    break
                continue
            consecutive = 0
            if not rows:
                break                              # genuinely past the end
            for coin in rows:
                mc = coin.get("market_cap")
                if mc is None or mc >= min_mcap:
                    ids.add(coin["id"])
            last_mc = rows[-1].get("market_cap")
            if (last_mc is not None and last_mc < min_mcap) or len(rows) < 250:
                break                              # past our mcap window -> stop

    keys = set()
    for cid in ids:
        for platform, addr in (id_platforms.get(cid) or {}).items():
            chain = CG_PLATFORM_TO_CHAIN.get(platform, platform)
            keys.add((chain, addr.lower()))
    return keys, len(ids)


def fetch_cg_meme_keys(id_platforms=None, min_mcap=MIN_MCAP, max_pages=8):
    """BULK set of CoinGecko meme-category (chain, contract) keys."""
    return _fetch_cg_category_keys(CG_MEME_CATEGORIES, id_platforms, min_mcap, max_pages)


def fetch_cg_stablecoin_keys(id_platforms=None, min_mcap=MIN_MCAP, max_pages=8):
    """BULK set of CoinGecko stablecoin-category (chain, contract) keys."""
    return _fetch_cg_category_keys(CG_STABLECOIN_CATEGORIES, id_platforms,
                                   min_mcap, max_pages)


def _check(kind_label, keywords, contract, chain, cg_keys, cmc_tags):
    """Shared category + tag check. Pure (no network I/O)."""
    signals = {}
    key = ((chain or "").lower(), (contract or "").lower())
    if key in cg_keys:
        signals["category"] = f"CoinGecko {kind_label} category"
    tag_hit = _list_match(cmc_tags, keywords)
    if tag_hit:
        signals["tag"] = f"CMC tag '{tag_hit}'"
    return {"hit": bool(signals), "signals": signals,
            "matched": "; ".join(signals.values()) if signals else None}


def is_meme(contract, chain, cg_meme_keys, cmc_tags=None):
    """Meme check. Returns {"is_meme", "signals", "matched"}."""
    r = _check("meme", MEME_KEYWORDS, contract, chain, cg_meme_keys, cmc_tags)
    return {"is_meme": r["hit"], "signals": r["signals"], "matched": r["matched"]}


def is_stablecoin(contract, chain, cg_stable_keys, cmc_tags=None):
    """Stablecoin check. Returns {"is_stablecoin", "signals", "matched"}."""
    r = _check("stablecoin", STABLECOIN_KEYWORDS, contract, chain, cg_stable_keys,
               cmc_tags)
    return {"is_stablecoin": r["hit"], "signals": r["signals"], "matched": r["matched"]}


def classify_excluded(contract, chain, meme_keys, stable_keys, cmc_tags=None):
    """'meme', 'stablecoin', or None (keep). Memes are checked first."""
    if is_meme(contract, chain, meme_keys, cmc_tags)["is_meme"]:
        return "meme"
    if is_stablecoin(contract, chain, stable_keys, cmc_tags)["is_stablecoin"]:
        return "stablecoin"
    return None


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
    mk, mn = fetch_cg_meme_keys()
    sk, sn = fetch_cg_stablecoin_keys()
    print(f"meme category:       {mn} ids -> {len(mk)} (chain,contract) keys")
    print(f"stablecoin category: {sn} ids -> {len(sk)} (chain,contract) keys")
