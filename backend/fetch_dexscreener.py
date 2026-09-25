"""
fetch_dexscreener.py
--------------------
Phase 2 - Step 1 (discover mode): find NEW / TRENDING / BOOSTED tokens from
the DexScreener public API automatically, WITHOUT needing a search term.

DexScreener is free and needs NO API key.

Why two stages?
  DexScreener's "discovery" endpoints (boosted tokens, token profiles) only
  tell us WHICH tokens are trending - basically just a contract address and a
  chain. They do NOT include price / liquidity / volume. So we:

    STAGE 1 (discover):  collect a broad list of {chain, address} candidates
                         from the boosts + profiles endpoints.
    STAGE 2 (enrich):    look each address up via the /tokens endpoint, which
                         returns the full trading data (pairs) for it.

  Then we group the pairs by token (one token can trade in many pools) and
  build one clean record per token - the same fields as before.

For now this ONLY fetches and PRINTS. It does not touch the database.

Run it:
    python fetch_dexscreener.py
"""

from datetime import datetime, timezone

import requests

# --- Discovery endpoints (return trending tokens, no search term needed) ---
BOOSTS_LATEST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
BOOSTS_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
PROFILES_LATEST_URL = "https://api.dexscreener.com/token-profiles/latest/v1"

# --- Enrichment endpoint (full market data for given token addresses) ------
# Accepts up to 30 comma-separated addresses per call.
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{addresses}"

# One shared HTTP session (slightly faster; reuses the connection).
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "crypto-screener/0.1"})


# ---------------------------------------------------------------------------
# Small formatting helpers (just to make the printed table readable)
# ---------------------------------------------------------------------------
def fmt_money(value):
    """Turn a big number into something like $1.23M or $4.56K."""
    if value is None:
        return "n/a"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "n/a"
    for suffix, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= size:
            return f"${value / size:.2f}{suffix}"
    return f"${value:,.2f}"


def fmt_price(value):
    """Format a token price, coping with both large and tiny prices."""
    if value is None:
        return "n/a"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if value == 0:
        return "$0"
    if value >= 1:
        return f"${value:,.4f}"
    if value >= 0.0001:
        return f"${value:.6f}"
    return f"${value:.2e}"  # scientific notation for very tiny prices


def short_addr(addr):
    """Shorten a long contract address so it fits in a table."""
    if not addr:
        return "n/a"
    return addr if len(addr) <= 13 else f"{addr[:6]}...{addr[-4:]}"


def age_in_days(created_ms):
    """Convert a 'pairCreatedAt' millisecond timestamp into age in days."""
    if not created_ms:
        return None
    created = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
    delta = datetime.now(timezone.utc) - created
    return delta.days


# ---------------------------------------------------------------------------
# A tiny, safe "GET some JSON" helper used by everything below.
# ---------------------------------------------------------------------------
def get_json(url, timeout=15):
    """
    Fetch a URL and return parsed JSON, or None if anything goes wrong.
    Never raises - it prints a clear message and returns None instead.
    """
    try:
        response = SESSION.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        print(f"[WARN] Timed out calling {url}")
    except requests.exceptions.ConnectionError:
        print(f"[WARN] Could not connect to {url} (are you online?)")
    except requests.exceptions.HTTPError as exc:
        print(f"[WARN] HTTP error from {url}: {exc}")
    except ValueError:
        print(f"[WARN] Got non-JSON response from {url}")
    return None


# ---------------------------------------------------------------------------
# STAGE 1 - discover candidate tokens (just chain + address)
# ---------------------------------------------------------------------------
def discover_candidates():
    """
    Ask the discovery endpoints for trending tokens and return a list of
    unique (chain, address) tuples. No search term involved.
    """
    seen = set()
    candidates = []

    # Each of these endpoints returns a JSON *list* of items shaped like:
    #   { "chainId": "solana", "tokenAddress": "...", ... }
    for label, url in (
        ("boosts-latest", BOOSTS_LATEST_URL),
        ("boosts-top", BOOSTS_TOP_URL),
        ("profiles-latest", PROFILES_LATEST_URL),
    ):
        data = get_json(url)
        if not data:
            continue
        count_before = len(candidates)
        for item in data:
            chain = item.get("chainId")
            address = item.get("tokenAddress")
            if not chain or not address:
                continue
            key = (chain, address)
            if key in seen:
                continue  # already have it from another endpoint
            seen.add(key)
            candidates.append(key)
        print(f"  - {label}: +{len(candidates) - count_before} new tokens")

    return candidates


