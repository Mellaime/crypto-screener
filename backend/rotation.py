"""
rotation.py
-----------
The ENTRY queue for the expensive per-coin filter steps (revenue -> volume ->
honeypot -> liquidity).

The filter is a ONE-TIME entry gate: every coin gets these expensive steps -
including its single CoinGecko volume check - exactly ONCE. Once a coin has
been analyzed it is never analyzed again (whether it entered active or not);
from then on, coins in active / dropped are tracked daily by Codex instead
(tracking.py + drop_logic.py). So CoinGecko is only ever spent on NEW coins,
and as the market gets discovered the daily CoinGecko use shrinks.

Each run still fetches the full candidate list, but already-analyzed coins are
skipped right away (before age). Of the rest, up to ROTATION_BUDGET per run are
picked from a single first-come-first-served queue (the coin_queue table):
  * a never-seen coin is inserted with first_seen = now, last_checked = NULL
  * picks are in arrival order (first_seen, then insertion order), so a new
    coin waits behind every older un-analyzed coin
  * a picked coin gets last_checked = now = analyzed - EXCEPT when its
    CoinGecko check failed transiently (network / rate limit): then it stays
    un-analyzed and gets another try next run.

SECOND CHANCE: a coin that was REJECTED (failed revenue / volume / honeypot /
liquidity) or DELETED by drop_logic after its 7 unhealthy days becomes
eligible again RECHECK_AFTER_DAYS (15) after that, for ONE more pass through
the entry filter (MAX_ATTEMPTS = 2 analyses in total). If it fails again it's
locked for good. Never-analyzed coins are always picked before re-checks.
(Coins that fail AGE never enter this queue, so they are simply re-checked
every run until they're old enough - unchanged.)
"""

import hashlib
from datetime import datetime, timedelta, timezone

# =========================================================================
# ENTRY BUDGET  <-- change this number to speed up / slow down entry analysis
# =========================================================================
# Never-analyzed coins that get the expensive steps per run (the daily job
# runs once every 24h, so this is also coins-per-day). The volume step costs
# one CoinGecko call per coin; CoinGecko's free (demo) tier allows 10,000
# calls/month. Once the backlog of never-analyzed coins is cleared, a run only
# analyzes the day's NEW coins, usually far fewer than this.
ROTATION_BUDGET = 300

RECHECK_AFTER_DAYS = 15   # rejected / deleted -> eligible again after this
MAX_ATTEMPTS = 2          # the first analysis + ONE second chance


def _key(chain, contract):
    return ((chain or "").lower(), (contract or "").lower())


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)   # naive UTC, like utcnow()


def _recheck_due(row, now):
    """True if an analyzed coin is due its one 15-day second chance."""
    return (row.last_checked is not None
            and row.outcome in ("rejected", "deleted")
            and (row.attempts or 0) < MAX_ATTEMPTS
            and row.last_checked <= now - timedelta(days=RECHECK_AFTER_DAYS))


def analyzed_keys(session, now=None):
    """(chain, contract) of every analyzed coin that is NOT due a second
    chance right now - skipped this run (before age)."""
    from models import CoinQueue
    now = now or _now()
    return {(r.chain, r.contract) for r in
            session.query(CoinQueue).filter(CoinQueue.last_checked.isnot(None))
            if not _recheck_due(r, now)}


def is_analyzed(token, analyzed):
    return _key(token["chain"], token["contract"]) in analyzed


def pick_batch(session, tokens, budget=ROTATION_BUDGET, now=None):
    """
    Queue any never-seen tokens at the back, then return up to `budget`
    tokens: NEVER-ANALYZED ones first (arrival order), then coins due their
    15-day second chance (oldest check first). Returns (picked_tokens, stats),
    stats = {"queued": waiting incl. picked, "new": first seen this run,
             "rechecks": second-chance coins picked}.
    `now` defaults to the current time (it's settable only for tests).
    """
    from models import CoinQueue

    by_key = {}
    for t in tokens:
        by_key.setdefault(_key(t["chain"], t["contract"]), t)

    # The session doesn't autoflush: write pending changes first, so queue
    # rows added earlier in this run (rehome_coin adds the new chain's row)
    # are seen here and not inserted a second time (duplicate-key crash).
    session.flush()
    rows = {(r.chain, r.contract): r for r in session.query(CoinQueue)}
    new_keys = [k for k in by_key if k not in rows]

    # Insert new coins at the back. Within one run they are inserted in a
    # stable hash order, not discovery order, so a batch isn't all one chain
    # or all the biggest market caps.
    now = now or _now()
    for k in sorted(new_keys, key=lambda k: hashlib.md5(f"{k[0]}:{k[1]}".encode()).hexdigest()):
        row = CoinQueue(chain=k[0], contract=k[1], first_seen=now, last_checked=None)
        session.add(row)
        rows[k] = row
    session.flush()                                   # assigns ids = insertion order

    fresh = sorted((k for k in by_key if rows[k].last_checked is None),
                   key=lambda k: (rows[k].first_seen, rows[k].id))
    due = sorted((k for k in by_key if _recheck_due(rows[k], now)),
                 key=lambda k: (rows[k].last_checked, rows[k].id))
    ordered = (fresh + due)[:budget]
    picked = [by_key[k] for k in ordered]
    rechecks = sum(1 for k in ordered if rows[k].last_checked is not None)
    return picked, {"queued": len(fresh) + len(due), "new": len(new_keys),
                    "rechecks": rechecks}


def mark_checked(session, tokens, outcome, when=None):
    """Mark tokens as analyzed with their outcome ("saved" / "rejected")."""
    from models import CoinQueue

    when = when or _now()
    for t in tokens:
        ch, ct = _key(t["chain"], t["contract"])
        row = session.query(CoinQueue).filter_by(chain=ch, contract=ct).first()
        if row is not None:
            row.last_checked = when
            row.outcome = outcome
            row.attempts = (row.attempts or 0) + 1


def mark_deleted(session, chain, contract, when=None):
    """drop_logic deleted this coin (7 unhealthy days): it may come back
    through the entry filter once, RECHECK_AFTER_DAYS after the deletion."""
    from models import CoinQueue

    when = when or _now()
    ch, ct = _key(chain, contract)
    row = session.query(CoinQueue).filter_by(chain=ch, contract=ct).first()
    if row is None:
        row = CoinQueue(chain=ch, contract=ct, first_seen=when, attempts=1)
        session.add(row)
    row.last_checked = when            # the 15-day clock starts at deletion
    row.outcome = "deleted"
    row.attempts = max(row.attempts or 0, 1)
