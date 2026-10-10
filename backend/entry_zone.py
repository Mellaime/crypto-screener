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

Sources: Codex daily bars (close, volume, liquidity) - one call; CoinGecko
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


def zone_change(session, coin, today, zone):
    """The 3-day change line data from entry_zone_history, or None if there's no
    record from ~CHANGE_LOOKBACK_DAYS days ago yet."""
    from models import EntryZoneHistory
    key = ((coin.chain or "").lower(), (coin.contract or "").lower())
    target = today - timedelta(days=CHANGE_LOOKBACK_DAYS)
    rows = (session.query(EntryZoneHistory)
            .filter(EntryZoneHistory.chain == key[0], EntryZoneHistory.contract == key[1],
                    EntryZoneHistory.day <= target, EntryZoneHistory.day >= target - timedelta(days=1))
            .order_by(EntryZoneHistory.day.desc()).first())
    if rows is None:
        return None
    old = rows.zone
    out = {"from": old, "from_label": ZONES.get(old, old), "from_day": rows.day.isoformat(), "to": zone,
           "changed": old != zone}
    if old != zone and old in ZONE_RANK and zone in ZONE_RANK:
        out["direction"] = "up" if ZONE_RANK[zone] > ZONE_RANK[old] else "down"
    return out


def save_zone(session, coin, today, res):
    """Today's zone into entry_zone_history (one row per coin per day)."""
    from models import EntryZoneHistory
    key = ((coin.chain or "").lower(), (coin.contract or "").lower())
    row = session.query(EntryZoneHistory).filter_by(chain=key[0], contract=key[1], day=today).first()
    if row is None:
        row = EntryZoneHistory(chain=key[0], contract=key[1], day=today)
        session.add(row)
    row.zone, row.position_pct = res["zone"], res.get("position_pct")


def get_entry_zone(session, coin, refresh=False):
    """Detail page (and the daily job with refresh=True): build() with our
    holders + DexScreener history, cached per coin per UTC day
    (entry_zone_cache); today's zone is saved to entry_zone_history. The
    3-day change line is added fresh each time. Never raises on a missing table."""
    from models import DailySnapshot, EntryZoneCache
    today = datetime.now(timezone.utc).date()
    key = ((coin.chain or "").lower(), (coin.contract or "").lower())
    res, row = None, None
    try:
        row = session.query(EntryZoneCache).filter_by(chain=key[0], contract=key[1]).first()
        if row is not None and row.day == today and not refresh:
            res = json.loads(row.data)
    except Exception:
        session.rollback()
        row = False                              # tables missing: compute, don't save
    if res is None:
        snaps = (session.query(DailySnapshot.date, DailySnapshot.holders, DailySnapshot.liquidity,
                               DailySnapshot.mcap)
                 .filter(DailySnapshot.coin_id == coin.id).order_by(DailySnapshot.date).all())
        res = build(coin.contract, coin.chain, today,
                    dex_liq=snaps[-1][2] if snaps else None,
                    holders=[(d, h) for d, h, _, _ in snaps if h],
                    liq_history=[(d, l) for d, _, l, _ in snaps if l],
                    mcap=next((m for *_, m in reversed(snaps) if m), None))
        if res.get("status") == "ok" and row is not False:
            try:
                if row is None:
                    row = EntryZoneCache(chain=key[0], contract=key[1])
                    session.add(row)
                row.day, row.data = today, json.dumps(res)
                save_zone(session, coin, today, res)
                session.commit()
            except Exception:
                session.rollback()
    if res.get("status") == "ok":
        try:
            res["change"] = zone_change(session, coin, today, res["zone"])
            res["change_available"] = True
        except Exception:
            session.rollback()
            res["change"], res["change_available"] = None, False
    return res


def run_entry_zone_snapshot():
    """Daily job: compute + save today's zone for every ACTIVE coin (one Codex
    call each), so the 3-day change line has history. Display only."""
    from database import SessionLocal
    from models import Coin
    session = SessionLocal()
    stats = {"coins": 0, "saved": 0}
    try:
        for coin in session.query(Coin).filter_by(status="active").all():
            stats["coins"] += 1
            res = get_entry_zone(session, coin, refresh=True)
            stats["saved"] += res.get("status") == "ok"
    finally:
        session.close()
    print(f"[INFO] Entry Zone snapshot: {stats['saved']}/{stats['coins']} active coins' zones saved")
    return stats
