"""
revenue_detail.py
-----------------
The REVENUE section on a coin's detail page (dashboard only - the revenue
FILTER step stays in revenue_check.py and is not changed).

Source: DefiLlama (free, no key). The coin is matched to a DefiLlama
protocol by CONTRACT, exactly like the revenue filter (revenue_check's
indices). If the protocol belongs to a parent (e.g. Orca DEX -> Orca), the
parent is used - that's the whole project the token belongs to.

  1. Daily revenue - /summary/fees/{slug}?dataType=dailyRevenue: the last
     WINDOW_DAYS (15) COMPLETE UTC days (today left out), fewer if the
     history is younger.
  2. MCap / Revenue - our stored CoinGecko/CMC market cap / annualized
     revenue (last 30 days x 365/30). Lower = cheaper vs its revenue.
  3. TVL - /protocol/{slug}: latest TVL vs the reading 7 days earlier.
     Protocols that don't hold deposits have no TVL -> "not applicable".

Most small coins aren't on DefiLlama -> "unknown" and no bars.
Cost: 2 DefiLlama requests per coin per day (cached in revenue_cache), plus
the contract -> protocol lookup lists (loaded once per process).
"""

import json
from datetime import datetime, timezone

import requests

WINDOW_DAYS = 15
FEES_URL = "https://api.llama.fi/summary/fees/{slug}"
PROTOCOL_URL = "https://api.llama.fi/protocol/{slug}"


def _get(url, params=None):
    """(json or None, ok). ok=False = network / server trouble (try later)."""
    try:
        r = requests.get(url, params=params or {}, timeout=40,
                         headers={"User-Agent": "crypto-screener/0.1"})
    except requests.exceptions.RequestException:
        return None, False
    if r.status_code == 429 or r.status_code >= 500:
        return None, False
    if r.status_code != 200:
        return None, True                      # e.g. 404: DefiLlama has no such data
    try:
        return r.json(), True
    except ValueError:
        return None, False


def resolve_slugs(contract):
    """DefiLlama slugs to try for this token, best first (parent project
    first), or [] if DefiLlama doesn't list the contract."""
    from revenue_check import _get_indices
    protos = _get_indices()["proto_addr"].get((contract or "").lower()) or []
    slugs = []
    for p in protos:
        if p.get("parent", "").startswith("parent#"):
            slugs.append(p["parent"].split("#", 1)[1])
        if p.get("slug"):
            slugs.append(p["slug"])
    return list(dict.fromkeys(slugs))           # unique, order kept


def fetch(contract, mcap):
    """Fresh DefiLlama lookup. Returns the section's data dict; "transient"
    is True if DefiLlama couldn't be reached (then don't cache)."""
    slugs = resolve_slugs(contract)
    if not slugs:
        return {"status": "unknown", "reason": "not tracked by DefiLlama"}

    today = datetime.now(timezone.utc).date()
    for slug in slugs:
        fees, ok = _get(FEES_URL.format(slug=slug), {"dataType": "dailyRevenue"})
        if not ok:
            return {"status": "unknown", "reason": "DefiLlama didn't answer - try again later",
                    "transient": True}
        chart = (fees or {}).get("totalDataChart") or []
        if not chart:
            continue                            # try the next slug
        days = [(datetime.fromtimestamp(t, tz=timezone.utc).date(), float(v or 0)) for t, v in chart]
        days = [(d, v) for d, v in days if d < today][-WINDOW_DAYS:]
        rev_30d = fees.get("total30d")
        annual = rev_30d * 365 / 30 if rev_30d else None
        out = {
            "status": "ok",
            "protocol": fees.get("displayName") or fees.get("name") or slug,
            "slug": slug,
            "bars": [{"date": d.isoformat(), "revenue": v} for d, v in days],
            "revenue_24h": fees.get("total24h"),
            "revenue_30d": rev_30d,
            "annual_revenue": annual,
            "mcap_to_revenue": round(mcap / annual, 2) if mcap and annual else None,
        }
        out["tvl"] = fetch_tvl(slug)
        if out["tvl"].pop("transient", False):
            out["transient"] = True
        return out
    return {"status": "unknown", "reason": "DefiLlama lists this token but has no revenue data for it"}


def fetch_tvl(slug):
    # DefiLlama occasionally answers with an incomplete protocol record, so
    # "no TVL" is only believed if a second request agrees (otherwise a
    # one-off glitch would be cached as "not applicable" for the whole day).
    for attempt in range(2):
        data, ok = _get(PROTOCOL_URL.format(slug=slug))
        if not ok:
            return {"status": "unknown", "transient": True}
        series = (data or {}).get("tvl") or []
        if series and series[-1].get("totalLiquidityUSD"):
            break
    else:
        return {"status": "not_applicable"}
    now_pt = series[-1]
    week = [p for p in series if p["date"] <= now_pt["date"] - 7 * 86400]
    now_v = float(now_pt["totalLiquidityUSD"])
    week_v = float(week[-1]["totalLiquidityUSD"]) if week else None
    return {
        "status": "ok",
        "now": now_v,
        "now_date": datetime.fromtimestamp(now_pt["date"], tz=timezone.utc).date().isoformat(),
        "week_ago": week_v,
        "change_pct": round((now_v / week_v - 1) * 100, 2) if week_v else None,
    }


def get_revenue(session, contract, chain, mcap):
    """fetch(), cached per coin per UTC day (revenue_cache). MCap / Revenue
    is recomputed from today's market cap even on a cached day."""
    from models import RevenueCache

    key_chain, key_contract = (chain or "").lower(), (contract or "").lower()
    today = datetime.now(timezone.utc).date()
    row = (session.query(RevenueCache)
           .filter_by(chain=key_chain, contract=key_contract).first())
    if row is not None and row.day == today:
        res = json.loads(row.data)
    else:
        res = fetch(contract, mcap)
        if res.pop("transient", False):
            if row is not None:
                res = json.loads(row.data)      # show the last saved data instead
        else:
            if row is None:
                row = RevenueCache(chain=key_chain, contract=key_contract)
                session.add(row)
            row.day = today
            row.data = json.dumps(res)
            session.commit()
    if res.get("annual_revenue") and mcap:
        res["mcap_to_revenue"] = round(mcap / res["annual_revenue"], 2)
    return res
