"""
security_check.py
-----------------
Phase 2 - filter #7: the HONEYPOT / SECURITY check. EVM: the free GoPlus
Security API. Solana: the free RugCheck API (GoPlus Solana only when
RugCheck is down). No API keys.

A "honeypot" is a token you can BUY but can't SELL - a trap.

The verdict is one of three values:
    "safe"     -> checked, none of the unsafe rules below hold
    "unsafe"   -> we must NOT save it (and the daily re-check drops it):
                  EVM    - honeypot, cannot sell all, buy or sell tax > 10%,
                           self-destruct
                  Solana - rugged, non-transferable, transfer fee > 10%
                  (owner-can-change-balances and an active freeze authority
                  are only recorded, never a reason to reject)
    "unknown"  -> we couldn't check (API down, unsupported chain, no data).
                  rejects() decides: on a chain GoPlus / RugCheck support,
                  entry and Check a Coin REJECT it (every coin must be
                  verified); on an unsupported chain (sui, hyperevm, ...) it
                  passes. The daily re-check never drops on "unknown".

It also returns verified / renounced / mint disabled / freeze disabled,
buy / sell tax (%), creator and owner address and the % of LP locked or
burned - store() saves them on the coin (entry filter + daily re-check).

Run it (tests a few coins):
    python security_check.py
"""

import time

import requests

# Free (keyless) GoPlus is rate-limited: after a burst of ~20 calls it answers
# code 4029 "too many requests". A 4029 must never become "unknown" (which
# PASSES the coin) - so we pace calls and back off + retry instead. Free GoPlus
# also returns only ONE token per request, so batching addresses doesn't help.
REQUEST_DELAY = 2.0        # seconds between calls (~30/min)
RATE_LIMIT_CODE = 4029
RETRY_WAITS = [10, 20, 40, 60]   # back-off before each retry after a 4029
MAX_TAX_PCT = 10.0               # buy or sell tax above this -> unsafe
BURN_ADDRESSES = ("0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead")

RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
RUGCHECK_DELAY = 1.0             # seconds before each RugCheck call (free, keyless)

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


def _goplus(url, params):
    """
    Paced GoPlus call that retries on rate limiting (code 4029). Returns the
    parsed JSON, or None on a real failure. After all retries are used up, the
    last 4029 response is returned so the caller reports it (as "unknown").
    """
    data = None
    for wait in [0] + RETRY_WAITS:
        if wait:
            print(f"[INFO] GoPlus rate limit - waiting {wait}s and retrying")
            time.sleep(wait)
        time.sleep(REQUEST_DELAY)
        data = _get_json(url, params)
        if not data or data.get("code") != RATE_LIMIT_CODE:
            return data
    return data


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
        "freeze_disabled": None, # no pause / blacklist (EVM) or freeze authority (Solana)
        "buy_tax": None,         # percent
        "sell_tax": None,        # percent
        "creator_address": None,
        "owner_address": None,
        "lp_locked_pct": None,   # % of LP locked or burned
        "supported": True,       # False: the chain has no GoPlus / RugCheck coverage
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
        renounced = owner.strip() == "" or owner.lower() in BURN_ADDRESSES

    buy_tax = _tax_pct(info.get("buy_tax"))
    sell_tax = _tax_pct(info.get("sell_tax"))
    pausable, blacklist = _flag(info.get("transfer_pausable")), _flag(info.get("is_blacklisted"))
    freeze_disabled = None if pausable is None or blacklist is None else not (pausable or blacklist)

    lp = info.get("lp_holders") or []
    lp_locked = (min(100.0, round(sum(float(h.get("percent") or 0) * 100 for h in lp
                                      if str(h.get("is_locked")) == "1"
                                      or (h.get("address") or "").lower() in BURN_ADDRESSES), 2))
                 if lp else None)

    if honeypot or cannot_sell:
        verdict, reason = "unsafe", "honeypot" if honeypot else "cannot sell all"
    elif (buy_tax or 0) > MAX_TAX_PCT or (sell_tax or 0) > MAX_TAX_PCT:
        verdict, reason = "unsafe", f"tax over {MAX_TAX_PCT:.0f}% (buy {buy_tax}%, sell {sell_tax}%)"
    elif _flag(info.get("selfdestruct")):
        verdict, reason = "unsafe", "contract can self-destruct"
    else:
        verdict, reason = "safe", "no honeypot / token is sellable"

    return _result(
        verdict, reason,
        is_honeypot=honeypot, cannot_sell=cannot_sell,
        verified=verified, renounced=renounced, mint_disabled=mint_disabled,
        freeze_disabled=freeze_disabled, buy_tax=buy_tax, sell_tax=sell_tax,
        creator_address=info.get("creator_address") or None, owner_address=owner or None,
        lp_locked_pct=lp_locked,
    )


