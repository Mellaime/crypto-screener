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


# ---------------------------------------------------------------------------
# Build the lookup indices from DefiLlama (fetched once, then cached).
# Everything is matched by CONTRACT ADDRESS -> DefiLlama protocol id. Names and
# symbols are NOT used for matching: a coin that merely shares a name with a
# fee-earning protocol must not pass as "has revenue".
# ---------------------------------------------------------------------------
_INDICES = None


def load_indices():
    """Fetch DefiLlama data and build the indices we match against."""
    # 1) Fees + revenue overviews -> protocols that generate fees/revenue,
    #    keyed by their DefiLlama id (and slug as a backup).
    common = {"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"}
    fees = _get_json(FEES_URL, common)
    revenue = _get_json(FEES_URL, {**common, "dataType": "dailyRevenue"})

    fee_by_id, fee_by_slug = {}, {}
    for src, field in ((fees, "fees"), (revenue, "revenue")):
        for p in (src or {}).get("protocols") or []:
            pid = str(p.get("defillamaId") or p.get("id") or "")
            slug = (p.get("slug") or "").lower()
            entry = fee_by_id.get(pid) or fee_by_slug.get(slug) or {
                "name": p.get("displayName") or p.get("name"), "fees": None, "revenue": None}
            entry[field] = p.get("total24h")
            if pid:
                fee_by_id[pid] = entry
            if slug:
                fee_by_slug[slug] = entry

    # 2) Full protocols list -> which protocols a token CONTRACT belongs to.
    #    One token can back several protocols (e.g. V2 + V3), so it's a list.
    proto_addr = {}
    protos = _get_json(PROTOCOLS_URL)
    if isinstance(protos, list):
        for p in protos:
            addr = p.get("address")
            if not addr or addr == "-":
                continue
            # addresses can look like "ethereum:0xabc..." -> keep the address part.
            key = addr.split(":")[-1].lower()
            proto_addr.setdefault(key, []).append({
                "id": str(p.get("id") or ""),
                "slug": (p.get("slug") or "").lower(),
                "name": p.get("name"),
                "parent": (p.get("parentProtocol") or ""),
            })

    return {"fee_by_id": fee_by_id, "fee_by_slug": fee_by_slug,
            "fee_count": len(fee_by_id), "proto_addr": proto_addr}


def _get_indices():
    global _INDICES
    if _INDICES is None:
        _INDICES = load_indices()
    return _INDICES


# ---------------------------------------------------------------------------
# Matching logic - by contract address only
# ---------------------------------------------------------------------------
def classify(contract, idx):
    """Return (verdict, reason, daily_revenue, matched_name)."""
    protocols = idx["proto_addr"].get((contract or "").lower())
    if not protocols:
        # DefiLlama doesn't list this contract -> we can't tell -> don't reject.
        return "UNKNOWN", "contract not listed on DefiLlama", None, None

    for proto in protocols:
        fee = (idx["fee_by_id"].get(proto["id"])
               or idx["fee_by_slug"].get(proto["slug"])
               or (proto["parent"] and idx["fee_by_id"].get(proto["parent"])))
        if fee:
            num = fee["revenue"] if fee["revenue"] is not None else fee["fees"]
            return ("HAS_REVENUE", f"contract is '{proto['name']}', which has fees",
                    num, proto["name"])

    # Tracked by contract, but none of its protocols has a fees mechanism.
    names = ", ".join(p["name"] or "?" for p in protocols)
    return "NO_REVENUE", f"DefiLlama tracks '{names}' but it has no fees", 0.0, names


def check_revenue(name, symbol, contract, chain=None):
    """
    Public function used by save_to_db. Returns a dict:
        {verdict, reason, daily_revenue, matched}
    Never raises; returns UNKNOWN if DefiLlama can't be reached.
    """
    idx = _get_indices()
    # If both DefiLlama sources failed to load, we simply don't know.
    if not idx["fee_by_id"] and not idx["proto_addr"]:
        return {"verdict": "UNKNOWN", "reason": "DefiLlama unavailable",
                "daily_revenue": None, "matched": None}

    # name / symbol are accepted for compatibility but NOT used for matching.
    verdict, reason, revenue, matched = classify(contract, idx)
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
        "fee_by_id": {}, "fee_by_slug": {},
        "proto_addr": {"0xdead": [{"id": "999999", "slug": "tracked-no-fee",
                                   "name": "TrackedNoFeeToken", "parent": ""}]},
    }
    verdict, reason, rev, matched = classify("0xDEAD", fake_idx)
    print(f"    verdict = {verdict}  ({reason})")


if __name__ == "__main__":
    main()
