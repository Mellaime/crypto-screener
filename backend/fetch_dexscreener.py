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

import time
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


def usable_for_dexscreener(address):
    """
    False for addresses that can't go in a /tokens URL. A "/" (Cosmos-style
    ibc/..., factory/..., persistence/...) breaks the URL path, so DexScreener
    404s the WHOLE batch and the other 29 good addresses get no data. Commas
    and whitespace would break the comma-separated list the same way.
    """
    return bool(address) and not any(ch in address for ch in "/, \t\n")


# Counts from the most recent fetch_pairs_for_addresses() call (for reporting).
LAST_FETCH_STATS = {}

RETRY_WAITS = [2, 5, 10]    # back-off for rate limits / server errors / timeouts

# /tokens returns at most 30 POOLS per response, however many addresses are
# asked for. With 30 addresses, coins with many pools crowd the others out and
# those get NO data - silently, the request still "succeeds". So we start with
# small batches and, whenever a response comes back AT the cap, split the batch
# and ask again, so every coin gets its pools.
BATCH_SIZE = 10
PAIRS_CAP = 30
REQUEST_DELAY = 0.2         # pace to DexScreener's 300 requests/minute


def _fetch_batch(batch):
    """
    One /tokens request. Returns (pairs, status): status is the HTTP code,
    or None on a timeout / connection error. Retries 429, 5xx and network
    errors; a 4xx like 404 is returned at once (a bad address - don't retry).
    """
    url = TOKENS_URL.format(addresses=",".join(batch))
    status = None
    for wait in [0] + RETRY_WAITS:
        time.sleep(wait or REQUEST_DELAY)
        try:
            r = SESSION.get(url, timeout=15)
        except requests.exceptions.RequestException:
            status = None
            continue                                    # network blip -> retry
        status = r.status_code
        if status == 200:
            try:
                return (r.json().get("pairs") or []), 200
            except ValueError:
                return [], 200
        if status == 429 or status >= 500:
            continue                                    # rate limit / server -> retry
        return [], status                               # 404 etc -> bad address
    return [], status


def _fetch_isolating_bad(batch, stats):
    """
    Fetch a batch, splitting it in halves and retrying each half when either:
      * DexScreener rejects it (404-type error) -> down to single addresses,
        so only the truly bad address is skipped; or
      * the response hit the 30-pool cap -> some coins may have been crowded
        out, so re-ask in smaller pieces until every coin gets its pools.
    """
    stats["requests"] += 1
    pairs, status = _fetch_batch(batch)
    if status == 200:
        if len(pairs) >= PAIRS_CAP and len(batch) > 1:
            stats["batches_split_cap"] += 1
            mid = len(batch) // 2
            return (_fetch_isolating_bad(batch[:mid], stats)
                    + _fetch_isolating_bad(batch[mid:], stats))
        return pairs
    if status is None or status == 429 or status >= 500:
        stats["lost_batches"] += 1                      # retries exhausted
        stats["lost_addresses"] += len(batch)
        print(f"[WARN] DexScreener batch of {len(batch)} failed after retries "
              f"(status {status}) - those coins get no data this run")
        return []
    if len(batch) == 1:
        stats["bad_addresses"].append(batch[0])        # the truly bad one
        return []
    stats["batches_split"] += 1
    mid = len(batch) // 2
    return (_fetch_isolating_bad(batch[:mid], stats)
            + _fetch_isolating_bad(batch[mid:], stats))


def fetch_pairs_for_addresses(addresses):
    """
    Look up trading data for a list of addresses via the /tokens endpoint.
    Returns a flat list of "pair" dicts (raw from DexScreener).

    Addresses DexScreener can't take are dropped BEFORE batching; any batch it
    still rejects is split until the bad address is isolated; and any response
    that hits the 30-pool cap is split and re-asked. So no coin loses its data
    because of the other coins it was batched with.
    """
    stats = _new_stats(len(addresses))
    pairs = _fetch_all_pools(addresses, stats)
    LAST_FETCH_STATS.clear()
    LAST_FETCH_STATS.update(stats)
    return pairs


def _new_stats(requested):
    return {"requested": requested, "skipped_unusable": 0, "batches": 0,
            "batches_split": 0, "batches_split_cap": 0, "bad_addresses": [],
            "lost_batches": 0, "lost_addresses": 0, "requests": 0,
            "v1_requests": 0, "v1_failed": 0, "v1_covered": 0,
            "fallback_addresses": 0, "young_rechecked": 0}


def _fetch_all_pools(addresses, stats):
    """The chain-agnostic /latest/dex/tokens endpoint (ALL pools per coin, up
    to 30), with the bad-address filter and split-on-404 / split-on-cap."""
    usable = [a for a in addresses if usable_for_dexscreener(a)]
    stats["skipped_unusable"] += len(addresses) - len(usable)
    all_pairs = []
    for batch in chunked(usable, BATCH_SIZE):
        stats["batches"] += 1
        all_pairs.extend(_fetch_isolating_bad(batch, stats))
    return all_pairs


