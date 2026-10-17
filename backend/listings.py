"""
listings.py
-----------
"Exchange Listings": which of 12 major exchanges each coin trades on, new
listings (🆕), recent delistings, and upcoming Binance listings (📅).
DISPLAY + ALERT ONLY - the filter, drop logic and score never use it.

Source: DropsTab (DROPSTAB_API_KEY, our plan) GET /exchanges/{slug}/pairs -
every trading pair an exchange has today (100 per page; no listing date).
The daily run reads the 12 exchanges' full lists (~260 requests of our
500K / period), stores them in exchange_pairs, and compares each coin with
the previous day:
  * Pairs carry only a TICKER, so a coin is matched by its ticker
    (coin_queue.symbol) AND confirmed by price: its pair there is the
    biggest one priced within PRICE_TOLERANCE of ours (a ticker clash - FUN
    = FunFair vs another FUN - is not a listing; an exchange often lists two
    tokens under one ticker, e.g. Gate AXS at $1.06 and at $6.59). A NEW
    listing also needs MIN_NEW_VOLUME of 24h volume (dead pairs aren't
    listings); a known listing stays listed while any matching pair exists,
    so a volume dip doesn't flip it to delisted and back.
  * A listing seen on a coin's first check (or on an exchange's first
    complete fetch) is the starting point (baseline), not "new".
  * NEW only if the exchange had NO pair at all with that ticker the
    previous day (a thin pair crossing the volume minimum, or a price
    moving back within the tolerance, is an old listing - not news).
  * DELISTED only when the ticker disappears from the exchange completely
    (on a complete fetch); a price / volume dip keeps it listed.
  * Rows of coins that no longer exist (deleted from Dropped) are removed.
An exchange whose pages didn't all arrive is skipped for the day (no false
delistings).

Upcoming listings: Binance's "New Cryptocurrency Listing" announcements
(the public feed behind binance.com/en/support/announcement; unofficial, no
key). "Binance Will List X (ABC)" -> tickers + the trading start time read
from the article ("... at 2026-10-20 10:00 (UTC)"). Matched by ticker only
(the coin isn't trading there yet, so there's no price to confirm).

A page view reads only the stored tables - 0 API calls. Check a Coin uses
the stored pair lists too (any coin, by ticker + price) - 0 calls.
"""

import re
import json
import time
import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("listings")

# The 12 monitored exchanges: DropsTab slug -> display name (spot markets).
EXCHANGES = {
    "binance": "Binance", "gdax": "Coinbase", "okex": "OKX", "bybit-spot": "Bybit",
    "upbit": "Upbit", "kraken": "Kraken", "kucoin": "KuCoin", "bitget": "Bitget",
    "gate": "Gate", "huobi": "HTX", "mxc": "MEXC", "bithumb": "Bithumb",
}
PAGE_SIZE = 100            # DropsTab's maximum for this endpoint
PRICE_TOLERANCE = 0.30     # exchange price within 30% of ours = same coin
NEW_DAYS = 7               # 🆕 badge shown this many days after first seen
DELISTED_SHOW_DAYS = 30    # "Recently delisted from" window
MAX_PAGES = 120            # safety stop per exchange
MIN_NEW_VOLUME = 1000      # USD 24h volume for a pair to count as a (new) listing

BINANCE_LIST_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
BINANCE_DETAIL_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"
BINANCE_ARTICLE_URL = "https://www.binance.com/en/support/announcement/detail/{code}"
BINANCE_LISTING_CATALOG = 48   # "New Cryptocurrency Listing"
ANNOUNCE_KEEP_DAYS = 60
_TICKER_RE = re.compile(r"\(([A-Z0-9]{2,15})\)")
_TIME_RE = re.compile(r"(20\d\d-\d\d-\d\d \d\d:\d\d)\s*\(UTC\)")


