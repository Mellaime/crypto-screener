"""
liquidity_check.py
------------------
Filter step 7 (LAST) - LIQUIDITY TREND, from Codex daily liquidity history.
Matched by CONTRACT + CHAIN. Uses CODEX_API_KEY from backend/.env.

Source - Codex GraphQL (https://graph.codex.io/graphql), free tier:
    getTokenBars(symbol: "<address>:<networkId>", resolution: "1D",
                 removeLeadingNullValues: true) { t liquidity }
One request per coin. removeLeadingNullValues trims the days before the coin
existed, so for a young coin the first bar is its creation day.

Window = min(30 days, coin's age):
    older than 30 days  -> liquidity ~30 days ago  vs  today
    younger             -> oldest available bar (creation)  vs  today

Rule (% change from the window's oldest value to today's):
    rising / stable / down <= 5%  -> KEEP
    down more than 5%             -> DROP
    unknown (no Codex data, chain not on Codex, zero baseline) -> PASS
      (not dropped - same "only drop on a positive signal" policy as the
       revenue and honeypot checks)

check_liquidity_trend(contract, chain) -> dict:
    {"verdict": "keep"|"drop"|"unknown", "pct": float|None,
     "base_date", "base_liq", "today_date", "today_liq", "reason"}
Never raises.
"""

import os
import time
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
CODEX_KEY = os.getenv("CODEX_API_KEY")
CODEX_URL = "https://graph.codex.io/graphql"

WINDOW_DAYS = 30          # compare against ~30 days ago (or creation if younger)
MAX_DROP_PCT = 5.0        # a fall of more than 5% drops the coin

# MINIMUM LIQUIDITY: below this on BOTH Codex and DexScreener the coin is too
# thin to analyze (its on-chain volume / transactions are near-empty - it
# trades mostly on exchanges we can't see). Codex sometimes under-reports
# (Magma: Codex $7.7K vs DexScreener $1.5M), so the HIGHER of the two counts.
MIN_LIQUIDITY_USD = 5_000


# SOURCE MISMATCH: when Codex reports more than this many times DexScreener's
# liquidity for a coin, Codex is treated as unreliable for it and DexScreener's
# reading is used instead (e.g. QAIT: Codex ~$580K vs DexScreener ~$1.5K).
MISMATCH_RATIO = 10
# (label, codex, dex) of every mismatch met in this process - printed in the
# run logs as "liquidity source mismatch - using DexScreener".
MISMATCHES = []


def codex_unreliable(codex_liq, dex_liq):
    """True when both readings exist and Codex is > MISMATCH_RATIO x DexScreener."""
    if codex_liq is None or dex_liq is None:
        return False
    return float(codex_liq) > MISMATCH_RATIO * max(float(dex_liq), 1.0)


# PARTIAL BULK READING (the other direction): Codex's BULK request
# (filterTokens, stored daily as daily_snapshots.codex_liquidity) sometimes
# covers only part of a coin's pools - ZyFAI: $5-8K stored vs ~$170K on both
# Codex's own daily bars (getBars) and DexScreener. Under 1/BULK_TOO_LOW_RATIO
# of DexScreener = a partial reading; the score then uses DexScreener instead.
BULK_TOO_LOW_RATIO = 3


def bulk_codex_too_low(codex_liq, dex_liq):
    """True when both readings exist and the bulk Codex liquidity is under
    1/BULK_TOO_LOW_RATIO of DexScreener's (a partial reading)."""
    if codex_liq is None or dex_liq is None:
        return False
    return float(codex_liq) * BULK_TOO_LOW_RATIO < float(dex_liq)


def best_liquidity(codex_liq, dex_liq, label=None):
    """(liquidity, mismatch). Normally the higher of Codex and DexScreener
    (Codex sometimes under-reports); if Codex is > MISMATCH_RATIO x
    DexScreener it is unreliable for this coin -> DexScreener's reading.
    None if neither source has a reading."""
    if codex_unreliable(codex_liq, dex_liq):
        if label is not None:
            MISMATCHES.append((label, float(codex_liq), float(dex_liq)))
        return float(dex_liq), True
    vals = [float(v) for v in (codex_liq, dex_liq) if v is not None]
    return (max(vals) if vals else None), False


