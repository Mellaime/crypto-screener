"""
dropstab.py
-----------
"Upcoming Unlocks" on the coin detail page, from the DropsTab API
(DROPSTAB_API_KEY; header x-dropstab-api-key). DISPLAY ONLY - the filter,
drop logic and score never use it.

Our plan only allows DropsTab's unlock OVERVIEW (/tokenUnlocks - per coin:
allocations with locked / unlocked amounts and the next / last unlock date);
the per-coin schedule (/tokenUnlocks/{slug}) is 403. So the size of the next
unlock event is COMPUTED (it matches DropsTab's own site - ZK: 172.88M =
Team 83.59M + Investors 89.30M):
  * next event = the earliest future nextTokenUnlockDate across allocations;
    the allocations sharing it are its "rounds"
  * each round's amount = its locked tokens / the unlocks left between its
    next and last date (monthly steps, e.g. ZK: 21 from Oct 2026 to Jun 2028;
    daily steps for linear vesting; 1 for a single remaining unlock)
  * a date that has already passed is rolled forward (Oct 17 -> Nov 17), so
    the page always shows the NEXT unlock, never a past one.
  * LINEAR (daily) when the next and last date are ~1 day apart, or the next
    date moved by ~1 day since the previous run; otherwise monthly / cliff
    (no daily badge). Can't tell -> no badge.

Daily job (run_unlock_update): our coins (active + dropped) are matched to
DropsTab by CONTRACT once (/coins/contract/{contract}, stored in
dropstab_coins), then the whole overview is read (~11 pages of 100) and each
matched coin's entry is stored in unlock_snapshots. A page view reads only
those tables - 0 DropsTab calls.
"""

import os
import json
import time
import logging
from calendar import monthrange
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
log = logging.getLogger("dropstab")

BASE_URL = "https://public-api.dropstab.com/api/v1"
PAGE_SIZE = 100            # the overview's maximum
REQUEST_DELAY = 0.7        # 100 requests/minute allowed
RECHECK_DAYS = 30          # "not on DropsTab" contracts are asked again after this
MAP_BUDGET = 300           # contract lookups per run (first run maps everything)
PERIOD_LIMIT = 500_000     # requests per ~30-day period on our plan
REMAINING = None           # DropsTab's own counter (x-rate-limit-period-remaining)


def _key():
    return os.getenv("DROPSTAB_API_KEY")


class DropstabKeyRejected(RuntimeError):
    """DropsTab refused the API key (HTTP 401 / 403)."""


def _check_auth(status, body, what):
    """Stop LOUDLY when DropsTab rejects the key. The two endpoints the bot
    uses (/coins/contract, /tokenUnlocks) are on our plan, so a 401 / 403
    there means a bad DROPSTAB_API_KEY - not something to retry quietly.
    The exception reaches scheduler.py, which logs it and flags the step in
    the Telegram report. The key itself is never logged."""
    if status in (401, 403):
        detail = ((body or {}).get("failureDetails") or "")[:120]
        log.error("DropsTab REJECTED the API key on %s (HTTP %s: %s) - check the "
                  "DROPSTAB_API_KEY secret (only the key, no name / quotes / spaces)",
                  what, status, detail)
        raise DropstabKeyRejected(f"DropsTab rejected DROPSTAB_API_KEY (HTTP {status}: {detail})")


def _get(path, **params):
    """(status, json or None). Never raises; the key is never logged."""
    global REMAINING
    time.sleep(REQUEST_DELAY)
    try:
        r = requests.get(BASE_URL + path, params=params,
                         headers={"x-dropstab-api-key": _key()}, timeout=40)
    except requests.exceptions.RequestException as e:
        return f"error {type(e).__name__}", None
    REMAINING = r.headers.get("x-rate-limit-period-remaining", REMAINING)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