# ---------------------------------------------------------------- fetching
def fetch_exchange_pairs(slug):
    """(pairs, requests made). pairs = None unless EVERY page arrived."""
    import dropstab
    out, calls, page = [], 0, 0
    while page < MAX_PAGES:
        for attempt in (1, 2):                      # one retry for a hiccup
            st, body = dropstab._get(f"/exchanges/{slug}/pairs", pageSize=PAGE_SIZE, page=page,
                                     sortingField="BASE", sortingOrder="ASC")
            calls += 1
            dropstab._check_auth(st, body, "/exchanges/{slug}/pairs")
            if st == 200:
                break
        data = (body or {}).get("data") if isinstance(body, dict) else None
        if st != 200 or not isinstance(data, dict):
            log.warning("DropsTab pairs %s page %d failed (HTTP %s) - exchange skipped today", slug, page, st)
            return None, calls
        out += data.get("content") or []
        if page + 1 >= (data.get("totalPages") or 0):
            return out, calls
        page += 1
    log.warning("DropsTab pairs %s: more than %d pages - exchange skipped today", slug, MAX_PAGES)
    return None, calls


def _parse_time(body):
    m = _TIME_RE.search(body or "")
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M") if m else None


def fetch_binance_announcements(known_codes=()):
    """Binance's latest "Will List" announcements:
    [{code, title, tickers, listing_at, released_at, url}]. Article pages are
    read only for announcements not seen before. Never raises."""
    try:
        r = requests.get(BINANCE_LIST_URL, params={"type": 1, "catalogId": BINANCE_LISTING_CATALOG,
                                                   "pageNo": 1, "pageSize": 20}, timeout=30)
        arts = r.json()["data"]["catalogs"][0]["articles"]
    except Exception as e:
        log.warning("Binance announcements unavailable (%s)", type(e).__name__)
        return None
    out = []
    for a in arts:
        title = a.get("title") or ""
        if "Will List" not in title or "Futures" in title:
            continue
        item = {"code": a["code"], "title": title, "tickers": _TICKER_RE.findall(title),
                "released_at": datetime.fromtimestamp(a["releaseDate"] / 1000, timezone.utc).replace(tzinfo=None),
                "url": BINANCE_ARTICLE_URL.format(code=a["code"]), "listing_at": None, "fetched": False}
        if a["code"] not in known_codes:
            try:
                d = requests.get(BINANCE_DETAIL_URL, params={"articleCode": a["code"]}, timeout=30).json()["data"]
                body = d.get("body") or ""
                item["listing_at"], item["fetched"] = _parse_time(body), True
                if not item["tickers"]:          # "Binance Will List RWUSD and ..." - no brackets
                    m = re.search(r"Will List ([A-Z0-9]{2,15})\b", title)
                    item["tickers"] = [m.group(1)] if m else []
            except Exception as e:
                log.warning("Binance article %s unreadable (%s)", a["code"], type(e).__name__)
        out.append(item)
    return out


# ---------------------------------------------------------------- matching
def _tickers(session, coin_ids=None):
    """{coin_id: upper-case ticker} for active + dropped coins (only
    `coin_ids` when given)."""
    from models import Coin, CoinQueue
    cq = session.query(Coin).filter(Coin.status.in_(["active", "dropped"]))
    if coin_ids is not None:
        cq = cq.filter(Coin.id.in_(list(coin_ids)))
    coins = cq.all()
    contracts = list({(c.contract or "").lower() for c in coins})
    q = {}
    for i in range(0, len(contracts), 500):
        for r in session.query(CoinQueue).filter(CoinQueue.contract.in_(contracts[i:i + 500])):
            q[(r.chain, r.contract)] = (r.symbol or "").upper()
    return {c.id: q.get(((c.chain or "").lower(), (c.contract or "").lower()), "") for c in coins}


def best_pair(pairs, ticker, price, min_volume=MIN_NEW_VOLUME):
    """The exchange's biggest `ticker` pair priced within PRICE_TOLERANCE of
    ours with at least `min_volume` 24h volume, else None (not listed /
    another coin / dead pair)."""
    if not ticker or not price:
        return None
    mine = [p for p in pairs if (p.get("base") or "").upper() == ticker and p.get("price_usd")
            and abs(p["price_usd"] / price - 1) <= PRICE_TOLERANCE and (p.get("volume_24h") or 0) >= min_volume]
    return max(mine, key=lambda p: p.get("volume_24h") or 0) if mine else None


