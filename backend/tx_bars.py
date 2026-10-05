"""
tx_bars.py
----------
Codex data for a coin's DETAIL PAGE (dashboard only - nothing here keeps or
drops a coin).

1. Daily bars - Codex getBars, resolution "1D", by contract + networkId.
   ONE request returns every day back to MAX_HISTORY_DAYS (Codex's limit is
   1,500 data points per request), with per-day buys, sells, buyers,
   sellers, buyVolume, sellVolume, volume and transactions. Codex sends the
   volume fields as STRINGS - converted to numbers here. From that single
   response we keep:
     * the Transactions chart: the last WINDOW_DAYS (15) COMPLETE UTC days -
       today's partial day is left out, like the volume check; a younger
       coin has fewer bars. The window rolls by itself each day.
     * 7d volume  = the last 7 complete days
     * Total volume since the coin's first trading day on Codex (capped at
       the last 1,500 days for coins older than ~4 years - `since` says
       from when)
     * liquidity now (latest bar) and 7 days ago - the Liquidity section's
       total, 7d growth and weekly Liquidity / Market cap. Same level as the
       liquidity tracking.py stores (both Codex), unlike
       getDetailedTokenStats' liquidity, which counts other pools - so the
       two are never mixed in one comparison.
   Cached per coin per UTC day (tx_bars_cache) - it only changes once a day.

2. Live 4h / 24h volume and 4h / 24h liquidity CHANGE - Codex
   getDetailedTokenStats (durations hour4, day1; it has no 7-day duration).
   The liquidity change is its own now-vs-then (never compared with the
   getBars level). These roll every minute, so they're cached per coin for
   LIVE_TTL_MINUTES (vol_stats_cache).

Cost: only when someone opens a coin's page - at most 1 getBars request per
coin per day plus 1 getDetailedTokenStats request per coin per hour.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
CODEX_KEY = os.getenv("CODEX_API_KEY")
CODEX_URL = "https://graph.codex.io/graphql"

WINDOW_DAYS = 15            # Transactions chart
MAX_HISTORY_DAYS = 1499     # one getBars request (Codex max 1,500 points)
LIVE_TTL_MINUTES = 60       # 4h / 24h volume cache

BARS_QUERY = """
query ($symbol: String!, $from: Int!, $to: Int!) {
  getBars(symbol: $symbol, from: $from, to: $to, resolution: "1D",
          removeLeadingNullValues: true) {
    t buys sells buyers sellers buyVolume sellVolume volume transactions liquidity
  }
}"""

STATS_QUERY = """
query ($addr: String!, $net: Int!) {
  getDetailedTokenStats(tokenAddress: $addr, networkId: $net,
                        durations: [hour4, day1], bucketCount: 1) {
    stats_hour4 { statsUsd { volume { currentValue } liquidity { currentValue previousValue } } }
    stats_day1  { statsUsd { volume { currentValue } liquidity { currentValue previousValue } } }
  }
}"""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _today_utc():
    return datetime.now(timezone.utc).date()


def _codex(query, variables):
    """(data, error) for one Codex GraphQL request. Never raises."""
    if not CODEX_KEY:
        return None, "no Codex key configured"
    try:
        body = requests.post(CODEX_URL, json={"query": query, "variables": variables},
                             headers={"Authorization": CODEX_KEY}, timeout=30).json()
    except (requests.exceptions.RequestException, ValueError) as e:
        return None, f"Codex request failed ({str(e)[:80]})"
    if body.get("errors"):
        return None, "Codex: " + (body["errors"][0].get("message") or "error")[:120]
    return body.get("data") or {}, None


def _network(chain):
    from liquidity_check import CHAIN_TO_NETWORK
    return CHAIN_TO_NETWORK.get((chain or "").lower())


def fetch_from_codex(contract, chain):
    """(daily, error). daily = {"bars": last WINDOW_DAYS complete days (oldest
    first), "vol_7d", "vol_total", "since"} from ONE getBars request."""
    network = _network(chain)
    if not network:
        return None, f"Codex doesn't cover the {chain} chain"

    today = _today_utc()
    day0 = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    frm = int((day0 - timedelta(days=MAX_HISTORY_DAYS)).timestamp())
    data, error = _codex(BARS_QUERY, {"symbol": f"{contract}:{network}",
                                      "from": frm, "to": int(time.time())})
    if error:
        return None, error
    b = data.get("getBars") or {}

    days, liq_by_day = [], {}
    for i, t in enumerate(b.get("t") or []):
        day = datetime.fromtimestamp(t, tz=timezone.utc).date()

        def at(field):
            vals = b.get(field) or []
            return vals[i] if i < len(vals) else None
        if at("liquidity") is not None:
            liq_by_day[day] = _num(at("liquidity"))
        if day >= today:
            continue                       # today's partial day - left out
        days.append({
            "date": day.isoformat(),
            "buys": int(_num(at("buys"))),
            "sells": int(_num(at("sells"))),
            "buyers": int(_num(at("buyers"))),
            "sellers": int(_num(at("sellers"))),
            "buy_volume": _num(at("buyVolume")),
            "sell_volume": _num(at("sellVolume")),
            "volume": _num(at("volume")),
            "transactions": int(_num(at("transactions"))),
        })
    # Liquidity now = the latest bar (today's, so far); 7 days ago = the bar
    # dated a week before today (or the nearest earlier one).
    liq_now = liq_by_day[max(liq_by_day)] if liq_by_day else None
    week_ago = [d for d in liq_by_day if d <= today - timedelta(days=7)]
    liq_7d_ago = liq_by_day[max(week_ago)] if week_ago else None
    return {
        "liq_now": liq_now,
        "liq_7d_ago": liq_7d_ago,
        "liq_7d_ago_date": max(week_ago).isoformat() if week_ago else None,
        "bars": days[-WINDOW_DAYS:],
        "vol_7d": sum(d["volume"] for d in days[-7:]) if days else None,
        "vol_total": sum(d["volume"] for d in days) if days else None,
        "since": days[0]["date"] if days else None,
    }, None


def get_tx_bars(session, contract, chain):
    """Daily data, cached per coin per UTC day. Returns
    {"bars", "vol_7d", "vol_total", "since", "error"}."""
    from models import TxBarsCache

    key_chain, key_contract = (chain or "").lower(), (contract or "").lower()
    today = _today_utc()
    row = (session.query(TxBarsCache)
           .filter_by(chain=key_chain, contract=key_contract).first())
    cached = json.loads(row.bars) if row is not None else None
    stale_format = cached is not None and not (isinstance(cached, dict) and "liq_now" in cached)
    if isinstance(cached, list):           # stored before 7d / total existed
        cached = {"bars": cached}
    empty = {"bars": [], "vol_7d": None, "vol_total": None, "since": None,
             "liq_now": None, "liq_7d_ago": None, "liq_7d_ago_date": None}
    if cached is not None:
        cached = {**empty, **cached}
    if row is not None and row.day == today and not stale_format:
        return {**cached, "error": None}

    daily, error = fetch_from_codex(contract, chain)
    if error:
        # Keep showing the last saved data rather than nothing if Codex failed.
        return {**(cached or empty), "error": error}

    if row is None:
        row = TxBarsCache(chain=key_chain, contract=key_contract)
        session.add(row)
    row.day = today
    row.bars = json.dumps(daily)
    row.fetched_at = _now_naive()
    session.commit()
    return {**daily, "error": None}


def get_live_volume(session, contract, chain):
    """4h / 24h volume + 4h / 24h liquidity change in % (getDetailedTokenStats),
    cached LIVE_TTL_MINUTES per coin. Returns
    {"vol_4h", "vol_24h", "liq_chg_4h", "liq_chg_24h", "error"}."""
    from models import VolStatsCache

    key_chain, key_contract = (chain or "").lower(), (contract or "").lower()
    row = (session.query(VolStatsCache)
           .filter_by(chain=key_chain, contract=key_contract).first())
    fresh_after = _now_naive() - timedelta(minutes=LIVE_TTL_MINUTES)
    def out(r, error=None):
        return {"vol_4h": r.vol_4h, "vol_24h": r.vol_24h, "liq_chg_4h": r.liq_chg_4h,
                "liq_chg_24h": r.liq_chg_24h, "error": error}

    if row is not None and row.fetched_at >= fresh_after:
        return out(row)

    network = _network(chain)
    error = None if network else f"Codex doesn't cover the {chain} chain"
    data = None
    if network:
        data, error = _codex(STATS_QUERY, {"addr": contract, "net": network})
    if error:
        if row is not None:
            return out(row, error)
        return {"vol_4h": None, "vol_24h": None, "liq_chg_4h": None,
                "liq_chg_24h": None, "error": error}

    s = data.get("getDetailedTokenStats") or {}

    def vol(key):
        v = (((s.get(key) or {}).get("statsUsd") or {}).get("volume") or {}).get("currentValue")
        return None if v is None else _num(v)

    def liq_change(key):
        lq = ((s.get(key) or {}).get("statsUsd") or {}).get("liquidity") or {}
        cur, prev = lq.get("currentValue"), lq.get("previousValue")
        if cur is None or not _num(prev):
            return None
        return (_num(cur) - _num(prev)) / _num(prev) * 100

    if row is None:
        row = VolStatsCache(chain=key_chain, contract=key_contract)
        session.add(row)
    row.vol_4h, row.vol_24h = vol("stats_hour4"), vol("stats_day1")
    row.liq_chg_4h, row.liq_chg_24h = liq_change("stats_hour4"), liq_change("stats_day1")
    row.fetched_at = _now_naive()
    session.commit()
    return out(row)
