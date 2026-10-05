"""
security_detail.py
------------------
The SECURITY CHECK block on a coin's detail page (dashboard only - the
honeypot FILTER step stays in security_check.py and is not changed).

GoPlus token_security (free, no key) by contract + chain, the same endpoints
and rate-limit handling as the filter (security_check._goplus). Fetched when a
detail page is opened, then cached per coin per UTC day (security_cache).

Five checks, each True (good) / False (bad) / None (unknown):
  1. Contract verified   - EVM is_open_source (not applicable on Solana)
  2. Ownership renounced - EVM: owner is empty / zero / dead address.
                           Solana: nobody can change metadata or balances
                           (metadata + balance-mutable authorities revoked)
  3. Mint disabled       - EVM is_mintable = 0 / Solana mint authority revoked
  4. Honeypot safe       - EVM: not a honeypot and can sell all /
                           Solana: token is transferable
  5. Liquidity locked    - % of the pool (LP) tokens that are locked or
                           burned (lp_holders); None if GoPlus lists none

Overall verdict:
  * "safe"    - honeypot safe AND mint disabled AND no RISK flag
  * "caution" - otherwise (a check failed or couldn't be confirmed, or a
                risky permission is present)
  * "unknown" - GoPlus doesn't cover the chain / has no data for the token
RISK flags (cause "caution"): honeypot / can't sell, mint enabled, hidden
owner, owner can reclaim ownership or change balances, self-destruct,
pausable transfers, blacklist, modifiable or high (>10%) tax, trading
cooldown, upgradeable proxy, unverified source (EVM); freeze / close /
balance-change authority, transfer fee / hook (Solana).
NOTE flags (shown, but don't make it "caution" on their own): ownership
not renounced, liquidity not / barely locked.
"""

import json
from datetime import datetime, timezone

DEAD_OWNERS = {"", "0x0000000000000000000000000000000000000000",
               "0x000000000000000000000000000000000000dead"}
HIGH_TAX = 0.10          # 10%
LOW_LOCK_PCT = 50        # below this "liquidity barely locked" is noted


def _flag(v):
    if v is None or v == "":
        return None
    return str(v).strip() in ("1", "true", "True")


def _sol_status(x):
    return _flag(x.get("status")) if isinstance(x, dict) else _flag(x)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _locked_pct(lp_holders):
    """% of LP tokens locked or burned (to a dead / zero address)."""
    if not lp_holders:
        return None
    total = 0.0
    for h in lp_holders:
        addr = (h.get("address") or h.get("account") or "").lower()
        if h.get("is_locked") in (1, "1") or addr in DEAD_OWNERS or addr.endswith("dead"):
            total += _num(h.get("percent")) or 0
    return round(min(total * 100, 100), 2)


def classify_evm(info):
    honeypot, cannot_sell = _flag(info.get("is_honeypot")), _flag(info.get("cannot_sell_all"))
    mintable = _flag(info.get("is_mintable"))
    owner = info.get("owner_address")
    checks = {
        "verified": _flag(info.get("is_open_source")),
        "renounced": None if owner is None else owner.strip().lower() in DEAD_OWNERS,
        "mint_disabled": None if mintable is None else not mintable,
        "honeypot_safe": (None if honeypot is None and cannot_sell is None
                          else not (honeypot or cannot_sell)),
        "liquidity_locked_pct": _locked_pct(info.get("lp_holders")),
    }
    risks = []
    for field, text in (("hidden_owner", "Hidden owner"),
                        ("can_take_back_ownership", "Owner can take back ownership"),
                        ("owner_change_balance", "Owner can change balances"),
                        ("selfdestruct", "Contract can self-destruct"),
                        ("transfer_pausable", "Transfers can be paused"),
                        ("is_blacklisted", "Has a blacklist"),
                        ("slippage_modifiable", "Tax can be changed"),
                        ("personal_slippage_modifiable", "Tax can be set per wallet"),
                        ("trading_cooldown", "Trading cooldown"),
                        ("cannot_buy", "Cannot be bought"),
                        ("is_proxy", "Upgradeable (proxy) contract")):
        if _flag(info.get(field)):
            risks.append(text)
    for field, text in (("buy_tax", "Buy tax"), ("sell_tax", "Sell tax")):
        t = _num(info.get(field))
        if t is not None and t > HIGH_TAX:
            risks.append(f"{text} {t * 100:.0f}%")
    return checks, risks


