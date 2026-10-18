"""
scoring.py
----------
Phase 2 - the SCORING system. It ranks the active coins 0-100, highest
first, and stores each coin's score back into its latest snapshot.

THE SCORE (max 100):
    Vol/Liq ratio ................ max 25 pts   (24h volume / liquidity)
    Vol/MCap ratio ............... max 15 pts   (24h volume / market cap)
    Liquidity growth (7d) ........ max 15 pts   (today vs 7 days ago)
    Price cheapness (2 months) ... max 20 pts   (position in the 60-day range)
    Buyers volume (15 days) ...... max 20 pts   (buy / sell volume ratio)
    Rank change .................. max  5 pts   (weekly, CoinGecko + CMC)

Every input comes from STORED data - no API calls here:
  * LIQUIDITY GROWTH - the detail page's "Liquidity growth 7d" figure, from
    the Codex daily readings (daily_snapshots) or the Codex history in
    entry_zone_bars; fix 4 -> DexScreener readings (see liquidity_growth()).
  * PRICE CHEAPNESS - the same 60 days of daily closes the Entry Zone uses
    (entry_zone_bars): 0% = at the 60-day low, 100% = at the high.
  * BUYERS VOLUME - Codex's daily 24h buy / sell volume, stored by the
    tracking step from its bulk request (trade_flows), summed over 15 days.
    Not counted when Codex is unreliable for the coin (fix 4).
Unknown (not enough history) = 0 points, like every component.

Run it:
    python scoring.py
"""

from database import SessionLocal, init_db
from models import Coin, DailySnapshot
from fetch_dexscreener import fmt_money

# Point ceilings for each component (documents the design in one place).
MAX_VOL_LIQ = 25
MAX_VOL_MCAP = 15
MAX_LIQ_GROWTH = 15
MAX_CHEAPNESS = 20
MAX_BUYERS = 20
MAX_RANK_CHANGE = 5
MAX_TOTAL = (MAX_VOL_LIQ + MAX_VOL_MCAP + MAX_LIQ_GROWTH + MAX_CHEAPNESS
             + MAX_BUYERS + MAX_RANK_CHANGE)  # = 100

LIQ_GROWTH_DAYS = 7       # liquidity growth: today vs this many days ago
LIQ_GROWTH_SLACK = 2      # "7 days ago" = the latest reading 7-9 days back
BUYERS_DAYS = 15          # buyers volume: the last 15 daily readings ...
BUYERS_MIN_DAYS = 3       # ... at least this many, else unknown


# ---------------------------------------------------------------------------
# Component 1: Volume / Liquidity ratio  (max 25 pts)
#   >5x=25, 3-5x=20, 2-3x=15, 1-2x=10, <1x=5
# ---------------------------------------------------------------------------
def score_vol_liq(volume, liquidity):
    """Return (points, ratio). ratio is None if it can't be computed."""
    if not liquidity or liquidity <= 0 or volume is None:
        return 0, None
    ratio = volume / liquidity
    if ratio > 5:
        pts = 25
    elif ratio >= 3:      # 3x - 5x
        pts = 20
    elif ratio >= 2:      # 2x - 3x
        pts = 15
    elif ratio >= 1:      # 1x - 2x
        pts = 10
    else:                 # < 1x
        pts = 5
    return pts, ratio


# ---------------------------------------------------------------------------
# Component 2: Volume / Market Cap ratio  (max 15 pts)
#   >60%=15, 40-60%=12, 25-40%=9, 10-25%=6, <10%=3
# ---------------------------------------------------------------------------
def score_vol_mcap(volume, mcap):
    """Return (points, percent). percent is None if it can't be computed."""
    if not mcap or mcap <= 0 or volume is None:
        return 0, None
    percent = (volume / mcap) * 100
    if percent > 60:
        pts = 15
    elif percent >= 40:   # 40% - 60%
        pts = 12
    elif percent >= 25:   # 25% - 40%
        pts = 9
    elif percent >= 10:   # 10% - 25%
        pts = 6
    else:                 # < 10%
        pts = 3
    return pts, percent


