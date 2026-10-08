"""
entry_zones_test.py
-------------------
READ-ONLY experiment: "Entry Zones" on 10 real active coins, using only the
daily prices already stored in daily_snapshots.price_usd. No API calls, and
nothing is written to the database or changed in the site / filter /
drop_logic.

Run (from the backend/ folder, with backend/.env present so DATABASE_URL points
at the real Supabase database):
    python entry_zones_test.py
Output: backend/entry_zones_live_test.xlsx

Per coin, per day (window = the last RANGE_DAYS stored days, today included, so
Position always lands between 0% and 100%):
    Range Low / High  = lowest / highest price in the window
    Position %        = (price - low) / (high - low) * 100   (0 = support)
    From Peak %       = (highest price so far - price) / highest price * 100
    3-Day Move %      = (price - price 3 days earlier) / price 3 days earlier * 100
Zone (first rule that matches):
    < 4 days of data                       -> TOO NEW
    pos <= 33 and |3d| < 15                -> GOOD ENTRY
    pos <= 33 and 3d < -15                 -> WAIT - falling
    from_peak < 10 and 3d > 20             -> TOO LATE
    pos >= 66                              -> WAIT - top of range
    otherwise                              -> MID
"""

from datetime import timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import func

from database import SessionLocal
from models import Coin, DailySnapshot

N_COINS = 10
MAX_DAYS = 15          # newest N stored price days per coin
MIN_DAYS = 10          # a coin needs at least this many price days to qualify
RANGE_DAYS = 14        # rolling range window
MIN_AGE_DAYS = 4

OUT_FILE = Path(__file__).resolve().parent / "entry_zones_live_test.xlsx"

# zone key -> (label, fill colour, font colour, meaning)
ZONES = {
    "new":   ("— TOO NEW", "D9D9D9", "000000", "Less than 4 days of stored prices - not enough history to judge."),
    "good":  ("GOOD ENTRY", "63BE7B", "000000", "Near the bottom of its range (Position <= 33%) and not moving hard (|3-day| < 15%)."),
    "fall":  ("WAIT — falling", "F4B183", "000000", "Near the bottom, but falling fast (3-day move < -15%) - wait for it to settle."),
    "late":  ("TOO LATE — near peak + pumping", "E06666", "FFFFFF", "Within 10% of its peak and up more than 20% in 3 days."),
    "top":   ("WAIT — top of range", "FFE699", "000000", "Upper third of its range (Position >= 66%)."),
    "mid":   ("MID — not cheap/expensive", "FFF2CC", "000000", "Middle of the range - neither cheap nor expensive."),
}


def pick_coins(session):
    """Active coins with >= MIN_DAYS daily prices, most price-days first."""
    rows = (session.query(Coin.id, Coin.name, Coin.chain, func.count(func.distinct(DailySnapshot.date)))
            .join(DailySnapshot, DailySnapshot.coin_id == Coin.id)
            .filter(Coin.status == "active", DailySnapshot.price_usd.isnot(None))
            .group_by(Coin.id, Coin.name, Coin.chain)
            .having(func.count(func.distinct(DailySnapshot.date)) >= MIN_DAYS)
            .order_by(func.count(func.distinct(DailySnapshot.date)).desc(), Coin.id)
            .limit(N_COINS).all())
    return rows


def load_prices(session, coin_id):
    """[(date, price)] oldest first, newest MAX_DAYS days; one price per day
    (the latest stored row of that day)."""
    by_day = {}
    for s in (session.query(DailySnapshot)
              .filter(DailySnapshot.coin_id == coin_id, DailySnapshot.price_usd.isnot(None))
              .order_by(DailySnapshot.date, DailySnapshot.id)):
        by_day[s.date] = s.price_usd
    days = sorted(by_day)[-MAX_DAYS:]
    return [(d, by_day[d]) for d in days]


def pct(a, b):
    return None if a is None or not b else a / b * 100


def zone_for(age, pos, from_peak, move3):
    if age < MIN_AGE_DAYS:
        return "new"
    if pos is not None and pos <= 33 and move3 is not None:
        if abs(move3) < 15:
            return "good"
        if move3 < -15:
            return "fall"
    if from_peak is not None and move3 is not None and from_peak < 10 and move3 > 20:
        return "late"
    if pos is not None and pos >= 66:
        return "top"
    return "mid"


def analyse(prices):
    """prices = [(date, price)] -> one dict per day."""
    out, peak = [], None
    by_date = dict(prices)
    for i, (d, p) in enumerate(prices):
        peak = p if peak is None else max(peak, p)
        window = [x for _, x in prices[max(0, i - RANGE_DAYS + 1): i + 1]]
        lo, hi = min(window), max(window)
        pos = (p - lo) / (hi - lo) * 100 if hi > lo else None
        from_peak = (peak - p) / peak * 100 if peak else None
        prev = by_date.get(d - timedelta(days=3))
        move3 = pct(p - prev, prev) if prev else None
        out.append({"date": d, "price": p, "lo": lo, "hi": hi, "pos": pos, "from_peak": from_peak,
                    "move3": move3, "zone": zone_for(i + 1, pos, from_peak, move3)})
    return out


