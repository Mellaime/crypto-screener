"""
entry_zone.py
-------------
Detail page + Check a Coin: the "Entry Zone" section. DISPLAY ONLY - the
filter and drop_logic never use it. Data, not financial advice.

What it shows (top to bottom on the page):
  * the current ZONE (badge) and how it changed vs 3 days ago (from the saved
    daily zone history, entry_zone_history);
  * the coin's last PRICE_DAYS (60) days of daily closes: low, high, now and
    where "now" sits in that range (0% = low, 100% = high). Fewer days ->
    whatever history exists (labelled); fewer than MIN_HISTORY_DAYS (15) ->
    "too new for long-term context";
  * the ZONES TABLE: each zone's price band, cut from the 60-day low -> high
    at BEST_MAX_PCT / GOOD_MAX_PCT / WAIT_MIN_PCT;
  * an Arabic opinion sentence (where it is, and what price to wait for);
  * the DETAILS TABLE: the last 15 days in 3 groups of 5 (oldest / middle /
    latest) for volume, holders and liquidity, each a signed %:
        volume    = the group's 5-day AVERAGE vs the previous 5 days' average
        holders,  = the value at the END of the group vs 5 days earlier
        liquidity   (levels, not daily flows)

Sources - ACTIVE coins: STORED data only (entry_zone_bars, filled once from
Codex and then extended daily by the tracking step; see "STORED HISTORY"
below) - no live Codex calls; the zone is re-evaluated every RECHECK_DAYS (3)
days per coin. Check a Coin (untracked coins): live Codex daily bars, CoinGecko
market_chart (daily) when Codex has fewer than MIN_HISTORY_DAYS days.
LIQUIDITY is measured across ALL the coin's chains (DexScreener lists its
pools per chain under the same contract): one Codex liquidity history per
chain, summed day by day for the 5-day / 15-day trend; the real liquidity
SIZE is the sum of each chain's liquidity (the higher of Codex / DexScreener).
On a chain where Codex reports > 10x DexScreener (the bot's "liquidity source
mismatch" rule) Codex is left out of the trend and DexScreener's reading
counts for the size. Holders: our own daily snapshots
(detail page only; Check a Coin has none -> "—").

ZONES - checked in this order, first match wins:
  1. RISK        real liquidity < MIN_LIQUIDITY_USD (on its own), OR liquidity's
                 LATEST 5-day group fell more than LIQ_DROP_PCT AND its 15-day
                 change is negative, OR the same for holders (HOLDERS_DROP_PCT).
                 A recent dip in a coin still up over 15 days is not RISK.
                 The liquidity-DROP trigger is skipped for big coins (market cap
                 above BIG_COIN_MCAP): their DEX liquidity is a tiny, noisy part
                 of their trading. The $5K size and holders triggers still apply.
  2. WAIT        price position >= WAIT_MIN_PCT
  3. FALLING     volume falling strongly: latest group < -VOL_FLAT_PCT, or all
                 three groups below 0
  4. BEST        position <= BEST_MAX_PCT AND volume rising (latest > VOL_FLAT_PCT)
  5. GOOD        BEST_MAX_PCT < position <= GOOD_MAX_PCT, or position <=
                 BEST_MAX_PCT with volume quiet (within +/-VOL_FLAT_PCT)
  6. ACCUMULATE  GOOD_MAX_PCT < position < WAIT_MIN_PCT AND volume rising
  else NEUTRAL ("no clear zone"); < MIN_HISTORY_DAYS of history -> TOO_NEW.
(Liquidity and holders "not down" is guaranteed for 4-6 by rule 1. Unknown
liquidity / holders never blocks a zone.)
"""

import json
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# THRESHOLDS (tune here)
# ---------------------------------------------------------------------------
PRICE_DAYS = 60            # long-term window (daily closes)
MIN_HISTORY_DAYS = 15      # fewer days -> "too new for long-term context"
BEST_MAX_PCT = 20          # 0-20% of the range  -> BEST ENTRY band
GOOD_MAX_PCT = 35          # 20-35%              -> GOOD band
WAIT_MIN_PCT = 70          # 35-70% ACCUMULATE band; 70-100% -> WAIT
GROUP_DAYS = 5             # details: 3 groups of 5 days (15 days)
VOL_FLAT_PCT = 10          # volume group within +/-10% = quiet / flat
MIN_LIQUIDITY_USD = 5_000  # real liquidity under this -> RISK, whatever else
BIG_COIN_MCAP = 100_000_000  # above this market cap the liquidity-DROP trigger is skipped
LIQ_DROP_PCT = 10          # latest 5-day liquidity group down more than this -> RISK
HOLDERS_DROP_PCT = 10      # latest 5-day holders group down more than this -> RISK
CHANGE_LOOKBACK_DAYS = 3   # "zone change" line compares with this many days ago