# ---------------------------------------------------------------------------
# Component 3: Liquidity growth  (max 15 pts)
#   7-day change: >+30%=15, +15-30%=11, +5-15%=7, 0-5%=3, falling=0
# ---------------------------------------------------------------------------
def score_liquidity_growth(pct):
    """Points for a 7-day liquidity change in % (None = unknown -> 0)."""
    if pct is None:
        return 0
    if pct > 30:
        return 15
    if pct >= 15:
        return 11
    if pct >= 5:
        return 7
    if pct >= 0:
        return 3
    return 0


# ---------------------------------------------------------------------------
# Component 4: Price cheapness, 2 months  (max 20 pts)
#   position in the 60-day range: <=20%=20, 20-35%=15, 35-50%=10, 50-70%=5,
#   >=70%=0 (the Entry Zone's BEST / GOOD / ACCUMULATE / WAIT cut-offs, with
#   ACCUMULATE split at 50%)
# ---------------------------------------------------------------------------
def score_cheapness(position_pct):
    """Points for the price's position in its 60-day range (None -> 0)."""
    if position_pct is None:
        return 0
    if position_pct <= 20:
        return 20
    if position_pct <= 35:
        return 15
    if position_pct <= 50:
        return 10
    if position_pct < 70:
        return 5
    return 0


# ---------------------------------------------------------------------------
# Component 5: Buyers volume, 15 days  (max 20 pts)
#   buy volume / sell volume: >1.15=20, 1.08-1.15=15, 1.03-1.08=10,
#   1.00-1.03=5, <1 (more selling)=0. Over 15 days the two stay close: real
#   coins sat between 0.76 and 1.14 (Oct 2026), so a 2x scale would never pay.
# ---------------------------------------------------------------------------
def score_buyers(ratio):
    """Points for the 15-day buy / sell volume ratio (None -> 0)."""
    if ratio is None:
        return 0
    if ratio > 1.15:
        return 20
    if ratio >= 1.08:
        return 15
    if ratio >= 1.03:
        return 10
    if ratio >= 1.0:
        return 5
    return 0


# ---------------------------------------------------------------------------
# Component 6: Rank change  (max 5 pts)
# ---------------------------------------------------------------------------
def score_rank_change(coin):
    # Max 5 pts based on how the coin's market rank moved vs last week
    # (averaged across CoinGecko + CoinMarketCap; see rank_check.py).
    signal = getattr(coin, "rank_signal", None)
    if signal == "strong_up":   # both sources moved up
        return 5
    if signal == "up":          # one source up (the other unknown)
        return 3
    if signal == "neutral":     # sources disagree / flat
        return 1
    return 0                    # "down", or no previous week yet


# ---------------------------------------------------------------------------
# The stored-data inputs (liquidity growth, price position, buy / sell ratio)
# ---------------------------------------------------------------------------
def _change_over(series):
    """% change of the latest reading vs the one LIQ_GROWTH_DAYS earlier (the
    latest reading within LIQ_GROWTH_SLACK days before that), or None.
    series = [(date, value)] oldest first."""
    from datetime import timedelta
    vals = [(d, v) for d, v in series if v]
    if not vals:
        return None
    last_day, now = vals[-1]
    target = last_day - timedelta(days=LIQ_GROWTH_DAYS)
    base = [v for d, v in vals if target - timedelta(days=LIQ_GROWTH_SLACK) <= d <= target]
    return (now / base[-1] - 1) * 100 if base else None


def _price_position(closes):
    """Latest close's position in the last PRICE_DAYS closes, in % (0 = the
    low, 100 = the high), or None with fewer than MIN_HISTORY_DAYS closes -
    the Entry Zone's own rule."""
    from entry_zone import PRICE_DAYS, MIN_HISTORY_DAYS
    closes = [c for c in closes if c][-PRICE_DAYS:]
    if len(closes) < MIN_HISTORY_DAYS:
        return None
    lo, hi, cur = min(closes), max(closes), closes[-1]
    return (cur - lo) / (hi - lo) * 100 if hi > lo else 50.0