# ---------------------------------------------------------------- daily job
def map_coins(session, coins, now):
    """Look up the DropsTab id of every coin not mapped yet (or "not found"
    more than RECHECK_DAYS ago), at most MAP_BUDGET per run. Returns calls."""
    from models import DropstabCoin
    have = {(r.chain, r.contract): r for r in session.query(DropstabCoin)}
    calls = 0
    for coin in coins:
        key = ((coin.chain or "").lower(), (coin.contract or "").lower())
        row = have.get(key)
        if row is not None and (row.dropstab_id is not None
                                or row.checked_at > now - timedelta(days=RECHECK_DAYS)):
            continue
        if calls >= MAP_BUDGET:
            break
        calls += 1
        st, body = _get(f"/coins/contract/{coin.contract}")
        _check_auth(st, body, "/coins/contract")
        if st == 404 or (st == 200 and not (body or {}).get("data")):
            found = None                              # DropsTab doesn't know it
        elif st == 200:
            found = body["data"][0]
        else:
            log.warning("DropsTab lookup for %s failed (HTTP %s) - retried next run",
                        coin.contract, st)
            continue                                  # transient -> next run
        if row is None:
            row = DropstabCoin(chain=key[0], contract=key[1])
            session.add(row)
            have[key] = row
        row.dropstab_id = found["id"] if found else None
        row.slug = found["slug"] if found else None
        row.symbol = found["symbol"] if found else None
        row.checked_at = now
    session.commit()
    return calls


def fetch_overview():
    """{dropstab coin id: overview entry} for every coin with unlock data, or
    None if a page failed (then nothing is replaced today). Returns
    (entries, calls)."""
    out, page, calls = {}, 0, 0
    while True:
        calls += 1
        st, body = _get("/tokenUnlocks", page=page, pageSize=PAGE_SIZE)
        _check_auth(st, body, "/tokenUnlocks")
        data = (body or {}).get("data") if st == 200 else None
        if not data:
            log.warning("DropsTab unlock overview page %d failed (HTTP %s)", page, st)
            return None, calls
        for e in data.get("content") or []:
            out[e["coinId"]] = e
        if page + 1 >= (data.get("totalPages") or 0):
            return out, calls
        page += 1


def run_unlock_update():
    """Daily job step: map new coins, refresh unlock_snapshots for our coins."""
    from database import SessionLocal, utcnow
    from models import Coin, DropstabCoin, UnlockSnapshot
    stats = {"mapped_calls": 0, "overview_calls": 0, "matched": 0, "with_unlocks": 0, "stored": 0,
             "all_stored": 0, "all_removed": 0}
    if not _key():
        log.warning("DropsTab unlocks skipped - DROPSTAB_API_KEY not set")
        return stats
    now = utcnow()
    session = SessionLocal()
    try:
        coins = session.query(Coin).filter(Coin.status.in_(["active", "dropped"])).all()
        stats["mapped_calls"] = map_coins(session, coins, now)
        keys = {((c.chain or "").lower(), (c.contract or "").lower()) for c in coins}
        ids = {r.dropstab_id for r in session.query(DropstabCoin)
               if (r.chain, r.contract) in keys and r.dropstab_id is not None}
        stats["matched"] = len(ids)
        entries, stats["overview_calls"] = fetch_overview()
        if entries is None:
            return stats
        rows = {r.dropstab_id: r for r in session.query(UnlockSnapshot)}
        today = now.date()
        for cid in ids:
            entry = entries.get(cid)
            if entry is None:
                continue
            stats["with_unlocks"] += 1
            row = rows.get(cid)
            if row is None:
                row = UnlockSnapshot(dropstab_id=cid)
                session.add(row)
            elif row.day < today:           # keep the previous DAY's dates (linear check)
                old = json.loads(row.data)
                row.prev_next = json.dumps({str(a["id"]): (a.get("tokenUnlockProgress") or {}).get("nextTokenUnlockDate")
                                            for a in old.get("allocations") or []})
            row.data, row.day, row.updated_at = json.dumps(entry), today, now
            stats["stored"] += 1
        session.commit()
        # The Token Unlocks page: EVERY overview entry (same request, no calls).
        stats["all_stored"], stats["all_removed"] = _store_all(session, entries, now)
        save_unlocks_display(session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    print("=" * 52)
    print("  UPCOMING UNLOCKS (DropsTab)")
    print("=" * 52)
    print(f"  coins matched to DropsTab ...... {stats['matched']:>5}   (new lookups: {stats['mapped_calls']})")
    print(f"  with unlock data (stored) ...... {stats['stored']:>5}   (overview pages: {stats['overview_calls']})")
    print(f"  Token Unlocks page (all coins) . {stats['all_stored']:>5}   (removed, gone from DropsTab: {stats['all_removed']})")
    print(f"  DropsTab requests this run ..... {stats['mapped_calls'] + stats['overview_calls']:>5}"
          f"   (period remaining: {REMAINING})")
    print("=" * 52)
    return stats


ALL_KEEP_DAYS = 2          # Token Unlocks page rows not refreshed for this long are removed


def _store_all(session, entries, now):
    """UPSERT every overview entry into dropstab_unlocks_all; delete rows not
    refreshed for ALL_KEEP_DAYS. Returns (stored, removed)."""
    from models import DropstabUnlockAll
    # Only the ids are read (not the ~4 MB of stored records): existing rows
    # are updated, new ones inserted, in bulk.
    have = dict(session.query(DropstabUnlockAll.dropstab_id, DropstabUnlockAll.id))
    updates, inserts = [], []
    for cid, e in entries.items():
        slug = e.get("coinSlug") or ""
        # The overview has no name: built from the slug ("the-graph" -> "The Graph").
        vals = {"dropstab_id": cid, "slug": slug, "symbol": e.get("coinSymbol"),
                "name": " ".join(w[:1].upper() + w[1:] for w in slug.split("-") if w) or e.get("coinSymbol"),
                "data": json.dumps(e), "day": now.date(), "updated_at": now}
        if cid in have:
            updates.append({**vals, "id": have[cid]})
        else:
            inserts.append(vals)
    if updates:
        session.bulk_update_mappings(DropstabUnlockAll, updates)
    if inserts:
        session.bulk_insert_mappings(DropstabUnlockAll, inserts)
    removed = (session.query(DropstabUnlockAll)
               .filter(DropstabUnlockAll.updated_at < now - timedelta(days=ALL_KEEP_DAYS))
               .delete(synchronize_session=False))
    session.commit()
    return len(entries), removed


# ---------------------------------------------------------------- the numbers
def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _add_months(d, k):
    m = d.month - 1 + k
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, monthrange(y, m)[1]))


