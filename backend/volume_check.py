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

Rising rule (LENIENT - endpoint comparison, not strict day-over-day):
    day5 (most recent complete day) >  day1 (oldest of the 5) -> rising  -> KEEP
    day5 <= day1                                              -> falling -> DROP
    no / insufficient CoinGecko history for this contract     -> unknown -> DROP

check_volume_rising(contract, chain) -> dict:
    {"verdict": "rising"|"falling"|"unknown",
     "volumes": [d1..d5] | None, "day1": float|None, "day5": float|None}
Never raises; any network/parse error degrades to "unknown".
"""

import os
import time
from datetime import date, datetime, timezone

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
FETCH_DAYS = 7       # request extra to cover the partial start + today

# ROTATING BUDGET: each run checks volume for at most this many coins. Without
# a candidate cap ~1,500 coins pass age daily, but each check is one CoinGecko
# call and the free tier allows 10,000/month (30/min). So each run picks the
# coins checked longest ago (never-checked first), and every candidate gets
# its turn over several days. Coins not picked this run are SKIPPED (neither
# saved nor dropped). Raise this if you move to a paid CoinGecko plan.
VOLUME_BUDGET = 150

# Per-process cache: fetch each coin's volume history at most once per run.
_memo = {}


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

    time.sleep(CG_DELAY)
    url = CG_MC_URL.format(platform=platform, contract=contract)
    try:
        r = requests.get(url, params={"vs_currency": "usd", "days": FETCH_DAYS},
                         headers={"x-cg-demo-api-key": CG_KEY}, timeout=30)
        if r.status_code != 200:
            _memo[key] = None          # coin not on CoinGecko by this contract
            return None
        tv = r.json().get("total_volumes") or []
    except (requests.exceptions.RequestException, ValueError):
        return None                    # transient -> unknown (don't cache)

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
    if not vols or len(vols) < WINDOW_DAYS:
        return {"verdict": "unknown", "volumes": None, "day1": None, "day5": None}
    day1, day5 = vols[0], vols[-1]
    verdict = "rising" if day5 > day1 else "falling"
    return {"verdict": verdict, "volumes": vols, "day1": day1, "day5": day5}


# ---------------------------------------------------------------------------
# Rotation (see VOLUME_BUDGET) - state lives in the volume_checks table so it
# survives between runs (the GitHub Actions runner starts fresh every day).
# ---------------------------------------------------------------------------
def _rotation_key(chain, contract):
    return ((chain or "").lower(), (contract or "").lower())


def pick_for_volume_check(session, coins, budget=VOLUME_BUDGET):
    """
    From (chain, contract) pairs, choose up to `budget` to check this run:
    never-checked first, then the ones checked longest ago. Returns the chosen
    keys as a set of (chain_lower, contract_lower).
    """
    from models import VolumeCheck

    last = {(r.chain, r.contract): r.last_checked
            for r in session.query(VolumeCheck.chain, VolumeCheck.contract,
                                   VolumeCheck.last_checked)}
    keys = sorted({_rotation_key(ch, ct) for ch, ct in coins},
                  key=lambda k: (last.get(k) is not None, last.get(k) or date.min, k))
    return set(keys[:budget])


def record_volume_check(session, chain, contract, verdict, today):
    """Remember that this coin's volume was checked today (and the verdict)."""
    from models import VolumeCheck

    ch, ct = _rotation_key(chain, contract)
    row = session.query(VolumeCheck).filter_by(chain=ch, contract=ct).first()
    if row is None:
        session.add(VolumeCheck(chain=ch, contract=ct, last_checked=today,
                                last_verdict=verdict))
    else:
        row.last_checked = today
        row.last_verdict = verdict


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