# ---------------------------------------------------------------------------
# STAGE 2 - enrich: fetch full market data for the discovered addresses
# ---------------------------------------------------------------------------
def chunked(items, size):
    """Yield successive `size`-length slices of a list."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_pairs_for_addresses(addresses):
    """
    Look up trading data for a list of addresses via the /tokens endpoint.
    Returns a flat list of "pair" dicts (raw from DexScreener).
    """
    all_pairs = []
    # The endpoint accepts up to 30 comma-separated addresses per request.
    for batch in chunked(addresses, 30):
        url = TOKENS_URL.format(addresses=",".join(batch))
        data = get_json(url)
        if not data:
            continue
        all_pairs.extend(data.get("pairs") or [])
    return all_pairs


# ---------------------------------------------------------------------------
# Turn raw pairs into one clean record per token
# ---------------------------------------------------------------------------
def build_token_records(pairs, keep_addresses=None):
    """
    Group raw pairs by token and summarise each into a single record.

    keep_addresses: optional set of addresses (lowercased) we care about, so
    we ignore unrelated tokens that can appear as the *quote* side of a pair.
    """
    def liquidity_usd(p):
        return (p.get("liquidity") or {}).get("usd") or 0

    grouped = {}
    for pair in pairs:
        base = pair.get("baseToken") or {}
        address = base.get("address")
        chain = pair.get("chainId")
        if not address or not chain:
            continue
        # If we were given a whitelist, only keep tokens we actually discovered.
        if keep_addresses is not None and address.lower() not in keep_addresses:
            continue
        grouped.setdefault((chain, address), []).append(pair)

    tokens = []
    for (chain, address), token_pairs in grouped.items():
        # The pool with the most liquidity is the most representative one.
        main = max(token_pairs, key=liquidity_usd)
        base = main.get("baseToken") or {}

        # Sum liquidity & 24h volume across all of the token's pools.
        total_liquidity = sum(liquidity_usd(p) for p in token_pairs)
        total_volume_24h = sum((p.get("volume") or {}).get("h24") or 0 for p in token_pairs)

        # Age = how long ago the OLDEST pool was created (first seen).
        created_times = [p.get("pairCreatedAt") for p in token_pairs if p.get("pairCreatedAt")]
        oldest_created = min(created_times) if created_times else None

        tokens.append({
            "chain": chain,
            "contract": address,
            "name": base.get("name"),
            "symbol": base.get("symbol"),
            "price_usd": main.get("priceUsd"),
            "market_cap": main.get("marketCap") or main.get("fdv"),
            "liquidity_usd": total_liquidity,
            "volume_24h": total_volume_24h,
            "pair_created_at": oldest_created,
            "age_days": age_in_days(oldest_created),
            "num_pools": len(token_pairs),
        })

    return tokens


# ---------------------------------------------------------------------------
# The main "discover" flow that ties both stages together
# ---------------------------------------------------------------------------
def discover_tokens():
    """Discover trending tokens and return clean, enriched records."""
    print("Stage 1: discovering trending tokens (no search term)...")
    candidates = discover_candidates()
    if not candidates:
        print("[ERROR] No candidate tokens discovered. Cannot continue.")
        return []
    print(f"  => {len(candidates)} unique candidate tokens discovered.\n")

    print("Stage 2: fetching full market data for each...")
    addresses = [addr for (_chain, addr) in candidates]
    pairs = fetch_pairs_for_addresses(addresses)
    if not pairs:
        print("[ERROR] Discovered tokens but got no market data back.")
        return []
    print(f"  => {len(pairs)} trading pairs returned.\n")

    # Only keep tokens we actually discovered (case-insensitive address match).
    keep = {addr.lower() for addr in addresses}
    tokens = build_token_records(pairs, keep_addresses=keep)

    # Sort by liquidity (biggest first) so the table reads sensibly.
    tokens.sort(key=lambda t: t["liquidity_usd"] or 0, reverse=True)
    return tokens


# ---------------------------------------------------------------------------
# Pretty-printing the result
# ---------------------------------------------------------------------------
def print_table(tokens, limit=15):
    """Print the first `limit` tokens as a neat fixed-width table."""
    if not tokens:
        print("No tokens to display.")
        return

    header = (
        f"{'#':>2}  {'SYMBOL':<10} {'CHAIN':<9} {'PRICE':>13} "
        f"{'MCAP':>10} {'LIQUIDITY':>11} {'VOL 24H':>11} "
        f"{'AGE(d)':>6} {'POOLS':>5}  {'CONTRACT':<15}"
    )
    print("=" * len(header))
    print("  DEXSCREENER - DISCOVERED TRENDING TOKENS")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    for i, t in enumerate(tokens[:limit], start=1):
        symbol = (t["symbol"] or "?")[:10]
        chain = (t["chain"] or "?")[:9]
        age = "n/a" if t["age_days"] is None else str(t["age_days"])
        print(
            f"{i:>2}  {symbol:<10} {chain:<9} {fmt_price(t['price_usd']):>13} "
            f"{fmt_money(t['market_cap']):>10} {fmt_money(t['liquidity_usd']):>11} "
            f"{fmt_money(t['volume_24h']):>11} {age:>6} {t['num_pools']:>5}  "
            f"{short_addr(t['contract']):<15}"
        )

    print("-" * len(header))
    print(f"Showing {min(limit, len(tokens))} of {len(tokens)} discovered tokens.")
    print("=" * len(header))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    tokens = discover_tokens()
    print_table(tokens, limit=15)


if __name__ == "__main__":
    main()
