"""
price_update.py
---------------
Refreshes the CURRENT USD price of every coin in the ACTIVE and DROPPED lists
on each daily run, so the price the dashboard shows is never more than ~24h
old. (Not real-time per page load - just refreshed once per run.)

Source: DexScreener, by contract - the price of the coin's most-liquid pool
(the same "main pool" price the filter stores). One batched pass for all
active + dropped coins.

Writes:
  * coins.price_usd + coins.price_updated_at  -> what the dashboard shows
  * today's snapshot price_usd, if the coin has a snapshot dated today
    (older snapshots keep the price of THEIR day - history isn't overwritten)

A coin DexScreener returns no price for this run keeps its previous price and
timestamp (so the dashboard can show how old it is).

Run it standalone:
    python price_update.py
"""

from datetime import date

from database import SessionLocal, init_db, utcnow
from models import Coin, DailySnapshot


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def run_price_update():
    """Refresh the current price of every active + dropped coin. Returns stats."""
    from fetch_dexscreener import fetch_pairs_for_addresses, build_token_records

    now = utcnow()
    today = date.today()
    stats = {"coins": 0, "updated": 0, "no_data": 0}

    session = SessionLocal()
    try:
        coins = (session.query(Coin)
                 .filter(Coin.status.in_(["active", "dropped"])).all())
        stats["coins"] = len(coins)
        if not coins:
            print("No active or dropped coins - nothing to price.")
            return stats

        addrs = [c.contract for c in coins]
        records = build_token_records(fetch_pairs_for_addresses(addrs),
                                      keep_addresses={a.lower() for a in addrs})
        price_by = {r["contract"].lower(): _to_float(r.get("price_usd")) for r in records}

        for coin in coins:
            price = price_by.get(coin.contract.lower())
            if price is None:
                stats["no_data"] += 1               # keep the previous price
                continue
            coin.price_usd = price
            coin.price_updated_at = now
            snap = (session.query(DailySnapshot)
                    .filter_by(coin_id=coin.id, date=today).first())
            if snap is not None:
                snap.price_usd = price
            stats["updated"] += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    line = "=" * 46
    print(line)
    print("  PRICE UPDATE (active + dropped)")
    print(line)
    print(f"  coins ................ {stats['coins']:>5}")
    print(f"  price refreshed ...... {stats['updated']:>5}")
    print(f"  no data (kept old) ... {stats['no_data']:>5}")
    print(line)
    return stats


if __name__ == "__main__":
    init_db()
    run_price_update()
