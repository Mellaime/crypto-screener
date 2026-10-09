"""
entry_zone.py
-------------
Detail page + Check a Coin: the "Entry Zone" section. DISPLAY ONLY - the
filter and drop_logic never use it. Data, not financial advice.

Two layers:
  * LONG-TERM CONTEXT - where today's price sits in its last PRICE_DAYS (60)
    days of daily closes: position 0% = at the low, 100% = at the high.
    Fewer days available -> whatever history exists (labelled); fewer than
    MIN_HISTORY_DAYS (15) -> "too new for long-term context".
  * SHORT-TERM - the last 15 complete days of volume in 3 groups of 5
    (oldest / middle / latest); each group = its 5-day average vs the 5 days
    before it, as a signed %. Plus liquidity and holders: % change over the
    last WINDOW_DAYS (15).

Sources: Codex daily bars (close, volume, liquidity) - one call; CoinGecko
market_chart (60 days, daily) when Codex doesn't cover the coin. Liquidity
uses DexScreener instead when Codex reports > 10x DexScreener (the same
"liquidity source mismatch" rule as the rest of the bot) - both for the
liquidity change and for the real liquidity SIZE (liquidity_check.best_liquidity:
the higher of Codex / DexScreener, or DexScreener on a mismatch). Holders: our own
daily snapshots (detail page only).

ZONES - checked in this order, first match wins:
  1. RISK        real liquidity under MIN_LIQUIDITY_USD (untradeable, whatever
                 else)  OR  liquidity fell > LIQ_DROP_PCT  OR  holders fell > HOLDERS_DROP_PCT
  2. WAIT        price position >= PRICE_HIGH_PCT (near the 60-day high)
  3. FALLING     volume falling strongly (latest group < -VOL_FLAT_PCT, or all
                 three groups below 0)  OR  holders fell > HOLDERS_DROP_PCT
  4. BEST        price <= PRICE_LOW_PCT AND volume rising (latest group
                 > VOL_FLAT_PCT) AND liquidity not down AND holders not down
  5. GOOD        price <= PRICE_LOW_PCT AND volume quiet (latest within
                 +/-VOL_FLAT_PCT) AND liquidity not down AND holders not down
  6. ACCUMULATE  PRICE_LOW_PCT < price < PRICE_HIGH_PCT AND volume rising
                 (latest > VOL_FLAT_PCT) AND liquidity not down AND holders not down
  else NEUTRAL (no clear zone).  < MIN_HISTORY_DAYS of history -> TOO_NEW.
"Not down" = not below -LIQ_DROP_PCT / -HOLDERS_DROP_PCT (flat is fine).
Unknown liquidity / holders (no data) never blocks a zone - shown as "—".
"""

import json
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# THRESHOLDS (tune here)
# ---------------------------------------------------------------------------
PRICE_DAYS = 60          # long-term window (daily closes)
MIN_HISTORY_DAYS = 15    # fewer days -> "too new for long-term context"
PRICE_LOW_PCT = 35       # position <= this -> price is cheap in its range
PRICE_HIGH_PCT = 70      # position >= this -> near the high -> WAIT
VOL_GROUP_DAYS = 5       # volume: 3 groups of 5 complete days ...
VOL_FLAT_PCT = 10        # ... each vs the 5 days before; within +/-10% = flat
VOL_STRONG_PCT = 30      # latest group >= +30% = "rising strongly" (sentence wording only)
WINDOW_DAYS = 15         # liquidity / holders change measured over this
LIQ_DROP_PCT = 5         # liquidity down more than this = fell (RISK)
MIN_LIQUIDITY_USD = 5_000  # real liquidity under this = untradeable -> RISK, whatever else
HOLDERS_DROP_PCT = 5     # holders down more than this = fell (RISK / FALLING)

ZONES = {
    "RISK": "⚠️ RISK", "WAIT": "🟠 WAIT", "FALLING": "🔴 FALLING", "BEST": "🟢 BEST ENTRY",
    "GOOD": "🔵 GOOD", "ACCUMULATE": "🟡 ACCUMULATE", "NEUTRAL": "⚪ NO CLEAR ZONE",
    "TOO_NEW": "⚪ TOO NEW",
}

BARS_QUERY = """query ($s: String!, $f: Int!, $t: Int!) { getBars(symbol: $s, from: $f, to: $t,
  resolution: "1D", removeLeadingNullValues: true) { t c volume liquidity } }"""


def _day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date()


