"""
discovery.py
------------
Candidate discovery. This REPLACES DexScreener as the SOURCE of the coin list.
Candidates are pulled from:
  * CoinGecko     - /coins/markets (mcap, paginated) joined with /coins/list
                    (contract + chain), filtered to MCap $5M-$350M.
  * CoinMarketCap - /cryptocurrency/listings/latest with market_cap_min/max
                    (contract + chain + mcap + tags, server-side filtered).

Each candidate carries: {contract, chain, mcap, tags, source}. They are merged,
DEDUPED by (chain, lowercased contract), and then MEME coins and STABLECOINS
are removed early (see meme_filter) before enrichment / the filter chain.

(Bitquery is no longer a discovery source - from_bitquery is kept for later use
elsewhere but is not called here.)

IMPORTANT: discover_candidates() does NOT call DexScreener at all. DexScreener
is only used AFTER discovery, by contract, to enrich age/volume/liquidity -
that happens in discover_tokens() below and reuses the untouched
fetch_dexscreener code.
"""

import os
import time

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

CG_KEY = os.getenv("COINGECKO_API_KEY")
CMC_KEY = os.getenv("COINMARKETCAP_API_KEY")
BQ_KEY = os.getenv("BITQUERY_API_KEY")

CG_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
CG_LIST_URL = "https://api.coingecko.com/api/v3/coins/list"
CMC_LISTINGS_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
BQ_URL = "https://streaming.bitquery.io/graphql"

# The market-cap window is enforced HERE ONLY (CoinGecko pages + the CMC
# listing's server-side filter) - there is no separate mcap step later.
MIN_MCAP = 5_000_000        # $5M
MAX_MCAP = 350_000_000      # $350M

CG_DELAY = 2.5              # CoinGecko demo rate limit friendliness

# CoinGecko platform id -> our chain name.
CG_PLATFORM_TO_CHAIN = {
    "ethereum": "ethereum", "solana": "solana", "base": "base",
    "binance-smart-chain": "bsc", "polygon-pos": "polygon",
    "arbitrum-one": "arbitrum", "optimistic-ethereum": "optimism",
    "avalanche": "avalanche", "fantom": "fantom", "cronos": "cronos",
    "zksync": "zksync", "linea": "linea", "scroll": "scroll",
    "mantle": "mantle", "blast": "blast", "sui": "sui", "aptos": "aptos",
    "the-open-network": "ton", "tron": "tron",
}
# CoinMarketCap platform name -> our chain name.
CMC_PLATFORM_TO_CHAIN = {
    "Ethereum": "ethereum", "Solana": "solana", "Base": "base",
    "BNB Smart Chain (BEP20)": "bsc", "BNB Smart Chain": "bsc",
    "Polygon": "polygon", "Arbitrum": "arbitrum", "Optimism": "optimism",
    "Avalanche C-Chain": "avalanche", "Avalanche": "avalanche",
    "Fantom": "fantom", "Cronos": "cronos", "zkSync Era": "zksync",
    "Linea": "linea", "Scroll": "scroll", "Mantle": "mantle",
    "Blast": "blast", "Sui": "sui", "Aptos": "aptos", "TON": "ton", "Tron": "tron",
}
# Solana majors we never want as "candidates" (huge, would fail filters anyway).
_SOL_MAJORS = {
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT (legacy)
}