def score_inputs(session, coins):
    """{coin.id: {...}} - each coin's stored-data score inputs, read with a
    few queries for any number of coins (no API calls):
      liq_growth / liq_source - 7-day liquidity change (see below)
      price_pos               - position in the 60-day price range, %
      buy_sell / buy_sell_days- 15-day buy / sell volume ratio, readings used
    LIQUIDITY GROWTH compares a reading only with one of the SAME kind -
    Codex's bulk filterTokens figure and its getBars daily bars differ a lot
    for a few coins (e.g. Orca $1.29M vs $537K the same day):
      "Codex daily"   - the daily tracking readings (daily_snapshots.
                        codex_liquidity, filterTokens): today vs 7 days ago.
      "Codex history" - until a coin has 7 days of those: the getBars days in
                        entry_zone_bars (the detail page tile's own series),
                        leaving out the days the tracking step appended.
      "DexScreener"   - Codex unreliable today or ~7 days ago - fix 4 (> 10x
                        DexScreener) or a partial bulk reading (< 1/3 of
                        DexScreener, liquidity_check.bulk_codex_too_low) ->
                        our daily snapshots' DexScreener readings.
    BUYERS is left out (unknown) only for fix 4 (Codex > 10x): a buy / sell
    RATIO stays meaningful on a partial bulk reading.
    Unknown values are None (0 points)."""
    import json
    from datetime import date as _date, timedelta
    from models import EntryZoneBars, TradeFlow
    from liquidity_check import codex_unreliable, bulk_codex_too_low

    def codex_bad(cx, dx):
        # fix 4 (Codex > 10x DexScreener) or a partial bulk reading (< 1/3)
        return codex_unreliable(cx, dx) or bulk_codex_too_low(cx, dx)

    if not coins:
        return {}
    keys = {((c.chain or "").lower(), (c.contract or "").lower()): c.id for c in coins}
    contracts = [k[1] for k in keys]
    bars = {}
    for row in session.query(EntryZoneBars).filter(EntryZoneBars.contract.in_(contracts)):
        cid = keys.get((row.chain, row.contract))
        if cid is not None:
            bars[cid] = [(_date.fromisoformat(b[0]), b[1], b[3]) for b in json.loads(row.data)["bars"]]
    since = _date.today() - timedelta(days=LIQ_GROWTH_DAYS + LIQ_GROWTH_SLACK + 3)
    snaps = {}
    for cid, d, cx, dx in (session.query(DailySnapshot.coin_id, DailySnapshot.date,
                                         DailySnapshot.codex_liquidity, DailySnapshot.liquidity)
                           .filter(DailySnapshot.coin_id.in_(list(keys.values())),
                                   DailySnapshot.date >= since)
                           .order_by(DailySnapshot.date)):
        snaps.setdefault(cid, []).append((d, cx, dx))
    flows = {}
    try:      # trade_flows is created by the daily job; before that: unknown
        for r in (session.query(TradeFlow)
                  .filter(TradeFlow.contract.in_(contracts),
                          TradeFlow.day >= _date.today() - timedelta(days=BUYERS_DAYS + 3))
                  .order_by(TradeFlow.day)):
            cid = keys.get((r.chain, r.contract))
            if cid is not None:
                flows.setdefault(cid, []).append((r.day, r.buy_volume, r.sell_volume))
    except Exception:
        session.rollback()

    out = {}
    for cid in keys.values():
        rows = snaps.get(cid, [])
        res = {"liq_growth": None, "liq_source": None, "price_pos": None,
               "buy_sell": None, "buy_sell_days": 0}
        # --- liquidity growth (fix 4: Codex unreliable today or ~7 days ago)
        recent = rows[-1:] + [r for r in rows if rows and
                              rows[-1][0] - timedelta(days=LIQ_GROWTH_DAYS + LIQ_GROWTH_SLACK)
                              <= r[0] <= rows[-1][0] - timedelta(days=LIQ_GROWTH_DAYS)]
        unreliable = any(codex_bad(cx, dx) for _, cx, dx in recent)
        if unreliable:
            res["liq_growth"], res["liq_source"] = _change_over([(d, dx) for d, _, dx in rows]), "DexScreener"
        else:
            pct = _change_over([(d, cx) for d, cx, _ in rows])
            if pct is not None:
                res["liq_growth"], res["liq_source"] = pct, "Codex daily"
            else:
                # The tracking step appends today's codex_liquidity to
                # entry_zone_bars as-is, so a stored day equal to that day's
                # snapshot reading is an appended filterTokens day - leave
                # those out, keep the getBars days.
                appended = {d: cx for d, cx, _ in rows if cx is not None}
                hist = [(d, v) for d, _, v in bars.get(cid, []) if v and appended.get(d) != v]
                pct = _change_over(hist)
                if pct is not None:
                    res["liq_growth"], res["liq_source"] = pct, "Codex history"
        # --- price position in the 60-day range
        res["price_pos"] = _price_position([c for _, c, _ in bars.get(cid, [])])
        # --- buy / sell volume, last BUYERS_DAYS readings (not when Codex is
        #     unreliable for the coin: its trades may be the phantom pool's)
        fl = [(b, s) for _, b, s in flows.get(cid, []) if b is not None and s is not None][-BUYERS_DAYS:]
        latest_unreliable = bool(rows) and codex_unreliable(rows[-1][1], rows[-1][2])
        if len(fl) >= BUYERS_MIN_DAYS and not latest_unreliable:
            buy, sell = sum(b for b, _ in fl), sum(s for _, s in fl)
            res["buy_sell"], res["buy_sell_days"] = (buy / sell if sell else None), len(fl)
        out[cid] = res
    return out