def classify_solana(info):
    non_transferable = _flag(info.get("non_transferable"))
    mintable = _sol_status(info.get("mintable"))
    meta = _sol_status(info.get("metadata_mutable"))
    bal = _sol_status(info.get("balance_mutable_authority"))
    checks = {
        "verified": None,                    # no "source verified" on Solana
        "renounced": None if meta is None and bal is None else not (meta or bal),
        "mint_disabled": None if mintable is None else not mintable,
        "honeypot_safe": None if non_transferable is None else not non_transferable,
        "liquidity_locked_pct": _locked_pct(info.get("lp_holders")),
    }
    risks = []
    if _sol_status(info.get("freezable")):
        risks.append("Accounts can be frozen")
    if _sol_status(info.get("closable")):
        risks.append("Token accounts can be closed")
    if bal:
        risks.append("Authority can change balances")
    if info.get("transfer_fee"):
        risks.append("Has a transfer fee")
    if info.get("transfer_hook"):
        risks.append("Has a transfer hook")
    return checks, risks


def assess(checks, risks, chain):
    """Overall verdict + the exact flags, from the 5 checks + risk list."""
    flags = []                               # (severity, text)
    if checks["honeypot_safe"] is False:
        flags.append(("risk", "Honeypot - can't sell" if chain != "solana" else "Non-transferable - can't sell"))
    elif checks["honeypot_safe"] is None:
        flags.append(("risk", "Couldn't confirm it can be sold"))
    if checks["mint_disabled"] is False:
        flags.append(("risk", "Mint not disabled - new tokens can be created"))
    elif checks["mint_disabled"] is None:
        flags.append(("risk", "Couldn't confirm minting is disabled"))
    if checks["verified"] is False:
        flags.append(("risk", "Contract source not verified"))
    flags += [("risk", r) for r in risks]
    if checks["renounced"] is False:
        flags.append(("note", "Ownership not renounced"))
    lock = checks["liquidity_locked_pct"]
    if lock is not None and lock < LOW_LOCK_PCT:
        flags.append(("note", "Liquidity not locked" if lock == 0 else f"Only {lock:g}% of liquidity locked"))
    verdict = "caution" if any(s == "risk" for s, _ in flags) else "safe"
    return verdict, flags


def check_coin(contract, chain):
    """Fresh GoPlus lookup -> {"verdict", "checks", "flags", "error"}."""
    from security_check import _goplus, GOPLUS_EVM, GOPLUS_SOLANA, CHAIN_IDS

    chain = (chain or "").lower()
    unknown = {"verdict": "unknown", "checks": None, "flags": []}
    if chain == "solana":
        data = _goplus(GOPLUS_SOLANA, {"contract_addresses": contract})
    elif chain in CHAIN_IDS:
        data = _goplus(f"{GOPLUS_EVM}/{CHAIN_IDS[chain]}", {"contract_addresses": contract})
    else:
        return {**unknown, "error": "Security data not available for this chain",
                "unsupported": True}
    if not data or data.get("code") != 1:
        return {**unknown, "error": "GoPlus didn't answer - try again later", "transient": True}
    result = data.get("result") or {}
    info = result.get(contract) or result.get(contract.lower()) or next(iter(result.values()), None)
    if not info:
        return {**unknown, "error": "GoPlus has no security data for this token"}
    checks, risks = classify_solana(info) if chain == "solana" else classify_evm(info)
    verdict, flags = assess(checks, risks, chain)
    return {"verdict": verdict, "checks": checks,
            "flags": [{"severity": s, "text": t} for s, t in flags], "error": None}


def get_security(session, contract, chain):
    """check_coin, cached per coin per UTC day (security_cache). A GoPlus
    hiccup is not cached (the last saved result is shown instead)."""
    from models import SecurityCache

    key_chain, key_contract = (chain or "").lower(), (contract or "").lower()
    today = datetime.now(timezone.utc).date()
    row = (session.query(SecurityCache)
           .filter_by(chain=key_chain, contract=key_contract).first())
    if row is not None and row.day == today:
        return json.loads(row.data)
    res = check_coin(contract, chain)
    if res.pop("transient", False):
        return json.loads(row.data) if row is not None else res
    res.pop("unsupported", None)
    if row is None:
        row = SecurityCache(chain=key_chain, contract=key_contract)
        session.add(row)
    row.day = today
    row.data = json.dumps(res)
    session.commit()
    return res