ZONES = {
    "BEST": "🟢 BEST ENTRY", "GOOD": "🔵 GOOD", "ACCUMULATE": "🟡 ACCUMULATE", "WAIT": "🟠 WAIT",
    "FALLING": "🔴 FALLING", "RISK": "⚠️ RISK", "NEUTRAL": "⚪ NO CLEAR ZONE", "TOO_NEW": "⚪ TOO NEW",
}
# Better entry first - for "improved / declined" in the 3-day change line.
ZONE_RANK = {"BEST": 6, "GOOD": 5, "ACCUMULATE": 4, "NEUTRAL": 3, "WAIT": 2, "FALLING": 1, "RISK": 0}

BARS_QUERY = """query ($s: String!, $f: Int!, $t: Int!) { getBars(symbol: $s, from: $f, to: $t,
  resolution: "1D", removeLeadingNullValues: true) { t c volume liquidity } }"""


def _day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date()


def _pct(new, old):
    return round((new / old - 1) * 100, 1) if new is not None and old else None


def _value_at(series, d, slack=2):
    """Last value on or before day d (within `slack` days), from [(date, value)]."""
    vals = [v for day, v in series if v is not None and d - timedelta(days=slack) <= day <= d]
    return vals[-1] if vals else None


# ---------------------------------------------------------------- data sources
def codex_daily(contract, chain):
    """[(date, close, volume, liquidity)] oldest first (~80 days), or []."""
    from liquidity_check import CHAIN_TO_NETWORK, CODEX_KEY, CODEX_URL
    net = CHAIN_TO_NETWORK.get((chain or "").lower())
    if not net or not CODEX_KEY or not contract:
        return []
    now = int(time.time())
    try:
        b = requests.post(CODEX_URL, json={"query": BARS_QUERY, "variables": {
            "s": f"{contract}:{net}", "f": now - (PRICE_DAYS + 22) * 86400, "t": now}},
            headers={"Authorization": CODEX_KEY}, timeout=30).json()
    except (requests.exceptions.RequestException, ValueError):
        return []
    bars = ((b or {}).get("data") or {}).get("getBars") or {}
    out = []
    for i, t in enumerate(bars.get("t") or []):
        def val(k):
            try:
                v = (bars.get(k) or [])[i]
                return float(v) if v is not None else None
            except (IndexError, TypeError, ValueError):
                return None
        if val("c"):
            out.append((_day(t), val("c"), val("volume"), val("liquidity")))
    return out


def coingecko_daily(contract, chain):
    """Fallback: [(date, price, volume, None)] from CoinGecko (daily)."""
    from age_check import CHAIN_TO_CG_PLATFORM
    from discovery import CG_KEY
    plat = CHAIN_TO_CG_PLATFORM.get((chain or "").lower())
    if not plat or not CG_KEY or not contract:
        return []
    addr = contract.lower() if contract.startswith("0x") else contract
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/coins/{plat}/contract/{addr}/market_chart",
                         params={"vs_currency": "usd", "days": PRICE_DAYS + 20, "interval": "daily"},
                         headers={"x-cg-demo-api-key": CG_KEY}, timeout=30)
        data = r.json() if r.status_code == 200 else {}
    except (requests.exceptions.RequestException, ValueError):
        return []
    vols = {_day(ts / 1000): v for ts, v in data.get("total_volumes") or []}
    days = {}
    for ts, p in data.get("prices") or []:
        if p:
            days[_day(ts / 1000)] = (p, vols.get(_day(ts / 1000)))
    return [(d, p, v, None) for d, (p, v) in sorted(days.items())]


# ---------------------------------------------------------------- the numbers
def _level_groups(series, today):
    """3 point-to-point changes for a level series (holders, liquidity): value
    at the end of each 5-day group vs 5 days earlier. [oldest, middle, latest]."""
    ends = [today - timedelta(days=GROUP_DAYS * k) for k in (3, 2, 1, 0)]   # -15, -10, -5, now
    vals = [_value_at(series, d) for d in ends]
    return [_pct(vals[i], vals[i - 1]) for i in (1, 2, 3)]