def _pct(new, old):
    return round((new / old - 1) * 100, 1) if new is not None and old else None


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
    """Fallback: [(date, price, volume, None)] from CoinGecko (60 days, daily)."""
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
def compute(bars, today, liq_override=None, holders=None, liq_usd=None):
    """The section's numbers from daily bars. liq_override = (pct, label) to
    use instead of the bars' liquidity; holders = [(date, count)] or None."""
    prices = [(d, c) for d, c, _, _ in bars][-PRICE_DAYS:]
    days = len(prices)
    res = {"history_days": days, "too_new": days < MIN_HISTORY_DAYS,
           "chart": [{"date": d.isoformat(), "price": c} for d, c in prices]}
    if prices:
        closes = [c for _, c in prices]
        lo, hi, cur = min(closes), max(closes), closes[-1]
        res.update(price=cur, low=lo, high=hi, above_low_pct=_pct(cur, lo), below_high_pct=_pct(cur, hi),
                   position_pct=round((cur - lo) / (hi - lo) * 100, 1) if hi > lo else 50.0)

    # Volume: complete days only, last 20 -> 4 blocks of 5 -> 3 group changes.
    vols = [v for d, _, v, _ in bars if d < today and v is not None][-(4 * VOL_GROUP_DAYS):]
    blocks = [vols[i:i + VOL_GROUP_DAYS] for i in range(len(vols) % VOL_GROUP_DAYS, len(vols), VOL_GROUP_DAYS)]
    avgs = [sum(b) / len(b) for b in blocks if len(b) == VOL_GROUP_DAYS]
    changes = [_pct(avgs[i], avgs[i - 1]) for i in range(1, len(avgs))]
    res["volume_groups"] = ([None] * (3 - len(changes)) + changes)[-3:]   # oldest, middle, latest

    # Liquidity over WINDOW_DAYS.
    if liq_override is not None:
        res["liquidity_pct"], res["liquidity_source"] = liq_override
    else:
        liq = [(d, l) for d, _, _, l in bars if l]
        base = next((l for d, l in liq if d >= today - timedelta(days=WINDOW_DAYS)), None)
        res["liquidity_pct"] = _pct(liq[-1][1], base) if liq and base else None
        res["liquidity_source"] = "Codex"

    # Holders over WINDOW_DAYS (our snapshots; may cover fewer days).
    h = [(d, n) for d, n in (holders or []) if n and d >= today - timedelta(days=WINDOW_DAYS)]
    res["holders_pct"] = _pct(h[-1][1], h[0][1]) if len(h) >= 2 else None
    res["holders_since"] = h[0][0].isoformat() if len(h) >= 2 else None
    res["liquidity_usd"] = liq_usd                  # real liquidity now (None = unknown)
    res["zone"] = classify(res)
    res["zone_label"] = ZONES[res["zone"]]
    res["line"] = sentence(res)
    return res


def classify(r):
    """The 6 zones, in order (see the module docstring)."""
    if r.get("too_new") or r.get("position_pct") is None:
        return "TOO_NEW"
    pos, liq, hold = r["position_pct"], r.get("liquidity_pct"), r.get("holders_pct")
    groups = [g for g in r.get("volume_groups") or [] if g is not None]
    latest = groups[-1] if groups else None
    liq_fell = liq is not None and liq < -LIQ_DROP_PCT
    hold_fell = hold is not None and hold < -HOLDERS_DROP_PCT
    ok = not liq_fell and not hold_fell              # "not down" (flat is fine, unknown passes)

    thin = r.get("liquidity_usd") is not None and r["liquidity_usd"] < MIN_LIQUIDITY_USD
    if thin or liq_fell or hold_fell:
        return "RISK"
    if pos >= PRICE_HIGH_PCT:
        return "WAIT"
    if (latest is not None and latest < -VOL_FLAT_PCT) or (len(groups) == 3 and all(g < 0 for g in groups)) or hold_fell:
        return "FALLING"
    if latest is None:
        return "NEUTRAL"
    if pos <= PRICE_LOW_PCT and latest > VOL_FLAT_PCT and ok:
        return "BEST"
    if pos <= PRICE_LOW_PCT and abs(latest) <= VOL_FLAT_PCT and ok:
        return "GOOD"
    if PRICE_LOW_PCT < pos < PRICE_HIGH_PCT and latest > VOL_FLAT_PCT and ok:
        return "ACCUMULATE"
    return "NEUTRAL"


