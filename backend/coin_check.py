"""
coin_check.py
-------------
"Check a Coin": run the SAME 7-step entry filter on ONE contract, on demand,
and return a full analysis for the dashboard. Read-only for the coin lists:
the checked coin is never saved to coins / snapshots / the queue (only the
finished result is cached per contract per UTC day in coin_check_cache).

The chain is AUTO-DETECTED: DexScreener (the chain of the token's pools,
most liquid first), else CoinGecko's contract list, else CoinMarketCap.

The filter steps reuse the pipeline's own functions, in the pipeline's order:
  1. Market cap in range         discovery.MIN_MCAP / MAX_MCAP, on the global
                                 CoinGecko (else CMC) market cap
  2. Meme / stablecoin / tokenized   meme_filter.classify_excluded +
                                 looks_like_stablecoin. The daily run checks
                                 bulk CoinGecko category lists; for one coin
                                 we use its OWN CoinGecko categories mapped to
                                 the same category ids (+ its CMC tags) - no
                                 bulk downloads.
  3. Age >= 10 days              age_check.decide_age (DexScreener pool age,
                                 else CMC date_added / CMC info)
  4. Revenue                     revenue_check.check_revenue (only a confirmed
                                 NO_REVENUE fails)
  5. Volume +35% over 5 days     volume_check.check_volume_rising
  6. Honeypot                    security_check.check_token_security
  7. Liquidity not down > 5%     liquidity_check.check_liquidity_trend
All steps run (for the numbers); the verdict is the FIRST step that fails.
Plus the detail-page data: 15-day Codex transaction bars (tx_bars) and the
DefiLlama revenue numbers (revenue_detail).
"""

import json
from datetime import datetime, timezone

import requests

DEX_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{addr}"
CG_CONTRACT_URL = "https://api.coingecko.com/api/v3/coins/{platform}/contract/{addr}"
CG_CATEGORIES_URL = "https://api.coingecko.com/api/v3/coins/categories/list"
CMC_INFO_URL = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/info"
CMC_QUOTES_URL = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest"

_CATEGORY_NAMES = None          # CoinGecko category id -> name (once per process)


def _get(url, params=None, headers=None, timeout=30):
    """(json or None, status). Never raises."""
    try:
        r = requests.get(url, params=params or {}, headers=headers or {}, timeout=timeout)
    except requests.exceptions.RequestException:
        return None, None
    try:
        return r.json(), r.status_code
    except ValueError:
        return None, r.status_code


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _evm(addr):
    return addr.lower().startswith("0x")


# ---------------------------------------------------------------- lookups
def _dexscreener(contract):
    """{chain: [pairs where this token is the base]}, from one request."""
    data, _ = _get(DEX_TOKENS_URL.format(addr=contract))
    by_chain = {}
    for p in (data or {}).get("pairs") or []:
        base = (p.get("baseToken") or {}).get("address") or ""
        if base.lower() == contract.lower():
            by_chain.setdefault(p.get("chainId"), []).append(p)
    return by_chain


def _cg_from_contract_list(contract):
    """(cg_id, chain) from CoinGecko's full contract list (cached per process)."""
    from discovery import _cg_id_to_platforms, CG_PLATFORM_TO_CHAIN
    for cg_id, plats in _cg_id_to_platforms().items():
        for plat, addr in plats.items():
            if addr.lower() == contract.lower():
                return cg_id, CG_PLATFORM_TO_CHAIN.get(plat, plat)
    return None, None


def _coingecko(contract, chain):
    """CoinGecko coin data by contract + chain (1 call), or None."""
    from age_check import CHAIN_TO_CG_PLATFORM
    from discovery import CG_KEY
    platform = CHAIN_TO_CG_PLATFORM.get(chain)
    if not platform or not CG_KEY:
        return None
    data, status = _get(CG_CONTRACT_URL.format(platform=platform, addr=contract),
                        headers={"x-cg-demo-api-key": CG_KEY})
    return data if status == 200 and isinstance(data, dict) else None