def _volume_groups(bars, today):
    """3 changes: each 5-day volume average vs the previous 5 days' (complete days)."""
    vols = [v for d, _, v, _ in bars if d < today and v is not None][-(4 * GROUP_DAYS):]
    blocks = [vols[i:i + GROUP_DAYS] for i in range(len(vols) % GROUP_DAYS, len(vols), GROUP_DAYS)]
    avgs = [sum(b) / len(b) for b in blocks if len(b) == GROUP_DAYS]
    changes = [_pct(avgs[i], avgs[i - 1]) for i in range(1, len(avgs))]
    return ([None] * (3 - len(changes)) + changes)[-3:]


def zone_bands(low, high):
    """Each zone's price band from the 60-day low -> high."""
    span = high - low
    at = lambda pct: low + span * pct / 100
    return {"BEST": [low, at(BEST_MAX_PCT)], "GOOD": [at(BEST_MAX_PCT), at(GOOD_MAX_PCT)],
            "ACCUMULATE": [at(GOOD_MAX_PCT), at(WAIT_MIN_PCT)], "WAIT": [at(WAIT_MIN_PCT), high]}


def compute(bars, today, liq_series=None, holders=None, liq_usd=None, liq_source="Codex",
            mcap=None, liq_chains=None):
    """The section's numbers. liq_series / holders = [(date, value)] level series."""
    prices = [(d, c) for d, c, _, _ in bars][-PRICE_DAYS:]
    res = {"history_days": len(prices), "too_new": len(prices) < MIN_HISTORY_DAYS}
    if prices:
        closes = [c for _, c in prices]
        lo, hi, cur = min(closes), max(closes), closes[-1]
        res.update(price=cur, low=lo, high=hi, above_low_pct=_pct(cur, lo),
                   position_pct=round((cur - lo) / (hi - lo) * 100, 1) if hi > lo else 50.0,
                   bands=zone_bands(lo, hi))
    if liq_series is None:
        liq_series = [(d, l) for d, _, _, l in bars if l]
    res["volume_groups"] = _volume_groups(bars, today)
    res["liquidity_groups"] = _level_groups(liq_series, today) if liq_series else [None] * 3
    res["holders_groups"] = _level_groups(holders, today) if holders else [None] * 3
    # 15-day changes: RISK needs these negative too (with a > 10% drop in the latest 5 days).
    def window_change(series):
        s = [(d, v) for d, v in (series or []) if v and d >= today - timedelta(days=3 * GROUP_DAYS)]
        return _pct(s[-1][1], s[0][1]) if len(s) >= 2 else None
    res["liquidity_pct"] = window_change(liq_series)
    res["holders_pct"] = window_change(holders)
    res["liquidity_usd"], res["liquidity_source"] = liq_usd, liq_source
    res["liquidity_chains"] = liq_chains or []
    res["mcap"] = mcap
    res["big_coin"] = mcap is not None and mcap > BIG_COIN_MCAP
    res["zone"] = classify(res)
    res["zone_label"] = ZONES[res["zone"]]
    res["band"] = band_of(res.get("position_pct"))
    res["line"] = sentence(res)
    return res


def band_of(pos):
    if pos is None:
        return None
    return ("BEST" if pos <= BEST_MAX_PCT else "GOOD" if pos <= GOOD_MAX_PCT
            else "ACCUMULATE" if pos < WAIT_MIN_PCT else "WAIT")


def classify(r):
    """The zones, in order (see the module docstring)."""
    if r.get("too_new") or r.get("position_pct") is None:
        return "TOO_NEW"
    pos = r["position_pct"]
    groups = [g for g in r.get("volume_groups") or [] if g is not None]
    latest = groups[-1] if groups else None
    thin = r.get("liquidity_usd") is not None and r["liquidity_usd"] < MIN_LIQUIDITY_USD
    liq_last = (r.get("liquidity_groups") or [None])[-1]
    hold_last = (r.get("holders_groups") or [None])[-1]
    # latest 5 days down > threshold AND the whole 15 days negative
    big = r.get("mcap") is not None and r["mcap"] > BIG_COIN_MCAP
    liq_fell = (not big and liq_last is not None and liq_last < -LIQ_DROP_PCT
                and r.get("liquidity_pct") is not None and r["liquidity_pct"] < 0)
    hold_fell = (hold_last is not None and hold_last < -HOLDERS_DROP_PCT
                 and r.get("holders_pct") is not None and r["holders_pct"] < 0)
    if thin or liq_fell or hold_fell:
        return "RISK"
    if pos >= WAIT_MIN_PCT:
        return "WAIT"
    if (latest is not None and latest < -VOL_FLAT_PCT) or (len(groups) == 3 and all(g < 0 for g in groups)):
        return "FALLING"
    rising = latest is not None and latest > VOL_FLAT_PCT
    if pos <= BEST_MAX_PCT:
        return "BEST" if rising else "GOOD"
    if pos <= GOOD_MAX_PCT:
        return "GOOD"
    return "ACCUMULATE" if rising else "NEUTRAL"