INPUT_FIELDS = ("price_pos", "liq_growth", "liq_source", "buy_sell", "buy_sell_days")


def save_score_inputs(session, inputs, days):
    """Store score_inputs() results (score_inputs_cache), one row per coin,
    tagged with the snapshot day they were computed for. Never raises."""
    from models import ScoreInputsCache
    from database import utcnow
    try:
        have = {r.coin_id: r for r in session.query(ScoreInputsCache).filter(ScoreInputsCache.coin_id.in_(list(inputs)))}
        now = utcnow()
        for cid, res in inputs.items():
            if days.get(cid) is None:
                continue
            row = have.get(cid)
            if row is None:
                row = ScoreInputsCache(coin_id=cid)
                session.add(row)
            row.day, row.updated_at = days[cid], now
            for k in INPUT_FIELDS:
                setattr(row, k, res.get(k))
        session.commit()
    except Exception:
        session.rollback()


def stored_score_inputs(session, coins, days):
    """score_inputs() for the coins, read from score_inputs_cache where the
    row matches the coin's latest snapshot day (days = {coin_id: date});
    the rest (no row yet, or a newer snapshot) are computed as before."""
    from models import ScoreInputsCache
    out = {}
    try:
        for r in (session.query(ScoreInputsCache.coin_id, ScoreInputsCache.day, ScoreInputsCache.price_pos,
                                ScoreInputsCache.liq_growth, ScoreInputsCache.liq_source,
                                ScoreInputsCache.buy_sell, ScoreInputsCache.buy_sell_days)
                  .filter(ScoreInputsCache.coin_id.in_([c.id for c in coins]))):
            if days.get(r.coin_id) is not None and r.day == days[r.coin_id]:
                out[r.coin_id] = {k: getattr(r, k) for k in INPUT_FIELDS}
                out[r.coin_id]["buy_sell_days"] = out[r.coin_id]["buy_sell_days"] or 0
    except Exception:
        session.rollback()
    missing = [c for c in coins if c.id not in out]
    if missing:
        out.update(score_inputs(session, missing))
    return out


def liquidity_growth(session, coins):
    """{coin.id: (pct or None, source)} - the 7-day liquidity change alone."""
    return {cid: (r["liq_growth"], r["liq_source"]) for cid, r in score_inputs(session, coins).items()}


# ---------------------------------------------------------------------------
# Combine everything into one total score for a snapshot.
# ---------------------------------------------------------------------------
def score_snapshot(snapshot, coin, inputs=None):
    """
    Compute the full breakdown + total (0-100) for one snapshot.
    inputs = the coin's entry from score_inputs() (None -> those three
    components count as unknown). Returns a dict to save and display.
    """
    inputs = inputs or {}
    vol_liq_pts, vol_liq_ratio = score_vol_liq(snapshot.volume, snapshot.liquidity)
    vol_mcap_pts, vol_mcap_pct = score_vol_mcap(snapshot.volume, snapshot.mcap)
    liq_growth_pts = score_liquidity_growth(inputs.get("liq_growth"))
    cheap_pts = score_cheapness(inputs.get("price_pos"))
    buyers_pts = score_buyers(inputs.get("buy_sell"))
    rank_change_pts = score_rank_change(coin)

    total = (vol_liq_pts + vol_mcap_pts + liq_growth_pts + cheap_pts
             + buyers_pts + rank_change_pts)

    return {
        "total": total,
        "vol_liq_pts": vol_liq_pts,
        "vol_liq_ratio": vol_liq_ratio,
        "vol_mcap_pts": vol_mcap_pts,
        "vol_mcap_pct": vol_mcap_pct,
        "liq_growth_pct": inputs.get("liq_growth"),
        "liq_growth_pts": liq_growth_pts,
        "price_pos": inputs.get("price_pos"),
        "cheap_pts": cheap_pts,
        "buy_sell": inputs.get("buy_sell"),
        "buyers_pts": buyers_pts,
        "rank_pts": rank_change_pts,
    }


