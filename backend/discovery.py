"""
discovery.py
------------
Candidate discovery. This REPLACES DexScreener as the SOURCE of the coin list.
Candidates are pulled from:
  * CoinGecko     - /coins/markets (mcap, paginated) joined with /coins/list
                    (contract + chain), filtered to MCap $1M-$500M.
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

MIN_MCAP = 1_000_000        # $1M
MAX_MCAP = 500_000_000      # $500M

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


def _cg_id_to_platforms():
    """Map every CoinGecko id -> {platform: contract}. Cached for the process so
    discovery and the meme filter share ONE fetch of the (large) coins list."""
    global _ID_PLATFORMS_CACHE
    if _ID_PLATFORMS_CACHE is not None:
        return _ID_PLATFORMS_CACHE
    data = _get(CG_LIST_URL, {"include_platform": "true"}, {"x-cg-demo-api-key": CG_KEY})
    out = {}
    for x in data or []:
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
    for page in range(1, max_pages + 1):
        time.sleep(CG_DELAY)
        rows = _get(CG_MARKETS_URL,
                    {"vs_currency": "usd", "order": "market_cap_desc",
                     "per_page": 250, "page": page},
                    {"x-cg-demo-api-key": CG_KEY})
        if not rows:
            break
        below = False
        for coin in rows:
            mc = coin.get("market_cap")
            if mc is None:
                continue
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
                    "source": "coingecko",
                })
        if below or len(rows) < 250:
            break
    return out


# ---------------------------------------------------------------------------
# Source 2: CoinMarketCap
# ---------------------------------------------------------------------------
def from_coinmarketcap(limit=5000):
    """CMC listings filtered to the mcap window (server-side)."""
    if not CMC_KEY:
        return []
    data = _get(CMC_LISTINGS_URL,
                {"start": 1, "limit": limit,
                 "market_cap_min": MIN_MCAP, "market_cap_max": MAX_MCAP},
                {"X-CMC_PRO_API_KEY": CMC_KEY, "Accept": "application/json"})
    out = []
    for x in (data or {}).get("data", []) or []:
        plat = x.get("platform")
        if not plat or not plat.get("token_address"):
            continue                        # native coin (no contract) -> skip
        name = plat.get("name")
        out.append({
            "contract": plat["token_address"],
            "chain": CMC_PLATFORM_TO_CHAIN.get(name, (name or "").lower()),
            "mcap": (x.get("quote", {}).get("USD", {}) or {}).get("market_cap"),
            "tags": x.get("tags") or [],     # CMC tag slugs (incl. "memes") - free here
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
    cg = from_coingecko()
    cmc = from_coinmarketcap()

    seen = set()
    merged = []
    for c in cg + cmc:                      # order = priority when duplicated
        contract = (c.get("contract") or "").strip()
        if not contract:
            continue
        key = (c.get("chain"), contract.lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(c)
    unique_count = len(merged)

    # --- MEME + STABLECOIN FILTER (step 2, before everything else) ----------
    # Free per coin: one BULK pull each of CoinGecko's meme and stablecoin
    # categories (set lookups) plus the CMC tags we already fetched. Drops
    # memes and stablecoins so they never reach DexScreener enrichment or the
    # age/revenue/... chain.
    from meme_filter import fetch_cg_meme_keys, fetch_cg_stablecoin_keys, classify_excluded
    meme_keys, _ = fetch_cg_meme_keys()          # reuse the cached id->platforms map
    stable_keys, _ = fetch_cg_stablecoin_keys()
    kept, meme_dropped, stable_dropped = [], 0, 0
    for c in merged:
        kind = classify_excluded(c["contract"], c["chain"], meme_keys, stable_keys,
                                 cmc_tags=c.get("tags"))
        if kind == "meme":
            meme_dropped += 1
        elif kind == "stablecoin":
            stable_dropped += 1
        else:
            kept.append(c)
    merged = kept
    after_excl = len(merged)

    # Remember this run's counts so the save summary / scheduler funnel can
    # show the full chain starting at discovery.
    LAST_COUNTS.clear()
    LAST_COUNTS.update({"discovery": unique_count, "meme_dropped": meme_dropped,
                        "stable_dropped": stable_dropped, "after_excl": after_excl})

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
        print(f"  STABLECOIN dropped ....... {stable_dropped:>5}")
        print(f"  AFTER meme+stable filter . {after_excl:>5}")
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
    tokens.sort(key=lambda t: t["liquidity_usd"] or 0, reverse=True)
    from fetch_dexscreener import LAST_FETCH_STATS as fs
    print(f"  => {len(tokens)} candidates had DexScreener data (enriched).")
    print(f"     {LAST_COUNTS['enrich_seconds']}s | main-pool requests {fs.get('v1_requests')} "
          f"(covered {fs.get('v1_covered')}, failed {fs.get('v1_failed')}) | all-pools "
          f"fallback {fs.get('fallback_addresses')} coins + young re-check "
          f"{fs.get('young_rechecked')} coins = {fs.get('requests')} requests | skipped "
          f"unusable (e.g. ibc/...) {fs.get('skipped_unusable')} | bad addresses isolated "
          f"{len(fs.get('bad_addresses', []))} | coins lost to failed batches "
          f"{fs.get('lost_addresses')}")
    LAST_COUNTS["enriched"] = len(tokens)
    return tokens


if __name__ == "__main__":
    discover_candidates()