def style_zone(cell, key):
    label, fill, font, _ = ZONES[key]
    cell.value = label
    cell.fill = PatternFill("solid", start_color=fill)
    cell.font = Font(bold=True, color=font)


def sheet_name(name, used):
    base = "".join(c for c in name if c not in '[]:*?/\\')[:28] or "coin"
    n, i = base, 2
    while n in used:
        n = f"{base[:25]}_{i}"
        i += 1
    used.add(n)
    return n


def price_fmt(v):
    return "0.00" if v >= 1 else "0.00000000"


def build(results):
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    head = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", start_color="1F2937")

    summary.append(["Coin", "Chain", "Days of data", "Zone (last day)", "Current price", "Position %", "From Peak %"])
    for c in summary[1]:
        c.font, c.fill = head, head_fill
    used = {"Summary", "Legend"}
    sheets = []
    for coin, rows in results:
        sheets.append((coin, rows, sheet_name(coin[1], used)))

    for r, (coin, rows, sname) in enumerate(sheets, start=2):
        last = rows[-1]
        summary.append([coin[1], coin[2], len(rows), None, last["price"],
                        None if last["pos"] is None else round(last["pos"], 1),
                        None if last["from_peak"] is None else round(last["from_peak"], 1)])
        style_zone(summary.cell(r, 4), last["zone"])
        summary.cell(r, 1).hyperlink = f"#'{sname}'!A1"
        summary.cell(r, 5).number_format = price_fmt(last["price"])
    for col, w in zip("ABCDEFG", (26, 12, 13, 32, 16, 12, 13)):
        summary.column_dimensions[col].width = w
    summary.freeze_panes = "A2"

    for coin, rows, sname in sheets:
        ws = wb.create_sheet(sname)
        ws.append(["Date", "Day", "Price", "Range Low", "Range High", "Position %", "From Peak %",
                   "3-Day Move %", "ENTRY ZONE"])
        for c in ws[1]:
            c.font, c.fill = head, head_fill
            c.alignment = Alignment(horizontal="center")
        for i, x in enumerate(rows, start=2):
            ws.append([x["date"], x["date"].strftime("%a"), x["price"], x["lo"], x["hi"],
                       None if x["pos"] is None else round(x["pos"], 1),
                       None if x["from_peak"] is None else round(x["from_peak"], 1),
                       None if x["move3"] is None else round(x["move3"], 1), None])
            style_zone(ws.cell(i, 9), x["zone"])
            ws.cell(i, 1).number_format = "yyyy-mm-dd"
            for col in (3, 4, 5):
                ws.cell(i, col).number_format = price_fmt(x["price"])
        for col, w in zip("ABCDEFGHI", (12, 7, 14, 14, 14, 12, 13, 14, 34)):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "A2"

    lg = wb.create_sheet("Legend")
    lg.append(["Zone", "Meaning"])
    for c in lg[1]:
        c.font, c.fill = head, head_fill
    for i, key in enumerate(ZONES, start=2):
        lg.append([None, ZONES[key][3]])
        style_zone(lg.cell(i, 1), key)
    n = len(ZONES) + 3
    notes = [
        "Rules are checked top to bottom; the first one that matches wins.",
        f"Range = lowest / highest price of the last {RANGE_DAYS} stored days (today included).",
        "Position % = (price - low) / (high - low); 0% = bottom of the range, 100% = top.",
        "From Peak % = distance below the highest price in the sheet. 3-Day Move % = vs the price 3 days earlier.",
        "Age = number of stored price days up to that date (not the coin's real age). Days with no stored price are skipped.",
        "Experiment only - on-chain price history, not financial advice.",
    ]
    for i, t in enumerate(notes):
        lg.cell(n + i, 1, t)
    lg.column_dimensions["A"].width = 34
    lg.column_dimensions["B"].width = 95
    wb.save(OUT_FILE)


def main():
    session = SessionLocal()
    try:
        coins = pick_coins(session)
        if not coins:
            print(f"No active coin has >= {MIN_DAYS} days of stored daily prices (daily_snapshots.price_usd).")
            return
        results = []
        for cid, name, chain, _ in coins:
            results.append(((cid, name, chain), analyse(load_prices(session, cid))))
    finally:
        session.close()
    build(results)
    print(f"Wrote {OUT_FILE}\n")
    print(f"{'#':>2}  {'Coin':<28} {'Days':>4}  Last-day zone")
    for i, ((_, name, chain), rows) in enumerate(results, start=1):
        print(f"{i:>2}  {name[:28]:<28} {len(rows):>4}  {ZONES[rows[-1]['zone']][0]}")


if __name__ == "__main__":
    main()