# ---------------------------------------------------------------------------
# Fast path for discovery: /tokens/v1/{chainId}/{addresses} returns ONE pool
# per coin (its main pool) for up to 30 coins per request, with no crowding -
# ~15x fewer requests than the all-pools endpoint.
# ---------------------------------------------------------------------------
TOKENS_V1_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{addresses}"
V1_BATCH = 30

# Chains DexScreener's v1 endpoint knows under OUR chain name. Coins on any
# other chain skip v1 and go straight to the chain-agnostic all-pools endpoint
# (an unknown chainId would just fail every request).
V1_CHAINS = {"ethereum", "bsc", "base", "solana", "polygon", "arbitrum", "optimism",
             "avalanche", "fantom", "cronos", "zksync", "linea", "scroll", "mantle",
             "blast", "sui", "aptos", "ton", "tron", "robinhood"}


def _fetch_v1(chain, batch, stats):
    """One /tokens/v1 request (retries 429 / 5xx / network). Returns the pools,
    or [] on failure - failed coins are then picked up by the fallback."""
    stats["v1_requests"] += 1
    url = TOKENS_V1_URL.format(chain=chain, addresses=",".join(batch))
    for wait in [0] + RETRY_WAITS:
        time.sleep(wait or REQUEST_DELAY)
        try:
            r = SESSION.get(url, timeout=15)
        except requests.exceptions.RequestException:
            continue
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                break
            return data if isinstance(data, list) else (data.get("pairs") or [])
        if r.status_code == 429 or r.status_code >= 500:
            continue
        break
    stats["v1_failed"] += 1
    return []


def _base_address(p):
    return ((p.get("baseToken") or {}).get("address") or "").lower()


def fetch_pairs_for_candidates(candidates):
    """
    Enrich discovery candidates ({"contract", "chain"}) with DexScreener pools.
      1. /tokens/v1 per chain, 30 coins/request -> each coin's main pool.
      2. Coins v1 didn't return (other chains, failed requests, not listed
         under that chain) -> the chain-agnostic all-pools endpoint.
      3. Coins that look younger than MIN_AGE_DAYS from those pools -> also
         fetch ALL their pools, so the age filter sees the OLDEST pool (a coin
         whose main pool is new, but which traded long before, is old).
    Pools are de-duplicated. Returns a flat list of pair dicts.
    """
    from age_check import MIN_AGE_DAYS

    stats = _new_stats(len(candidates))
    v1_groups, other = {}, []
    for c in candidates:
        addr = c.get("contract") or ""
        if not usable_for_dexscreener(addr):
            stats["skipped_unusable"] += 1
            continue
        chain = (c.get("chain") or "").lower()
        if chain in V1_CHAINS:
            v1_groups.setdefault(chain, []).append(addr)
        else:
            other.append(addr)

    pairs = []
    for chain, addrs in v1_groups.items():
        for batch in chunked(addrs, V1_BATCH):
            pairs.extend(_fetch_v1(chain, batch, stats))
    covered = {_base_address(p) for p in pairs}
    stats["v1_covered"] = len(covered)

    # 2. Everything v1 didn't cover -> all-pools endpoint (chain-agnostic).
    missing = other + [a for addrs in v1_groups.values() for a in addrs
                       if a.lower() not in covered]
    stats["fallback_addresses"] = len(missing)
    pairs.extend(_fetch_all_pools(missing, stats))

    # 3. Young-looking coins -> check ALL their pools for an older one.
    #    (Re-fetch with the original spelling: Solana addresses are case-sensitive.)
    original = {a.lower(): a for a in other}
    original.update({a.lower(): a for addrs in v1_groups.values() for a in addrs})
    created = {}
    for p in pairs:
        if p.get("pairCreatedAt"):
            k = _base_address(p)
            created[k] = min(created.get(k, p["pairCreatedAt"]), p["pairCreatedAt"])
    young = [original[k] for k in {_base_address(p) for p in pairs}
             if k in original and (age_in_days(created.get(k)) or 0) < MIN_AGE_DAYS]
    stats["young_rechecked"] = len(young)
    pairs.extend(_fetch_all_pools(young, stats))

    # De-duplicate pools that came back from more than one request.
    seen, unique = set(), []
    for p in pairs:
        k = (p.get("chainId"), p.get("pairAddress"))
        if k not in seen:
            seen.add(k)
            unique.append(p)

    LAST_FETCH_STATS.clear()
    LAST_FETCH_STATS.update(stats)
    return unique


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