def sentence(r):
    """The Arabic opinion: where the coin is now and what price to wait for."""
    from relative_strength import _ltr

    def usd(v):
        if v is None:
            return "—"
        return _ltr("$" + (f"{v:,.2f}" if v >= 1 else f"{v:.4g}"))

    def p(v):
        return _ltr(f"{v:+.0f}%") if v is not None else "—"
    z = r["zone"]
    if z == "TOO_NEW":
        return f"العملة جديدة ({_ltr(r['history_days'])} يوم فقط من البيانات) — ما كاينش سياق طويل المدى بعد."
    pos = _ltr(f"{int(r['position_pct'] + 0.5)}%")
    b = r["bands"]
    best_top, good_top, acc_top = usd(b["BEST"][1]), usd(b["GOOD"][1]), usd(b["ACCUMULATE"][1])
    now = usd(r["price"])
    groups = [g for g in r["volume_groups"] if g is not None]
    vol = p(groups[-1]) if groups else "—"
    if z == "RISK":
        if r.get("liquidity_usd") is not None and r["liquidity_usd"] < MIN_LIQUIDITY_USD:
            return (f"السيولة الفعلية {_ltr('$' + format(r['liquidity_usd'], ',.0f'))} فقط (أقل من {_ltr(f'${MIN_LIQUIDITY_USD:,}')})"
                    " — صعيب التداول عليها، ماشي منطقة دخول مهما كان السعر.")
        bits = []
        liq_last = (r.get("liquidity_groups") or [None])[-1]
        hold_last = (r.get("holders_groups") or [None])[-1]
        if liq_last is not None and liq_last < -LIQ_DROP_PCT and (r.get("liquidity_pct") or 0) < 0:
            bits.append(f"السيولة طاحت {p(liq_last)} فآخر 5 أيام ({p(r['liquidity_pct'])} ف15 يوم)")
        if hold_last is not None and hold_last < -HOLDERS_DROP_PCT and (r.get("holders_pct") or 0) < 0:
            bits.append(f"الحاملين طاحو {p(hold_last)} فآخر 5 أيام ({p(r['holders_pct'])} ف15 يوم)")
        return f"إشارة خطر: {' و'.join(bits)} — تسنّى حتى تستقر قبل أي دخول."
    if z == "WAIT":
        return (f"السعر {now} قريب من القمة ({pos} من المدى) — دخول متأخر؛"
                f" تسنّى السعر ينزل تحت {acc_top} لمنطقة {_ltr('ACCUMULATE')} أو تحت {best_top} لمنطقة {_ltr('BEST ENTRY')}.")
    if z == "FALLING":
        return (f"الفوليم كيطيح ({vol} فآخر 5 أيام) والسعر {now} ({pos} من المدى)"
                f" — تسنّى حتى يرجع الفوليم يطلع؛ منطقة {_ltr('BEST ENTRY')} تحت {best_top}.")
    if z == "BEST":
        return (f"السعر {now} فمنطقة {_ltr('BEST ENTRY')} ({pos} من المدى) والفوليم طالع ({vol})"
                " — أحسن منطقة دخول حسب البيانات.")
    if z == "GOOD":
        return (f"السعر {now} فمنطقة {_ltr('GOOD')} ({pos} من المدى) — دخول مقبول؛"
                f" السعر الأحسن تحت {best_top} لمنطقة {_ltr('BEST ENTRY')}.")
    if z == "ACCUMULATE":
        return (f"السعر {now} فالوسط ({pos} من المدى) والفوليم طالع ({vol}) — منطقة تجميع؛"
                f" للدخول الأحسن تسنّى السعر ينزل تحت {good_top} ({_ltr('GOOD')}) أو {best_top} ({_ltr('BEST ENTRY')}).")
    return (f"السعر {now} فالوسط ({pos} من المدى) والفوليم هادئ ({vol}) — ما كايناش منطقة واضحة؛"
            f" تسنّى السعر ينزل تحت {good_top} لمنطقة {_ltr('GOOD')} أو حتى يطلع الفوليم.")


