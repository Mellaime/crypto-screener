"""
revenue_check.py
----------------
Phase 2 - filter #6: does the project generate ANY fees / revenue?

The point is to remove EMPTY memecoins (a token with no product behind it),
while keeping real projects. The rule is about EXISTENCE, not amount: even
$0.01/day of fees counts as "has revenue". We are NOT checking if revenue is
high or growing.

We use the free DefiLlama API (no key needed) and return one of:
    "HAS_REVENUE" -> DefiLlama shows it generates fees/revenue (ANY amount)  -> PASS
    "NO_REVENUE"  -> DefiLlama tracks it but it has NO fees mechanism         -> REJECT
    "UNKNOWN"     -> DefiLlama simply doesn't list it                         -> PASS
                     (many small/new coins aren't on DefiLlama; we must NOT
                      reject them or we'd lose good coins)

Why two DefiLlama sources?
    * /overview/fees  -> the list of protocols that DO generate fees/revenue.
                         A match here = HAS_REVENUE.
    * /protocols      -> everything DefiLlama tracks. If a coin is tracked here
                         (matched by contract address) but is NOT in the fees
                         list, that is a POSITIVE "no fees mechanism" = NO_REVENUE.
    * Not in either   -> UNKNOWN (we don't know, so we don't reject).

Run it (tests a few coins):
    python revenue_check.py
"""

import re
import requests

FEES_URL = "https://api.llama.fi/overview/fees"
PROTOCOLS_URL = "https://api.llama.fi/protocols"


def _get_json(url, params=None, timeout=30):
    """GET JSON, or None on any failure (never raises)."""
    try:
        r = requests.get(url, params=params or {}, timeout=timeout,
                         headers={"User-Agent": "crypto-screener/0.1"})
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print(f"[WARN] DefiLlama request failed ({url}): {e}")
        return None
    except ValueError:
        print(f"[WARN] DefiLlama returned invalid JSON ({url})")
        return None