def _same_day_of_month(a, b):
    """True if b falls on a's day of the month (month-end clamping allowed:
    the 31st -> the 30th / 28th)."""
    end_a = a.day == monthrange(a.year, a.month)[1]
    end_b = b.day == monthrange(b.year, b.month)[1]
    return a.day == b.day or (b.day < a.day and end_b) or (end_a and end_b)


def _cadence(nxt, last, prev, now):
    """'single' (one unlock left), 'daily' (linear) or 'monthly'.
    DAILY when any of:
      * next and last are ~1 day apart (the last days of a drip);
      * the next date moved by ~1 day since the previous run;
      * the next unlock is within ~1 day AND falls on a different day of the
        month than the last one - a monthly schedule always keeps its day
        (ZK 17th -> 17th), a daily drip's "next" is simply tomorrow
        (Avantis: next Oct 14, last Apr 23).
    Otherwise monthly; when in doubt, monthly (no daily badge)."""
    if last <= nxt + timedelta(hours=1):
        return "single"
    if last - nxt <= timedelta(days=1, hours=12):
        return "daily"
    if prev is not None and timedelta(hours=20) <= nxt - prev <= timedelta(hours=28):
        return "daily"                       # moved by one day since yesterday's run
    if nxt - now <= timedelta(days=1, hours=12) and not _same_day_of_month(nxt, last):
        return "daily"
    return "monthly"