# ---------------------------------------------------------------- entry points
def dex_chains(contract):
    """{chain: DexScreener liquidity} of every chain where DexScreener lists
    pools for this exact contract (1 call), or {} if it can't say."""
    try:
        pairs = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{contract}", timeout=20).json().get("pairs") or []
    except (requests.exceptions.RequestException, ValueError):
        return {}
    out = {}
    for p in pairs:
        if ((p.get("baseToken") or {}).get("address") or "").lower() != (contract or "").lower():
            continue
        ch = (p.get("chainId") or "").lower()
        out[ch] = out.get(ch, 0) + ((p.get("liquidity") or {}).get("usd") or 0)
    return out


def _sum_series(series_list, today):
    """Day-by-day sum of several [(date, value)] series (a day counts only when
    every series has a reading within 2 days of it)."""
    out = []
    for k in range(3 * GROUP_DAYS + 3, -1, -1):
        d = today - timedelta(days=k)
        vals = [_value_at(s, d) for s in series_list]
        if vals and all(v is not None for v in vals):
            out.append((d, sum(vals)))
    return out


def build(contract, chain, today=None, dex_liq=None, holders=None, liq_history=None, mcap=None):
    """Fetch + compute. dex_liq = current DexScreener liquidity on `chain`;
    liq_history = [(date, DexScreener liquidity)] on `chain` (our snapshots)
    used when Codex is unreliable there; mcap = market cap (big-coin exemption)."""
    from liquidity_check import codex_unreliable, best_liquidity, CHAIN_TO_NETWORK
    today = today or datetime.now(timezone.utc).date()
    chain = (chain or "").lower()
    bars, source = codex_daily(contract, chain), "Codex"
    if len(bars) < MIN_HISTORY_DAYS:
        cg = coingecko_daily(contract, chain)
        if len(cg) > len(bars):
            bars, source = cg, "CoinGecko"
    if not bars:
        return {"status": "unknown", "reason": "No daily price history on Codex or CoinGecko"}

    # Liquidity across ALL chains (same contract), source rules per chain.
    ds = dex_chains(contract)
    if dex_liq is not None:
        ds[chain] = max(ds.get(chain, 0), dex_liq)
    ds.setdefault(chain, dex_liq)
    trend_series, size, per_chain, unreliable = [], 0.0, [], False
    for ch, ds_now in sorted(ds.items(), key=lambda kv: -(kv[1] or 0)):
        if ch == chain and source == "Codex":
            series = [(d, l) for d, _, _, l in bars if l]
        elif ch in CHAIN_TO_NETWORK:
            series = [(d, l) for d, _, _, l in codex_daily(contract, ch) if l]
        else:
            series = []
        codex_now = series[-1][1] if series else None
        if codex_unreliable(codex_now, ds_now):
            unreliable = True
            series = (liq_history or []) if ch == chain else []      # our DexScreener snapshots, if any
            part = ds_now
            src = "DexScreener (Codex unreliable)"
        else:
            part = best_liquidity(codex_now, ds_now)[0]
            src = "Codex" if series else "DexScreener"
        if series:
            trend_series.append(series)
        if part:
            size += part
            per_chain.append({"chain": ch, "liquidity": part, "source": src})
    if source == "CoinGecko" and not trend_series and liq_history:
        trend_series = [liq_history]
    liq_series = _sum_series(trend_series, today) if trend_series else []
    liq_source = ("all chains" if len(per_chain) > 1 else "Codex") + (" · Codex unreliable on some" if unreliable else "")
    res = compute(bars, today, liq_series, holders, size or None, liq_source, mcap, per_chain)
    res.update(status="ok", source=source)
    return res


# ===========================================================================
# STORED HISTORY (active coins): the Entry Zone of a tracked coin is computed
# from entry_zone_bars - its daily (close, volume, primary-chain liquidity,
# all-chains liquidity) - never from live Codex calls. Codex is used ONCE per
# coin, the first time it is tracked (backfill: that history doesn't exist in
# our database). After that the daily tracking step appends one row per day
# from what it already stores (price, Codex 24h volume, liquidity), plus ONE
# bulk Codex request for the extra chains of every multi-chain coin.
# ===========================================================================
RECHECK_DAYS = 3           # a coin's zone is re-evaluated every 3 days
BARS_KEEP_DAYS = 90        # stored daily history per coin
CHAINS_REFRESH_DAYS = 7    # re-list a coin's chains (DexScreener) after this


def _bars_row(session, coin):
    from models import EntryZoneBars
    return (session.query(EntryZoneBars)
            .filter_by(chain=(coin.chain or "").lower(), contract=(coin.contract or "").lower()).first())