def _pair_dict(p):
    return {"base": (p.get("base") or "").upper(), "quote": p.get("quote"), "pair_type": p.get("type"),
            "price_usd": p.get("priceUsd"), "volume_24h": p.get("volume24h"), "link": p.get("link")}


def run_listings_update():
    """Daily job step. Returns stats incl. "new", "delisted", "scheduled"
    lists for the Telegram report."""
    import dropstab
    from database import SessionLocal, utcnow
    from models import Coin, ExchangePair, ExchangeWatch, ExchangeListing, ExchangeAnnouncement
    stats = {"exchanges_ok": 0, "exchanges_failed": [], "calls": 0, "pairs": 0, "coins": 0,
             "listings": 0, "new": [], "delisted": [], "scheduled": []}
    now = utcnow()
    today = now.date()
    session = SessionLocal()
    try:
        # 1. Binance upcoming listings (independent of DropsTab).
        known = {a.code: a for a in session.query(ExchangeAnnouncement)}
        anns = fetch_binance_announcements(set(known))
        for a in anns or []:
            row = known.get(a["code"])
            if row is None:
                row = ExchangeAnnouncement(exchange="binance", code=a["code"])
                session.add(row)
                known[a["code"]] = row
            if a["fetched"] or row.title is None:
                row.title, row.tickers, row.listing_at = a["title"], json.dumps(a["tickers"]), a["listing_at"]
            row.released_at, row.url, row.checked_at = a["released_at"], a["url"], now
        session.query(ExchangeAnnouncement).filter(
            ExchangeAnnouncement.released_at < now - timedelta(days=ANNOUNCE_KEEP_DAYS)).delete(synchronize_session=False)
        session.commit()

        if not dropstab._key():
            log.warning("DROPSTAB_API_KEY not set - skipping exchange listings.")
            return stats
        # 2. The 12 exchanges' pair lists.
        fresh = {}
        for slug in EXCHANGES:
            pairs, calls = fetch_exchange_pairs(slug)
            stats["calls"] += calls
            if pairs is None:
                stats["exchanges_failed"].append(EXCHANGES[slug])
                continue
            fresh[slug] = [_pair_dict(p) for p in pairs if p.get("base")]
        had_before = {e for (e,) in session.query(ExchangePair.exchange).distinct()}
        # Every ticker each exchange had BEFORE today's lists replace them.
        bases_before = {(e, b) for e, b in session.query(ExchangePair.exchange, ExchangePair.base)
                        .filter(ExchangePair.exchange.in_(list(fresh))).distinct()}
        for slug, rows in fresh.items():
            session.query(ExchangePair).filter_by(exchange=slug).delete(synchronize_session=False)
            session.bulk_insert_mappings(ExchangePair, [{**r, "exchange": slug, "day": today} for r in rows])
            stats["pairs"] += len(rows)
        stats["exchanges_ok"] = len(fresh)
        session.commit()

        # 3. Our coins vs today's lists.
        tick = _tickers(session)
        coins = {c.id: c for c in session.query(Coin).filter(Coin.id.in_(list(tick)))}
        watch = {w.coin_id: w for w in session.query(ExchangeWatch)}
        existing = {(l.coin_id, l.exchange): l for l in session.query(ExchangeListing)}
        by_base = {}
        for slug, rows in fresh.items():
            for r in rows:
                by_base.setdefault((slug, r["base"]), []).append(r)
        for cid, ticker in tick.items():
            c = coins.get(cid)
            if c is None or not ticker:
                continue
            stats["coins"] += 1
            first_check = cid not in watch
            if first_check:
                watch[cid] = ExchangeWatch(coin_id=cid, since=today)
                session.add(watch[cid])
            for slug in fresh:
                row = existing.get((cid, slug))
                listed_now = row is not None and row.delisted_on is None
                top = best_pair(by_base.get((slug, ticker), []), ticker, c.price_usd,
                                min_volume=0 if listed_now else MIN_NEW_VOLUME)
                if top:
                    stats["listings"] += 1
                    known = first_check or slug not in had_before or (slug, ticker) in bases_before
                    if row is None:
                        baseline = known
                        row = ExchangeListing(coin_id=cid, exchange=slug, first_seen=today, baseline=baseline)
                        session.add(row)
                        existing[(cid, slug)] = row
                        if not baseline:
                            stats["new"].append((c.name, EXCHANGES[slug], f"{ticker}/{top['quote']}"))
                    elif row.delisted_on is not None:             # listed again
                        row.delisted_on = None
                        if not known:
                            row.first_seen, row.baseline = today, False
                            stats["new"].append((c.name, EXCHANGES[slug], f"{ticker}/{top['quote']}"))
                    row.last_seen = today
                    row.top_pair, row.volume_24h = f"{ticker}/{top['quote']}", top["volume_24h"]
                    row.price_usd, row.link = top["price_usd"], top["link"]
                elif (row is not None and row.delisted_on is None and row.last_seen < today
                      and (slug, ticker) not in by_base):              # ticker gone from the exchange
                    row.delisted_on = today
                    stats["delisted"].append((c.name, EXCHANGES[slug]))
        # Coins deleted from our database (e.g. 7 days after being dropped).
        gone = [cid for (cid,) in session.query(ExchangeWatch.coin_id) if cid not in coins]
        if gone:
            stats["cleaned"] = session.query(ExchangeListing).filter(ExchangeListing.coin_id.in_(gone)).delete(
                synchronize_session=False)
            session.query(ExchangeWatch).filter(ExchangeWatch.coin_id.in_(gone)).delete(synchronize_session=False)
        session.commit()

        # 4. Upcoming Binance listings for our coins (ticker match).
        for name, ex, when, _ in upcoming_for(session, {cid: t for cid, t in tick.items() if t}, coins):
            stats["scheduled"].append((name, ex, when))
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    log.info("Exchange listings: %d exchanges read (%d failed), %d pairs, %d DropsTab requests; "
             "%d coins checked, %d listings, %d new, %d delisted, %d scheduled; %d rows of deleted coins removed.",
             stats["exchanges_ok"], len(stats["exchanges_failed"]), stats["pairs"], stats["calls"],
             stats["coins"], stats["listings"], len(stats["new"]), len(stats["delisted"]), len(stats["scheduled"]),
             stats.get("cleaned", 0))
    return stats


