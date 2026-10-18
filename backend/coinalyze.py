"""
coinalyze.py
------------
"Futures Data" on the coin detail page: open interest (USD), funding rate and
long / short ratio from the Coinalyze API (COINALYZE_API_KEY; header
api_key). DISPLAY ONLY - the filter, drop logic and score never use it.

Coverage: only coins with a perpetual futures market (~34 of ~160).

Matching (coinalyze_coins, once per coin): Coinalyze has no contract
addresses, so a coin is matched by TICKER (coin_queue.symbol) to a perpetual
market and CONFIRMED BY PRICE (the market's last close within 30% of our
price) - a ticker clash (META = MetaDAO vs another coin) is not matched.
Each coin gets ONE main market: USDT-margined perpetual, Binance first, then
Bybit, OKX, ... (markets with long / short data preferred). There are no
cross-exchange aggregate symbols on this API, so the numbers are that
exchange's. Unmatched coins are checked again after RECHECK_DAYS.

Daily job (run_coinalyze_update): new active coins are matched, then each
matched coin's daily OI / funding / long-short for the last HISTORY_DAYS days
is stored in futures_snapshots (kept KEEP_DAYS): the rate limit counts coins,
not days, so the whole window costs the same as one day - the 7-day table is
full from the first run and a missed run fills itself in. Today's row is the
day so far. A page view reads only that table.

Rate limit: ~40 COINS per minute (every symbol in a request counts), HTTP
429 + Retry-After when exceeded. The pacer below keeps under it.
"""

import os
import time
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
log = logging.getLogger("coinalyze")

BASE_URL = "https://api.coinalyze.net/v1"
COINS_PER_MINUTE = 38      # Coinalyze allows ~40 coins / minute; keep a margin
BATCH = 20                 # symbols per request (the limit counts coins anyway)
PRICE_TOLERANCE = 0.30     # market price within 30% of ours = same coin
RECHECK_DAYS = 30          # unmatched coins are looked at again after this
KEEP_DAYS = 90             # futures_snapshots rows kept
SHOW_DAYS = 7              # the detail page's table
HISTORY_DAYS = 8           # days fetched (and stored) per run
# Main market per coin: exchange codes in order of preference
# (A Binance, 6 Bybit, 3 OKX, Y Gate.io, 4 Huobi, S Aster, H Hyperliquid, W WOO X).
EXCHANGE_PREF = ["A", "6", "3", "Y", "4", "S", "H", "W"]

_sent = deque()            # (time, coins) of recent requests - the pacer


def _key():
    return os.getenv("COINALYZE_API_KEY")


def _pace(coins):
    """Wait until `coins` more symbols fit in the last minute's budget."""
    while True:
        now = time.time()
        while _sent and now - _sent[0][0] > 60:
            _sent.popleft()
        used = sum(n for _, n in _sent)
        if used + coins <= COINS_PER_MINUTE or not _sent:
            _sent.append((now, coins))
            return
        time.sleep(max(1.0, 60 - (now - _sent[0][0]) + 0.5))


def _get(path, coins=1, wait=True, **params):
    """(status, json or None). Paced; on 429 waits Retry-After (or 60s) and
    retries ONCE. wait=False (live web requests): no pacing and no waiting -
    a 429 is returned at once. Never raises; the key is never logged."""
    for attempt in (1, 2):
        if wait:
            _pace(coins)
        try:
            r = requests.get(BASE_URL + path, params=params, headers={"api_key": _key()}, timeout=40)
        except requests.exceptions.RequestException as e:
            log.warning("Coinalyze %s failed (%s)", path, type(e).__name__)
            return None, None
        if r.status_code == 429 and attempt == 1 and wait:
            wait = float(r.headers.get("Retry-After") or 60)
            log.warning("Coinalyze rate limit on %s - waiting %.0fs", path, wait)
            time.sleep(wait + 1)
            _sent.clear()
            continue
        if r.status_code in (401, 403):
            log.error("Coinalyze REJECTED the API key on %s (HTTP %s) - check COINALYZE_API_KEY", path, r.status_code)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    return 429, None


def _window(days=2):
    now = datetime.now(timezone.utc)
    return {"from": int((now - timedelta(days=days)).timestamp()), "to": int(now.timestamp())}