def sentence(r):
    """One Arabic sentence (numbers / % stay left-to-right)."""
    from relative_strength import _ltr

    def p(v):
        return _ltr(f"{v:+.0f}%") if v is not None else "—"
    if r["zone"] == "TOO_NEW":
        return f"العملة جديدة جداً ({_ltr(r['history_days'])} يوماً فقط من البيانات) — لا يوجد سياق طويل المدى بعد."
    n = _ltr(r["history_days"])
    pos = _ltr(f"{int(r['position_pct'] + 0.5)}%")          # round half up, like the page
    groups = [g for g in r["volume_groups"] if g is not None]
    vol = p(groups[-1]) if groups else "—"
    tail = " وصف للبيانات وليس نصيحة مالية."
    z = r["zone"]
    if z == "RISK":
        bits = []
        if r.get("liquidity_usd") is not None and r["liquidity_usd"] < MIN_LIQUIDITY_USD:
            have = _ltr("$" + format(r["liquidity_usd"], ",.0f"))
            need = _ltr("$" + format(MIN_LIQUIDITY_USD, ","))
            return (f"إشارة خطر: السيولة الفعلية {have} فقط، أقل من {need}"
                    " — يصعب التداول عليها مهما كانت باقي الإشارات." + tail)
        if r.get("liquidity_pct") is not None and r["liquidity_pct"] < -LIQ_DROP_PCT:
            bits.append(f"السيولة انخفضت {p(r['liquidity_pct'])}")
        if r.get("holders_pct") is not None and r["holders_pct"] < -HOLDERS_DROP_PCT:
            bits.append(f"عدد الحاملين انخفض {p(r['holders_pct'])}")
        return f"إشارة خطر: {' و'.join(bits)} خلال آخر {_ltr(WINDOW_DAYS)} يوماً — الأفضل الانتظار حتى تستقر." + tail
    if z == "WAIT":
        return (f"السعر قرب أعلى مستوى في آخر {n} يوماً ({pos} من النطاق، {p(r['above_low_pct'])} فوق القاع)"
                f" — دخول متأخر وعالي المخاطرة، الأفضل الانتظار." + tail)
    if z == "FALLING":
        return f"حجم التداول يتراجع ({vol} في آخر 5 أيام مقارنة بما قبلها) — الزخم يضعف." + tail
    if z == "BEST":
        strong = groups and groups[-1] >= VOL_STRONG_PCT
        return (f"السعر في الجزء المنخفض من نطاق {n} يوماً ({pos}) مع ارتفاع {'قوي ' if strong else ''}في حجم التداول ({vol})"
                f"، والسيولة والحاملون غير متراجعين — أفضل منطقة دخول حسب البيانات." + tail)
    if z == "GOOD":
        return (f"السعر منخفض في نطاق {n} يوماً ({pos}) وحجم التداول هادئ ({vol})"
                f"، والسيولة والحاملون غير متراجعين — منطقة دخول جيدة." + tail)
    if z == "ACCUMULATE":
        return (f"السعر في منتصف نطاق {n} يوماً ({pos}) وحجم التداول يرتفع ({vol})"
                f"، والسيولة والحاملون غير متراجعين — منطقة تجميع." + tail)
    return f"لا توجد منطقة واضحة الآن: السعر عند {pos} من نطاق {n} يوماً وحجم التداول {vol}." + tail


# ---------------------------------------------------------------- entry points
def build(contract, chain, today=None, dex_liq=None, holders=None, liq_history=None):
    """Fetch + compute. dex_liq = current DexScreener liquidity (for the
    source-mismatch rule); liq_history = [(date, DexScreener liquidity)] used
    if Codex is unreliable for this coin."""
    from liquidity_check import codex_unreliable
    today = today or datetime.now(timezone.utc).date()
    bars, source = codex_daily(contract, chain), "Codex"
    if len(bars) < MIN_HISTORY_DAYS:
        cg = coingecko_daily(contract, chain)
        if len(cg) > len(bars):
            bars, source = cg, "CoinGecko"
    if not bars:
        return {"status": "unknown", "reason": "No daily price history on Codex or CoinGecko"}
    override = None
    codex_liq = next((l for _, _, _, l in reversed(bars) if l), None)
    if source == "Codex" and codex_unreliable(codex_liq, dex_liq):
        h = [(d, l) for d, l in (liq_history or []) if l and d >= today - timedelta(days=WINDOW_DAYS)]
        override = (_pct(h[-1][1], h[0][1]) if len(h) >= 2 else None, "DexScreener · Codex unreliable")
    elif source == "CoinGecko":
        override = (None, None)
    from liquidity_check import best_liquidity
    liq_usd = best_liquidity(codex_liq if source == "Codex" else None, dex_liq)[0]
    res = compute(bars, today, override, holders, liq_usd)
    res.update(status="ok", source=source)
    return res


def get_entry_zone(session, coin):
    """Detail page: build() with our holders + DexScreener history, cached
    per coin per UTC day (entry_zone_cache). Never raises on a missing cache table."""
    from models import DailySnapshot, EntryZoneCache
    today = datetime.now(timezone.utc).date()
    key = ((coin.chain or "").lower(), (coin.contract or "").lower())
    row = None
    try:
        row = session.query(EntryZoneCache).filter_by(chain=key[0], contract=key[1]).first()
        if row is not None and row.day == today:
            return json.loads(row.data)
    except Exception:
        session.rollback()
        row = False                              # table missing: don't try to save
    snaps = (session.query(DailySnapshot.date, DailySnapshot.holders, DailySnapshot.liquidity)
             .filter(DailySnapshot.coin_id == coin.id).order_by(DailySnapshot.date).all())
    res = build(coin.contract, coin.chain, today,
                dex_liq=snaps[-1][2] if snaps else None,
                holders=[(d, h) for d, h, _ in snaps],
                liq_history=[(d, l) for d, _, l in snaps])
    if res.get("status") == "ok" and row is not False:
        try:
            if row is None:
                row = EntryZoneCache(chain=key[0], contract=key[1])
                session.add(row)
            row.day, row.data = today, json.dumps(res)
            session.commit()
        except Exception:
            session.rollback()
    return res