def _get(url, params=None, headers=None, timeout=40):
    """GET JSON, or None on failure (never raises)."""
    try:
        r = requests.get(url, params=params or {}, headers=headers or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print(f"[WARN] request failed {url}: {str(e)[:100]}")
        return None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Source 1: CoinGecko
# ---------------------------------------------------------------------------
_ID_PLATFORMS_CACHE = None

# Counts from the most recent discover_candidates()/discover_tokens() run
# (discovery, meme, cap, enriched), read by the save summary and the funnel.
LAST_COUNTS = {}

# CoinGecko market-cap rank of every coin seen on this run's discovery pages,
# keyed by lowercased contract. Free (the pages are fetched anyway), so
# rank_check reads ranks from here instead of one CoinGecko call per coin.
LAST_CG_RANKS = {}

# The coin's real (global) market cap from CoinGecko / CMC, keyed by
# lowercased contract on EVERY chain the coin lives on. Covers every coin on
# the CoinGecko pages walked (all coins above MIN_MCAP, also those above
# MAX_MCAP), so tracking.py can refresh tracked coins' market caps without
# trusting a single DexScreener pool (tiny for bridged copies).
LAST_MCAPS = {}


# Retry a failed discovery request with back-off before giving up on it.
RETRY_WAITS = [5, 15, 30]
# If this many pages IN A ROW still fail after retries, the API is down: stop
# that source (loudly) instead of hammering it for every remaining page.
MAX_CONSECUTIVE_FAILED_PAGES = 3

# Pages that still failed after retries in the latest run (for reporting).
DISCOVERY_ISSUES = {}


def _get_retry(url, params=None, headers=None, timeout=40):
    """
    _get() with retries: a failed request (network error, HTTP error, bad
    JSON) is retried after 5s, 15s, 30s. Returns the JSON, or None if every
    attempt failed. NOTE: an EMPTY result ([]) is a success, not a failure -
    callers must treat None ("failed") and [] ("genuinely empty") differently.
    """
    for wait in [0] + RETRY_WAITS:
        if wait:
            print(f"[INFO] retrying in {wait}s: {url}")
            time.sleep(wait)
        data = _get(url, params, headers, timeout)
        if data is not None:
            return data
    return None


def _cg_id_to_platforms():
    """Map every CoinGecko id -> {platform: contract}. Cached for the process so
    discovery and the meme filter share ONE fetch of the (large) coins list."""
    global _ID_PLATFORMS_CACHE
    if _ID_PLATFORMS_CACHE is not None:
        return _ID_PLATFORMS_CACHE
    data = _get_retry(CG_LIST_URL, {"include_platform": "true"},
                      {"x-cg-demo-api-key": CG_KEY})
    if data is None:
        # Without this list no CoinGecko coin has a contract address, so the
        # whole CoinGecko source would come back empty. Don't cache the
        # failure - a later caller in this run tries again.
        print("[WARN] CoinGecko coins list failed after retries - CoinGecko "
              "candidates will be missing this run")
        DISCOVERY_ISSUES["coingecko_coins_list_failed"] = True
        return {}
    out = {}
    for x in data:
        plats = {p: a for p, a in (x.get("platforms") or {}).items() if a}
        if plats:
            out[x["id"]] = plats
    _ID_PLATFORMS_CACHE = out
    return out


def from_coingecko(max_pages=30):
    """CoinGecko coins in the mcap window, as (contract, chain, mcap) candidates."""
    if not CG_KEY:
        return []
    id_platforms = _cg_id_to_platforms()
    out = []
    failed_pages, consecutive = [], 0
    for page in range(1, max_pages + 1):
        time.sleep(CG_DELAY)
        rows = _get_retry(CG_MARKETS_URL,
                          {"vs_currency": "usd", "order": "market_cap_desc",
                           "per_page": 250, "page": page},
                          {"x-cg-demo-api-key": CG_KEY})
        if rows is None:
            # This page failed even after retries: skip JUST this page and
            # carry on with the next one - don't end discovery.
            failed_pages.append(page)
            consecutive += 1
            print(f"[WARN] CoinGecko markets page {page} failed after retries - skipped")
            if consecutive >= MAX_CONSECUTIVE_FAILED_PAGES:
                print(f"[WARN] {consecutive} CoinGecko pages in a row failed - "
                      f"CoinGecko looks down, stopping CoinGecko discovery")
                break
            continue
        consecutive = 0
        if not rows:
            break                           # genuinely past the end of the list
        below = False
        for coin in rows:
            # Remember every coin's CoinGecko market-cap rank: rank_check reads
            # it from here instead of one CoinGecko call per coin.
            if coin.get("market_cap_rank") is not None:
                for addr in id_platforms.get(coin["id"], {}).values():
                    LAST_CG_RANKS[addr.lower()] = coin["market_cap_rank"]
            mc = coin.get("market_cap")
            if mc is None:
                continue
            for addr in id_platforms.get(coin["id"], {}).values():
                LAST_MCAPS[addr.lower()] = mc
            if mc > MAX_MCAP:
                continue                    # still in the too-big top pages
            if mc < MIN_MCAP:
                below = True                # descending list -> we've gone past the range
                break
            for cg_plat, addr in id_platforms.get(coin["id"], {}).items():
                out.append({
                    "contract": addr,
                    "chain": CG_PLATFORM_TO_CHAIN.get(cg_plat, cg_plat),
                    "mcap": mc,
                    "name": coin.get("name"),
                    "symbol": (coin.get("symbol") or "").upper() or None,
                    "price": coin.get("current_price"),
                    "coin_key": "cg:" + coin["id"],   # same coin on every chain
                    "chg_24h": coin.get("price_change_percentage_24h"),
                    "high_24h": coin.get("high_24h"),
                    "low_24h": coin.get("low_24h"),
                    "source": "coingecko",
                })
        if below or len(rows) < 250:
            break
    DISCOVERY_ISSUES["coingecko_failed_pages"] = failed_pages
    return out


# ---------------------------------------------------------------------------
# Source 2: CoinMarketCap
# ---------------------------------------------------------------------------
def _parse_date(s):
    """CMC ISO timestamp -> naive UTC datetime, or None."""
    from datetime import datetime, timezone
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def from_coinmarketcap(limit=5000):
    """CMC listings filtered to the mcap window (server-side)."""
    if not CMC_KEY:
        return []
    data = _get_retry(CMC_LISTINGS_URL,
                {"start": 1, "limit": limit,
                 "market_cap_min": MIN_MCAP, "market_cap_max": MAX_MCAP},
                {"X-CMC_PRO_API_KEY": CMC_KEY, "Accept": "application/json"})
    if data is None:
        print("[WARN] CoinMarketCap listings failed after retries - CMC "
              "candidates will be missing this run")
        DISCOVERY_ISSUES["cmc_failed"] = True
    out = []
    for x in (data or {}).get("data", []) or []:
        plat = x.get("platform")
        if not plat or not plat.get("token_address"):
            continue                        # native coin (no contract) -> skip
        name = plat.get("name")
        usd = x.get("quote", {}).get("USD", {}) or {}
        out.append({
            "contract": plat["token_address"],
            "chain": CMC_PLATFORM_TO_CHAIN.get(name, (name or "").lower()),
            "mcap": usd.get("market_cap"),
            "name": x.get("name"),
            "symbol": x.get("symbol"),
            "price": usd.get("price"),
            "coin_key": f"cmc:{x.get('id')}",   # re-keyed to CoinGecko's id when matched
            "chg_24h": usd.get("percent_change_24h"),
            "chg_7d": usd.get("percent_change_7d"),
            "tags": x.get("tags") or [],     # CMC tag slugs (incl. "memes") - free here
            # When CMC listed it - the age fallback, free here (age_check.py).
            "listed_at": _parse_date(x.get("date_added")),
            "source": "coinmarketcap",
        })
    return out


# ---------------------------------------------------------------------------
# Source 3: Bitquery (realtime, Solana - new/active tokens by recent volume)
# ---------------------------------------------------------------------------
def from_bitquery(limit=120, since_hours=24):
    """Recently/actively traded Solana tokens from Bitquery realtime.

    NOTE: no longer used as a discovery source (discover_candidates uses only
    CoinGecko + CoinMarketCap). Kept for later use elsewhere.
    """
    if not BQ_KEY:
        return []
    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = ('{ Solana(dataset: realtime) { DEXTradeByTokens('
             'limit: {count: %d} orderBy: {descendingByField: "vol"} '
             'where: {Block: {Time: {since: "%s"}}}) { '
             'Trade { Currency { MintAddress Symbol Name } } '
             'vol: sum(of: Trade_Side_AmountInUSD) } } }' % (limit, since))
    try:
        r = requests.post(BQ_URL, headers={"Authorization": "Bearer " + BQ_KEY,
                                           "Content-Type": "application/json"},
                          json={"query": query}, timeout=60)
        r.raise_for_status()
        rows = (r.json().get("data", {}) or {}).get("Solana", {}).get("DEXTradeByTokens", []) or []
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"[WARN] Bitquery failed: {str(e)[:100]}")
        return []
    out = []
    for t in rows:
        mint = (((t.get("Trade") or {}).get("Currency") or {}).get("MintAddress"))
        if not mint or mint in _SOL_MAJORS:
            continue
        out.append({"contract": mint, "chain": "solana", "mcap": None, "source": "bitquery"})
    return out