def _cmc(contract):
    """(info, quote) from CoinMarketCap by contract (2 calls), or (None, None)."""
    from discovery import CMC_KEY
    if not CMC_KEY:
        return None, None
    h = {"X-CMC_PRO_API_KEY": CMC_KEY, "Accept": "application/json"}
    # CMC wants EVM addresses lowercased; Solana etc. are case-sensitive.
    data, status = _get(CMC_INFO_URL, {"address": contract.lower() if _evm(contract) else contract}, h)
    if status != 200 or not isinstance(data, dict):
        return None, None
    info = None
    for v in (data.get("data") or {}).values():
        info = v[0] if isinstance(v, list) else v
        break
    if not info:
        return None, None
    q, status = _get(CMC_QUOTES_URL, {"id": info["id"]}, h)
    quote = ((q or {}).get("data") or {}).get(str(info["id"])) if status == 200 else None
    return info, quote


def _category_names():
    global _CATEGORY_NAMES
    if _CATEGORY_NAMES is None:
        from discovery import CG_KEY
        data, status = _get(CG_CATEGORIES_URL, headers={"x-cg-demo-api-key": CG_KEY})
        _CATEGORY_NAMES = ({c["category_id"]: c["name"] for c in data}
                           if status == 200 and isinstance(data, list) else {})
    return _CATEGORY_NAMES


def detect_chain(contract):
    """(chain, source, dex_pairs_by_chain, cg_id) or (None, ...) if not found."""
    dex = _dexscreener(contract)
    if dex:
        def liq(ch):
            return sum((p.get("liquidity") or {}).get("usd") or 0 for p in dex[ch])
        return max(dex, key=liq), "DexScreener", dex, None
    cg_id, chain = _cg_from_contract_list(contract)
    if chain:
        return chain, "CoinGecko", dex, cg_id
    from discovery import CMC_PLATFORM_TO_CHAIN
    info, _ = _cmc(contract)
    if info:
        plat = (info.get("platform") or {}).get("name")
        if plat:
            return CMC_PLATFORM_TO_CHAIN.get(plat, plat.lower()), "CoinMarketCap", dex, None
    return None, None, dex, None