def load_bars(session, coin):
    """(bars, meta) from entry_zone_bars: bars = [(date, close, volume, liq_all)]."""
    row = _bars_row(session, coin)
    if row is None:
        return None, None
    data = json.loads(row.data)
    # liq_all only: never fall back to the raw main-chain Codex number (it may be
    # the unreliable reading the mismatch rule rejected).
    bars = [(datetime.fromisoformat(d).date(), c, v, la) for d, c, v, lp, la in data["bars"]]
    return bars, data


def _save_bars(session, coin, bars5, chains, chains_day):
    """bars5 = [(date, close, volume, liq_primary, liq_all)] -> entry_zone_bars."""
    from models import EntryZoneBars
    row = _bars_row(session, coin)
    if row is None:
        row = EntryZoneBars(chain=(coin.chain or "").lower(), contract=(coin.contract or "").lower())
        session.add(row)
    bars5 = sorted({b[0]: b for b in bars5}.values())[-BARS_KEEP_DAYS:]
    row.data = json.dumps({"bars": [[d.isoformat(), c, v, lp, la] for d, c, v, lp, la in bars5],
                           "chains": chains, "chains_day": chains_day.isoformat() if chains_day else None})
    row.updated_day = bars5[-1][0] if bars5 else None


def backfill_bars(session, coin, today, dex_liq=None):
    """ONE-TIME Codex fetch of a coin's daily history (the data doesn't exist
    in our database yet): primary chain bars + each extra chain's liquidity,
    summed (source-mismatch rule per chain). Returns the number of Codex calls."""
    from liquidity_check import codex_unreliable, CHAIN_TO_NETWORK
    chain = (coin.chain or "").lower()
    bars, calls = codex_daily(coin.contract, chain), 1
    if len(bars) < MIN_HISTORY_DAYS:
        cg = coingecko_daily(coin.contract, chain)
        if len(cg) > len(bars):
            bars = cg
    if not bars:
        return calls
    ds = dex_chains(coin.contract)
    if dex_liq is not None:
        ds[chain] = max(ds.get(chain, 0) or 0, dex_liq)
    ds.setdefault(chain, dex_liq)
    extra = {}
    for ch, ds_now in ds.items():
        if ch == chain or ch not in CHAIN_TO_NETWORK:
            continue
        s = [(d, l) for d, _, _, l in codex_daily(coin.contract, ch) if l]
        calls += 1
        if s and not codex_unreliable(s[-1][1], ds_now):
            extra[ch] = s
    primary_ok = not codex_unreliable(next((l for *_, l in reversed(bars) if l), None), ds.get(chain))
    # Codex unreliable on the main chain -> our stored DexScreener readings for it
    # (only exist since the coin was first tracked; older days stay unknown).
    from models import DailySnapshot
    ds_hist = [(d, l) for d, l in session.query(DailySnapshot.date, DailySnapshot.liquidity)
               .filter(DailySnapshot.coin_id == coin.id, DailySnapshot.liquidity.isnot(None))
               .order_by(DailySnapshot.date)]
    if ds.get(chain) is not None and not any(d == today for d, _ in ds_hist):
        ds_hist.append((today, ds[chain]))
    out = []
    for d, c, v, lp in bars:
        main_liq = lp if (primary_ok and lp is not None) else _value_at(ds_hist, d)
        parts = [main_liq] + [_value_at(s, d) for s in extra.values()]
        known = [x for x in parts if x is not None]
        out.append((d, c, v, lp, sum(known) if len(known) == len(parts) else None))
    _save_bars(session, coin, out, {k: v for k, v in ds.items() if v is not None}, today)
    return calls


def codex_bulk_liquidity(tokens):
    """{(contract_lower, networkId): liquidity} for [(contract, chain)] via ONE
    bulk Codex filterTokens request per 200 tokens."""
    from liquidity_check import CHAIN_TO_NETWORK, CODEX_KEY, CODEX_URL
    ids = [f"{ct}:{CHAIN_TO_NETWORK[ch]}" for ct, ch in tokens if ch in CHAIN_TO_NETWORK]
    q = """query ($tokens: [String]) { filterTokens(tokens: $tokens, limit: 200) {
      results { liquidity token { address networkId } } } }"""
    out, calls = {}, 0
    for i in range(0, len(ids), 200):
        calls += 1
        try:
            body = requests.post(CODEX_URL, json={"query": q, "variables": {"tokens": ids[i:i + 200]}},
                                 headers={"Authorization": CODEX_KEY}, timeout=60).json()
        except (requests.exceptions.RequestException, ValueError):
            continue
        for r in ((body.get("data") or {}).get("filterTokens") or {}).get("results") or []:
            t = r.get("token") or {}
            try:
                out[((t.get("address") or "").lower(), int(t.get("networkId")))] = float(r.get("liquidity"))
            except (TypeError, ValueError):
                pass
    return out, calls


