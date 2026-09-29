"""
rotation.py
-----------
ONE rotation queue for the expensive per-coin steps (revenue -> volume ->
honeypot -> liquidity).

Every run still fetches the full candidate list and applies the cheap steps
(meme/stablecoin filter, age) to all of it. But the expensive steps only run on
ROTATION_BUDGET age-passers per run, taken first-come-first-served from a
single queue stored in the coin_queue table:

  * NEVER-ANALYZED coins go first, in arrival order (first_seen, then
    insertion order). A NEW coin is inserted with first_seen = now and
    last_checked = NULL, so it waits behind every OLDER un-analyzed coin -
    but ahead of repeat analyses.
  * then RE-CHECKS of already-analyzed coins, the one analyzed longest ago
    first.
  * every coin picked in a run gets last_checked = now, so its next turn is a
    re-check.

So each coin gets the expensive checks about once per cycle:
    cycle length (days) ~= queued age-passers / ROTATION_BUDGET
"""

import hashlib
from datetime import datetime, timezone

# =========================================================================
# ROTATION BUDGET  <-- change this number to speed up / slow down the cycle
# =========================================================================
# Coins that get the expensive steps (revenue, volume, honeypot, liquidity)
# per run. The daily job runs once every 24h, so this is also coins-per-day.
# The volume step costs one CoinGecko call per coin, and CoinGecko's free
# (demo) tier allows 10,000 calls/month - check the monthly total before
# raising this. Raise it freely on a paid CoinGecko plan.
ROTATION_BUDGET = 300


def _key(chain, contract):
    return ((chain or "").lower(), (contract or "").lower())


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)   # naive UTC, like utcnow()


def pick_batch(session, tokens, budget=ROTATION_BUDGET, now=None):
    """
    Queue any never-seen tokens at the back, then return the `budget` tokens
    that have waited longest (oldest last_checked / first_seen first).
    Returns (picked_tokens, stats) where stats = {"queued", "new", "known"}.
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

    # Never-analyzed coins first, in arrival order (first_seen, then insertion
    # id); only then re-checks, the one analyzed longest ago first.
    ordered = sorted(by_key, key=lambda k: (rows[k].last_checked is not None,
                                            rows[k].last_checked or rows[k].first_seen,
                                            rows[k].id))
    picked = [by_key[k] for k in ordered[:budget]]
    return picked, {"queued": len(by_key), "new": len(new_keys),
                    "known": len(by_key) - len(new_keys)}


def mark_checked(session, tokens, when=None):
    """Send the given tokens to the back of the queue (last_checked = now)."""
    from models import CoinQueue

    when = when or _now()
    for t in tokens:
        ch, ct = _key(t["chain"], t["contract"])
        row = session.query(CoinQueue).filter_by(chain=ch, contract=ct).first()
        if row is not None:
            row.last_checked = when
