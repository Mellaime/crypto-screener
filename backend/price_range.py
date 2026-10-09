"""
price_range.py
--------------
Dashboard detail page + Check a Coin: the "Price Range (All-Time)" section.
DISPLAY ONLY - the filter and drop_logic never use it.

Source - CoinGecko coin data by contract (same data as /coins/{id}; uses
COINGECKO_API_KEY), its free `market_data` fields:
    ath, ath_date, ath_change_percentage      (USD)
    atl, atl_date, atl_change_percentage      (USD)
    current_price                             (USD)
Position = (current - atl) / (ath - atl) * 100   (0% = at the ATL, 100% = at the ATH)

get_price_range(session, contract, chain) caches one CoinGecko call per coin
per UTC day in price_range_cache. from_market_data(md) builds the same dict
from a CoinGecko response already in hand (Check a Coin - no extra call).
"""

import json
from datetime import datetime, timezone

import requests

CG_CONTRACT_URL = "https://api.coingecko.com/api/v3/coins/{platform}/contract/{addr}"
CG_PARAMS = {"localization": "false", "tickers": "false", "community_data": "false",
             "developer_data": "false", "sparkline": "false"}


def _usd(md, key):
    v = (md.get(key) or {}).get("usd")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def from_market_data(md):
    """The section's dict from a CoinGecko `market_data` object."""
    md = md or {}
    ath, atl, cur = _usd(md, "ath"), _usd(md, "atl"), _usd(md, "current_price")
    if ath is None or atl is None or cur is None:
        return {"status": "unknown", "reason": "CoinGecko has no all-time high / low for this coin"}
    pos = None
    if ath > atl:
        pos = round(min(100.0, max(0.0, (cur - atl) / (ath - atl) * 100)), 2)
    return {
        "status": "ok",
        "current": cur,
        "ath": ath, "ath_date": (md.get("ath_date") or {}).get("usd"),
        "ath_change_pct": _usd(md, "ath_change_percentage"),
        "atl": atl, "atl_date": (md.get("atl_date") or {}).get("usd"),
        "atl_change_pct": _usd(md, "atl_change_percentage"),
        "position_pct": pos,
    }


def fetch(contract, chain):
    """Fresh CoinGecko lookup. "transient" = True if CoinGecko couldn't be
    reached / rate-limited (then don't cache)."""
    from age_check import CHAIN_TO_CG_PLATFORM
    from discovery import CG_KEY
    platform = CHAIN_TO_CG_PLATFORM.get((chain or "").lower())
    if not platform or not CG_KEY:
        return {"status": "unknown", "reason": f"CoinGecko doesn't cover {chain}"}
    try:
        r = requests.get(CG_CONTRACT_URL.format(platform=platform, addr=contract),
                         params=CG_PARAMS, headers={"x-cg-demo-api-key": CG_KEY}, timeout=20)
    except requests.exceptions.RequestException:
        return {"status": "unknown", "reason": "CoinGecko didn't answer - try again later",
                "transient": True}
    if r.status_code == 429 or r.status_code >= 500:
        return {"status": "unknown", "reason": "CoinGecko didn't answer - try again later",
                "transient": True}
    if r.status_code != 200:
        return {"status": "unknown", "reason": "CoinGecko doesn't list this contract"}
    try:
        return from_market_data(r.json().get("market_data"))
    except ValueError:
        return {"status": "unknown", "reason": "CoinGecko sent a bad answer", "transient": True}


def get_price_range(session, contract, chain):
    """fetch(), cached per coin per UTC day (price_range_cache)."""
    from models import PriceRangeCache

    key_chain, key_contract = (chain or "").lower(), (contract or "").lower()
    today = datetime.now(timezone.utc).date()
    try:
        row = (session.query(PriceRangeCache)
               .filter_by(chain=key_chain, contract=key_contract).first())
    except Exception:                       # cache table missing - still show the data
        session.rollback()
        res = fetch(contract, chain)
        res.pop("transient", None)
        return res
    if row is not None and row.day == today:
        return json.loads(row.data)
    res = fetch(contract, chain)
    if res.pop("transient", False):
        return json.loads(row.data) if row is not None else res   # last saved data
    if row is None:
        row = PriceRangeCache(chain=key_chain, contract=key_contract)
        session.add(row)
    row.day = today
    row.data = json.dumps(res)
    session.commit()
    return res
