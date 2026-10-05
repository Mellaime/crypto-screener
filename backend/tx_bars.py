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
   Cached per coin per UTC day (tx_bars_cache) - it only changes once a day.

2. Live 4h / 24h volume - Codex getDetailedTokenStats (durations hour4,
   day1; it has no 7-day duration). These roll every minute, so they're
   cached per coin for LIVE_TTL_MINUTES (vol_stats_cache).

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
    t buys sells buyers sellers buyVolume sellVolume volume transactions
  }
}"""

STATS_QUERY = """
query ($addr: String!, $net: Int!) {
  getDetailedTokenStats(tokenAddress: $addr, networkId: $net,
                        durations: [hour4, day1], bucketCount: 1) {
    stats_hour4 { statsUsd { volume { currentValue } } }
    stats_day1  { statsUsd { volume { currentValue } } }
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

    days = []
    for i, t in enumerate(b.get("t") or []):
        day = datetime.fromtimestamp(t, tz=timezone.utc).date()
        if day >= today:
            continue                       # today's partial day - left out

        def at(field):
            vals = b.get(field) or []
            return vals[i] if i < len(vals) else None
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
    return {
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
    if isinstance(cached, list):           # stored before 7d / total existed
        cached = {"bars": cached, "vol_7d": None, "vol_total": None, "since": None}
        stale_format = True
    else:
        stale_format = False
    if row is not None and row.day == today and not stale_format:
        return {**cached, "error": None}

    daily, error = fetch_from_codex(contract, chain)
    if error:
        # Keep showing the last saved data rather than nothing if Codex failed.
        empty = {"bars": [], "vol_7d": None, "vol_total": None, "since": None}
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
    """4h / 24h volume (getDetailedTokenStats), cached LIVE_TTL_MINUTES per
    coin. Returns {"vol_4h", "vol_24h", "error"}."""
    from models import VolStatsCache

    key_chain, key_contract = (chain or "").lower(), (contract or "").lower()
    row = (session.query(VolStatsCache)
           .filter_by(chain=key_chain, contract=key_contract).first())
    fresh_after = _now_naive() - timedelta(minutes=LIVE_TTL_MINUTES)
    if row is not None and row.fetched_at >= fresh_after:
        return {"vol_4h": row.vol_4h, "vol_24h": row.vol_24h, "error": None}

    network = _network(chain)
    error = None if network else f"Codex doesn't cover the {chain} chain"
    data = None
    if network:
        data, error = _codex(STATS_QUERY, {"addr": contract, "net": network})
    if error:
        if row is not None:
            return {"vol_4h": row.vol_4h, "vol_24h": row.vol_24h, "error": error}
        return {"vol_4h": None, "vol_24h": None, "error": error}

    s = data.get("getDetailedTokenStats") or {}

    def vol(key):
        v = (((s.get(key) or {}).get("statsUsd") or {}).get("volume") or {}).get("currentValue")
        return None if v is None else _num(v)

    if row is None:
        row = VolStatsCache(chain=key_chain, contract=key_contract)
        session.add(row)
    row.vol_4h, row.vol_24h = vol("stats_hour4"), vol("stats_day1")
    row.fetched_at = _now_naive()
    session.commit()
    return {"vol_4h": row.vol_4h, "vol_24h": row.vol_24h, "error": None}