# ---------------------------------------------------------------------------
# Merge + dedupe
# ---------------------------------------------------------------------------
def discover_candidates(verbose=True):
    """
    Pull CoinGecko + CoinMarketCap, merge and dedupe by (chain, contract), then
    remove memes and stablecoins EARLY (before enrichment / the filter chain).
    Every remaining candidate goes on to the filter - there is no cap.
    """
    DISCOVERY_ISSUES.clear()
    LAST_CG_RANKS.clear()
    LAST_MCAPS.clear()
    cg = from_coingecko()
    cmc = from_coinmarketcap()

    seen = {}
    merged = []
    for c in cg + cmc:                      # order = priority when duplicated
        contract = (c.get("contract") or "").strip()
        if not contract:
            continue
        key = (c.get("chain"), contract.lower())
        if key in seen:
            first = seen[key]
            for field in ("listed_at", "tags", "name", "symbol", "price",
                          "chg_24h", "chg_7d", "high_24h", "low_24h"):
                if not first.get(field) and c.get(field):
                    first[field] = c[field]
            continue
        seen[key] = c
        merged.append(c)
    unique_count = len(merged)

    # One identity per COIN (not per chain): a CMC entry whose contract is
    # one of a CoinGecko coin's addresses is the same coin -> same key.
    cg_key_by_addr = {}
    for cg_id, plats in _cg_id_to_platforms().items():
        for addr in plats.values():
            cg_key_by_addr.setdefault(addr.lower(), "cg:" + cg_id)
    for c in merged:
        c["coin_key"] = cg_key_by_addr.get(c["contract"].lower(), c.get("coin_key"))
    for c in cmc:                            # CMC global mcap, by contract
        if c.get("mcap"):
            LAST_MCAPS.setdefault(c["contract"].lower(), c["mcap"])

    # --- MEME + STABLECOIN FILTER (step 2, before everything else) ----------
    # Free per coin: one BULK pull each of CoinGecko's meme and stablecoin
    # categories (set lookups) plus the CMC tags we already fetched. Drops
    # memes and stablecoins so they never reach DexScreener enrichment or the
    # age/revenue/... chain.
    from meme_filter import (fetch_cg_meme_keys, fetch_cg_stablecoin_keys,
                             fetch_cg_tokenized_keys, classify_excluded)
    meme_keys, _ = fetch_cg_meme_keys()          # reuse the cached id->platforms map
    stable_keys, _ = fetch_cg_stablecoin_keys()
    tokenized_keys, _ = fetch_cg_tokenized_keys()
    from meme_filter import looks_like_stablecoin
    kept, meme_dropped, stable_dropped, tokenized_dropped = [], 0, 0, 0
    price_stable_dropped = 0
    for c in merged:
        kind = classify_excluded(c["contract"], c["chain"], meme_keys, stable_keys,
                                 cmc_tags=c.get("tags"), tokenized_keys=tokenized_keys)
        if kind == "meme":
            meme_dropped += 1
        elif kind == "stablecoin":
            stable_dropped += 1
        elif kind == "tokenized":
            tokenized_dropped += 1
        elif looks_like_stablecoin(c):
            stable_dropped += 1              # $1 peg that doesn't move
            price_stable_dropped += 1
        else:
            kept.append(c)
    merged = kept
    after_excl = len(merged)

    # Remember this run's counts so the save summary / scheduler funnel can
    # show the full chain starting at discovery.
    LAST_COUNTS.clear()
    LAST_COUNTS.update({"discovery": unique_count, "meme_dropped": meme_dropped,
                        "stable_dropped": stable_dropped,
                        "tokenized_dropped": tokenized_dropped,
                        "price_stable_dropped": price_stable_dropped,
                        "after_excl": after_excl})

    if verbose:
        print("=" * 52)
        print("  DISCOVERY (CoinGecko + CoinMarketCap)")
        print("=" * 52)
        print(f"  CoinGecko returned ....... {len(cg):>5}")
        print(f"  CoinMarketCap returned ... {len(cmc):>5}")
        print(f"  {'-'*48}")
        print(f"  TOTAL before dedup ....... {len(cg)+len(cmc):>5}")
        print(f"  AFTER dedup (unique) ..... {unique_count:>5}")
        print(f"  MEME dropped ............. {meme_dropped:>5}")
        print(f"  STABLECOIN dropped ....... {stable_dropped:>5}"
              f"   (of which by $1 price peg: {price_stable_dropped})")
        print(f"  TOKENIZED stock/fund drop  {tokenized_dropped:>5}")
        print(f"  AFTER meme+stable filter . {after_excl:>5}")
        failed = DISCOVERY_ISSUES.get("coingecko_failed_pages") or []
        print(f"  CoinGecko pages skipped (failed after retries): "
              f"{failed if failed else 'none'}")
        if DISCOVERY_ISSUES.get("cmc_failed"):
            print("  CoinMarketCap listings: FAILED after retries")
        print("=" * 52)
    return merged


