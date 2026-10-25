"""
scoring.py
----------
The SCORING system (V3, calibrated): ranks the active coins 0-100 once per
daily run, stores each coin's score on its latest snapshot and its parts in
score_history. The API and the website READ these stored scores (no
recomputing per request), so every page shows the same numbers.

THE SCORE (max 100) - 9 parts in 6 components. Tiers come from the real
distribution of our coins (Oct 2026: median all-exchange turnover ~0.3%/day,
median 7-day holder growth ~0.03%, median 7-day liquidity change ~-1.5%):
  Turnover & absorption (30)
    turnover    20  all-exchange 24h volume / market cap (daily_snapshots.
                    total_volume: CoinGecko, CMC fallback)
                    >=20% 20 | >=8% 16 | >=3% 12 | >=1% 8 | >=0.3% 4 | else 0
    absorption  10  24h volume vs the previous 7 days' average while the 24h
                    price (Codex) stays between -5% and +10% and today's
                    turnover is at least 0.3%: >= +80% 10 | +40-80% 5 | else 0
  Buyers (25)
    buyers      25  15-day Codex buy / sell volume (trade_flows), ratio capped
                    at 1.5x, only with >= $20K total 15-day volume:
                    >=1.15 25 | >=1.08 18 | >=1.03 13 | >=1.00 7 | else 0;
                    halved when buy transactions <= sell transactions
  Holders (20)
    holders     15  7-day holder growth: >=2% 15 | >=1% 11 | >=0.3% 6 |
                    >=0% 3 | falling 0
    holder_vel   5  today's holder growth vs the 7-day daily average:
                    >= +0.3% and > 2x 5 | >= +0.15% and > 1.5x 3 | else 0
  Liquidity & structure (15)
    liquidity   10  7-day liquidity growth (start >= $10K, capped at +100%):
                    >=15% 10 | >=5% 7 | >=1% 4 | >=0% 2 | falling 0
    structure    5  higher low over 3 days, or 7-day price change 0..+15%,
                    while under 70% of the 60-day range -> 5
  Rank (5)       rank_change  weekly CG + CMC rank: both up 5 | one up 3 |
                    mixed 1 | down 0
  Cheapness (5)  cheapness    60-day range position: <=20% 5 | <=40% 3 |
                    <=60% 1 | else 0
ANTI-CHASING: 24h price up > 15% -> absorption and holder velocity halved;
> 25% -> zero. MISSING DATA (or failing the $20K / $10K gates) = the median
of that part across the active coins scored in the same run - never 0, never
half. SCORE CHANGE (Active table arrow, detail page) = the change of the
parts that had real data on BOTH days, so data appearing / disappearing
never fakes a jump.

Run it:
    python scoring.py
"""

import json
from datetime import date as _date, timedelta
from statistics import median

from database import SessionLocal, init_db
from models import Coin, DailySnapshot
from fetch_dexscreener import fmt_money

# (score_history column, label, component, max points)
PARTS = [("turnover", "Turnover", "Turnover & absorption", 20),
         ("absorption", "Quiet absorption", "Turnover & absorption", 10),
         ("buyers", "Buyers (15d)", "Buyers", 25),
         ("holders", "Holder growth (7d)", "Holders", 15),
         ("holder_vel", "Holder velocity", "Holders", 5),
         ("liquidity", "Liquidity growth (7d)", "Liquidity & structure", 10),
         ("structure", "Price structure", "Liquidity & structure", 5),
         ("rank_change", "Rank change", "Rank", 5),
         ("cheapness", "Price cheapness", "Cheapness", 5)]
COMPONENTS = ["Turnover & absorption", "Buyers", "Holders", "Liquidity & structure", "Rank", "Cheapness"]
MAX_TOTAL = sum(p[3] for p in PARTS)                        # = 100

BUYERS_DAYS, BUYERS_MIN_DAYS, BUYERS_MIN_USD, BUYERS_CAP = 15, 3, 20_000, 1.5
LIQ_GROWTH_DAYS, LIQ_GROWTH_SLACK, LIQ_MIN_BASE, LIQ_CAP = 7, 2, 10_000, 100
ABSORB_PRICE_BAND = (-5, 10)
ABSORB_MIN_TURNOVER = 0.3                                   # % of market cap traded today, else no absorption
CHASE_HALF, CHASE_ZERO = 15, 25                             # 24h price up % -> 24h parts halved / zeroed


def _tier(v, cuts):
    for t, pts in cuts:
        if v >= t:
            return pts
    return 0