def _check_evm(contract, chain_id):
    data = _goplus(f"{GOPLUS_EVM}/{chain_id}", {"contract_addresses": contract})
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
    data = _goplus(GOPLUS_SOLANA, {"contract_addresses": contract})
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
        freeze_disabled=None if freezable is None else not freezable,
    )


def classify_rugcheck(d):
    """A RugCheck report -> our verdict + fields."""
    tok = d.get("token") or {}
    # an authority is an address string, or sometimes an account-info object (set, address not given)
    mint_auth = d.get("mintAuthority") or tok.get("mintAuthority") or None
    freeze_auth = d.get("freezeAuthority") or tok.get("freezeAuthority") or None
    addr = lambda v: v if isinstance(v, str) else None
    fee = (d.get("transferFee") or {}).get("pct") or 0          # Token-2022 transfer fee, percent
    exts = d.get("token_extensions")                            # Token-2022 only; {"nonTransferable": false, ...}
    non_transferable = bool(exts.get("nonTransferable")) if isinstance(exts, dict) else False
    lp = [m.get("lp") or {} for m in d.get("markets") or []]
    lp_usd = sum((x.get("baseUSD") or 0) + (x.get("quoteUSD") or 0) for x in lp)
    lp_locked = round(min(100.0, sum(x.get("lpLockedUSD") or 0 for x in lp) / lp_usd * 100), 2) if lp_usd else None

    if d.get("rugged") is True:
        verdict, reason = "unsafe", "rugged (RugCheck)"
    elif non_transferable:
        verdict, reason = "unsafe", "non-transferable (cannot sell)"
    elif fee > MAX_TAX_PCT:
        verdict, reason = "unsafe", f"transfer fee over {MAX_TAX_PCT:.0f}% ({fee}%)"
    else:
        verdict, reason = "safe", "token is sellable"

    return _result(
        verdict, reason,
        cannot_sell=non_transferable or fee >= 100,
        verified=True,                               # standard SPL token program, nothing to verify
        renounced=mint_auth is None and freeze_auth is None,
        mint_disabled=mint_auth is None, freeze_disabled=freeze_auth is None,
        buy_tax=float(fee), sell_tax=float(fee),
        creator_address=addr(d.get("creator")) or None,
        owner_address=addr(mint_auth) or addr((d.get("tokenMeta") or {}).get("updateAuthority")) or None,
        lp_locked_pct=lp_locked,
    )


def _check_rugcheck(contract):
    """RugCheck first; GoPlus Solana only when RugCheck is down (5xx / timeout)."""
    time.sleep(RUGCHECK_DELAY)
    try:
        r = requests.get(RUGCHECK.format(mint=contract), timeout=30, headers={"User-Agent": "crypto-screener/0.1"})
    except requests.exceptions.RequestException as e:
        print(f"[WARN] RugCheck request failed ({type(e).__name__}) - trying GoPlus")
        return _check_solana(contract)
    if r.status_code >= 500:
        print(f"[WARN] RugCheck HTTP {r.status_code} - trying GoPlus")
        return _check_solana(contract)
    if r.status_code != 200:
        return _unknown(f"RugCheck HTTP {r.status_code}")
    try:
        return classify_rugcheck(r.json())
    except (ValueError, TypeError, AttributeError, ZeroDivisionError) as e:
        return _unknown(f"RugCheck bad response ({type(e).__name__})")


SECURITY_COLUMNS = ("verified", "renounced", "mint_disabled", "freeze_disabled", "buy_tax", "sell_tax",
                    "creator_address", "owner_address", "lp_locked_pct")


def rejects(sec):
    """Entry / Check a Coin: unsafe, or unverified on a chain we can verify."""
    return sec["verdict"] == "unsafe" or (sec["verdict"] == "unknown" and sec["supported"])


def store(coin, sec):
    """Save a check_token_security result on the coin."""
    coin.security_verdict = sec["verdict"]
    for col in SECURITY_COLUMNS:
        setattr(coin, col, sec[col])


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
        return _check_rugcheck(contract)

    chain_id = CHAIN_IDS.get(chain)
    if not chain_id:
        return {**_unknown(f"chain '{chain}' not supported by GoPlus"), "supported": False}

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