def _rounds(entry, prev_next, now):
    """Each allocation with a future unlock: its next date (rolled forward
    past dates), cadence and the amount of each of its unlocks."""
    out = []
    for a in entry.get("allocations") or []:
        p = a.get("tokenUnlockProgress") or {}
        nxt, last = _dt(p.get("nextTokenUnlockDate")), _dt(p.get("lastTokenUnlockDate"))
        locked = p.get("lockedTokensAmount") or 0
        if not nxt or not last or locked <= 0:
            continue
        cad = _cadence(nxt, last, _dt((prev_next or {}).get(str(a["id"]))), now)
        if cad == "single":
            steps = 1
        elif cad == "daily":
            steps = max(1, round((last - nxt) / timedelta(days=1)) + 1)
        else:
            steps = max(1, (last.year - nxt.year) * 12 + last.month - nxt.month + 1)
        tranche = locked / steps
        k, when = 0, nxt
        while when <= now:                  # roll a passed date forward
            k += 1
            if k >= steps:
                when = None
                break
            when = nxt + timedelta(days=k) if cad == "daily" else _add_months(nxt, k)
        if when is None or when > last + timedelta(hours=1):
            continue
        out.append({"id": a["id"], "name": a.get("name"), "when": when, "cadence": cad,
                    "amount": tranche, "last": last})
    return out


def compute(entry, prev_next=None, now=None):
    """The section's data from one stored overview entry."""
    now = now or datetime.now(timezone.utc)
    price = entry.get("priceUsd") or 0
    supply = sum(a.get("tokensAllocatedAmount") or 0 for a in entry.get("allocations") or [])
    fdv, mcap = entry.get("fdv"), entry.get("marketCap")
    rounds = _rounds(entry, prev_next, now)
    if not rounds:
        return {"status": "unknown", "reason": "No upcoming unlock on DropsTab"}
    when = min(r["when"] for r in rounds)
    event = sorted((r for r in rounds if abs((r["when"] - when).total_seconds()) < 3600),
                   key=lambda r: -r["amount"])
    amount = sum(r["amount"] for r in event)
    usd = amount * price

    def pct(v, base):
        return v / base * 100 if base else None

    unlocked_p = entry.get("totalTokensUnlockedPercent") or 0
    locked_p = entry.get("totalTokensLockedPercent") or 0
    untracked_p = max(0.0, 100 - unlocked_p - locked_p)
    lasts = [_dt((a.get("tokenUnlockProgress") or {}).get("lastTokenUnlockDate"))
             for a in entry.get("allocations") or []]
    end = max((d for d in lasts if d), default=None)

    def row(p):
        tokens = p / 100 * supply if supply else None
        return {"pct": p, "tokens": tokens, "usd": tokens * price if tokens is not None else None,
                "fdv_pct": pct(tokens * price, fdv) if tokens is not None and fdv else None}

    daily = [r for r in event if r["cadence"] == "daily"]
    return {
        "status": "ok",
        "symbol": entry.get("coinSymbol"),
        "vesting": {"progress_pct": unlocked_p,
                    "end": end.isoformat() if end else None,
                    "days_left": max(0, (end - now).days) if end else None,
                    "unlocked": row(unlocked_p), "locked": row(locked_p), "untracked": row(untracked_p)},
        "next": {"at": when.isoformat(), "amount": amount, "supply_pct": pct(amount, supply),
                 "usd": usd, "mcap_pct": pct(usd, mcap),
                 "rounds": [{"name": r["name"], "amount": r["amount"], "supply_pct": pct(r["amount"], supply),
                             "usd": r["amount"] * price} for r in event],
                 "daily": ({"amount": sum(r["amount"] for r in daily),
                            "usd": sum(r["amount"] for r in daily) * price} if daily else None)},
        "updated": entry.get("updatedAt"),
    }


ALL_CACHE_S = 600          # the Token Unlocks list is cached in memory this long
_all_cache = {"at": 0.0, "rows": None}


def save_unlocks_display(session):
    """Daily run: build the Token Unlocks page's list once and store it
    (unlocks_display_cache) - the page then reads ~0.3 MB instead of the
    ~4.3 MB of raw records. Never raises."""
    from models import UnlocksDisplayCache
    try:
        rows = _build_unlocks(session)
        row = session.get(UnlocksDisplayCache, 1)
        if row is None:
            row = UnlocksDisplayCache(id=1)
            session.add(row)
        row.data, row.computed_at = json.dumps(rows, separators=(",", ":")), datetime.now(timezone.utc).replace(tzinfo=None)
        session.commit()
        _all_cache["rows"] = None
    except Exception as e:
        session.rollback()
        log.warning("Token Unlocks list not stored (%s) - the page builds it itself.", type(e).__name__)