# ---------------------------------------------------------------- reading
def _upcoming(session, now=None):
    """Binance announcements whose trading hasn't started yet (or started
    today / has no time and was announced in the last 7 days)."""
    from models import ExchangeAnnouncement
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    out = []
    for a in session.query(ExchangeAnnouncement):
        if a.listing_at is not None:
            if a.listing_at < now - timedelta(hours=12):
                continue
        elif a.released_at is None or a.released_at < now - timedelta(days=7):
            continue
        out.append(a)
    return out


def _announcement_dict(a):
    return {"exchange": EXCHANGES.get(a.exchange, a.exchange.title()), "title": a.title,
            "at": a.listing_at.strftime("%Y-%m-%dT%H:%M:00Z") if a.listing_at else None,
            "url": a.url}


def upcoming_for(session, tickers, coins=None):
    """[(coin name, exchange, listing time or None, coin_id)] for coins whose
    ticker is in an upcoming announcement."""
    anns = _upcoming(session)
    out = []
    for cid, t in tickers.items():
        for a in anns:
            if t in json.loads(a.tickers or "[]"):
                name = coins[cid].name if coins and cid in coins else t
                out.append((name, EXCHANGES.get(a.exchange, a.exchange), a.listing_at, cid))
    return out


def badges(session, coin_ids):
    """{coin_id: [{"kind": "new"|"scheduled", "exchange", "date"}]} for the
    tables (🆕 first seen in the last NEW_DAYS days, not baseline; 📅 an
    upcoming Binance listing). Never raises."""
    from models import ExchangeListing
    out = {}
    try:
        since = datetime.now(timezone.utc).date() - timedelta(days=NEW_DAYS)
        for l in (session.query(ExchangeListing)
                  .filter(ExchangeListing.coin_id.in_(list(coin_ids)), ExchangeListing.baseline.is_(False),
                          ExchangeListing.delisted_on.is_(None), ExchangeListing.first_seen >= since)):
            out.setdefault(l.coin_id, []).append({"kind": "new", "exchange": EXCHANGES.get(l.exchange, l.exchange),
                                                  "date": l.first_seen.isoformat()})
        tick = {cid: t for cid, t in _tickers(session, coin_ids).items() if t} if _upcoming(session) else {}
        for _, ex, when, cid in upcoming_for(session, tick):
            out.setdefault(cid, []).append({"kind": "scheduled", "exchange": ex,
                                            "date": when.strftime("%Y-%m-%dT%H:%M:00Z") if when else None})
    except Exception:
        session.rollback()
    return out


