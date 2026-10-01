"""
volume_check.py
---------------
Filter 3 - VOLUME RISING, from CoinGecko daily volume history (by contract).
Replaces the temporary "24h volume > $50K" placeholder. It runs right after
the Age gate and, like age, is a CORE filter: unknown -> DROP.

Source - CoinGecko market_chart by contract (uses COINGECKO_API_KEY):
    /coins/{platform}/contract/{address}/market_chart?vs_currency=usd&days=7
`total_volumes` is CoinGecko's rolling-24h volume sampled through the window.
We collapse it to one value per UTC day (the last reading of each day), then
drop the current in-progress day and keep the last 5 COMPLETE days.

Rolling 5-day window: always the 5 most recent COMPLETE days relative to today.
We EXCLUDE today's partial day on purpose - the daily job runs early in the UTC
day, so today would only hold a few hours of volume and almost always look
"falling". Comparing whole days makes the result independent of run time. Each
new day shifts the window automatically (newest complete day in, oldest drops),
and because CoinGecko returns real history it works from day one - no waiting
to accumulate snapshots.

Rising rule (endpoint comparison, not strict day-over-day) - volume must have
risen at least VOLUME_MIN_RISE (+35%) over the 5-day window:
    day5 (most recent complete day) >= day1 (oldest of the 5) x 1.35 -> rising  -> KEEP
    day5 <  day1 x 1.35                                                -> falling -> DROP
    no / insufficient CoinGecko history for this contract     -> unknown -> DROP

check_volume_rising(contract, chain) -> dict:
    {"verdict": "rising"|"falling"|"unknown",
     "volumes": [d1..d5] | None, "day1": float|None, "day5": float|None}
Never raises; any network/parse error degrades to "unknown".
"""

import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

# Reuse the one chain -> CoinGecko asset-platform map (defined in age_check).
from age_check import CHAIN_TO_CG_PLATFORM

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
CG_KEY = os.getenv("COINGECKO_API_KEY")

CG_MC_URL = ("https://api.coingecko.com/api/v3/coins/{platform}"
             "/contract/{contract}/market_chart")

CG_DELAY = 2.5       # be gentle with CoinGecko's demo rate limit
WINDOW_DAYS = 5      # compare over the last 5 COMPLETE days
VOLUME_MIN_RISE = 1.35   # day5 must be at least 35% above day1 to pass entry
FETCH_DAYS = 7       # request extra to cover the partial start + today

# This is the ONE-TIME entry check: rotation.py picks never-analyzed coins
# (ROTATION_BUDGET per run); a coin is never checked here twice.

# Live CoinGecko calls made by the volume step in this process (for reporting).
CALLS = 0

# Per-process cache: fetch each coin's volume history at most once per run.
_memo = {}
# Coins whose check failed TRANSIENTLY this run (network error, 429 rate limit,
# 5xx) - "unknown" for them means "couldn't ask", not "CoinGecko has nothing".
_transient = set()


def get_daily_volumes(contract, chain, window=WINDOW_DAYS):
    """
    Return the last `window` COMPLETE daily volumes (oldest -> newest) for a
    coin, matched by contract + chain, or None if CoinGecko has no / too little
    history (or the chain isn't on CoinGecko). Never raises.
    """
    platform = CHAIN_TO_CG_PLATFORM.get((chain or "").lower())
    if not platform or not contract or not CG_KEY:
        return None

    key = (platform, contract.lower())
    if key in _memo:
        return _memo[key]

    global CALLS
    CALLS += 1
    time.sleep(CG_DELAY)
    url = CG_MC_URL.format(platform=platform, contract=contract)
    try:
        r = requests.get(url, params={"vs_currency": "usd", "days": FETCH_DAYS},
                         headers={"x-cg-demo-api-key": CG_KEY}, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            _transient.add(key)        # rate limit / server error -> try again later
            return None
        if r.status_code != 200:
            _memo[key] = None          # coin not on CoinGecko by this contract
            return None
        tv = r.json().get("total_volumes") or []
    except (requests.exceptions.RequestException, ValueError):
        _transient.add(key)            # network problem -> try again later
        return None

    # Collapse to one value per UTC date (last reading of that day).
    by_day = {}
    for point in tv:
        try:
            ts, vol = point[0], point[1]
        except (TypeError, IndexError):
            continue
        day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        by_day[day] = vol

    today = datetime.now(timezone.utc).date()
    complete = sorted((d, v) for d, v in by_day.items() if d < today)  # drop today
    if len(complete) < window:
        _memo[key] = None
        return None
    vols = [v for _, v in complete[-window:]]
    _memo[key] = vols
    return vols


def check_volume_rising(contract, chain):
    """
    Decide the volume filter for one coin. Returns a dict (see module docstring):
    rising -> KEEP, falling -> DROP, unknown -> DROP.
    """
    vols = get_daily_volumes(contract, chain)
    platform = CHAIN_TO_CG_PLATFORM.get((chain or "").lower())
    transient = (platform, (contract or "").lower()) in _transient
    if not vols or len(vols) < WINDOW_DAYS:
        return {"verdict": "unknown", "volumes": None, "day1": None, "day5": None,
                "transient": transient}
    day1, day5 = vols[0], vols[-1]
    verdict = "rising" if day5 >= day1 * VOLUME_MIN_RISE else "falling"
    return {"verdict": verdict, "volumes": vols, "day1": day1, "day5": day5,
            "transient": False}


# ---------------------------------------------------------------------------
# Manual test
# ---------------------------------------------------------------------------
def main():
    samples = [
        ("Pepe", "ethereum", "0x6982508145454Ce325dDbE47a25d4ec3d2311933"),
        ("Fake", "ethereum", "0xdead000000000000000000000000000000000000"),
    ]
    print("=" * 62)
    print("  VOLUME RISING (CoinGecko 5 complete days, by contract)")
    print("=" * 62)
    for name, chain, contract in samples:
        res = check_volume_rising(contract, chain)
        vols = res["volumes"]
        vstr = "  ".join(f"${v/1e6:.2f}M" for v in vols) if vols else "n/a"
        decision = "KEEP" if res["verdict"] == "rising" else "DROP"
        print(f"  {name:<8} [{chain}]")
        print(f"     daily: {vstr}")
        print(f"     -> {decision} ({res['verdict']})")
    print("=" * 62)


if __name__ == "__main__":
    main()
