"""
security_check.py
-----------------
Phase 2 - filter #7: the HONEYPOT / SECURITY check, using the free GoPlus
Security API (no API key needed).

A "honeypot" is a token you can BUY but can't SELL - a trap. This module asks
GoPlus whether a token is safe to trade.

The verdict is one of three values:
    "safe"     -> GoPlus checked it and found no honeypot / cannot-sell problem
    "unsafe"   -> confirmed honeypot or cannot-sell -> we must NOT save it
    "unknown"  -> we couldn't check (API down, unsupported chain, no data)
                  -> we do NOT reject it (we don't want to lose good coins)

It also captures extra info when available: contract verified, ownership
renounced, mint disabled, and buy/sell tax.

GoPlus has separate endpoints for EVM chains (Ethereum, BSC, Base, ...) and
for Solana. Chains it doesn't support return "unknown".

Run it (tests a few coins):
    python security_check.py
"""

import requests

# EVM chains: /token_security/{chain_id}?contract_addresses=...
GOPLUS_EVM = "https://api.gopluslabs.io/api/v1/token_security"
# Solana has its own endpoint.
GOPLUS_SOLANA = "https://api.gopluslabs.io/api/v1/solana/token_security"

# Map a DexScreener chain name -> GoPlus numeric chain id (EVM chains only).
CHAIN_IDS = {
    "ethereum": "1",
    "bsc": "56",
    "base": "8453",
    "polygon": "137",
    "arbitrum": "42161",
    "optimism": "10",
    "avalanche": "43114",
    "fantom": "250",
    "cronos": "25",
    "zksync": "324",
    "linea": "59144",
    "scroll": "534352",
    "mantle": "5000",
    "opbnb": "204",
    "gnosis": "100",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _get_json(url, params, timeout=15):
    """GET a URL and return parsed JSON, or None on any failure (never raises)."""
    try:
        r = requests.get(url, params=params, timeout=timeout,
                         headers={"User-Agent": "crypto-screener/0.1"})
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print(f"[WARN] GoPlus request failed: {e}")
        return None
    except ValueError:
        print("[WARN] GoPlus returned invalid JSON")
        return None


def _flag(v):
    """GoPlus uses strings '1'/'0'. Turn into True/False, or None if missing."""
    if v is None:
        return None
    return str(v) == "1"


def _tax_pct(v):
    """GoPlus tax is a fraction ('0.1' = 10%). Return it as a percent number."""
    if v is None or v == "":
        return None
    try:
        return round(float(v) * 100, 2)
    except (TypeError, ValueError):
        return None


def _result(verdict, reason, **extra):
    """Build a uniform result dict, defaulting the info fields to None."""
    base = {
        "verdict": verdict,
        "reason": reason,
        "is_honeypot": None,
        "cannot_sell": None,
        "verified": None,        # is the contract source verified / open?
        "renounced": None,       # is ownership renounced?
        "mint_disabled": None,   # is minting disabled (supply can't inflate)?
        "buy_tax": None,         # percent
        "sell_tax": None,        # percent
    }
    base.update(extra)
    return base


def _unknown(reason):
    return _result("unknown", reason)


# ---------------------------------------------------------------------------
# Classify a GoPlus EVM response into our verdict.
# ---------------------------------------------------------------------------
def classify_evm(info):
    honeypot = _flag(info.get("is_honeypot"))
    cannot_sell = _flag(info.get("cannot_sell_all"))
    verified = _flag(info.get("is_open_source"))
    mintable = _flag(info.get("is_mintable"))
    mint_disabled = None if mintable is None else (not mintable)

    owner = info.get("owner_address")
    if owner is None:
        renounced = None
    else:
        renounced = owner.strip() == "" or owner.lower() in (
            "0x0000000000000000000000000000000000000000",
            "0x000000000000000000000000000000000000dead",
        )

    buy_tax = _tax_pct(info.get("buy_tax"))
    sell_tax = _tax_pct(info.get("sell_tax"))

    if honeypot or cannot_sell:
        verdict = "unsafe"
        reason = "honeypot" if honeypot else "cannot sell all"
    else:
        verdict = "safe"
        reason = "no honeypot / token is sellable"

    return _result(
        verdict, reason,
        is_honeypot=honeypot, cannot_sell=cannot_sell,
        verified=verified, renounced=renounced, mint_disabled=mint_disabled,
        buy_tax=buy_tax, sell_tax=sell_tax,
    )


def _check_evm(contract, chain_id):
    data = _get_json(f"{GOPLUS_EVM}/{chain_id}", {"contract_addresses": contract})
    if not data:
        return _unknown("GoPlus API error")
    if data.get("code") != 1:
        return _unknown(f"GoPlus code {data.get('code')}: {data.get('message')}")
    result = data.get("result") or {}
    # GoPlus keys the result by the lowercased address.
    info = result.get(contract.lower())
    if info is None and result:
        info = next(iter(result.values()))
    if not info:
        return _unknown("no security data for this token")
    return classify_evm(info)


def _solana_status(x):
    """Solana fields like 'mintable' are objects {status, authority}."""
    if isinstance(x, dict):
        return _flag(x.get("status"))
    return _flag(x)


def _check_solana(contract):
    data = _get_json(GOPLUS_SOLANA, {"contract_addresses": contract})
    if not data:
        return _unknown("GoPlus API error (solana)")
    if data.get("code") != 1:
        return _unknown(f"GoPlus code {data.get('code')} (solana)")
    result = data.get("result") or {}
    info = result.get(contract)
    if info is None and result:
        info = next(iter(result.values()))
    if not info:
        return _unknown("no security data (solana)")

    # On Solana the honeypot analog is a NON-TRANSFERABLE token (can't be sold).
    non_transferable = _flag(info.get("non_transferable"))
    mintable = _solana_status(info.get("mintable"))
    freezable = _solana_status(info.get("freezable"))
    mint_disabled = None if mintable is None else (not mintable)

    if non_transferable is True:
        verdict, reason = "unsafe", "non-transferable (cannot sell)"
    elif non_transferable is False:
        verdict, reason = "safe", "token is transferable"
    else:
        # Couldn't determine transferability -> stay safe from false rejects.
        verdict, reason = "unknown", "no transferability data (solana)"

    return _result(
        verdict, reason,
        cannot_sell=non_transferable, mint_disabled=mint_disabled,
        freezable=freezable,
    )


# ---------------------------------------------------------------------------
# The public function used by save_to_db.py
# ---------------------------------------------------------------------------
def check_token_security(contract, chain):
    """
    Return a verdict dict for a coin. Never raises; returns "unknown" on any
    problem so a failed check can't wrongly reject a good coin.
    """
    chain = (chain or "").lower()

    if chain == "solana":
        return _check_solana(contract)

    chain_id = CHAIN_IDS.get(chain)
    if not chain_id:
        return _unknown(f"chain '{chain}' not supported by GoPlus")

    return _check_evm(contract, chain_id)


# ---------------------------------------------------------------------------
# Manual test
# ---------------------------------------------------------------------------
def _print(label, r):
    print(f"{label}")
    print(f"    verdict = {r['verdict'].upper():<8} ({r['reason']})")
    print(f"    honeypot={r['is_honeypot']}  cannot_sell={r['cannot_sell']}  "
          f"verified={r['verified']}  renounced={r['renounced']}  "
          f"mint_disabled={r['mint_disabled']}  buy_tax={r['buy_tax']}  sell_tax={r['sell_tax']}")
    print()


def main():
    # A mix of chains: a known-safe blue chip, our own DB coins, and an
    # unsupported chain (robinhood) which must come back "unknown".
    test_coins = [
        ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "ethereum", "USDC (blue chip, expect SAFE)"),
        ("0x6982508145454Ce325dDbE47a25d4ec3d2311933", "ethereum", "PEPE (expect SAFE)"),
        ("0x10c56F005a379f8eAfc88ff5c3f40d30F0031AC9", "base",     "Strike Robot (our coin, base)"),
        ("VzqFToxrHqs7BFtfWvwmGAn6nv7p9W3zdmW95iTpump", "solana",  "YesBlob (our coin, solana)"),
        ("E4Ap4icMLwKot8rkkTbq5JkS5kZxt5XCE3yfxbzYBjHx", "solana", "Buy The Cat (our coin, solana)"),
        ("0x0000000000000000000000000000000000000000", "robinhood", "robinhood chain (unsupported -> UNKNOWN)"),
    ]

    print("=" * 70)
    print("  GoPlus SECURITY CHECK - live results")
    print("=" * 70)
    for contract, chain, label in test_coins:
        _print(label, check_token_security(contract, chain))

    # Prove the UNSAFE branch works, using a simulated honeypot response
    # (this is a logic test of our classifier, NOT a live API call).
    print("=" * 70)
    print("  Logic self-test: simulated honeypot response (not a live call)")
    print("=" * 70)
    fake_honeypot = {
        "is_honeypot": "1", "cannot_sell_all": "1", "is_open_source": "0",
        "is_mintable": "1", "owner_address": "0x1234abcd",
        "buy_tax": "0.05", "sell_tax": "0.99",
    }
    _print("Simulated honeypot token", classify_evm(fake_honeypot))


if __name__ == "__main__":
    main()
