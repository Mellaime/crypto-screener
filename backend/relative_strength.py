"""
relative_strength.py
--------------------
Dashboard detail page + Check a Coin: the "Relative Strength - why is it
moving?" section. Is the coin moving on its own, or carried by the market
(Bitcoin) or its sector? DISPLAY ONLY - the filter and drop_logic never use it.

Sources (CoinGecko, free / demo key):
    coin    /coins/{platform}/contract/{addr}  market_data.price_change_percentage_24h,
            and its `categories` list
    market  /simple/price?ids=bitcoin&include_24hr_change=true
    sector  /coins/categories  ->  market_cap_change_24h of the coin's category
Bitcoin and the categories list are fetched ONCE per process and shared by
every coin (re-fetched after SHARED_TTL_S so a long-lived server stays fresh).
The coin's own lookup is cached per coin per UTC day (rel_strength_cache).

Sector = the coin's main category: CoinGecko's list minus launchpad /
ecosystem / backer labels (narrative_check's rules), and only categories that
actually report a 24h change (about half of CoinGecko's don't). No usable
category -> the sector row shows "—" and the verdict uses the market only.
"""

import json
import threading
import time
from datetime import datetime, timezone

import requests

CG_BASE = "https://api.coingecko.com/api/v3"
CG_COIN_PARAMS = {"localization": "false", "tickers": "false", "community_data": "false",
                  "developer_data": "false", "sparkline": "false"}
SHARED_TTL_S = 30 * 60          # Bitcoin + categories: re-fetch after 30 minutes

# ---------------------------------------------------------------------------
# VERDICT THRESHOLDS (tune here). All values are 24h % changes.
#   ref = the higher of market (BTC) and sector (sector only if known)
#   1. STRONG         coin >= ref + STRONG_GAP_PP  and
#                     (ref <= 0  or  coin >= ref * STRONG_MULT)
#                     -> clearly outperforming both: something coin-specific.
#   2. WEAK           coin < FLAT_PCT  (down, or flat / barely up)
#   3. SECTOR-DRIVEN  sector > 0 and sector >= market  (coin up, rides its sector)
#   4. MARKET-DRIVEN  market > 0                       (coin up, follows BTC)
#   5. otherwise      WEAK - coin a little up while market and sector are
#                     down, but not by enough (STRONG_GAP_PP) to call it its own move.
# ---------------------------------------------------------------------------
STRONG_GAP_PP = 5.0     # percentage points above the higher of market / sector
STRONG_MULT = 1.5       # ... and at least 1.5x it (when that reference is up)
FLAT_PCT = 1.0          # below +1% counts as flat / down

_shared = {"at": 0.0, "btc": None, "cats": None}
_shared_lock = threading.Lock()


def _cg_get(path, params=None):
    """(json or None, transient). transient = network / rate limit / 5xx."""
    from discovery import CG_KEY
    if not CG_KEY:
        return None, False
    try:
        r = requests.get(CG_BASE + path, params=params or {},
                         headers={"x-cg-demo-api-key": CG_KEY}, timeout=20)
    except requests.exceptions.RequestException:
        return None, True
    if r.status_code == 429 or r.status_code >= 500:
        return None, True
    if r.status_code != 200:
        return None, False
    try:
        return r.json(), False
    except ValueError:
        return None, True


def shared_market():
    """(btc_24h_change or None, {category name: market_cap_change_24h}) -
    fetched once and shared by all coins (refreshed after SHARED_TTL_S)."""
    with _shared_lock:
        fresh = time.time() - _shared["at"] < SHARED_TTL_S
        if not (fresh and _shared["btc"] is not None and _shared["cats"]):
            btc, _ = _cg_get("/simple/price", {"ids": "bitcoin", "vs_currencies": "usd",
                                               "include_24hr_change": "true"})
            cats, _ = _cg_get("/coins/categories")
            btc_chg = ((btc or {}).get("bitcoin") or {}).get("usd_24h_change")
            cat_map = {c["name"]: c["market_cap_change_24h"] for c in (cats or [])
                       if c.get("name") and c.get("market_cap_change_24h") is not None}
            if btc_chg is not None:
                _shared["btc"] = btc_chg
            if cat_map:
                _shared["cats"] = cat_map
            if btc_chg is not None and cat_map:
                _shared["at"] = time.time()
        return _shared["btc"], dict(_shared["cats"] or {})