def _history(path, symbols, days=2, wait=True, failed=None, **extra):
    """{symbol: {date: daily candle}} for the symbols over the last `days`
    days (batched; today = the day so far). `failed` (a list) collects the
    paths of batches that failed."""
    out = {}
    for i in range(0, len(symbols), BATCH):
        part = symbols[i:i + BATCH]
        st, body = _get(path, coins=len(part), wait=wait, symbols=",".join(part), interval="daily",
                        **_window(days), **extra)
        if st != 200 or not isinstance(body, list):
            log.warning("Coinalyze %s batch failed (HTTP %s)", path, st)
            if failed is not None:
                failed.append(path)
            continue
        for row in body:
            out[row["symbol"]] = {datetime.fromtimestamp(h["t"], timezone.utc).date(): h
                                  for h in row.get("history") or []}
    return out


def _latest(path, symbols, **extra):
    """{symbol: latest daily candle} for the symbols."""
    return {s: h[max(h)] for s, h in _history(path, symbols, **extra).items() if h}


def _main_markets(markets):
    """{TICKER: main perpetual symbol} from /future-markets."""
    best = {}
    for m in markets:
        if not m.get("is_perpetual") or m.get("margined") != "STABLE":
            continue
        ticker = (m.get("base_asset") or "").upper()
        ex = m.get("exchange")
        rank = (EXCHANGE_PREF.index(ex) if ex in EXCHANGE_PREF else 99,
                0 if m.get("has_long_short_ratio_data") else 1)
        if ticker and (ticker not in best or rank < best[ticker][0]):
            best[ticker] = (rank, m["symbol"])
    return {t: s for t, (_, s) in best.items()}


def _match_coins(session, now):
    """Match active coins that have no mapping yet (or an unmatched one older
    than RECHECK_DAYS). Returns (new matches, coins checked)."""
    from models import Coin, CoinQueue, CoinalyzeCoin
    rows = {r.coin_id: r for r in session.query(CoinalyzeCoin)}
    todo = [c for c in session.query(Coin).filter_by(status="active")
            if c.id not in rows or (rows[c.id].coinalyze_symbol is None
                                    and rows[c.id].checked_at < now - timedelta(days=RECHECK_DAYS))]
    if not todo:
        return 0, 0
    st, markets = _get("/future-markets")
    if st != 200 or not isinstance(markets, list):
        log.warning("Coinalyze market list failed (HTTP %s) - matching skipped", st)
        return 0, 0
    main = _main_markets(markets)
    tickers = {(q.chain, q.contract): (q.symbol or "").upper()
               for q in session.query(CoinQueue.chain, CoinQueue.contract, CoinQueue.symbol)}
    cand = {}
    for c in todo:
        t = tickers.get(((c.chain or "").lower(), (c.contract or "").lower()), "")
        cand[c.id] = (c, t, main.get(t))
    closes = _latest("/ohlcv-history", sorted({s for _, _, s in cand.values() if s}))
    new = 0
    for cid, (c, t, sym) in cand.items():
        close = (closes.get(sym) or {}).get("c") if sym else None
        ok = bool(close and c.price_usd and abs(close / c.price_usd - 1) <= PRICE_TOLERANCE)
        row = rows.get(cid)
        if row is None:
            row = CoinalyzeCoin(coin_id=cid)
            session.add(row)
        row.ticker, row.coinalyze_symbol, row.checked_at = t or None, (sym if ok else None), now
        new += ok
    session.commit()
    return new, len(cand)


def run_coinalyze_update():
    """Daily job step: match new coins, store today's OI / funding / L-S."""
    from database import SessionLocal, utcnow
    from models import CoinalyzeCoin, FuturesSnapshot
    stats = {"new_matches": 0, "checked": 0, "matched": 0, "stored": 0}
    if not _key():
        log.warning("COINALYZE_API_KEY not set - skipping futures data.")
        return stats
    now = utcnow()
    today = now.date()
    session = SessionLocal()
    try:
        stats["new_matches"], stats["checked"] = _match_coins(session, now)
        mapped = {r.coinalyze_symbol: r.coin_id for r in session.query(CoinalyzeCoin)
                  if r.coinalyze_symbol}
        stats["matched"] = len(mapped)
        syms = sorted(mapped)
        oi = _history("/open-interest-history", syms, HISTORY_DAYS, convert_to_usd="true")
        fr = _history("/funding-rate-history", syms, HISTORY_DAYS)
        ls = _history("/long-short-ratio-history", syms, HISTORY_DAYS)
        start = today - timedelta(days=HISTORY_DAYS)
        have = {(r.coin_id, r.day): r for r in
                session.query(FuturesSnapshot).filter(FuturesSnapshot.day >= start)}
        for sym, cid in mapped.items():
            days = set(oi.get(sym, {})) | set(fr.get(sym, {})) | set(ls.get(sym, {}))
            for day in sorted(d for d in days if d <= today):
                row = have.get((cid, day))
                if row is None:
                    row = FuturesSnapshot(coin_id=cid, day=day)
                    session.add(row)
                    have[(cid, day)] = row
                row.open_interest_usd = (oi.get(sym, {}).get(day) or {}).get("c")
                row.funding_rate = (fr.get(sym, {}).get(day) or {}).get("c")
                row.long_pct = (ls.get(sym, {}).get(day) or {}).get("l")
                row.short_pct = (ls.get(sym, {}).get(day) or {}).get("s")
            stats["stored"] += bool(days)
        session.query(FuturesSnapshot).filter(
            FuturesSnapshot.day < today - timedelta(days=KEEP_DAYS)).delete(synchronize_session=False)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    log.info("Coinalyze: fetched OI/funding/L-S for %d coins (%d new matches this run, %d coins checked).",
             stats["stored"], stats["new_matches"], stats["checked"])
    return stats