def store_daily_bars(session, coins, today):
    """Daily tracking step: append today's row to every tracked coin's stored
    history from today's snapshot (price, Codex 24h volume, liquidity) - no
    per-coin Codex calls - plus one bulk Codex request for all extra chains.
    New coins (no stored history yet) get the one-time backfill.
    Returns {"appended", "backfilled", "codex_calls"}."""
    from liquidity_check import codex_unreliable, best_liquidity, CHAIN_TO_NETWORK
    from models import DailySnapshot
    stats = {"appended": 0, "backfilled": 0, "codex_calls": 0}
    have, need_extra = {}, []
    for coin in coins:
        bars, meta = load_bars(session, coin)
        have[coin.id] = meta
        for ch in (meta or {}).get("chains", {}):
            if ch != (coin.chain or "").lower() and ch in CHAIN_TO_NETWORK:
                need_extra.append((coin.contract, ch))
    bulk, calls = codex_bulk_liquidity(need_extra) if need_extra else ({}, 0)
    stats["codex_calls"] += calls
    for coin in coins:
        snap = (session.query(DailySnapshot).filter_by(coin_id=coin.id, date=today).first())
        meta = have[coin.id]
        if meta is None:                                   # new coin -> one-time backfill
            stats["codex_calls"] += backfill_bars(session, coin, today, snap.liquidity if snap else None)
            stats["backfilled"] += 1
            continue
        if snap is None or not snap.price_usd:
            continue
        chain = (coin.chain or "").lower()
        chains = meta.get("chains") or {}
        lp = snap.codex_liquidity
        primary = (snap.liquidity if codex_unreliable(lp, snap.liquidity)
                   else best_liquidity(lp, snap.liquidity)[0])
        parts = [primary]
        for ch, ds_now in chains.items():
            if ch == chain or ch not in CHAIN_TO_NETWORK:
                continue
            cx = bulk.get(((coin.contract or "").lower(), CHAIN_TO_NETWORK[ch]))
            parts.append(ds_now if codex_unreliable(cx, ds_now) else best_liquidity(cx, ds_now)[0])
        known = [x for x in parts if x is not None]
        la = sum(known) if known else None
        vol = snap.codex_volume24 if snap.codex_volume24 is not None else snap.volume
        bars5 = [(datetime.fromisoformat(d).date(), c, v, l1, l2) for d, c, v, l1, l2 in meta["bars"]]
        bars5.append((today, snap.price_usd, vol, lp, la))
        chains_day = datetime.fromisoformat(meta["chains_day"]).date() if meta.get("chains_day") else None
        if chains_day is None or (today - chains_day).days >= CHAINS_REFRESH_DAYS:
            fresh = dex_chains(coin.contract)              # DexScreener only (no Codex)
            if fresh:
                chains, chains_day = fresh, today
        _save_bars(session, coin, bars5, chains, chains_day)
        stats["appended"] += 1
    session.commit()
    return stats


def compute_stored(session, coin, today):
    """The Entry Zone of a tracked coin from STORED data only (no Codex)."""
    from models import DailySnapshot
    bars, meta = load_bars(session, coin)
    if not bars:
        return {"status": "unknown", "reason": "No stored daily history yet - filled by the next daily run"}
    snaps = (session.query(DailySnapshot.date, DailySnapshot.holders, DailySnapshot.mcap,
                           DailySnapshot.liquidity)
             .filter(DailySnapshot.coin_id == coin.id).order_by(DailySnapshot.date).all())
    four = [(d, c, v, la) for d, c, v, la in bars]
    liq_series = [(d, la) for d, _, _, la in bars if la]
    chains = (meta or {}).get("chains") or {}
    # Real liquidity SIZE, as the rest of the bot: the higher of Codex (stored
    # all-chains figure) and DexScreener (today's main-chain reading + the other
    # chains' DexScreener liquidity) - Codex under-reports many coins.
    main = (coin.chain or "").lower()
    ds_now = next((l for *_, l in reversed(snaps) if l), None)
    ds_total = (ds_now or 0) + sum(v or 0 for ch, v in chains.items() if ch != main)
    stored_now = liq_series[-1][1] if liq_series else None
    size = max(stored_now or 0, ds_total) or None
    res = compute(four, today, liq_series, [(d, h) for d, h, _, _ in snaps if h],
                  size,
                  "all chains" if len(chains) > 1 else "stored",
                  next((m for _, _, m, _ in reversed(snaps) if m), None),
                  [{"chain": ch, "liquidity": v} for ch, v in chains.items()])
    res.update(status="ok", source="stored")
    return res


