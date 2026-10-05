"""
tx_bars.py
----------
Daily TRANSACTION bars for a coin's detail page (dashboard only - nothing
here keeps or drops a coin).

Source: Codex getBars, resolution "1D", by contract + networkId. One request
returns every day at once, with per-day buys, sells, buyers, sellers,
buyVolume, sellVolume, volume and transactions (the free tier returns all of
them). Codex sends the volume fields as STRINGS - converted to numbers here.

Window: the last WINDOW_DAYS (15) COMPLETE UTC days - today's partial day is
left out, like the volume check. A coin younger than that just has fewer
bars (from its first trading day). The window rolls by itself: each new day
adds a bar and the oldest falls off.

Cost: fetched when a detail page is opened, then CACHED per coin per UTC day
(tx_bars_cache table) - the bars only change once a day, when a day
completes. So at most 1 Codex request per coin per day, and only for coins
someone actually opens; never more than one request per active coin per day.
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

WINDOW_DAYS = 15

BARS_QUERY = """
query ($symbol: String!, $from: Int!, $to: Int!) {
  getBars(symbol: $symbol, from: $from, to: $to, resolution: "1D",
          removeLeadingNullValues: true) {
    t buys sells buyers sellers buyVolume sellVolume volume transactions
  }
}"""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _today_utc():
    return datetime.now(timezone.utc).date()


def fetch_from_codex(contract, chain):
    """(bars, error). bars = up to WINDOW_DAYS complete days, oldest first."""
    from liquidity_check import CHAIN_TO_NETWORK

    network = CHAIN_TO_NETWORK.get((chain or "").lower())
    if not network:
        return [], f"Codex doesn't cover the {chain} chain"
    if not CODEX_KEY:
        return [], "no Codex key configured"

    today = _today_utc()
    day0 = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    frm = int((day0 - timedelta(days=WINDOW_DAYS + 1)).timestamp())
    to = int(time.time())
    try:
        body = requests.post(CODEX_URL, json={"query": BARS_QUERY, "variables": {
            "symbol": f"{contract}:{network}", "from": frm, "to": to}},
            headers={"Authorization": CODEX_KEY}, timeout=30).json()
    except (requests.exceptions.RequestException, ValueError) as e:
        return [], f"Codex request failed ({str(e)[:80]})"
    if body.get("errors"):
        return [], "Codex: " + (body["errors"][0].get("message") or "error")[:120]
    b = (body.get("data") or {}).get("getBars") or {}

    bars = []
    for i, t in enumerate(b.get("t") or []):
        day = datetime.fromtimestamp(t, tz=timezone.utc).date()
        if day >= today:
            continue                       # today's partial day - left out

        def at(field):
            vals = b.get(field) or []
            return vals[i] if i < len(vals) else None
        bars.append({
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
    return bars[-WINDOW_DAYS:], None


def get_tx_bars(session, contract, chain):
    """Cached per coin per UTC day. Returns {"bars", "error", "cached"}."""
    from models import TxBarsCache

    key_chain, key_contract = (chain or "").lower(), (contract or "").lower()
    today = _today_utc()
    row = (session.query(TxBarsCache)
           .filter_by(chain=key_chain, contract=key_contract).first())
    if row is not None and row.day == today:
        return {"bars": json.loads(row.bars), "error": None, "cached": True}

    bars, error = fetch_from_codex(contract, chain)
    if error:
        # Keep showing yesterday's bars rather than nothing if Codex failed.
        stale = json.loads(row.bars) if row is not None else []
        return {"bars": stale, "error": error, "cached": bool(stale)}

    if row is None:
        row = TxBarsCache(chain=key_chain, contract=key_contract)
        session.add(row)
    row.day = today
    row.bars = json.dumps(bars)
    row.fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.commit()
    return {"bars": bars, "error": None, "cached": False}