def get_listings(session, coin, ticker=None):
    """Detail page / Check a Coin (tracked coin): the stored card.
    status "ok" -> listed [{exchange, first_seen, baseline, top_pair,
    volume, link}], delisted [...last 30 days], scheduled [...];
    status "pending" before the first daily run with this feature."""
    from models import ExchangeListing, ExchangeWatch
    try:
        w = session.query(ExchangeWatch).filter_by(coin_id=coin.id).first()
        rows = session.query(ExchangeListing).filter_by(coin_id=coin.id).all()
        if ticker is None:
            ticker = _tickers(session, [coin.id]).get(coin.id)
        sched = [_announcement_dict(a) for a in _upcoming(session) if ticker and ticker in json.loads(a.tickers or "[]")]
    except Exception:
        session.rollback()
        return {"status": "pending", "monitored": len(EXCHANGES)}
    if w is None:
        return {"status": "pending", "monitored": len(EXCHANGES), "scheduled": sched}
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=DELISTED_SHOW_DAYS)
    listed = sorted(({"exchange": EXCHANGES.get(r.exchange, r.exchange), "first_seen": r.first_seen.isoformat(),
                      "baseline": r.baseline, "top_pair": r.top_pair, "volume": r.volume_24h, "link": r.link}
                     for r in rows if r.delisted_on is None), key=lambda x: -(x["volume"] or 0))
    delisted = [{"exchange": EXCHANGES.get(r.exchange, r.exchange), "date": r.delisted_on.isoformat()}
                for r in rows if r.delisted_on is not None and r.delisted_on >= cutoff]
    return {"status": "ok", "monitored": len(EXCHANGES), "since": w.since.isoformat(),
            "listed": listed, "delisted": delisted, "scheduled": sched, "new_days": NEW_DAYS}


def listings_live(ticker, price, contract=None):
    """Check a Coin. A coin we track (active / dropped) -> its stored card.
    Otherwise the stored pair lists of the 12 exchanges are searched by
    ticker + price (0 API calls; as of the last daily run). Never raises."""
    from sqlalchemy import func
    from database import SessionLocal
    from models import Coin, ExchangePair
    session = SessionLocal()
    try:
        if contract:
            coin = (session.query(Coin).filter(func.lower(Coin.contract) == contract.lower(),
                                               Coin.status.in_(["active", "dropped"])).first())
            if coin is not None:
                stored = get_listings(session, coin)
                if stored["status"] == "ok":
                    return {**stored, "tracked": True}
        t = (ticker or "").upper()
        sched = [_announcement_dict(a) for a in _upcoming(session) if t and t in json.loads(a.tickers or "[]")]
        rows = session.query(ExchangePair).filter(ExchangePair.base == t).all() if t else []
        day = session.query(func.max(ExchangePair.day)).scalar()
        if day is None:
            return {"status": "pending", "monitored": len(EXCHANGES), "scheduled": sched}
        listed = []
        for slug, name in EXCHANGES.items():
            top = best_pair([{"base": r.base, "quote": r.quote, "price_usd": r.price_usd,
                              "volume_24h": r.volume_24h, "link": r.link} for r in rows if r.exchange == slug], t, price)
            if top:
                listed.append({"exchange": name, "first_seen": None, "baseline": True,
                               "top_pair": f"{t}/{top['quote']}", "volume": top["volume_24h"], "link": top["link"]})
        listed.sort(key=lambda x: -(x["volume"] or 0))
        return {"status": "ok", "monitored": len(EXCHANGES), "listed": listed, "delisted": [],
                "scheduled": sched, "as_of": day.isoformat(), "live": True}
    except Exception as e:
        session.rollback()
        log.warning("Listings lookup failed (%s)", type(e).__name__)
        return {"status": "pending", "monitored": len(EXCHANGES)}
    finally:
        session.close()