def too_thin(codex_liq, dex_liq, label=None):
    """(is_too_thin, liquidity). Too thin when the liquidity that counts
    (best_liquidity: the higher of Codex and DexScreener, or DexScreener alone
    on a source mismatch) is under MIN_LIQUIDITY_USD. No reading from either
    source = unknown -> not too thin (unknown passes)."""
    best, _ = best_liquidity(codex_liq, dex_liq, label)
    if best is None:
        return False, None
    return best < MIN_LIQUIDITY_USD, best
REQUEST_DELAY = 0.3       # small pause between calls (free tier: 10k req/month)

# Our chain name -> Codex networkId (from Codex getNetworks).
CHAIN_TO_NETWORK = {
    "ethereum": 1, "base": 8453, "solana": 1399811149, "bsc": 56,
    "polygon": 137, "arbitrum": 42161, "optimism": 10, "avalanche": 43114,
    "cronos": 25, "zksync": 324, "linea": 59144, "scroll": 534352,
    "mantle": 5000, "blast": 81457, "sui": 101, "aptos": 49705,
    "tron": 728126428, "robinhood": 4663,
}

BARS_QUERY = """
query ($symbol: String!, $from: Int!, $to: Int!) {
  getTokenBars(symbol: $symbol, from: $from, to: $to, resolution: "1D",
               removeLeadingNullValues: true) { t liquidity }
}"""

# Per-process cache: one Codex call per coin per run.
_memo = {}


def _unknown(reason):
    return {"verdict": "unknown", "pct": None, "base_date": None, "base_liq": None,
            "today_date": None, "today_liq": None, "reason": reason}


def get_liquidity_series(contract, chain):
    """Daily (date, liquidity) list, oldest -> newest, or None. Never raises."""
    network = CHAIN_TO_NETWORK.get((chain or "").lower())
    if not network or not contract or not CODEX_KEY:
        return None

    key = (network, contract.lower())
    if key in _memo:
        return _memo[key]

    now = int(time.time())
    frm = now - (WINDOW_DAYS + 2) * 86400        # a little extra for the boundary
    time.sleep(REQUEST_DELAY)
    try:
        r = requests.post(
            CODEX_URL,
            json={"query": BARS_QUERY,
                  "variables": {"symbol": f"{contract}:{network}", "from": frm, "to": now}},
            headers={"Authorization": CODEX_KEY},
            timeout=30,
        )
        body = r.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"[WARN] Codex request failed: {str(e)[:100]}")
        return None                               # transient -> don't cache

    if body.get("errors"):
        print(f"[WARN] Codex error: {body['errors'][0].get('message', '')[:120]}")
        _memo[key] = None
        return None

    bars = (body.get("data") or {}).get("getTokenBars") or {}
    series = []
    for t, liq in zip(bars.get("t") or [], bars.get("liquidity") or []):
        try:
            val = float(liq)
        except (TypeError, ValueError):
            continue
        series.append((datetime.fromtimestamp(t, tz=timezone.utc).date(), val))
    _memo[key] = series or None
    return _memo[key]


def check_liquidity_trend(contract, chain):
    """keep / drop / unknown by the % change over min(30 days, age). See docstring."""
    series = get_liquidity_series(contract, chain)
    if not series or len(series) < 2:
        return _unknown("no Codex liquidity history")

    today_date, today_liq = series[-1]
    cutoff = today_date - timedelta(days=WINDOW_DAYS)
    # Oldest bar inside the window: ~30 days ago, or creation if the coin is younger.
    base_date, base_liq = next(((d, v) for d, v in series if d >= cutoff), series[0])
    if not base_liq:
        return _unknown("zero/missing baseline liquidity")

    pct = (today_liq - base_liq) / base_liq * 100
    verdict = "drop" if pct < -MAX_DROP_PCT else "keep"
    return {"verdict": verdict, "pct": round(pct, 1),
            "base_date": base_date, "base_liq": base_liq,
            "today_date": today_date, "today_liq": today_liq,
            "reason": f"{pct:+.1f}% since {base_date}"}


if __name__ == "__main__":
    for name, chain, contract in [
        ("Pepe", "ethereum", "0x6982508145454Ce325dDbE47a25d4ec3d2311933"),
        ("Unsupported chain", "near", "whatever"),
    ]:
        print(name, check_liquidity_trend(contract, chain))