def all_unlocks(session):
    """Token Unlocks page: every stored DropsTab coin with its next unlock
    (compute() - the same numbers as the detail page's card), from the list
    the daily run stored. A coin whose next unlock has passed since then is
    recomputed from its raw record (so the page never shows a past date).
    Cached for ALL_CACHE_S per running process. Never raises (no table -> [])."""
    if _all_cache["rows"] is not None and time.time() - _all_cache["at"] < ALL_CACHE_S:
        return _all_cache["rows"]
    from models import UnlocksDisplayCache
    try:
        cached = session.query(UnlocksDisplayCache.data).filter(UnlocksDisplayCache.id == 1).scalar()
    except Exception:
        session.rollback()
        cached = None
    if not cached:
        out = _build_unlocks(session)                          # nothing stored yet
    else:
        out = json.loads(cached)
        now_iso = datetime.now(timezone.utc).isoformat()
        stale = [r["dropstab_id"] for r in out if r.get("next_unlock") and (r["next_unlock"].get("date") or "") < now_iso]
        if stale:
            fresh = {r["dropstab_id"]: r for r in _build_unlocks(session, ids=stale)}
            out = [fresh.get(r["dropstab_id"], r) for r in out]
    _all_cache["rows"], _all_cache["at"] = out, time.time()
    return out


def _build_unlocks(session, ids=None):
    """The Token Unlocks rows from the raw stored records (all, or `ids`)."""
    from models import DropstabUnlockAll
    try:
        q = session.query(DropstabUnlockAll)
        if ids is not None:
            q = q.filter(DropstabUnlockAll.dropstab_id.in_(list(ids)))
        stored = q.all()
    except Exception:
        session.rollback()
        return []
    now = datetime.now(timezone.utc)
    out = []
    for r in stored:
        try:
            e = json.loads(r.data)
            price, mcap = e.get("priceUsd"), e.get("marketCap")
            total = sum(a.get("tokensAllocatedAmount") or 0 for a in e.get("allocations") or []) or None
            unl = e.get("totalTokensUnlockedPercent")
            circ = (mcap / price if mcap and price
                    else total * unl / 100 if total and unl is not None else None)
            pct = (circ / total * 100 if circ and total else e.get("circulationSupplyPercent"))
            c = compute(e, None, now)
            nxt = None
            if c.get("status") == "ok":
                n = c["next"]
                nxt = {"date": n["at"], "amount": n["amount"], "usd": n["usd"], "supply_pct": n["supply_pct"],
                       "beneficiaries": [x["name"] for x in n["rounds"] if x.get("name")],
                       "daily": bool(n["daily"])}
            out.append({"dropstab_id": r.dropstab_id, "slug": r.slug, "symbol": r.symbol, "name": r.name,
                        "platform": (e.get("icoPlatforms") or [None])[0],
                        "price_usd": price, "circulating": circ, "total": total,
                        "unlocked_pct": None if pct is None else round(min(pct, 100.0), 2),
                        "next_unlock": nxt})
        except Exception as ex:
            log.warning("Token Unlocks: skipped %s (%s)", r.slug, type(ex).__name__)
    return out


def get_unlocks(session, coin):
    """Detail page: the coin's Upcoming Unlocks from stored data (no API
    call). Never raises - missing tables / data -> Unknown."""
    from models import DropstabCoin, UnlockSnapshot
    try:
        m = (session.query(DropstabCoin)
             .filter_by(chain=(coin.chain or "").lower(), contract=(coin.contract or "").lower()).first())
        if m is None or m.dropstab_id is None:
            return {"status": "unknown", "reason": "Not matched on DropsTab"}
        snap = session.query(UnlockSnapshot).filter_by(dropstab_id=m.dropstab_id).first()
        if snap is None:
            return {"status": "unknown", "reason": "No unlock data on DropsTab"}
        return compute(json.loads(snap.data), json.loads(snap.prev_next) if snap.prev_next else None)
    except Exception:
        session.rollback()
        return {"status": "unknown", "reason": "Unlock data not available yet"}
