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


def _cmc(contract, chain=None):
    """(info, quote) from CoinMarketCap by contract (2 calls), or (None, None).
    Only the entry listing this contract on `chain` (never a name match)."""
    from discovery import CMC_KEY
    if not CMC_KEY:
        return None, None
    h = {"X-CMC_PRO_API_KEY": CMC_KEY, "Accept": "application/json"}
    # CMC wants EVM addresses lowercased; Solana etc. are case-sensitive.
    data, status = _get(CMC_INFO_URL, {"address": contract.lower() if _evm(contract) else contract}, h)
    if status != 200 or not isinstance(data, dict):
        return None, None
    from price_sources import cmc_matches
    info = None
    for v in (data.get("data") or {}).values():
        v = v[0] if isinstance(v, list) else v
        if not chain or cmc_matches(v, contract, chain):
            info = v
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
    from liquidity_check import check_liquidity_trend, MAX_DROP_PCT, MIN_LIQUIDITY_USD
    from narrative_check import _pick_category
    from queue_view import narrative_from_tags
    from tx_bars import fetch_from_codex
    from revenue_detail import fetch as fetch_revenue
    from price_range import from_market_data
    from socials import handle_from, handle_from_cmc, x_url
    from relative_strength import from_coingecko as rel_from_cg, _ltr

    def money(v):
        return _ltr(f"${v:,.0f}")

    # Outside lookups run in PARALLEL (a check used to take 16-27 s one after
    # another): first CoinGecko + CMC together (name, market cap, tags)...
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=7)
    f_cg, f_cmc = pool.submit(_coingecko, contract, chain), pool.submit(_cmc, contract, chain)
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
    # Liquidity Sources: the Codex daily reading (same cached series as the
    # trend step) + DexScreener + a LIVE Birdeye lookup (3 CU).
    from liquidity_verify import live_sources

    def sources_live():
        return live_sources(chain, contract, _num(dex_rec.get("liquidity_usd")),
                            f_liq.result().get("today_liq"))
    f_src = pool.submit(sources_live)
    f_tx = pool.submit(fetch_from_codex, contract, chain)
    from entry_zone import build as entry_zone_build
    # A coin we track: the detail page's own Entry Zone (stored history,
    # re-evaluated every 3 days) - no live Codex fetch needed for it.
    stored_ez = _stored_entry_zone(contract)
    f_ez = None if stored_ez else pool.submit(entry_zone_build, contract, chain, None,
                                               dex_rec.get("liquidity_usd"), None, None, mcap)
    f_rev = pool.submit(revenue_both)
    # Futures Data (Coinalyze, live): main perpetual by ticker, confirmed by price.
    from coinalyze import futures_data_live
    f_fut = pool.submit(futures_data_live, symbol, price)
    # Exchange Listings: stored for a coin we track, else the stored pair lists (0 calls).
    from listings import listings_live
    from volume_anomaly import spike_for_contract
    f_lst = pool.submit(listings_live, symbol, price, contract)
    pool.shutdown(wait=False)

    steps = []

    def step(key, label, status, detail, fail_reason=None):
        steps.append({"key": key, "label": label, "status": status, "detail": detail,
                      "fail_reason": fail_reason})

    # 1. Market cap range (discovery)
    rng = f"${MIN_MCAP / 1e6:g}M–${MAX_MCAP / 1e6:g}M"
    if mcap is None:
        step("mcap", f"Market cap {rng}", "fail", "no market cap on CoinGecko or CoinMarketCap",
             f"غير مُدرج بقيمة سوقية على {_ltr('CoinGecko')} أو {_ltr('CoinMarketCap')} (البوت لا يجد إلا العملات المدرجة هناك)")
    elif mcap < MIN_MCAP:
        step("mcap", f"Market cap {rng}", "fail", f"${mcap:,.0f}",
             f"القيمة السوقية خارج النطاق ({money(mcap)}، أقل من {_ltr(f'${MIN_MCAP / 1e6:g}M')})")
    elif mcap > MAX_MCAP:
        step("mcap", f"Market cap {rng}", "fail", f"${mcap:,.0f}",
             f"القيمة السوقية خارج النطاق ({money(mcap)}، أعلى من {_ltr(f'${MAX_MCAP / 1e6:g}M')})")
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
        words_ar = {"meme": "عملة ميم", "stablecoin": "عملة مستقرة",
                    "tokenized": "سهم أو صندوق مُرمَّز (tokenized)",
                    "wrapped": "رمز مُغلَّف / مُخزَّن / إيصال (يتتبّع أصلاً آخر)"}
        step("type", "Not a meme / stablecoin / tokenized / wrapped", "fail", words[kind], f"إنها {words_ar[kind]}")
    else:
        step("type", "Not a meme / stablecoin / tokenized / wrapped", "pass", "none of these")

    # 3. Age
    keep_age, age_days, age_src = f_age.result()
    if age_days is None:
        step("age", "Age ≥ 10 days", "fail", "unknown",
             f"العمر غير معروف (لا يوجد تاريخ مجمّع على {_ltr('DexScreener')} ولا تاريخ إدراج)")
    elif not keep_age:
        step("age", "Age ≥ 10 days", "fail", f"{age_days} days",
             f"العمر أقل من {_ltr(MIN_AGE_DAYS)} أيام (العمر الحالي: {_ltr(age_days)} يوم)")
    else:
        step("age", "Age ≥ 10 days", "pass", f"{age_days} days ({age_src})")

    # 4. Revenue
    rev, revd = f_rev.result()
    if rev["verdict"] == "NO_REVENUE":
        step("revenue", "Revenue (not 'no fees')", "fail", rev["reason"],
             f"لا توجد إيرادات / رسوم ({_ltr('DefiLlama')} يتتبّعه دون رسوم)")
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
        reason = (f"الحجم ينخفض خلال آخر 5 أيام ({_ltr(f'{vol_pct:+.0f}%')})" if vol_pct is not None and vol_pct < 0
                  else f"الحجم لم يرتفع بما يكفي خلال آخر 5 أيام ({_ltr(f'{vol_pct:+.0f}%')}، والمطلوب {_ltr(f'+{need:.0f}%')})")
        step("volume", "Volume up ≥ 35% over 5 days", "fail", f"{vol_pct:+.0f}%", reason)
    else:
        step("volume", "Volume up ≥ 35% over 5 days", "fail", "no CoinGecko volume history",
             f"لا يوجد سجل حجم على {_ltr('CoinGecko')} لهذا العقد (الفلتر يحتاج 5 أيام)")

    # 6. Honeypot
    sec = f_sec.result()
    if sec["verdict"] == "unsafe":
        step("honeypot", "Not a honeypot", "fail", sec["reason"], "إنها فخ (honeypot) / لا يمكن بيعها")
    else:
        step("honeypot", "Not a honeypot", "pass",
             "sellable" if sec["verdict"] == "safe" else f"unknown ({sec['reason']}) - passes")

    # 7. Liquidity (not down more than 5% over min(30 days, age))
    liq = f_liq.result()
    if liq["verdict"] == "drop":
        step("liquidity", "Liquidity not down > 5%", "fail", f"{liq['pct']:+.1f}%",
             f"السيولة انخفضت بأكثر من {_ltr(f'{MAX_DROP_PCT:g}%')} ({_ltr(format(liq['pct'], '+.1f') + '%')} منذ {_ltr(liq['base_date'])})")
    elif liq["verdict"] == "keep":
        step("liquidity", "Liquidity not down > 5%", "pass", f"{liq['pct']:+.1f}% since {liq['base_date']}")
    else:
        step("liquidity", "Liquidity not down > 5%", "pass", f"unknown ({liq.get('reason')}) - passes")

    # Minimum liquidity (part of step 7): the 3-source rule (DexScreener +
    # Codex + a live Birdeye lookup) - 2+ sources with a reading and 2+ at
    # the minimum or more. A failed Birdeye request -> decided without it.
    sources = f_src.result()
    best_liq = sources["value"]
    thin = sources["verdict"] not in ("healthy", "unknown")
    ez = stored_ez or f_ez.result()   # Entry Zone (also the score's price position)
    from liquidity_verify import money as usd0
    src_text = (f"DexScreener {usd0(sources['dexscreener'])} / Codex {usd0(sources['codex'])}"
                f" / Birdeye {usd0(sources['birdeye'])}")
    if thin:
        why = {"removed_insufficient_sources": "مصادر السيولة غير كافية (أقل من مصدرين فيهم داتا)",
               "removed_dead": f"السيولة أقل من {money(3_000)} (ميتة)",
               "dropped_low_liquidity": (f"السيولة بين {money(3_000)} و{money(MIN_LIQUIDITY_USD)} (حدّية)"
                                         if (sources["reason"] or "").startswith("Liquidity between")
                                         else f"السيولة فوق {money(MIN_LIQUIDITY_USD)} في مصدر واحد فقط")}[sources["verdict"]]
        step("min_liquidity", f"Liquidity at least ${MIN_LIQUIDITY_USD:,} (2 of 3 sources)", "fail",
             f"{sources['reason']} ({src_text})", f"{why} ({_ltr(src_text)})")
    else:
        step("min_liquidity", f"Liquidity at least ${MIN_LIQUIDITY_USD:,} (2 of 3 sources)", "pass",
             (f"undecided - {sources['reason']} - passes ({src_text})" if sources["verdict"] == "unknown"
              else f"{sources['n_above']} of 3 sources at ${sources['threshold']:,}+ ({src_text})"))

    first_fail = next((s for s in steps if s["status"] == "fail"), None)
    pending = any(s["status"] == "unknown" for s in steps)
    if first_fail:
        verdict = {"passes": False, "reason": first_fail["fail_reason"]}
    elif pending:
        verdict = {"passes": None, "reason": "تعذّر التحقق من إحدى الخطوات الآن — حاول مرة أخرى لاحقاً."}
    else:
        verdict = {"passes": True, "reason": None}

    daily, tx_err = f_tx.result()
    liq_trend = None
    if liq.get("pct") is not None:
        p = liq["pct"]
        liq_trend = "rising" if p > 1 else "falling" if p < -MAX_DROP_PCT else "stable"

    x_handle = handle_from(cg) or handle_from_cmc(cmc_info, contract, chain)
    result = {
        "found": True,
        "contract": contract, "chain": chain, "chain_source": chain_source,
        "name": name, "symbol": symbol, "price": price, "narrative": narrative,
        # X / Twitter from the CoinGecko response above (no extra call).
        # ...else CoinMarketCap's info response above (contract + chain match).
        "twitter": x_url(x_handle), "twitter_handle": x_handle,
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
        # Liquidity Sources section (DexScreener / Codex / live Birdeye + verdict).
        "liquidity_sources": sources,
        # Display only: from the CoinGecko response above (no extra call).
        "price_range": from_market_data(md) if md else
                       {"status": "unknown", "reason": "CoinGecko doesn't list this contract"},
        "rel_strength": rel_from_cg(cg) if cg else
                        {"status": "unknown", "reason": "CoinGecko doesn't list this contract"},
        # Entry Zone (Codex daily bars; no holders history for an unlisted coin).
        "entry_zone": ez,
        "score": check_score(md, mcap, ez, contract, chain, liq, daily, _num(dex_rec.get("liquidity_usd"))),
        "supply": check_supply(md),
        # Futures Data (Coinalyze OI / funding / long-short; nothing stored).
        "futures": f_fut.result(),
        # Exchange Listings on the 12 monitored exchanges (stored data).
        "listings": f_lst.result(),
        # Volume Alert: a tracked coin's stored spike (last 7 days); None otherwise.
        "volume_spike": spike_for_contract(contract),
        "tx_bars": (daily or {}).get("bars") or [],
        "tx_error": tx_err,
        "tx_window_days": 15,
        "steps": steps,
        "verdict": verdict,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    result["summary"], result["opinion"] = summarize(result)
    # A Coinalyze hiccup isn't cached for the day either.
    result["_transient"] = pending or bool(result["futures"].pop("error", False))
    return result


def summarize(r):
    """Plain-language description (in Arabic) of what the on-chain numbers
    show, plus a short factual take. Numbers, dates and source names stay
    left-to-right. Not investment advice - no buy / sell language."""
    from relative_strength import _ltr
    parts, plus, minus, has_revenue = [], 0, 0, False

    def pct0(x):
        return _ltr(format(x, "+.0f") + "%")

    def pct1(x):
        return _ltr(format(x, "+.1f") + "%")
    v = r["volume"]
    if v["pct"] is not None:
        if v["pct"] >= 35:
            parts.append(f"الحجم يرتفع بقوة خلال آخر 5 أيام ({pct0(v['pct'])})."); plus += 2
        elif v["pct"] > 0:
            parts.append(f"الحجم ارتفع قليلاً خلال آخر 5 أيام ({pct0(v['pct'])})."); plus += 1
        else:
            parts.append(f"الحجم ينخفض خلال آخر 5 أيام ({pct0(v['pct'])})."); minus += 1
    else:
        parts.append("لا يوجد سجل حجم كافٍ للحكم على الاتجاه.")
    lq = r["liquidity"]
    since = _ltr(lq["since"]) if lq.get("since") else ""
    if lq["trend"] == "rising":
        parts.append(f"السيولة تتزايد ({pct1(lq['pct'])} منذ {since})."); plus += 1
    elif lq["trend"] == "stable":
        parts.append(f"السيولة مستقرة تقريباً ({pct1(lq['pct'])} منذ {since}).")
    elif lq["trend"] == "falling":
        parts.append(f"السيولة تتناقص ({pct1(lq['pct'])} منذ {since})."); minus += 1
    else:
        parts.append("سجل السيولة غير متاح.")
    rv = r["revenue"]
    llama = _ltr("DefiLlama")
    if rv["status"] == "ok" and (rv.get("d30") or 0) > 0:
        d30 = _ltr("$" + format(rv["d30"], ",.0f"))
        parts.append(f"يحقق إيرادات فعلية ({d30} خلال آخر 30 يوماً، حسب {llama})."); plus += 1
        has_revenue = True
    elif rv["status"] == "ok":
        parts.append(f"يتتبّعه {llama} لكنه لا يُظهر إيرادات خلال آخر 30 يوماً."); minus += 1
    else:
        parts.append(f"لا توجد بيانات إيرادات (لا يتتبّعه {llama}).")
    sc = r.get("score") or {}
    if sc.get("full_max"):
        tot, mx = sc["total"], sc["full_max"]
        if tot >= 32:                      # the site's green badge (about the top 20%)
            parts.append(f"تقدير النقاط مرتفع ({_ltr(f'{tot}/{mx}')})."); plus += 1
        elif tot < 15:                     # red badge (about the bottom 40%)
            parts.append(f"تقدير النقاط منخفض ({_ltr(f'{tot}/{mx}')}) — إشارات الشراء ضعيفة حالياً."); minus += 1
    vd = r["verdict"]
    parts.append("يجتاز خطوات الفلتر السبع كلها." if vd["passes"] else
                 f"لا يجتاز الفلتر: {vd['reason']}." if vd["passes"] is False else vd["reason"])

    if plus >= 3 and minus == 0 and has_revenue:
        opinion = "إشارات قوية على السلسلة: النشاط والسيولة في نمو، وتدعمهما إيرادات فعلية."
    elif plus >= 3 and minus == 0:
        opinion = "إشارات قوية في النشاط والسيولة، لكن لا توجد بيانات إيرادات تدعمها."
    elif plus > minus:
        opinion = "إشارات إيجابية في معظمها، لكنها لا تتوافق كلها."
    elif minus > plus:
        opinion = "إشارات ضعيفة: النشاط يتراجع، أو أن الأساسيات التي يبحث عنها الفلتر غائبة."
    else:
        opinion = "إشارات متباينة: لا شيء يبرز بقوة في أي اتجاه."
    return " ".join(parts), opinion + " هذا وصف للبيانات على السلسلة فقط — وليس نصيحة مالية."


def check_score(md, mcap, ez, contract, chain, liq, daily, dex_liq):
    """The V3 score for any coin, with the SAME point functions as the daily
    score (scoring.py), from data the check already fetched (no extra API
    calls). Live here: turnover (CoinGecko all-exchange volume / market cap),
    15-day buyers (Codex daily bars), 7-day liquidity growth (Codex daily
    liquidity, the filter step's series), price cheapness and structure
    (our stored daily closes for a tracked coin, else the live Entry Zone
    position). Not available live (need our daily history): quiet
    absorption, holder growth / velocity, rank change - these take the
    latest daily run's median, as a tracked coin's missing parts do.
    Fix 4: Codex unreliable for the coin (> 10x DexScreener) -> no buyers /
    liquidity from Codex."""
    import scoring as S
    from database import SessionLocal
    from liquidity_check import get_liquidity_series, codex_unreliable
    stored = _stored_score(contract)
    if stored is not None:          # an active coin we score daily: exactly its stored score
        return stored
    unreliable = codex_unreliable(liq.get("today_liq"), dex_liq)
    raw, detail = {}, {}
    vol = ((md or {}).get("total_volume") or {}).get("usd")
    turnover = vol / mcap * 100 if vol and mcap else None
    raw["turnover"] = S.pts_turnover(turnover)
    detail["turnover"] = f"{turnover:.2f}% of market cap traded (all exchanges, 24h)" if turnover is not None else None
    bars = [b for b in ((daily or {}).get("bars") or []) if b.get("buy_volume") is not None][-S.BUYERS_DAYS:]
    if len(bars) >= S.BUYERS_MIN_DAYS and not unreliable:
        raw["buyers"], ratio = S.pts_buyers(sum(b["buy_volume"] for b in bars), sum(b["sell_volume"] or 0 for b in bars),
                                            sum(b.get("buys") or 0 for b in bars), sum(b.get("sells") or 0 for b in bars))
        detail["buyers"] = (f"buy/sell {ratio:.2f} over {len(bars)} days" if ratio is not None
                            else f"under ${S.BUYERS_MIN_USD:,} traded in {len(bars)} days")
    else:
        raw["buyers"] = None
        detail["buyers"] = "Codex unreliable for this coin" if unreliable else "not enough trade history"
    pct, base = (None, None) if unreliable else S.change_over(get_liquidity_series(contract, chain) or [])
    raw["liquidity"] = S.pts_liquidity(pct, base)
    detail["liquidity"] = (f"{min(pct, S.LIQ_CAP):+.1f}% over 7 days" if raw["liquidity"] is not None
                           else "Codex unreliable for this coin" if unreliable else
                           f"starting liquidity under ${S.LIQ_MIN_BASE:,}" if base is not None else "not enough liquidity history")
    closes = _stored_closes(contract)
    pos = S.price_position(closes) if closes else (
        ez.get("position_pct") if isinstance(ez, dict) and ez.get("status") == "ok" else None)
    raw["cheapness"], raw["structure"] = S.pts_cheapness(pos), (S.pts_structure(closes) if closes else None)
    detail["cheapness"] = f"{pos:.0f}% of the 60-day range" + (" (tracked)" if closes else "") if pos is not None else None
    if raw["structure"] is not None:
        detail["structure"] = ("higher low or steady 7-day trend, under 70% of the range" if raw["structure"]
                               else "no higher low or steady 7-day trend, or above 70% of the range")
    for k in ("absorption", "holders", "holder_vel", "rank_change"):
        raw[k] = None
    raw = S.anti_chase(raw, (md or {}).get("price_change_percentage_24h"))
    session = SessionLocal()
    try:
        med = S.latest_medians(session)
    finally:
        session.close()
    filled, missing = S.fill(raw, {k: med.get(k, 0) for k, *_ in S.PARTS})
    parts = [{"label": label, "pts": round(filled[col], 1), "max": mx, "available": col not in missing,
              "detail": detail.get(col) if col not in missing else (detail.get(col) or "needs our daily history")
              + " - tracked coins' median used"}
             for col, label, _, mx in S.PARTS]
    return {"total": round(sum(filled.values()), 1), "full_max": S.MAX_TOTAL, "parts": parts,
            "median_parts": len(missing)}


def _stored_entry_zone(contract):
    """The detail page's Entry Zone (entry_zone.get_entry_zone - stored
    history, 3-day cycle) when the contract is a coin we track (active /
    dropped) and it has one; marked "tracked". None otherwise (-> live).
    Never raises."""
    from sqlalchemy import func
    from database import SessionLocal
    from models import Coin
    from entry_zone import get_entry_zone
    session = SessionLocal()
    try:
        coin = (session.query(Coin).filter(func.lower(Coin.contract) == (contract or "").lower(),
                                           Coin.status.in_(["active", "dropped"])).first())
        if coin is None:
            return None
        ez = get_entry_zone(session, coin)
        return {**ez, "tracked": True} if ez.get("status") == "ok" else None
    except Exception:
        session.rollback()
        return None
    finally:
        session.close()


def _stored_score(contract):
    """The daily run's score (score_history, latest V3 row) for an ACTIVE coin
    we track - so Check a Coin shows the same score as the Active table and
    detail page. None otherwise. Never raises."""
    import scoring as S
    from sqlalchemy import func
    from database import SessionLocal
    from models import Coin, ScoreHistory
    session = SessionLocal()
    try:
        coin = (session.query(Coin).filter(func.lower(Coin.contract) == (contract or "").lower(),
                                           Coin.status == "active").first())
        h = (session.query(ScoreHistory).filter(ScoreHistory.coin_id == coin.id, ScoreHistory.missing.isnot(None))
             .order_by(ScoreHistory.recorded_at.desc()).first()) if coin else None
        if h is None:
            return None
        missing = set(filter(None, h.missing.split(",")))
        parts = [{"label": label, "pts": round(getattr(h, col), 1), "max": mx, "available": col not in missing,
                  "detail": f"daily run {h.recorded_at:%b %d}" + (" - no data, active coins' median used" if col in missing else "")}
                 for col, label, _, mx in S.PARTS]
        return {"total": round(h.score, 1), "full_max": S.MAX_TOTAL, "parts": parts, "median_parts": len(missing),
                "tracked": True}
    except Exception:
        session.rollback()
        return None
    finally:
        session.close()


def _stored_closes(contract):
    """Our stored daily closes (entry_zone_bars, Codex) when the contract is a
    coin we track (active / dropped) - what its daily score uses. None if
    untracked or no stored history. Never raises."""
    import json
    from sqlalchemy import func
    from database import SessionLocal
    from models import Coin, EntryZoneBars
    session = SessionLocal()
    try:
        coin = (session.query(Coin).filter(func.lower(Coin.contract) == (contract or "").lower(),
                                           Coin.status.in_(["active", "dropped"])).first())
        row = (session.query(EntryZoneBars).filter_by(chain=(coin.chain or "").lower(), contract=coin.contract.lower())
               .first()) if coin else None
        return [b[1] for b in json.loads(row.data)["bars"] if b[1]] if row else None
    except Exception:
        session.rollback()
        return None
    finally:
        session.close()


def check_supply(md):
    """Supply section data (same shape as the detail page's) from the
    CoinGecko response already fetched. No API calls."""
    total, circ, mx = md.get("total_supply"), md.get("circulating_supply"), md.get("max_supply")
    return {
        "total": total, "circulating": circ,
        "circulating_pct": round(circ / total * 100, 2) if circ and total else None,
        "max": mx,
        "uncapped": mx is None and (total is not None or circ is not None),
        "burned": None, "upcoming_unlock": None, "updated": None,
    }


def rules_signature():
    """The filter settings a check result depends on. A cached result made
    under different settings (e.g. before a range change) is recomputed."""
    from discovery import MIN_MCAP, MAX_MCAP
    from age_check import MIN_AGE_DAYS
    from volume_check import VOLUME_MIN_RISE
    from liquidity_check import MAX_DROP_PCT, MIN_LIQUIDITY_USD
    return (f"mcap {MIN_MCAP}-{MAX_MCAP}|age {MIN_AGE_DAYS}|vol {VOLUME_MIN_RISE}|liq {MAX_DROP_PCT}/{MIN_LIQUIDITY_USD}"
            "|pricerange|relstrength-ar|summary-ar|twitter-cmc|entryzone2|futures|score3|supply|ez-tracked|listings|volspike"
            "|liq3src|liq3k|score-v3")   # result layout: has these display sections


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
