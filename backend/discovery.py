"""
discovery.py
------------
Multi-source candidate discovery. This REPLACES DexScreener as the SOURCE of
the coin list. Candidates are pulled from:
  * CoinGecko     - /coins/markets (mcap, paginated) joined with /coins/list
                    (contract + chain), filtered to MCap $1M-$500M.
  * CoinMarketCap - /cryptocurrency/listings/latest with market_cap_min/max
                    (contract + chain + mcap server-side filtered).
  * Bitquery      - realtime top-traded Solana tokens (new/active pairs).

Each candidate carries: {contract, chain, mcap (may be None), source}.
They are merged and DEDUPED by (chain, lowercased contract).

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

# TEMPORARY stopgap: after discovery + dedup we keep only the top-N candidates
# by market cap, so a daily run stays fast/green instead of enriching thousands
# of coins via DexScreener (~1 call per 30 addresses, rate-limit risk). Raise or
# remove this once Moralis / Volume-OHLCV can supply age + volume for coins that
# aren't on DexScreener - only then is the full candidate list actually usable.
MAX_CANDIDATES = 300

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
def _cg_id_to_platforms():
    """Map every CoinGecko id -> {platform: contract} (one call)."""
    data = _get(CG_LIST_URL, {"include_platform": "true"}, {"x-cg-demo-api-key": CG_KEY})
    out = {}
    for x in data or []:
        plats = {p: a for p, a in (x.get("platforms") or {}).items() if a}
        if plats:
            out[x["id"]] = plats
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
            "source": "coinmarketcap",
        })
    return out


# ---------------------------------------------------------------------------
# Source 3: Bitquery (realtime, Solana - new/active tokens by recent volume)
# ---------------------------------------------------------------------------
def from_bitquery(limit=120, since_hours=24):
    """Recently/actively traded Solana tokens from Bitquery realtime."""
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
    """Pull all three sources, merge, and dedupe by (chain, contract)."""
    cg = from_coingecko()
    cmc = from_coinmarketcap()
    bq = from_bitquery()

    seen = set()
    merged = []
    for c in cg + cmc + bq:                 # order = priority when duplicated
        contract = (c.get("contract") or "").strip()
        if not contract:
            continue
        key = (c.get("chain"), contract.lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(c)
    unique_count = len(merged)

    # --- TEMPORARY cap (see MAX_CANDIDATES) --------------------------------
    # Keep only the top-N candidates by market cap so a run stays fast/green.
    # Bitquery candidates have mcap=None (realtime trade data, not a mcap), so
    # they sort to the bottom and are the first to be cut by this stopgap.
    merged.sort(key=lambda c: c.get("mcap") or 0, reverse=True)
    merged = merged[:MAX_CANDIDATES]

    if verbose:
        print("=" * 52)
        print("  DISCOVERY (CoinGecko + CoinMarketCap + Bitquery)")
        print("=" * 52)
        print(f"  CoinGecko returned ....... {len(cg):>5}")
        print(f"  CoinMarketCap returned ... {len(cmc):>5}")
        print(f"  Bitquery returned ........ {len(bq):>5}")
        print(f"  {'-'*48}")
        print(f"  TOTAL before dedup ....... {len(cg)+len(cmc)+len(bq):>5}")
        print(f"  AFTER dedup (unique) ..... {unique_count:>5}")
        print(f"  AFTER cap (top {MAX_CANDIDATES} mcap) ... {len(merged):>5}")
        print("=" * 52)
    return merged


# ---------------------------------------------------------------------------
# Drop-in replacement for the old discover_tokens(): candidates + DexScreener
# enrichment by contract (age/volume/liquidity). DexScreener is used ONLY here,
# for enrichment - never for discovery.
# ---------------------------------------------------------------------------
def discover_tokens():
    from fetch_dexscreener import fetch_pairs_for_addresses, build_token_records

    candidates = discover_candidates()
    addresses = [c["contract"] for c in candidates]

    print(f"Enriching {len(addresses)} candidates via DexScreener (by contract)...")
    pairs = fetch_pairs_for_addresses(addresses)
    keep = {a.lower() for a in addresses}
    tokens = build_token_records(pairs, keep_addresses=keep)
    tokens.sort(key=lambda t: t["liquidity_usd"] or 0, reverse=True)
    print(f"  => {len(tokens)} candidates had DexScreener data (enriched).")
    return tokens


if __name__ == "__main__":
    discover_candidates()