# ---------------------------------------------------------------------------
# Drop-in replacement for the old discover_tokens(): candidates + DexScreener
# enrichment by contract (age/volume/liquidity). DexScreener is used ONLY here,
# for enrichment - never for discovery.
# ---------------------------------------------------------------------------
def discover_tokens():
    import time as _time
    from fetch_dexscreener import fetch_pairs_for_candidates, build_token_records

    candidates = discover_candidates()
    addresses = [c["contract"] for c in candidates]

    print(f"Enriching {len(addresses)} candidates via DexScreener (by contract)...")
    t0 = _time.time()
    pairs = fetch_pairs_for_candidates(candidates)
    LAST_COUNTS["enrich_seconds"] = round(_time.time() - t0)
    keep = {a.lower() for a in addresses}
    tokens = build_token_records(pairs, keep_addresses=keep)
    enriched = len(tokens)

    # Attach the free CMC listing date to every record (age fallback), and
    # add a minimal record for every candidate DexScreener has NO data for:
    # a missing DexScreener listing is NOT a drop - age_check still tries the
    # CMC listing date, and only an age unknown everywhere drops.
    by_contract = {}
    for c in candidates:
        by_contract.setdefault(c["contract"].lower(), c)
    for t in tokens:
        c = by_contract.get(t["contract"].lower())
        t["listed_at"] = c.get("listed_at") if c else None
        # CoinGecko/CMC market cap (display only - the dashboard queue table).
        t["discovery_mcap"] = c.get("mcap") if c else None
        t["cmc_tags"] = (c.get("tags") or []) if c else []   # queue narrative
        t["coin_key"] = c.get("coin_key") if c else None      # dedup across chains
    have = {t["contract"].lower() for t in tokens}
    for low, c in by_contract.items():
        if low in have:
            continue
        tokens.append({
            "chain": c["chain"],
            "contract": c["contract"],
            "name": c.get("name"),
            "symbol": c.get("symbol"),
            "price_usd": c.get("price"),
            "market_cap": c.get("mcap"),
            "liquidity_usd": None,
            "volume_24h": None,
            "pair_created_at": None,
            "age_days": None,            # -> age_check falls back to CMC
            "num_pools": 0,
            "listed_at": c.get("listed_at"),
            "discovery_mcap": c.get("mcap"),
            "cmc_tags": c.get("tags") or [],
            "coin_key": c.get("coin_key"),
            "no_dexscreener": True,
        })
    tokens.sort(key=lambda t: t["liquidity_usd"] or 0, reverse=True)
    from fetch_dexscreener import LAST_FETCH_STATS as fs
    print(f"  => {enriched} candidates had DexScreener data (enriched); "
          f"{len(tokens) - enriched} without it still go on to the age fallback.")
    print(f"     {LAST_COUNTS['enrich_seconds']}s | main-pool requests {fs.get('v1_requests')} "
          f"(covered {fs.get('v1_covered')}, failed {fs.get('v1_failed')}) | all-pools "
          f"fallback {fs.get('fallback_addresses')} coins + young re-check "
          f"{fs.get('young_rechecked')} coins = {fs.get('requests')} requests | skipped "
          f"unusable (e.g. ibc/...) {fs.get('skipped_unusable')} | bad addresses isolated "
          f"{len(fs.get('bad_addresses', []))} | coins lost to failed batches "
          f"{fs.get('lost_addresses')}")
    LAST_COUNTS["enriched"] = enriched
    LAST_COUNTS["no_dexscreener"] = len(tokens) - enriched
    return tokens


if __name__ == "__main__":
    discover_candidates()
