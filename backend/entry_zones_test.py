"""
entry_zones_test.py
-------------------
READ-ONLY experiment: "Entry Zones" on 10 real active coins. The coins come
from our database (active list); their DAILY PRICES come from Codex getBars
(resolution 1D, the daily close) - Codex already has 15+ days of history,
while our own snapshots only start at the Oct 5 wipe. One Codex request per
coin. Nothing is written to the database or changed in the site / filter /
drop_logic.

Run (from the backend/ folder, with backend/.env present so DATABASE_URL and
CODEX_API_KEY are set):
    python entry_zones_test.py
Output: backend/entry_zones_live_test.xlsx

Per coin, per day (window = the last RANGE_DAYS price days, today included, so
Position always lands between 0% and 100%):
    Range Low / High  = lowest / highest price in the window
    Position %        = (price - low) / (high - low) * 100   (0 = support)
    From Peak %       = (highest price so far - price) / highest price * 100
    3-Day Move %      = (price - price 3 days earlier) / price 3 days earlier * 100
Zone (first rule that matches):
    < 4 days of price history              -> TOO NEW
    pos <= 33 and |3d| < 15                -> GOOD ENTRY
    pos <= 33 and 3d < -15                 -> WAIT - falling
    from_peak < 10 and 3d > 20             -> TOO LATE
    pos >= 66                              -> WAIT - top of range
    otherwise                              -> MID
"""

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from database import SessionLocal
from models import Coin
from liquidity_check import CHAIN_TO_NETWORK

load_dotenv(Path(__file__).resolve().parent / ".env")
CODEX_KEY = os.getenv("CODEX_API_KEY")
CODEX_URL = "https://graph.codex.io/graphql"
BARS_QUERY = """
query ($symbol: String!, $from: Int!, $to: Int!) {
  getBars(symbol: $symbol, from: $from, to: $to, resolution: "1D",
          removeLeadingNullValues: true) { t c }
}"""

N_COINS = 10
MAX_DAYS = 15          # newest N daily prices per coin (Codex, today included)
MIN_DAYS = 10          # a coin needs at least this many price days to qualify
RANGE_DAYS = 14        # rolling range window
MIN_AGE_DAYS = 4

OUT_FILE = Path(__file__).resolve().parent / "entry_zones_live_test.xlsx"

# zone key -> (label, fill colour, font colour, meaning)
ZONES = {
    "new":   ("— TOO NEW", "D9D9D9", "000000", "Less than 4 days of price history - not enough to judge."),
    "good":  ("GOOD ENTRY", "63BE7B", "000000", "Near the bottom of its range (Position <= 33%) and not moving hard (|3-day| < 15%)."),
    "fall":  ("WAIT — falling", "F4B183", "000000", "Near the bottom, but falling fast (3-day move < -15%) - wait for it to settle."),
    "late":  ("TOO LATE — near peak + pumping", "E06666", "FFFFFF", "Within 10% of its peak and up more than 20% in 3 days."),
    "top":   ("WAIT — top of range", "FFE699", "000000", "Upper third of its range (Position >= 66%)."),
    "mid":   ("MID — not cheap/expensive", "FFF2CC", "000000", "Middle of the range - neither cheap nor expensive."),
}


def codex_prices(contract, chain):
    """[(date, close)] oldest first: the newest MAX_DAYS daily closes from
    Codex getBars (1D), today's (still-running) bar included. [] if Codex
    doesn't cover the chain or has nothing."""
    net = CHAIN_TO_NETWORK.get((chain or "").lower())
    if not net or not CODEX_KEY:
        return []
    now = int(time.time())
    try:
        body = requests.post(CODEX_URL, json={"query": BARS_QUERY, "variables": {
            "symbol": f"{contract}:{net}", "from": now - (MAX_DAYS + 3) * 86400, "to": now}},
            headers={"Authorization": CODEX_KEY}, timeout=30).json()
    except (requests.exceptions.RequestException, ValueError):
        return []
    bars = ((body or {}).get("data") or {}).get("getBars") or {}
    by_day = {}
    for t, c in zip(bars.get("t") or [], bars.get("c") or []):
        if c is not None and float(c) > 0:
            by_day[datetime.fromtimestamp(t, tz=timezone.utc).date()] = float(c)
    days = sorted(by_day)[-MAX_DAYS:]
    return [(d, by_day[d]) for d in days]


def pick_coins(session):
    """The first N_COINS active coins (same order as before: by id) that have
    >= MIN_DAYS daily prices on Codex. Returns [((id, name, chain), prices)]."""
    picked, tried = [], 0
    for coin in session.query(Coin).filter(Coin.status == "active").order_by(Coin.id):
        if len(picked) == N_COINS:
            break
        tried += 1
        prices = codex_prices(coin.contract, coin.chain)
        if len(prices) >= MIN_DAYS:
            picked.append(((coin.id, coin.name, coin.chain), prices))
    print(f"(checked {tried} active coins on Codex to find {len(picked)} with >= {MIN_DAYS} days)")
    return picked


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
        f"Range = lowest / highest price of the last {RANGE_DAYS} days (today included).",
        "Position % = (price - low) / (high - low); 0% = bottom of the range, 100% = top.",
        "From Peak % = distance below the highest price in the sheet. 3-Day Move % = vs the price 3 days earlier.",
        "Prices = Codex daily close (getBars, 1D); today's value is the latest price so far.",
        "Age = number of price days up to that date in this sheet (not the coin's real age). Days with no price are skipped.",
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
        coins = pick_coins(session)                    # read-only: active coin list
    finally:
        session.close()
    if not coins:
        print(f"No active coin has >= {MIN_DAYS} days of daily prices on Codex.")
        return
    results = [(coin, analyse(prices)) for coin, prices in coins]
    build(results)
    print(f"Wrote {OUT_FILE}\n")
    print(f"{'#':>2}  {'Coin':<28} {'Chain':<10} {'Days':>4}  {'From':<10} {'To':<10}  Last-day zone")
    for i, ((_, name, chain), rows) in enumerate(results, start=1):
        print(f"{i:>2}  {name[:28]:<28} {chain:<10} {len(rows):>4}  {rows[0]['date']}  {rows[-1]['date']}  "
              f"{ZONES[rows[-1]['zone']][0]}")


if __name__ == "__main__":
    main()
