"""
scoring.py
----------
Phase 2 - the SCORING system. It ranks the coins we've saved, 0-100,
highest first, and stores each coin's score back into its latest snapshot.

THE SCORE (max 100 when everything is implemented):
    Vol/Liq ratio ....... max 25 pts   [ACTIVE today]
    Vol/MCap ratio ...... max 20 pts   [ACTIVE today]
    Revenue ............. max 20 pts   [TODO - needs revenue data]
    Liquidity growth .... max 15 pts   [TODO - needs history]
    Holder growth ....... max 15 pts   [TODO - needs holders data + history]
    Rank change ......... max  5 pts   [TODO - needs CoinGecko/CMC]

Right now only the first two compute, so today's maximum is 45 points.
The rest are wired in as 0 with clear TODOs, ready to fill in later.

Run it:
    python scoring.py
"""

from database import SessionLocal, init_db
from models import Coin, DailySnapshot
from fetch_dexscreener import fmt_money

# Point ceilings for each component (documents the design in one place).
MAX_VOL_LIQ = 25
MAX_VOL_MCAP = 20
MAX_REVENUE = 20
MAX_LIQ_GROWTH = 15
MAX_HOLDER_GROWTH = 15
MAX_RANK_CHANGE = 5

# Today's achievable maximum (Vol/Liq + Vol/MCap + Rank change + Holders).
MAX_TODAY = MAX_VOL_LIQ + MAX_VOL_MCAP + MAX_RANK_CHANGE + MAX_HOLDER_GROWTH  # = 65


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
# Component 2: Volume / Market Cap ratio  (max 20 pts)
#   >60%=20, 40-60%=16, 25-40%=12, 10-25%=8, <10%=4
# ---------------------------------------------------------------------------
def score_vol_mcap(volume, mcap):
    """Return (points, percent). percent is None if it can't be computed."""
    if not mcap or mcap <= 0 or volume is None:
        return 0, None
    percent = (volume / mcap) * 100
    if percent > 60:
        pts = 20
    elif percent >= 40:   # 40% - 60%
        pts = 16
    elif percent >= 25:   # 25% - 40%
        pts = 12
    elif percent >= 10:   # 10% - 25%
        pts = 8
    else:                 # < 10%
        pts = 4
    return pts, percent


# ---------------------------------------------------------------------------
# Future components - wired as 0 for now, ready to implement later.
# ---------------------------------------------------------------------------
def score_revenue(snapshot):
    # TODO: score protocol/token revenue (max 20 pts). Revenue data isn't
    # collected yet - we'll add a source for it later, then score it here.
    return 0


def score_liquidity_growth(coin):
    # TODO: score how much liquidity has grown (max 15 pts). Needs several
    # days of snapshot history to measure a trend. Fill in once history exists.
    return 0


def score_holder_growth(snapshot):
    # Max 15 pts. TEMPORARY: until we have several days of holder history to
    # measure real GROWTH, award partial points by holder-count SIZE (a larger,
    # more distributed holder base is a positive sign). Switch to a growth-based
    # score (see holders_check.is_holders_rising / % change) once history exists.
    holders = getattr(snapshot, "holders", None)
    if holders is None:
        return 0
    if holders >= 20000:
        return 15
    if holders >= 10000:
        return 12
    if holders >= 5000:
        return 9
    if holders >= 1000:
        return 6
    if holders >= 500:
        return 3
    if holders >= 100:
        return 1
    return 0


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
# Combine everything into one total score for a snapshot.
# ---------------------------------------------------------------------------
def score_snapshot(snapshot, coin):
    """
    Compute the full breakdown + total (0-100 scale) for one snapshot.
    Returns a dict so the caller can both save and display the details.
    """
    vol_liq_pts, vol_liq_ratio = score_vol_liq(snapshot.volume, snapshot.liquidity)
    vol_mcap_pts, vol_mcap_pct = score_vol_mcap(snapshot.volume, snapshot.mcap)

    revenue_pts = score_revenue(snapshot)              # 0 (TODO)
    liq_growth_pts = score_liquidity_growth(coin)      # 0 (TODO)
    holder_growth_pts = score_holder_growth(snapshot)  # partial (by count size)
    rank_change_pts = score_rank_change(coin)          # up to 5

    total = (
        vol_liq_pts + vol_mcap_pts + revenue_pts
        + liq_growth_pts + holder_growth_pts + rank_change_pts
    )

    return {
        "total": total,
        "vol_liq_pts": vol_liq_pts,
        "vol_liq_ratio": vol_liq_ratio,
        "vol_mcap_pts": vol_mcap_pts,
        "vol_mcap_pct": vol_mcap_pct,
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

            breakdown = score_snapshot(snap, coin)

            # Save the score back into the snapshot.
            snap.score = float(breakdown["total"])

            # Capture plain values NOW, while the session is still open, so the
            # printing step later doesn't touch detached ORM objects.
            results.append({
                "name": coin.name,
                "chain": coin.chain,
                "mcap": snap.mcap,
                "score": snap.score,
                "vol_liq_ratio": breakdown["vol_liq_ratio"],
                "vol_liq_pts": breakdown["vol_liq_pts"],
                "vol_mcap_pct": breakdown["vol_mcap_pct"],
                "vol_mcap_pts": breakdown["vol_mcap_pts"],
            })

        session.commit()  # persist all the scores at once
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    # Highest score first.
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def print_ranking(results):
    if not results:
        print("No scored coins to show (is the database empty? run save_to_db.py first).")
        return

    header = (
        f"{'#':>2}  {'NAME':<16} {'CHAIN':<9} {'MCAP':>10}  "
        f"{'VOL/LIQ':>13}  {'VOL/MCAP':>13}  {'SCORE':>9}"
    )
    print("=" * len(header))
    print("  CRYPTO SCREENER - RANKED BY SCORE (highest first)")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    for rank, r in enumerate(results, start=1):
        # e.g. "4.10x (25)" or "n/a" if it couldn't be computed.
        if r["vol_liq_ratio"] is None:
            vol_liq = "n/a"
        else:
            vol_liq = f"{r['vol_liq_ratio']:.2f}x ({r['vol_liq_pts']})"
        if r["vol_mcap_pct"] is None:
            vol_mcap = "n/a"
        else:
            vol_mcap = f"{r['vol_mcap_pct']:.1f}% ({r['vol_mcap_pts']})"

        print(
            f"{rank:>2}  {(r['name'] or '')[:16]:<16} {r['chain']:<9} "
            f"{fmt_money(r['mcap']):>10}  {vol_liq:>13}  {vol_mcap:>13}  "
            f"{r['score']:>6.1f}/100"
        )

    print("-" * len(header))
    print(f"Scored {len(results)} coins. "
          f"Today's max possible = {MAX_TODAY}/100 "
          f"(Vol/Liq {MAX_VOL_LIQ} + Vol/MCap {MAX_VOL_MCAP} + "
          f"Rank {MAX_RANK_CHANGE} + Holders {MAX_HOLDER_GROWTH}); "
          f"the other {100 - MAX_TODAY} pts (revenue, liq-growth) are TODO.")
    print("=" * len(header))


def main():
    init_db()
    results = score_all()
    print_ranking(results)


if __name__ == "__main__":
    main()
