"""
queue_view.py
-------------
The dashboard's "Coming up" table: the next coins in the entry queue
(rotation.py) - coins that already passed discovery, meme/stablecoin removal
and age, and are waiting for the expensive steps (revenue -> volume ->
honeypot -> liquidity).

DISPLAY ONLY. Nothing here makes an API call or changes which coins get
picked - it just mirrors rotation.pick_batch's order:
  1. never-analyzed coins, oldest-waiting first (first_seen, then id)
  2. then coins due their 15-day second chance (oldest check first)
ROTATION_BUDGET (300) are analyzed per daily run, so the first 300 rows are
the next run's batch and rows 301-600 the run after.

Only coins seen in the most recent run are shown (the queue only picks from
the coins discovered that run). Coins discovered in a later run join the
never-analyzed part, so the second batch is an estimate.
"""

from rotation import ROTATION_BUDGET, _now, _recheck_due

# Narrative from CMC tags (already downloaded in discovery - no extra call).
# Most specific first: the FIRST group with a matching tag wins. Tags not
# listed here (ecosystems, exchange listings, VC portfolios, airdrops...)
# say nothing about what the project does, so they're ignored.
NARRATIVE_TAGS = [
    ("AI Agents", {"ai-agents"}),
    ("DeFAI", {"defai"}),
    ("AI", {"ai-big-data", "generative-ai", "ai-applications", "bittensor-subnets"}),
    ("DePIN", {"depin", "iot"}),
    ("Compute", {"distributed-computing"}),
    ("Storage", {"storage", "filesharing"}),
    ("Tokenized Stock", {"tokenized-stock", "tokenized-etfs", "bstocks"}),
    ("RWA", {"real-world-assets-protocols", "tokenized-assets"}),
    ("Perps", {"perpetuals"}),
    ("Derivatives", {"derivatives"}),
    ("DEX", {"decentralized-exchange-dex-token", "dex", "amm"}),
    ("Lending", {"lending-borowing"}),
    ("Restaking", {"restaking"}),
    ("Liquid Staking", {"liquid-staking-derivatives"}),
    ("Yield", {"yield-farming", "yield-aggregator"}),
    ("Oracle", {"oracles"}),
    ("ZK", {"zero-knowledge-proofs"}),
    ("Privacy", {"privacy", "privacy-coins", "privacy-blockchain"}),
    ("Gaming", {"gaming", "play-to-earn"}),
    ("Metaverse", {"metaverse"}),
    ("NFT", {"collectibles-nfts"}),
    ("Layer 2", {"layer-2", "rollups", "scaling"}),
    ("Layer 1", {"layer-1"}),
    ("Interoperability", {"interoperability", "cross-chain"}),
    ("Modular", {"modular-blockchain"}),
    ("Payments", {"payments"}),
    ("Wallet", {"wallet"}),
    ("Launchpad", {"launchpad"}),
    ("Exchange", {"centralized-exchange"}),
    ("Social", {"communications-social-media"}),
    ("Fan Token", {"fan-token", "sports"}),
    ("Identity", {"identity"}),
    ("Media", {"entertainment", "media", "content-creation"}),
    ("Marketplace", {"marketplace"}),
    ("Asset Management", {"asset-management"}),
    ("DeFi", {"defi"}),
    ("Governance", {"dao", "governance"}),
    ("Web3", {"web3", "dapp"}),
    ("Platform", {"platform", "smart-contracts"}),
]


def narrative_from_tags(tags):
    """Most meaningful label for a coin's CMC tags, or None."""
    tags = set(tags or [])
    for label, slugs in NARRATIVE_TAGS:
        if tags & slugs:
            return label
    return None


QUEUE_DISPLAY_RUNS = 2                      # show the next 2 runs' batches
QUEUE_DISPLAY_LIMIT = ROTATION_BUDGET * QUEUE_DISPLAY_RUNS   # = 600


def remember_queue_display(session, tokens, when=None):
    """Copy name / symbol / market cap / CMC-tag narrative (already in hand
    from discovery) onto each queued coin's row, and stamp last_seen. No API
    calls."""
    from models import CoinQueue

    when = when or _now()
    by_key = {((t["chain"] or "").lower(), (t["contract"] or "").lower()): t for t in tokens}
    for row in session.query(CoinQueue):
        t = by_key.get((row.chain, row.contract))
        if t is None:
            continue
        row.name = t.get("name") or row.name
        row.symbol = t.get("symbol") or row.symbol
        mcap = t.get("discovery_mcap") or t.get("market_cap")
        try:
            row.mcap = float(mcap) if mcap is not None else row.mcap
        except (TypeError, ValueError):
            pass
        row.narrative = narrative_from_tags(t.get("cmc_tags")) or row.narrative
        row.last_seen = when


def upcoming(session, limit=QUEUE_DISPLAY_LIMIT, now=None):
    """The next `limit` queued coins, in the order the daily runs pick them,
    as dicts with the 5 display fields (+ position / run)."""
    from models import Coin, CoinQueue

    now = now or _now()
    rows = session.query(CoinQueue).filter(CoinQueue.last_seen.isnot(None)).all()
    if not rows:
        return []
    # Only coins discovered in the latest run (the queue picks from those).
    latest_run = max(r.last_seen for r in rows).date()
    rows = [r for r in rows if r.last_seen.date() == latest_run]

    fresh = sorted((r for r in rows if r.last_checked is None),
                   key=lambda r: (r.first_seen, r.id))
    due = sorted((r for r in rows if _recheck_due(r, now)),
                 key=lambda r: (r.last_checked, r.id))
    nxt = (fresh + due)[:limit]

    # Narrative = from its CMC tags; else the CoinGecko category we already
    # stored on a coin (if any); else "unknown".
    cats = {(c.contract or "").lower(): c.category for c in
            session.query(Coin.contract, Coin.category)
            .filter(Coin.category.isnot(None), Coin.category != "")}

    out = []
    for i, r in enumerate(nxt):
        label = r.symbol or r.name
        if r.symbol and r.name and r.name.lower() != r.symbol.lower():
            label = f"{r.symbol} / {r.name}"
        out.append({
            "position": i + 1,
            "run": "next run" if i < ROTATION_BUDGET else "run after",
            "name": label or "unknown",
            "chain": r.chain,
            "narrative": r.narrative or cats.get(r.contract) or "unknown",
            "contract": r.contract,
            "mcap": r.mcap,
        })
    return out