# ---------------------------------------------------------------------------
# Points per part (None in -> None out = missing data)
# ---------------------------------------------------------------------------
def pts_turnover(pct):
    return None if pct is None else _tier(pct, [(20, 20), (8, 16), (3, 12), (1, 8), (0.3, 4)])


def pts_absorption(vol_chg, chg24, turnover):
    if vol_chg is None or chg24 is None:
        return None
    # A % jump on near-zero volume is noise (Neutrino: +317% at 0.001% turnover).
    if (turnover or 0) < ABSORB_MIN_TURNOVER or not ABSORB_PRICE_BAND[0] <= chg24 <= ABSORB_PRICE_BAND[1]:
        return 0
    return 10 if vol_chg >= 80 else 5 if vol_chg >= 40 else 0


def pts_buyers(buy, sell, buys, sells):
    """15-day totals -> (points, ratio); (None, None) under the $20K gate."""
    if buy is None or sell is None or buy + sell < BUYERS_MIN_USD or not sell:
        return None, None
    ratio = min(buy / sell, BUYERS_CAP)
    pts = _tier(ratio, [(1.15, 25), (1.08, 18), (1.03, 13), (1.0, 7)])
    return (pts if (buys or 0) > (sells or 0) else pts / 2), ratio


def pts_holders(h7):
    return None if h7 is None else (0 if h7 < 0 else _tier(h7, [(2, 15), (1, 11), (0.3, 6), (0, 3)]))


def pts_holder_vel(h1, h7):
    if h1 is None or h7 is None:
        return None
    avg = max(h7 / 7, 0)
    return 5 if h1 >= 0.3 and h1 > 2 * avg else 3 if h1 >= 0.15 and h1 > 1.5 * avg else 0


def pts_liquidity(pct, base):
    if pct is None or base is None or base < LIQ_MIN_BASE:
        return None
    pct = min(pct, LIQ_CAP)
    return 0 if pct < 0 else _tier(pct, [(15, 10), (5, 7), (1, 4), (0, 2)])


def price_position(closes):
    """Latest close's position in the last 60 closes, % (0 = low, 100 =
    high), or None with fewer than 15 - the Entry Zone's own rule."""
    from entry_zone import PRICE_DAYS, MIN_HISTORY_DAYS
    closes = [c for c in closes if c][-PRICE_DAYS:]
    if len(closes) < MIN_HISTORY_DAYS:
        return None
    lo, hi, cur = min(closes), max(closes), closes[-1]
    return (cur - lo) / (hi - lo) * 100 if hi > lo else 50.0


def pts_structure(closes):
    closes = [c for c in closes if c][-60:]
    pos = price_position(closes)
    if pos is None or len(closes) < 8:
        return None
    higher_low = min(closes[-3:]) > min(closes[-6:-3])
    m7 = (closes[-1] / closes[-8] - 1) * 100
    return 5 if (higher_low or 0 <= m7 <= 15) and pos < 70 else 0


def pts_cheapness(pos):
    return None if pos is None else (5 if pos <= 20 else 3 if pos <= 40 else 1 if pos <= 60 else 0)


def pts_rank(signal):
    return {"strong_up": 5, "up": 3, "neutral": 1, "down": 0}.get(signal)


def anti_chase(pts, chg24):
    """The 24h parts halved / zeroed after a big 24h price rise."""
    if chg24 is None or chg24 <= CHASE_HALF:
        return pts
    keep = 0.5 if chg24 <= CHASE_ZERO else 0
    return {k: (v * keep if k in ("absorption", "holder_vel") and v is not None else v) for k, v in pts.items()}


def change_over(series):
    """(% change, base) of the latest reading vs the one LIQ_GROWTH_DAYS
    earlier (the latest reading within LIQ_GROWTH_SLACK days before that),
    or (None, None). series = [(date, value)] oldest first."""
    vals = [(d, v) for d, v in series if v]
    if not vals:
        return None, None
    last_day, now = vals[-1]
    target = last_day - timedelta(days=LIQ_GROWTH_DAYS)
    base = [v for d, v in vals if target - timedelta(days=LIQ_GROWTH_SLACK) <= d <= target]
    return ((now / base[-1] - 1) * 100, base[-1]) if base else (None, None)