def _norm(s):
    """Normalize a name/symbol: lowercase, keep only letters+digits."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _first_word_key(name):
    """Normalized FIRST word of a protocol name ('Raydium AMM' -> 'raydium')."""
    if not name:
        return ""
    parts = name.strip().split()
    return _norm(parts[0]) if parts else ""


def _better_entry(a, b):
    """When two protocols share a key, keep the one with more fees/revenue."""
    av = (a["revenue"] if a["revenue"] is not None else a["fees"]) or 0
    bv = (b["revenue"] if b["revenue"] is not None else b["fees"]) or 0
    return a if av >= bv else b


# ---------------------------------------------------------------------------
# Build the lookup indices from DefiLlama (fetched once, then cached).
# ---------------------------------------------------------------------------
_INDICES = None


def load_indices():
    """Fetch DefiLlama data and build the indices we match against."""
    # 1) Fees + revenue overview -> protocols that generate fees/revenue.
    common = {"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"}
    fees = _get_json(FEES_URL, common)
    revenue = _get_json(FEES_URL, {**common, "dataType": "dailyRevenue"})

    # Look up each metric by normalized protocol name.
    fees_by_key = {}
    if fees and fees.get("protocols"):
        for p in fees["protocols"]:
            fees_by_key[_norm(p.get("name"))] = p.get("total24h")
    revenue_by_key = {}
    if revenue and revenue.get("protocols"):
        for p in revenue["protocols"]:
            revenue_by_key[_norm(p.get("name"))] = p.get("total24h")

    # A protocol counts as "generates fees/revenue" if it appears in the fees
    # list OR the revenue list -> ONE is enough to pass (any amount). We take
    # the UNION of both lists so a coin passes on whichever DefiLlama provides.
    #
    # Keys are the protocol's full normalized name AND its first word (so
    # "Aave" matches "Aave V3" and "Raydium" matches "Raydium AMM"), but NOT
    # arbitrary substrings (so the memecoin "Pepe" does NOT match "PepeTeam").
    fee_map = {}
    fee_count = 0
    seen = set()
    all_protocols = []
    if fees and fees.get("protocols"):
        all_protocols += fees["protocols"]
    if revenue and revenue.get("protocols"):
        all_protocols += revenue["protocols"]
    for p in all_protocols:
        name = p.get("name")
        full = _norm(name)
        if not full or full in seen:
            continue
        seen.add(full)
        fee_count += 1
        entry = {
            "name": name,
            "fees": fees_by_key.get(full),        # from the fees list (or None)
            "revenue": revenue_by_key.get(full),  # from the revenue list (or None)
        }
        for k in {full, _first_word_key(name)}:
            if not k:
                continue
            fee_map[k] = _better_entry(fee_map[k], entry) if k in fee_map else entry

    # 2) Full protocols list -> "does DefiLlama track this token at all?"
    #    Indexed by contract address (the definitive, collision-free match).
    proto_addr = {}
    protos = _get_json(PROTOCOLS_URL)
    if isinstance(protos, list):
        for p in protos:
            addr = p.get("address")
            if not addr:
                continue
            # addresses can look like "ethereum:0xabc..." -> keep the 0x part.
            key = addr.split(":")[-1].lower()
            proto_addr[key] = {
                "name": p.get("name"),
                "slug": p.get("slug"),
                "symbol": p.get("symbol"),
                "category": p.get("category"),
            }

    return {"fee_map": fee_map, "fee_count": fee_count, "proto_addr": proto_addr}


def _get_indices():
    global _INDICES
    if _INDICES is None:
        _INDICES = load_indices()
    return _INDICES


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------
def _match_fees(coin_keys, idx):
    """Return the fee entry this coin matches (by full name or first word), else None."""
    fee_map = idx["fee_map"]
    for ck in coin_keys:
        if ck and ck in fee_map:
            return fee_map[ck]
    return None


def classify(name, symbol, contract, idx):
    """Return (verdict, reason, daily_revenue, matched_name)."""
    coin_keys = {_norm(name), _norm(symbol)}

    # HAS_REVENUE: the coin looks like a protocol that generates fees/revenue.
    match = _match_fees(coin_keys, idx)
    if match:
        num = match["revenue"] if match["revenue"] is not None else match["fees"]
        return "HAS_REVENUE", f"matches '{match['name']}' in DefiLlama fees/revenue", num, match["name"]

    # Is this exact token tracked by DefiLlama at all (by contract address)?
    entry = idx["proto_addr"].get((contract or "").lower())
    if entry:
        # It's tracked. Is its protocol in the fees list?
        proto_keys = {_norm(entry["name"]), _norm(entry["slug"])}
        m2 = _match_fees(proto_keys, idx)
        if m2:
            num = m2["revenue"] if m2["revenue"] is not None else m2["fees"]
            return "HAS_REVENUE", f"tracked as '{entry['name']}' (has fees)", num, entry["name"]
        # Tracked but NO fees mechanism -> positively confirmed no revenue.
        return "NO_REVENUE", f"DefiLlama tracks '{entry['name']}' but it has no fees", 0.0, entry["name"]

    # DefiLlama doesn't list it -> we can't tell -> don't reject.
    return "UNKNOWN", "not listed on DefiLlama", None, None


def check_revenue(name, symbol, contract, chain=None):
    """
    Public function used by save_to_db. Returns a dict:
        {verdict, reason, daily_revenue, matched}
    Never raises; returns UNKNOWN if DefiLlama can't be reached.
    """
    idx = _get_indices()
    # If both DefiLlama sources failed to load, we simply don't know.
    if not idx["fee_map"] and not idx["proto_addr"]:
        return {"verdict": "UNKNOWN", "reason": "DefiLlama unavailable",
                "daily_revenue": None, "matched": None}

    verdict, reason, revenue, matched = classify(name, symbol, contract, idx)
    return {"verdict": verdict, "reason": reason,
            "daily_revenue": revenue, "matched": matched}


# ---------------------------------------------------------------------------
# Manual test
# ---------------------------------------------------------------------------
def _fmt_rev(v):
    if v is None:
        return "n/a"
    return f"${v:,.2f}/day"


def main():
    test_coins = [
        ("Uniswap", "UNI", "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984", "ethereum", "DEX - expect HAS_REVENUE"),
        ("Aave", "AAVE", "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9", "ethereum", "Lending - expect HAS_REVENUE"),
        ("Raydium", "RAY", "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R", "solana", "Solana DEX - expect HAS_REVENUE"),
        ("Jupiter", "JUP", "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "solana", "Solana aggregator - expect HAS_REVENUE"),
        ("Pepe", "PEPE", "0x6982508145454Ce325dDbE47a25d4ec3d2311933", "ethereum", "Pure memecoin"),
        ("YesBlob", "YES", "VzqFToxrHqs7BFtfWvwmGAn6nv7p9W3zdmW95iTpump", "solana", "Our coin (memecoin)"),
    ]

    print("Loading DefiLlama data (fees + protocols)...")
    idx = _get_indices()
    print(f"  fees/revenue protocols: {idx['fee_count']}")
    print(f"  tracked protocols (by address): {len(idx['proto_addr'])}\n")

    print("=" * 74)
    print("  DEFILLAMA REVENUE CHECK - live results")
    print("=" * 74)
    for name, symbol, contract, chain, label in test_coins:
        r = check_revenue(name, symbol, contract, chain)
        print(f"{name} ({symbol}) - {label}")
        print(f"    verdict = {r['verdict']:<12} revenue = {_fmt_rev(r['daily_revenue'])}")
        print(f"    reason  = {r['reason']}")
        print()

    # Prove the NO_REVENUE branch with a synthetic 'tracked but no fees' token.
    print("=" * 74)
    print("  Logic self-test: token DefiLlama tracks but with no fees (synthetic)")
    print("=" * 74)
    fake_idx = {
        "fee_map": {},
        "proto_addr": {"0xdead": {"name": "TrackedNoFeeToken", "slug": "tracked-no-fee",
                                  "symbol": "TNF", "category": "Token"}},
    }
    verdict, reason, rev, matched = classify("TrackedNoFeeToken", "TNF", "0xDEAD", fake_idx)
    print(f"    verdict = {verdict}  ({reason})")


if __name__ == "__main__":
    main()