# ---------------------------------------------------------------------------
# Score every coin's latest snapshot, save the scores, and print a ranking.
# ---------------------------------------------------------------------------
def score_all():
    session = SessionLocal()
    results = []
    try:
        # Only score ACTIVE coins - dropped coins aren't ranked or shown.
        coins = session.query(Coin).filter_by(status="active").all()
        inputs = score_inputs(session, coins)
        snap_days = {}
        for coin in coins:
            # The most recent snapshot for this coin (latest date).
            snap = (
                session.query(DailySnapshot)
                .filter(DailySnapshot.coin_id == coin.id)
                .order_by(DailySnapshot.date.desc(), DailySnapshot.id.desc())
                .first()
            )
            if snap is None:
                continue  # coin has no snapshot yet - skip
            snap_days[coin.id] = snap.date

            breakdown = score_snapshot(snap, coin, inputs.get(coin.id))

            # Save the score back into the snapshot.
            snap.score = float(breakdown["total"])

            # Capture plain values NOW, while the session is still open, so the
            # printing step later doesn't touch detached ORM objects.
            results.append({"coin_id": coin.id, "name": coin.name, "chain": coin.chain,
                            "mcap": snap.mcap, "score": snap.score, **breakdown})

        session.commit()  # persist all the scores at once
        # The Active table reads these instead of recomputing them (egress).
        save_score_inputs(session, inputs, snap_days)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    # Highest score first.
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Score history: one score per active coin per day (score_history), for the
# Active table's delta arrow and the detail page's "All Score" table.
# ---------------------------------------------------------------------------
SCORE_HISTORY_KEEP_DAYS = 30      # rows older than this are deleted (Supabase free-tier egress)
SCORE_HISTORY_SHOW_DAYS = 7       # the detail page's table
# The score's parts as stored in score_history: (column, score_snapshot key, label).
SCORE_PARTS = [("vol_liq", "vol_liq_pts", "Volume/Liquidity"),
               ("vol_mcap", "vol_mcap_pts", "Volume/MCap"),
               ("liq_growth", "liq_growth_pts", "Liq. Growth"),
               ("cheapness", "cheap_pts", "Price Cheapness"),
               ("buyers", "buyers_pts", "Buyers"),
               ("rank_change", "rank_pts", "Rank Change")]


def record_scores(results, day=None):
    """Save today's score for every scored coin (results of score_all):
    insert, or update a row already written today (re-run). Then delete rows
    older than SCORE_HISTORY_KEEP_DAYS. Returns rows written. No API calls."""
    from datetime import date as _date, timedelta
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
            for col, key, _ in SCORE_PARTS:          # the 6 parts, for the breakdown
                v = r.get(key)
                setattr(h, col, None if v is None else float(v))
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


def previous_scores(session, coins_days):
    """{coin_id: score} - each coin's most recent recorded score from BEFORE
    the given day (normally the day of its latest snapshot, so "yesterday's"
    run, also when a run was missed). coins_days = {coin_id: date}. Never
    raises: no table / no history -> {}."""
    from sqlalchemy import func
    from models import ScoreHistory
    if not coins_days:
        return {}
    try:
        days = set(coins_days.values())
        out = {}
        for day in days:                  # normally one day for every coin
            ids = [cid for cid, d in coins_days.items() if d == day]
            latest = (session.query(ScoreHistory.coin_id, func.max(ScoreHistory.recorded_at).label("d"))
                      .filter(ScoreHistory.coin_id.in_(ids), ScoreHistory.recorded_at < day)
                      .group_by(ScoreHistory.coin_id).subquery())
            for cid, score in (session.query(ScoreHistory.coin_id, ScoreHistory.score)
                               .join(latest, (ScoreHistory.coin_id == latest.c.coin_id)
                                     & (ScoreHistory.recorded_at == latest.c.d))):
                out[cid] = score
        return out
    except Exception:
        session.rollback()
        return {}