# ---------------------------------------------------------------------------
# Today's raw points for every active coin (stored data only, no API calls)
# ---------------------------------------------------------------------------
def coin_points(session, coins):
    """{coin.id: {"pts": {part: points or None}, "readings": {...}}} from
    stored data: daily_snapshots (volume, market cap, holders, liquidity),
    entry_zone_bars (Codex daily closes), trade_flows (Codex buy / sell)."""
    from models import EntryZoneBars, TradeFlow
    from liquidity_check import codex_unreliable, bulk_codex_too_low
    if not coins:
        return {}
    keys = {((c.chain or "").lower(), (c.contract or "").lower()): c.id for c in coins}
    bars = {}
    for row in session.query(EntryZoneBars).filter(EntryZoneBars.contract.in_([k[1] for k in keys])):
        cid = keys.get((row.chain, row.contract))
        if cid is not None:
            bars[cid] = [(_date.fromisoformat(b[0]), b[1], b[3]) for b in json.loads(row.data)["bars"]]
    since = _date.today() - timedelta(days=BUYERS_DAYS + 3)
    snaps = {}
    for s in (session.query(DailySnapshot.coin_id, DailySnapshot.date, DailySnapshot.total_volume,
                            DailySnapshot.mcap, DailySnapshot.holders, DailySnapshot.codex_liquidity,
                            DailySnapshot.liquidity)
              .filter(DailySnapshot.coin_id.in_(list(keys.values())), DailySnapshot.date >= since)
              .order_by(DailySnapshot.date)):
        snaps.setdefault(s.coin_id, []).append(s)
    flows = {}
    for r in (session.query(TradeFlow).filter(TradeFlow.contract.in_([k[1] for k in keys]), TradeFlow.day >= since)
              .order_by(TradeFlow.day)):
        cid = keys.get((r.chain, r.contract))
        if cid is not None:
            flows.setdefault(cid, []).append(r)
    signal = {c.id: c.rank_signal for c in coins}

    out = {}
    for cid in keys.values():
        rows = snaps.get(cid, [])
        last = rows[-1] if rows else None
        closes = [c for _, c, _ in bars.get(cid, []) if c]
        chg24 = (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 else None
        rd = {"chg24": chg24}
        pts = {}
        # turnover + absorption
        turnover = (last.total_volume / last.mcap * 100) if last and last.total_volume and last.mcap else None
        prev = [r.total_volume for r in rows[:-1] if r.total_volume and last and
                last.date - timedelta(days=7) <= r.date < last.date]
        vol_chg = (last.total_volume / (sum(prev) / len(prev)) - 1) * 100 \
            if last and last.total_volume and len(prev) >= 5 else None
        pts["turnover"], pts["absorption"] = pts_turnover(turnover), pts_absorption(vol_chg, chg24, turnover)
        rd.update(turnover=turnover, vol_chg=vol_chg)
        # buyers (fix 4: not when Codex is unreliable for the coin today)
        fl = [r for r in flows.get(cid, []) if r.buy_volume is not None and r.sell_volume is not None][-BUYERS_DAYS:]
        bad = last is not None and codex_unreliable(last.codex_liquidity, last.liquidity)
        if len(fl) >= BUYERS_MIN_DAYS and not bad:
            pts["buyers"], rd["buy_sell"] = pts_buyers(sum(r.buy_volume for r in fl), sum(r.sell_volume for r in fl),
                                                       sum(r.buy_count or 0 for r in fl), sum(r.sell_count or 0 for r in fl))
        else:
            pts["buyers"], rd["buy_sell"] = None, None
        # holders
        h = [(r.date, r.holders) for r in rows if r.holders]
        h7 = h1 = None
        if h:
            d0, v0 = h[-1]
            base = [v for d, v in h if d0 - timedelta(days=9) <= d <= d0 - timedelta(days=7)]
            yday = [v for d, v in h if d == d0 - timedelta(days=1)]
            h7 = (v0 / base[-1] - 1) * 100 if base else None
            h1 = (v0 / yday[0] - 1) * 100 if yday else None
        pts["holders"], pts["holder_vel"] = pts_holders(h7), pts_holder_vel(h1, h7)
        rd.update(h7=h7, h1=h1)
        # liquidity: Codex daily readings; Codex unreliable today or ~7 days
        # ago (fix 4, or a partial bulk reading) -> DexScreener readings;
        # no 7 days of Codex readings yet -> Codex's own daily history.
        recent = rows[-1:] + [r for r in rows if rows and rows[-1].date - timedelta(days=9) <= r.date
                              <= rows[-1].date - timedelta(days=7)]
        if any(codex_unreliable(r.codex_liquidity, r.liquidity) or bulk_codex_too_low(r.codex_liquidity, r.liquidity)
               for r in recent):
            pct, base = change_over([(r.date, r.liquidity) for r in rows])
        else:
            pct, base = change_over([(r.date, r.codex_liquidity) for r in rows])
            if pct is None:
                appended = {r.date: r.codex_liquidity for r in rows if r.codex_liquidity is not None}
                pct, base = change_over([(d, v) for d, _, v in bars.get(cid, []) if v and appended.get(d) != v])
        pts["liquidity"] = pts_liquidity(pct, base)
        rd["liq_growth"] = None if pct is None else min(pct, LIQ_CAP)
        # structure, cheapness, rank
        pos = price_position(closes)
        pts["structure"], pts["cheapness"], pts["rank_change"] = pts_structure(closes), pts_cheapness(pos), pts_rank(signal[cid])
        rd["price_pos"] = pos
        out[cid] = {"pts": anti_chase(pts, chg24), "readings": rd}
    return out


def part_medians(points):
    """{part: median of the real points across the coins} (0 when none has data)."""
    out = {}
    for col, *_ in PARTS:
        vals = [p["pts"][col] for p in points if p["pts"][col] is not None]
        out[col] = median(vals) if vals else 0
    return out


def fill(pts, medians):
    """(filled points, missing parts) - missing parts take the median."""
    missing = [c for c, *_ in PARTS if pts[c] is None]
    return {c: (medians[c] if pts[c] is None else pts[c]) for c, *_ in PARTS}, missing


def display_ratios(snap):
    """(DEX volume / liquidity in x, DEX volume / market cap in %) - display
    only (narrative, the "hot" badge, Compare); not part of the score."""
    if snap is None or snap.volume is None:
        return None, None
    vl = snap.volume / snap.liquidity if snap.liquidity else None
    vm = snap.volume / snap.mcap * 100 if snap.mcap else None
    return vl, vm


# ---------------------------------------------------------------------------
# The daily scoring step
# ---------------------------------------------------------------------------
def score_all():
    """Score every ACTIVE coin's latest snapshot: snapshots.score = total.
    Returns [{coin_id, name, chain, mcap, score, parts (filled), missing,
    readings}], highest first (record_scores stores the parts)."""
    session = SessionLocal()
    results = []
    try:
        coins = session.query(Coin).filter_by(status="active").all()
        points = coin_points(session, coins)
        medians = part_medians(list(points.values()))
        for coin in coins:
            snap = (session.query(DailySnapshot).filter(DailySnapshot.coin_id == coin.id)
                    .order_by(DailySnapshot.date.desc(), DailySnapshot.id.desc()).first())
            if snap is None:
                continue
            filled, missing = fill(points[coin.id]["pts"], medians)
            snap.score = round(sum(filled.values()), 1)
            results.append({"coin_id": coin.id, "name": coin.name, "chain": coin.chain, "mcap": snap.mcap,
                            "score": snap.score, "parts": filled, "missing": missing,
                            "readings": points[coin.id]["readings"]})
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Score history: one row per active coin per day (score_history) - the
# Active table's change arrow and the detail page's "All Score" table.
# ---------------------------------------------------------------------------
SCORE_HISTORY_KEEP_DAYS = 30      # rows older than this are deleted (Supabase free-tier egress)
SCORE_HISTORY_SHOW_DAYS = 7       # the detail page's table


def record_scores(results, day=None):
    """Save today's score + its 9 parts (median-filled) + which parts had no
    data (missing, comma-separated; "" = none) for every scored coin. A
    re-run the same day updates the row. Then delete rows older than
    SCORE_HISTORY_KEEP_DAYS. Returns rows written."""
    from models import ScoreHistory
    day = day or _date.today()           # the same day the snapshots use
    rows = [r for r in results if r.get("score") is not None]
    session = SessionLocal()
    try:
        have = {h.coin_id: h for h in session.query(ScoreHistory).filter(ScoreHistory.recorded_at == day)}
        for r in rows:
            h = have.get(r["coin_id"])
            if h is None:
                h = ScoreHistory(coin_id=r["coin_id"], recorded_at=day)
                session.add(h)
            h.score = float(r["score"])
            for col, *_ in PARTS:
                setattr(h, col, float(r["parts"][col]))
            h.missing = ",".join(r["missing"])
        session.query(ScoreHistory).filter(
            ScoreHistory.recorded_at < day - timedelta(days=SCORE_HISTORY_KEEP_DAYS)
        ).delete(synchronize_session=False)
        session.commit()
        return len(rows)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _shared_change(now, prev):
    """{part: change} over the parts with real data on BOTH days; None when
    either row is from before V3 (missing IS NULL - different parts)."""
    if now is None or prev is None or now.missing is None or prev.missing is None:
        return None
    gone = set(filter(None, now.missing.split(","))) | set(filter(None, prev.missing.split(",")))
    return {c: getattr(now, c) - getattr(prev, c) for c, *_ in PARTS if c not in gone}


def score_changes(session, coin_ids):
    """{coin_id: (score the previous recorded day, change)} - change = the
    sum of the parts with real data on both days (None before two V3 days).
    Never raises."""
    from models import ScoreHistory
    if not coin_ids:
        return {}
    try:
        rows = {}
        since = _date.today() - timedelta(days=SCORE_HISTORY_KEEP_DAYS)
        for h in (session.query(ScoreHistory).filter(ScoreHistory.coin_id.in_(list(coin_ids)),
                                                     ScoreHistory.recorded_at >= since)
                  .order_by(ScoreHistory.recorded_at.desc())):
            if len(rows.setdefault(h.coin_id, [])) < 2:
                rows[h.coin_id].append(h)
        out = {}
        for cid, hs in rows.items():
            if len(hs) == 2:
                ch = _shared_change(hs[0], hs[1])
                out[cid] = (hs[1].score, None if ch is None else round(sum(ch.values()), 1))
        return out
    except Exception:
        session.rollback()
        return {}


def score_history(session, coin_id, days=SCORE_HISTORY_SHOW_DAYS):
    """The coin's last `days` recorded scores, newest first:
    [{"date", "score", "parts": {component: points} or None (before V3),
      "first": True when there is no earlier recorded day,
      "changes": [{"label", "delta"}] per component vs the previous day,
                 real-data parts only, biggest first; None when not comparable,
      "delta": the day's score change over those parts (None when not comparable)}].
    Never raises (-> [])."""
    from models import ScoreHistory
    try:
        rows = (session.query(ScoreHistory).filter(ScoreHistory.coin_id == coin_id)
                .order_by(ScoreHistory.recorded_at.desc()).limit(days + 1).all())
    except Exception:
        session.rollback()
        return []

    def components(vals):
        out = dict.fromkeys(COMPONENTS, 0.0)
        for col, _, comp, _ in PARTS:
            out[comp] += vals.get(col, 0)
        return {k: round(v, 1) for k, v in out.items()}

    out = []
    for i, row in enumerate(rows[:days]):
        prev = rows[i + 1] if i + 1 < len(rows) else None
        parts = components({c: getattr(row, c) for c, *_ in PARTS}) if row.missing is not None else None
        ch = _shared_change(row, prev)
        changes = None
        if ch is not None:
            comp = components(ch)
            changes = sorted(({"label": k, "delta": v} for k, v in comp.items() if abs(v) >= 0.05),
                             key=lambda c: -abs(c["delta"]))
        out.append({"date": row.recorded_at.isoformat(), "score": round(row.score, 1), "parts": parts,
                    "first": prev is None, "changes": changes,
                    "delta": None if ch is None else round(sum(ch.values()), 1),
                    "estimated": sorted({comp for col, _, comp, _ in PARTS                # median-filled
                                         if col in (row.missing or "").split(",")})})
    return out


def latest_medians(session):
    """{part: median real points} from the latest recorded day - the
    fallback for parts Check a Coin can't compute live. {} if none."""
    from sqlalchemy import func
    from models import ScoreHistory
    try:
        day = session.query(func.max(ScoreHistory.recorded_at)).filter(ScoreHistory.missing.isnot(None)).scalar()
        if day is None:
            return {}
        rows = session.query(ScoreHistory).filter(ScoreHistory.recorded_at == day).all()
    except Exception:
        session.rollback()
        return {}
    out = {}
    for col, *_ in PARTS:
        vals = [getattr(r, col) for r in rows if col not in (r.missing or "").split(",")]
        out[col] = median(vals) if vals else 0
    return out


def print_ranking(results):
    if not results:
        print("No scored coins to show.")
        return
    print("=" * 96)
    print(f"  CRYPTO SCREENER - RANKED BY SCORE (V3, max {MAX_TOTAL})")
    print("=" * 96)
    for rank, r in enumerate(results, start=1):
        p = r["parts"]
        print(f"{rank:>3}  {(r['name'] or '')[:20]:<20} {r['chain']:<9} {fmt_money(r['mcap']):>9}  "
              f"T{p['turnover']:g}+A{p['absorption']:g} B{p['buyers']:g} H{p['holders']:g}+V{p['holder_vel']:g} "
              f"L{p['liquidity']:g}+S{p['structure']:g} R{p['rank_change']:g} C{p['cheapness']:g}  "
              f"= {r['score']:>5.1f}" + (f"  (median: {', '.join(r['missing'])})" if r["missing"] else ""))
    print("=" * 96)


def main():
    init_db()
    print_ranking(score_all())


if __name__ == "__main__":
    main()