def pick_sector(categories, cat_map):
    """(name, change) of the coin's main category that has a 24h change, or (None, None)."""
    from narrative_check import _pick_category
    usable = [c for c in (categories or []) if c in cat_map]
    name = _pick_category(usable)
    return (name, cat_map[name]) if name else (None, None)


def verdict(coin, market, sector):
    """STRONG / SECTOR-DRIVEN / MARKET-DRIVEN / WEAK (see THRESHOLDS above)."""
    refs = [v for v in (market, sector) if v is not None]
    ref = max(refs) if refs else 0.0
    if coin >= ref + STRONG_GAP_PP and (ref <= 0 or coin >= ref * STRONG_MULT):
        return "STRONG"
    if coin < FLAT_PCT:
        return "WEAK"
    if sector is not None and sector > 0 and (market is None or sector >= market):
        return "SECTOR-DRIVEN"
    if market is not None and market > 0:
        return "MARKET-DRIVEN"
    return "WEAK"


def _pct(v):
    return f"{v:+.1f}%"


def explain(v, coin, market, sector, sector_name):
    """One plain-language line (data, not advice)."""
    ctx = []
    if market is not None:
        ctx.append(f"BTC is {_pct(market)}")
    if sector is not None:
        ctx.append(f"its sector, {sector_name}, is {_pct(sector)}")
    ctx_txt = " and ".join(ctx) if ctx else "there's no market data"
    moved = f"{'Up' if coin >= 0 else 'Down'} {_pct(coin)} while {ctx_txt}"
    both = "the market and its sector" if sector is not None else "the market"
    tail = {
        "STRONG": f"moving on its own, well ahead of {both}.",
        "SECTOR-DRIVEN": "moving with its sector rather than on its own.",
        "MARKET-DRIVEN": "following the overall market rather than moving on its own.",
        "WEAK": (f"weaker than {both}." if coin < 0 and coin < max(
                     [x for x in (market, sector) if x is not None] or [0]) else
                 "down or flat - no strength of its own." if coin < FLAT_PCT else
                 "only slightly up - not enough to call it its own move."),
    }[v]
    return f"{moved} — {tail}"


def build(coin_chg, categories):
    """The section's dict from a coin's 24h change + CoinGecko categories."""
    if coin_chg is None:
        return {"status": "unknown", "reason": "CoinGecko has no 24h price change for this coin"}
    market, cat_map = shared_market()
    sector_name, sector = pick_sector(categories, cat_map)
    v = verdict(coin_chg, market, sector)
    return {"status": "ok", "coin_24h": coin_chg, "market_24h": market,
            "sector_name": sector_name, "sector_24h": sector, "verdict": v,
            "line": explain(v, coin_chg, market, sector, sector_name),
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def from_coingecko(cg):
    """Check a Coin: build from a CoinGecko coin response already in hand."""
    cg = cg or {}
    return build((cg.get("market_data") or {}).get("price_change_percentage_24h"),
                 cg.get("categories"))


def fetch(contract, chain):
    """Fresh lookup (one CoinGecko coin call + the shared market data).
    "transient" = True if CoinGecko couldn't be reached (then don't cache)."""
    from age_check import CHAIN_TO_CG_PLATFORM
    platform = CHAIN_TO_CG_PLATFORM.get((chain or "").lower())
    if not platform:
        return {"status": "unknown", "reason": f"CoinGecko doesn't cover {chain}"}
    data, transient = _cg_get(f"/coins/{platform}/contract/{contract}", CG_COIN_PARAMS)
    if data is None:
        return {"status": "unknown", "transient": transient,
                "reason": "CoinGecko didn't answer - try again later" if transient
                          else "CoinGecko doesn't list this contract"}
    res = from_coingecko(data)
    if res["status"] == "ok" and res["market_24h"] is None:
        res["transient"] = True             # BTC lookup failed - don't keep for the day
    return res


def get_rel_strength(session, contract, chain):
    """fetch(), cached per coin per UTC day (rel_strength_cache)."""
    from models import RelStrengthCache

    key_chain, key_contract = (chain or "").lower(), (contract or "").lower()
    today = datetime.now(timezone.utc).date()
    try:
        row = (session.query(RelStrengthCache)
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
        row = RelStrengthCache(chain=key_chain, contract=key_contract)
        session.add(row)
    row.day = today
    row.data = json.dumps(res)
    session.commit()
    return res
