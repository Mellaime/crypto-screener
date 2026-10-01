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
  * a picked coin gets last_checked = now = analyzed, done for good - EXCEPT
    when its CoinGecko check failed transiently (network / rate limit): then
    it stays un-analyzed and gets another try next run.
"""

import hashlib
from datetime import datetime, timezone

# =========================================================================
# ENTRY BUDGET  <-- change this number to speed up / slow down entry analysis
# =========================================================================
# Never-analyzed coins that get the expensive steps per run (the daily job
# runs once every 24h, so this is also coins-per-day). The volume step costs
# one CoinGecko call per coin; CoinGecko's free (demo) tier allows 10,000
# calls/month. Once the backlog of never-analyzed coins is cleared, a run only
# analyzes the day's NEW coins, usually far fewer than this.
ROTATION_BUDGET = 300


def _key(chain, contract):
    return ((chain or "").lower(), (contract or "").lower())


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)   # naive UTC, like utcnow()


def analyzed_keys(session):
    """(chain, contract) of every coin already analyzed - skipped for good."""
    from models import CoinQueue
    return {(r.chain, r.contract) for r in
            session.query(CoinQueue.chain, CoinQueue.contract)
            .filter(CoinQueue.last_checked.isnot(None))}


def is_analyzed(token, analyzed):
    return _key(token["chain"], token["contract"]) in analyzed


def pick_batch(session, tokens, budget=ROTATION_BUDGET, now=None):
    """
    Queue any never-seen tokens at the back, then return up to `budget`
    NEVER-ANALYZED tokens, in arrival order. Returns (picked_tokens, stats),
    stats = {"queued": waiting incl. picked, "new": first seen this run}.
    `now` defaults to the current time (it's settable only for tests).
    """
    from models import CoinQueue

    by_key = {}
    for t in tokens:
        by_key.setdefault(_key(t["chain"], t["contract"]), t)

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

    waiting = [k for k in by_key if rows[k].last_checked is None]
    ordered = sorted(waiting, key=lambda k: (rows[k].first_seen, rows[k].id))
    picked = [by_key[k] for k in ordered[:budget]]
    return picked, {"queued": len(waiting), "new": len(new_keys)}


def mark_checked(session, tokens, when=None):
    """Mark tokens as analyzed - they will never be picked again."""
    from models import CoinQueue

    when = when or _now()
    for t in tokens:
        ch, ct = _key(t["chain"], t["contract"])
        row = session.query(CoinQueue).filter_by(chain=ch, contract=ct).first()
        if row is not None:
            row.last_checked = when