# ---------------------------------------------------------------- live (Check a Coin)
MARKETS_TTL_S = 3600       # the futures market list is cached in memory this long
_markets = {"at": 0.0, "main": None}


def _main_markets_cached():
    """{TICKER: main perpetual symbol}, fetched at most once per MARKETS_TTL_S
    (per running process). None if Coinalyze couldn't be asked."""
    if _markets["main"] is None or time.time() - _markets["at"] > MARKETS_TTL_S:
        st, markets = _get("/future-markets", wait=False)
        if st != 200 or not isinstance(markets, list):
            return _markets["main"]                   # last good list, or None
        _markets["main"], _markets["at"] = _main_markets(markets), time.time()
    return _markets["main"]


def futures_data_live(ticker, price, days=SHOW_DAYS):
    """Check a Coin: the same Futures Data for ANY coin, looked up live
    (ticker -> main perpetual market, confirmed by price within 30%, then the
    last HISTORY_DAYS of OI / funding / long-short). Nothing is stored.
    Returns {"matched", "symbol", "history"} like futures_data(); "error":
    True when Coinalyze couldn't be asked (the caller then doesn't cache the
    result). No key, no ticker / price -> not matched. Never raises."""
    none = {"matched": False, "history": []}
    if not _key() or not ticker or not price:
        return none
    try:
        main = _main_markets_cached()
        if main is None:
            return {**none, "error": True}
        sym = main.get(ticker.upper())
        if not sym:
            return none
        failed = []
        close = (_latest("/ohlcv-history", [sym], wait=False, failed=failed).get(sym) or {}).get("c")
        if failed:
            return {**none, "error": True}
        if not close or abs(close / price - 1) > PRICE_TOLERANCE:
            return none                                 # ticker clash: another coin
        oi = _history("/open-interest-history", [sym], HISTORY_DAYS, wait=False, failed=failed,
                      convert_to_usd="true").get(sym, {})
        fr = _history("/funding-rate-history", [sym], HISTORY_DAYS, wait=False, failed=failed).get(sym, {})
        ls = _history("/long-short-ratio-history", [sym], HISTORY_DAYS, wait=False, failed=failed).get(sym, {})
        if failed:
            return {**none, "error": True}
        hist = [{"date": d.isoformat(), "oi": (oi.get(d) or {}).get("c"), "funding": (fr.get(d) or {}).get("c"),
                 "long_pct": (ls.get(d) or {}).get("l"), "short_pct": (ls.get(d) or {}).get("s")}
                for d in sorted(set(oi) | set(fr) | set(ls))]
        return {"matched": True, "symbol": sym, "history": hist[-days:]}
    except Exception as e:
        log.warning("Coinalyze live lookup failed (%s)", type(e).__name__)
        return {**none, "error": True}


def futures_data(session, coin_id, days=SHOW_DAYS):
    """Detail page: the coin's last `days` futures rows, oldest first.
    matched: True / False, or None when the tables don't exist yet (before
    the first daily run after this feature shipped). Never raises."""
    from models import CoinalyzeCoin, FuturesSnapshot
    try:
        m = session.query(CoinalyzeCoin).filter_by(coin_id=coin_id).first()
        if m is None or not m.coinalyze_symbol:
            return {"matched": False, "history": []}
        rows = (session.query(FuturesSnapshot).filter_by(coin_id=coin_id)
                .order_by(FuturesSnapshot.day.desc()).limit(days).all())
        return {"matched": True, "symbol": m.coinalyze_symbol,
                "history": [{"date": r.day.isoformat(), "oi": r.open_interest_usd, "funding": r.funding_rate,
                             "long_pct": r.long_pct, "short_pct": r.short_pct} for r in reversed(rows)]}
    except Exception:
        session.rollback()
        return {"matched": None, "history": []}