# ---------------------------------------------------------------- the check
def run_check(contract):
    contract = (contract or "").strip()
    if len(contract) < 20 or any(ch in contract for ch in " /,\\"):
        return {"found": False, "message": "That doesn't look like a contract address."}

    chain, chain_source, dex, _ = detect_chain(contract)
    if not chain:
        return {"found": False, "message": "Coin not found — check the contract address."}
    pairs = dex.get(chain) or []
    if pairs:                                   # DexScreener has the exact casing
        contract = pairs[0]["baseToken"]["address"]

    from discovery import MIN_MCAP, MAX_MCAP
    from fetch_dexscreener import build_token_records
    from meme_filter import (classify_excluded, looks_like_stablecoin, CG_MEME_CATEGORIES,
                             CG_STABLECOIN_CATEGORIES, CG_TOKENIZED_CATEGORIES,
                             CG_WRAPPED_CATEGORIES, WRAPPED_KEEP_CG_IDS)
    from age_check import decide_age, MIN_AGE_DAYS
    from revenue_check import check_revenue
    from volume_check import check_volume_rising, VOLUME_MIN_RISE
    from security_check import check_token_security
    from liquidity_check import check_liquidity_trend, MAX_DROP_PCT, too_thin, MIN_LIQUIDITY_USD
    from narrative_check import _pick_category
    from queue_view import narrative_from_tags
    from tx_bars import fetch_from_codex
    from revenue_detail import fetch as fetch_revenue
    from price_range import from_market_data
    from relative_strength import from_coingecko as rel_from_cg

    # Outside lookups run in PARALLEL (a check used to take 16-27 s one after
    # another): first CoinGecko + CMC together (name, market cap, tags)...
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=7)
    f_cg, f_cmc = pool.submit(_coingecko, contract, chain), pool.submit(_cmc, contract)
    cg = f_cg.result() or {}
    cmc_info, cmc_q = f_cmc.result()
    cmc_info, cmc_q = cmc_info or {}, cmc_q or {}
    md = cg.get("market_data") or {}
    usd = (cmc_q.get("quote") or {}).get("USD") or {}
    dex_rec = (build_token_records(pairs, keep_addresses={contract.lower()}) or [{}])[0] if pairs else {}

    name = cg.get("name") or cmc_info.get("name") or dex_rec.get("name")
    symbol = (cg.get("symbol") or "").upper() or cmc_info.get("symbol") or dex_rec.get("symbol")
    price = (md.get("current_price") or {}).get("usd") or usd.get("price") or _num(dex_rec.get("price_usd"))
    mcap = (md.get("market_cap") or {}).get("usd") or usd.get("market_cap") or None
    mcap_source = ("CoinGecko" if (md.get("market_cap") or {}).get("usd")
                   else "CoinMarketCap" if usd.get("market_cap") else None)
    cmc_tags = cmc_info.get("tags") or []
    narrative = (narrative_from_tags(cmc_tags) or _pick_category(cg.get("categories") or [])
                 or "unknown")

    # ...then every filter step / data source at once. The two DefiLlama
    # lookups share one thread so its protocol lists download only once.
    listed = cmc_info.get("date_added")
    token = {"contract": contract, "chain": chain, "age_days": dex_rec.get("age_days"),
             "listed_at": datetime.fromisoformat(listed.replace("Z", "+00:00")) if listed else None}

    def revenue_both():
        return check_revenue(name, symbol, contract, chain), fetch_revenue(contract, mcap)
    f_age = pool.submit(decide_age, token)                   # no DB session: nothing stored
    f_vol = pool.submit(check_volume_rising, contract, chain)
    f_sec = pool.submit(check_token_security, contract, chain)
    f_liq = pool.submit(check_liquidity_trend, contract, chain)
    f_tx = pool.submit(fetch_from_codex, contract, chain)
    f_rev = pool.submit(revenue_both)
    pool.shutdown(wait=False)

    steps = []

    def step(key, label, status, detail, fail_reason=None):
        steps.append({"key": key, "label": label, "status": status, "detail": detail,
                      "fail_reason": fail_reason})

    # 1. Market cap range (discovery)
    rng = f"${MIN_MCAP / 1e6:g}M–${MAX_MCAP / 1e6:g}M"
    if mcap is None:
        step("mcap", f"Market cap {rng}", "fail", "no market cap on CoinGecko or CoinMarketCap",
             "Not listed with a market cap on CoinGecko or CoinMarketCap (the bot only finds coins listed there)")
    elif mcap < MIN_MCAP:
        step("mcap", f"Market cap {rng}", "fail", f"${mcap:,.0f}",
             f"Market cap out of range (${mcap:,.0f}, below ${MIN_MCAP / 1e6:g}M)")
    elif mcap > MAX_MCAP:
        step("mcap", f"Market cap {rng}", "fail", f"${mcap:,.0f}",
             f"Market cap out of range (${mcap:,.0f}, above ${MAX_MCAP / 1e6:g}M)")
    else:
        step("mcap", f"Market cap {rng}", "pass", f"${mcap:,.0f} ({mcap_source})")

    # 2. Meme / stablecoin / tokenized (same classifier as the filter)
    names = _category_names()
    cats = set(cg.get("categories") or [])
    key = {(chain, contract.lower())}

    def in_cats(ids):
        return key if cats & {names.get(i) for i in ids if names.get(i)} else set()
    kind = classify_excluded(contract, chain, in_cats(CG_MEME_CATEGORIES),
                             in_cats(CG_STABLECOIN_CATEGORIES), cmc_tags,
                             tokenized_keys=in_cats(CG_TOKENIZED_CATEGORIES),
                             wrapped_keys=(set() if cg.get("id") in WRAPPED_KEEP_CG_IDS
                                           else in_cats(CG_WRAPPED_CATEGORIES)),
                             name=name)
    if kind is None and looks_like_stablecoin({
            "price": price, "chg_24h": md.get("price_change_percentage_24h", usd.get("percent_change_24h")),
            "chg_7d": usd.get("percent_change_7d"),
            "high_24h": (md.get("high_24h") or {}).get("usd"), "low_24h": (md.get("low_24h") or {}).get("usd")}):
        kind = "stablecoin"
    words = {"meme": "a meme coin", "stablecoin": "a stablecoin", "tokenized": "a tokenized stock / fund",
             "wrapped": "a wrapped / staked / receipt token (it tracks another asset)"}
    if kind:
        step("type", "Not a meme / stablecoin / tokenized / wrapped", "fail", words[kind], f"It's {words[kind]}")
    else:
        step("type", "Not a meme / stablecoin / tokenized / wrapped", "pass", "none of these")

    # 3. Age
    keep_age, age_days, age_src = f_age.result()
    if age_days is None:
        step("age", "Age ≥ 10 days", "fail", "unknown",
             "Age unknown (no DexScreener pool date and no listing date)")
    elif not keep_age:
        step("age", "Age ≥ 10 days", "fail", f"{age_days} days",
             f"Age is under {MIN_AGE_DAYS} days (it's {age_days} day{'s' if age_days != 1 else ''} old)")
    else:
        step("age", "Age ≥ 10 days", "pass", f"{age_days} days ({age_src})")

    # 4. Revenue
    rev, revd = f_rev.result()
    if rev["verdict"] == "NO_REVENUE":
        step("revenue", "Revenue (not 'no fees')", "fail", rev["reason"], "No revenue / fees found (DefiLlama tracks it with no fees)")
    else:
        step("revenue", "Revenue (not 'no fees')", "pass",
             "has revenue" if rev["verdict"] == "HAS_REVENUE" else "not tracked by DefiLlama (passes)")

    # 5. Volume (5 complete days, day 5 >= day 1 x 1.35)
    vol = f_vol.result()
    vol_pct = (vol["day5"] / vol["day1"] - 1) * 100 if vol.get("day1") else None
    need = (VOLUME_MIN_RISE - 1) * 100
    if vol.get("transient"):
        step("volume", "Volume up ≥ 35% over 5 days", "unknown", "CoinGecko didn't answer - try again later")
    elif vol["verdict"] == "rising":
        step("volume", "Volume up ≥ 35% over 5 days", "pass", f"{vol_pct:+.0f}%")
    elif vol["verdict"] == "falling":
        reason = (f"Volume is falling over the last 5 days ({vol_pct:+.0f}%)" if vol_pct is not None and vol_pct < 0
                  else f"Volume didn't rise enough over the last 5 days ({vol_pct:+.0f}%, needs +{need:.0f}%)")
        step("volume", "Volume up ≥ 35% over 5 days", "fail", f"{vol_pct:+.0f}%", reason)
    else:
        step("volume", "Volume up ≥ 35% over 5 days", "fail", "no CoinGecko volume history",
             "No CoinGecko volume history for this contract (the filter needs 5 days)")

    # 6. Honeypot
    sec = f_sec.result()
    if sec["verdict"] == "unsafe":
        step("honeypot", "Not a honeypot", "fail", sec["reason"], "It's a honeypot / not sellable")
    else:
        step("honeypot", "Not a honeypot", "pass",
             "sellable" if sec["verdict"] == "safe" else f"unknown ({sec['reason']}) - passes")

    # 7. Liquidity (not down more than 5% over min(30 days, age))
    liq = f_liq.result()
    if liq["verdict"] == "drop":
        step("liquidity", "Liquidity not down > 5%", "fail", f"{liq['pct']:+.1f}%",
             f"Liquidity dropped more than {MAX_DROP_PCT}% ({liq['pct']:+.1f}% since {liq['base_date']})")
    elif liq["verdict"] == "keep":
        step("liquidity", "Liquidity not down > 5%", "pass", f"{liq['pct']:+.1f}% since {liq['base_date']}")
    else:
        step("liquidity", "Liquidity not down > 5%", "pass", f"unknown ({liq.get('reason')}) - passes")

    # Minimum liquidity (part of step 7): under $5K on BOTH sources = too thin.
    thin, best_liq = too_thin(liq.get("today_liq"), dex_rec.get("liquidity_usd"))
    if thin:
        step("min_liquidity", f"Liquidity at least ${MIN_LIQUIDITY_USD:,}", "fail",
             f"${best_liq:,.0f} (Codex + DexScreener)",
             f"Liquidity is below our ${MIN_LIQUIDITY_USD:,} minimum (${best_liq:,.0f}) — too thin to analyze")
    else:
        step("min_liquidity", f"Liquidity at least ${MIN_LIQUIDITY_USD:,}", "pass",
             "unknown - passes" if best_liq is None else f"${best_liq:,.0f} (higher of Codex / DexScreener)")

    first_fail = next((s for s in steps if s["status"] == "fail"), None)
    pending = any(s["status"] == "unknown" for s in steps)
    if first_fail:
        verdict = {"passes": False, "reason": first_fail["fail_reason"]}
    elif pending:
        verdict = {"passes": None, "reason": "One step couldn't be checked right now - try again later."}
    else:
        verdict = {"passes": True, "reason": None}

    daily, tx_err = f_tx.result()
    liq_trend = None
    if liq.get("pct") is not None:
        p = liq["pct"]
        liq_trend = "rising" if p > 1 else "falling" if p < -MAX_DROP_PCT else "stable"

    result = {
        "found": True,
        "contract": contract, "chain": chain, "chain_source": chain_source,
        "name": name, "symbol": symbol, "price": price, "narrative": narrative,
        "mcap": mcap, "mcap_source": mcap_source,
        "ranks": {"coingecko": cg.get("market_cap_rank"), "cmc": cmc_q.get("cmc_rank")},
        "age": {"days": age_days, "source": age_src},
        "volume": {"verdict": vol["verdict"], "pct": None if vol_pct is None else round(vol_pct, 1),
                   "day1": vol.get("day1"), "day5": vol.get("day5"), "daily": vol.get("volumes")},
        # Current liquidity: the filter's Codex reading, else today's from the
        # transaction-bars request (display only - the filter verdict is unchanged).
        "liquidity": {"now": best_liq or liq.get("today_liq") or (daily or {}).get("liq_now"), "pct": liq.get("pct"),
                      "too_thin": thin, "minimum": MIN_LIQUIDITY_USD,
                      "since": str(liq.get("base_date")) if liq.get("base_date") else None,
                      "trend": liq_trend},
        "revenue": {"status": revd.get("status"), "reason": revd.get("reason"),
                    "all_time": revd.get("revenue_all_time"), "d30": revd.get("revenue_30d"),
                    "annual": revd.get("annual_revenue"), "protocol": revd.get("protocol"),
                    "filter_verdict": rev["verdict"]},
        "honeypot": sec["verdict"],
        # Display only: from the CoinGecko response above (no extra call).
        "price_range": from_market_data(md) if md else
                       {"status": "unknown", "reason": "CoinGecko doesn't list this contract"},
        "rel_strength": rel_from_cg(cg) if cg else
                        {"status": "unknown", "reason": "CoinGecko doesn't list this contract"},
        "tx_bars": (daily or {}).get("bars") or [],
        "tx_error": tx_err,
        "tx_window_days": 15,
        "steps": steps,
        "verdict": verdict,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    result["summary"], result["opinion"] = summarize(result)
    result["_transient"] = pending
    return result


def summarize(r):
    """Plain-language description of what the on-chain numbers show, plus a
    short factual take. Not investment advice - no buy / sell language."""
    parts, plus, minus, has_revenue = [], 0, 0, False
    v = r["volume"]
    if v["pct"] is not None:
        if v["pct"] >= 35:
            parts.append(f"Volume is rising strongly over the last 5 days ({v['pct']:+.0f}%)."); plus += 2
        elif v["pct"] > 0:
            parts.append(f"Volume is up a little over the last 5 days ({v['pct']:+.0f}%)."); plus += 1
        else:
            parts.append(f"Volume is falling over the last 5 days ({v['pct']:+.0f}%)."); minus += 1
    else:
        parts.append("There isn't enough volume history to judge the trend.")
    lq = r["liquidity"]
    if lq["trend"] == "rising":
        parts.append(f"Liquidity is building ({lq['pct']:+.1f}% since {lq['since']})."); plus += 1
    elif lq["trend"] == "stable":
        parts.append(f"Liquidity is roughly stable ({lq['pct']:+.1f}% since {lq['since']}).")
    elif lq["trend"] == "falling":
        parts.append(f"Liquidity is draining ({lq['pct']:+.1f}% since {lq['since']})."); minus += 1
    else:
        parts.append("Liquidity history isn't available.")
    rv = r["revenue"]
    if rv["status"] == "ok" and (rv.get("d30") or 0) > 0:
        parts.append(f"It earns real revenue (${rv['d30']:,.0f} in the last 30 days, per DefiLlama)."); plus += 1
        has_revenue = True
    elif rv["status"] == "ok":
        parts.append("DefiLlama tracks it but shows no revenue in the last 30 days."); minus += 1
    else:
        parts.append("No revenue data (not tracked by DefiLlama).")
    vd = r["verdict"]
    parts.append("It passes all 7 filter steps." if vd["passes"] else
                 f"It does not pass the filter: {vd['reason']}." if vd["passes"] is False else vd["reason"])

    if plus >= 3 and minus == 0 and has_revenue:
        opinion = "Strong on-chain signals: activity and liquidity are growing, backed by real revenue."
    elif plus >= 3 and minus == 0:
        opinion = "Strong activity and liquidity signals, but there's no revenue data to back them up."
    elif plus > minus:
        opinion = "Mostly positive signals, but not all of them line up."
    elif minus > plus:
        opinion = "Weak signals: activity is fading or the fundamentals the screener looks for are missing."
    else:
        opinion = "Mixed signals: nothing stands out strongly either way."
    return " ".join(parts), opinion + " This describes the on-chain data only - it isn't financial advice."


def rules_signature():
    """The filter settings a check result depends on. A cached result made
    under different settings (e.g. before a range change) is recomputed."""
    from discovery import MIN_MCAP, MAX_MCAP
    from age_check import MIN_AGE_DAYS
    from volume_check import VOLUME_MIN_RISE
    from liquidity_check import MAX_DROP_PCT, MIN_LIQUIDITY_USD
    return (f"mcap {MIN_MCAP}-{MAX_MCAP}|age {MIN_AGE_DAYS}|vol {VOLUME_MIN_RISE}|liq {MAX_DROP_PCT}/{MIN_LIQUIDITY_USD}"
            "|pricerange|relstrength-ar")   # result layout: has these display sections


def get_check(session, contract):
    """run_check, cached per contract per UTC day (coin_check_cache). Results
    with a step that couldn't be checked (API hiccup), and "not found", aren't
    cached."""
    from models import CoinCheckCache

    key = (contract or "").strip().lower()
    today = datetime.now(timezone.utc).date()
    row = session.query(CoinCheckCache).filter_by(contract=key).first() if key else None
    if row is not None and row.day == today:
        res = json.loads(row.data)
        if res.get("rules") == rules_signature():          # same filter settings
            res["cached"] = True
            return res
    res = run_check(contract)
    res["rules"] = rules_signature()
    transient = res.pop("_transient", False)
    # "Not found" isn't cached: a brief outage at DexScreener / CoinGecko / CMC
    # must not make a real coin look missing for the rest of the day.
    if key and not transient and res.get("found"):
        if row is None:
            row = CoinCheckCache(contract=key)
            session.add(row)
        row.day = today
        row.data = json.dumps(res, default=str)
        session.commit()
    res["cached"] = False
    return res