# ===========================================================================
# 3-DAY CYCLE (per coin): a coin's zone is evaluated once, kept as-is for
# RECHECK_DAYS - 1 days, then re-evaluated. The re-check is what produces the
# change line ("من X ← ولات Y" / "بقات نفسها"). entry_zone_cache holds the
# coin's last evaluation (day = the day it was evaluated); entry_zone_history
# gets one row per evaluation.
# ===========================================================================
def save_zone(session, coin, today, res):
    from models import EntryZoneHistory
    key = ((coin.chain or "").lower(), (coin.contract or "").lower())
    row = session.query(EntryZoneHistory).filter_by(chain=key[0], contract=key[1], day=today).first()
    if row is None:
        row = EntryZoneHistory(chain=key[0], contract=key[1], day=today)
        session.add(row)
    row.zone, row.position_pct = res["zone"], res.get("position_pct")


def evaluate(session, coin, today=None, force=False):
    """Re-evaluate the coin's zone if it was never evaluated or its last
    evaluation is RECHECK_DAYS or more old (or force). Returns (result, evaluated_now)."""
    from models import EntryZoneCache
    today = today or datetime.now(timezone.utc).date()
    key = ((coin.chain or "").lower(), (coin.contract or "").lower())
    row = session.query(EntryZoneCache).filter_by(chain=key[0], contract=key[1]).first()
    prev = json.loads(row.data) if row is not None else None
    if prev and prev.get("status") == "ok" and not force and (today - row.day).days < RECHECK_DAYS:
        return prev, False                                  # days 1-2: keep the saved zone
    res = compute_stored(session, coin, today)
    if res.get("status") != "ok":
        return (prev or res), False
    if prev and prev.get("status") == "ok" and prev.get("zone") in ZONE_RANK | {"TOO_NEW": 0}:
        old, new = prev["zone"], res["zone"]
        res["change"] = {"from": old, "to": new, "from_day": row.day.isoformat(), "changed": old != new}
        if old != new and old in ZONE_RANK and new in ZONE_RANK:
            res["change"]["direction"] = "up" if ZONE_RANK[new] > ZONE_RANK[old] else "down"
    else:
        res["change"] = None                                # first evaluation
    res["evaluated_day"] = today.isoformat()
    res["next_check_day"] = (today + timedelta(days=RECHECK_DAYS)).isoformat()
    if row is None:
        row = EntryZoneCache(chain=key[0], contract=key[1])
        session.add(row)
    row.day, row.data = today, json.dumps(res)
    save_zone(session, coin, today, res)
    session.commit()
    return res, True


def get_entry_zone(session, coin, refresh=False):
    """Detail page: the coin's saved zone (evaluated every RECHECK_DAYS by
    the daily job); evaluated now from stored data if it has none yet.
    No Codex calls. Never raises on missing tables."""
    try:
        res, _ = evaluate(session, coin, force=refresh)
    except Exception:
        session.rollback()
        return {"status": "unknown", "reason": "Entry Zone data not available yet"}
    res = dict(res)
    res["change_available"] = True
    return res


def run_entry_zone_snapshot():
    """Daily job: re-evaluate every ACTIVE coin whose zone is due (last
    evaluated RECHECK_DAYS or more ago, or never) - from stored data only."""
    from database import SessionLocal
    from models import Coin
    session = SessionLocal()
    stats = {"coins": 0, "evaluated": 0, "kept": 0, "changed": 0}
    try:
        for coin in session.query(Coin).filter_by(status="active").all():
            stats["coins"] += 1
            try:
                res, now = evaluate(session, coin)
            except Exception:
                session.rollback()
                continue
            if now:
                stats["evaluated"] += 1
                stats["changed"] += bool((res.get("change") or {}).get("changed"))
            else:
                stats["kept"] += 1
    finally:
        session.close()
    print(f"[INFO] Entry Zone: {stats['evaluated']} re-evaluated ({stats['changed']} changed zone), "
          f"{stats['kept']} kept (re-check every {RECHECK_DAYS} days) - no Codex calls")
    return stats