def score_history(session, coin_id, days=SCORE_HISTORY_SHOW_DAYS):
    """The coin's last `days` recorded scores, newest first:
    [{"date", "score", "parts": {label: points} or None,
      "first": True when there is no earlier recorded day,
      "changes": [{"label", "delta"}] vs the previous recorded day - only the
                 parts that changed, biggest change first; None when either
                 day has no breakdown (rows saved before the parts existed)}].
    Reads one extra (older) day so the oldest shown day has its comparison.
    Never raises (-> [])."""
    from models import ScoreHistory
    cols = [getattr(ScoreHistory, c) for c, _, _ in SCORE_PARTS]
    try:
        rows = (session.query(ScoreHistory.recorded_at, ScoreHistory.score, *cols)
                .filter(ScoreHistory.coin_id == coin_id)
                .order_by(ScoreHistory.recorded_at.desc()).limit(days + 1).all())
    except Exception:
        session.rollback()
        return []

    def parts(row):
        vals = row[2:]
        if any(v is None for v in vals):
            return None
        return {label: round(v, 1) for (_, _, label), v in zip(SCORE_PARTS, vals)}

    out = []
    for i, row in enumerate(rows[:days]):
        prev = rows[i + 1] if i + 1 < len(rows) else None
        now_p, prev_p = parts(row), parts(prev) if prev is not None else None
        changes = None
        if now_p is not None and prev_p is not None:
            changes = sorted(({"label": k, "delta": round(now_p[k] - prev_p[k], 1)} for k in now_p
                              if abs(now_p[k] - prev_p[k]) >= 0.05), key=lambda c: -abs(c["delta"]))
        out.append({"date": row[0].isoformat(), "score": round(row[1], 1), "parts": now_p,
                    "first": prev is None, "changes": changes})
    return out


def print_ranking(results):
    if not results:
        print("No scored coins to show (is the database empty? run save_to_db.py first).")
        return

    header = (
        f"{'#':>2}  {'NAME':<16} {'CHAIN':<9} {'MCAP':>10}  "
        f"{'VOL/LIQ':>12}  {'VOL/MCAP':>12}  {'LIQ 7D':>10}  {'60D POS':>9}  "
        f"{'BUY/SELL':>10}  {'RANK':>4}  {'SCORE':>9}"
    )
    print("=" * len(header))
    print("  CRYPTO SCREENER - RANKED BY SCORE (highest first)")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    def cell(v, fmt, pts):
        return "n/a" if v is None else f"{format(v, fmt)} ({pts})"

    for rank, r in enumerate(results, start=1):
        print(
            f"{rank:>2}  {(r['name'] or '')[:16]:<16} {r['chain']:<9} "
            f"{fmt_money(r['mcap']):>10}  "
            f"{cell(r['vol_liq_ratio'], '.2f', r['vol_liq_pts']):>12}  "
            f"{cell(r['vol_mcap_pct'], '.1f', r['vol_mcap_pts']):>12}  "
            f"{cell(r['liq_growth_pct'], '+.0f', r['liq_growth_pts']):>10}  "
            f"{cell(r['price_pos'], '.0f', r['cheap_pts']):>9}  "
            f"{cell(r['buy_sell'], '.2f', r['buyers_pts']):>10}  "
            f"{r['rank_pts']:>4}  {r['score']:>6.1f}/100"
        )

    print("-" * len(header))
    print(f"Scored {len(results)} coins. Max {MAX_TOTAL}/100 = Vol/Liq {MAX_VOL_LIQ} + "
          f"Vol/MCap {MAX_VOL_MCAP} + Liquidity growth {MAX_LIQ_GROWTH} + "
          f"Price cheapness {MAX_CHEAPNESS} + Buyers volume {MAX_BUYERS} + Rank {MAX_RANK_CHANGE}.")
    print("=" * len(header))


def main():
    init_db()
    results = score_all()
    print_ranking(results)


if __name__ == "__main__":
    main()
