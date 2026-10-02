"""
models.py
---------
This file defines the TABLES in our database, using SQLAlchemy "models".

Each Python class below = one table. Each attribute = one column.
SQLAlchemy reads these classes and knows how to build the real tables.
"""

from datetime import date

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from database import Base, utcnow


class Coin(Base):
    """
    One row per coin we have ever seen.

    Requested columns: contract, chain, name, first_seen, status
    (plus an 'id' primary key, which every table needs so other tables
    can point to it.)
    """
    __tablename__ = "coins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contract = Column(String, nullable=False, unique=True, index=True)  # the coin's contract address
    chain = Column(String, nullable=False)                              # e.g. "ethereum", "solana"
    name = Column(String, nullable=False)                               # human-readable name
    first_seen = Column(DateTime, default=utcnow)                       # when we first recorded it
    status = Column(String, default="active")                           # "active" / "dropped" / etc.
    # True once the coin has passed ALL filters at least once (a real
    # qualifying run). Coins that never qualified are removed silently and
    # never appear in the dropped_coins table.
    ever_qualified = Column(Boolean, default=False)
    # When it (last) entered the active list - fresh or recovered. drop_logic
    # leaves a coin alone for its first 5 days in active.
    active_since = Column(DateTime, default=utcnow)
    # Holder count when it entered active: the baseline for drop_logic's
    # "holders down > 10%" check. None -> not enough history, check skipped.
    holders_at_entry = Column(Integer)
    # CURRENT USD price (DexScreener, most-liquid pool). Refreshed on EVERY
    # daily run for active + dropped coins (tracking.py) - this is the
    # price the dashboard shows, never more than ~24h old.
    price_usd = Column(Float)
    price_updated_at = Column(DateTime)

    # Security info (from GoPlus, filter #7) - stored so the dashboard shows it.
    security_verdict = Column(String)   # "safe" / "unsafe" / "unknown"
    verified = Column(Boolean)          # contract source code verified / open
    renounced = Column(Boolean)         # ownership renounced
    mint_disabled = Column(Boolean)     # minting disabled (supply can't inflate)

    # Real category/narrative from CoinGecko (e.g. "Meme", "DePIN"); None if
    # CoinGecko doesn't list it -> the UI falls back to a generic sentence.
    category = Column(String)

    # Market rank tracking (filter/scoring #Rank-change). Current-week ranks
    # from each source, plus the averaged change vs last week and a signal.
    cg_rank = Column(Integer)         # CoinGecko market-cap rank this week
    cmc_rank = Column(Integer)        # CoinMarketCap rank this week
    rank_change = Column(Integer)     # avg places moved vs last week (+ = up)
    rank_signal = Column(String)      # "strong_up" / "up" / "neutral" / "down"

    # Convenience links to the related tables (not columns, just Python helpers):
    snapshots = relationship("DailySnapshot", back_populates="coin")
    drops = relationship("DroppedCoin", back_populates="coin")

    def __repr__(self):
        return f"<Coin {self.name} ({self.chain}) status={self.status}>"


class DailySnapshot(Base):
    """
    A daily record of a coin's metrics. One row per coin per day.

    Requested columns: coin_id, date, volume, liquidity, mcap, holders,
    revenue, score
    """
    __tablename__ = "daily_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin_id = Column(Integer, ForeignKey("coins.id"), nullable=False, index=True)
    date = Column(Date, default=date.today, index=True)
    volume = Column(Float)      # 24h trading volume
    liquidity = Column(Float)   # available liquidity
    mcap = Column(Float)        # market capitalization
    holders = Column(Integer)   # number of holders
    revenue = Column(Float)     # protocol/token revenue
    price_usd = Column(Float)   # USD price on that day (DexScreener)
    # Daily Codex readings for active + dropped coins (tracking.py). drop_logic
    # computes its volume + liquidity TRENDS from these, day by day - a
    # separate, consistent source from the display volume/liquidity above.
    codex_liquidity = Column(Float)
    codex_volume24 = Column(Float)
    score = Column(Float)       # our computed ranking score

    coin = relationship("Coin", back_populates="snapshots")

    def __repr__(self):
        return f"<DailySnapshot coin_id={self.coin_id} date={self.date} score={self.score}>"


class DroppedCoin(Base):
    """
    Records coins that got dropped from the screener, and why.

    Requested columns: coin_id, dropped_date, reason
    """
    __tablename__ = "dropped_coins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin_id = Column(Integer, ForeignKey("coins.id"), nullable=False, index=True)
    dropped_date = Column(DateTime, default=utcnow)
    reason = Column(Text)  # free-text explanation, e.g. "liquidity fell below threshold"

    coin = relationship("Coin", back_populates="drops")

    def __repr__(self):
        return f"<DroppedCoin coin_id={self.coin_id} reason={self.reason!r}>"


class RankHistory(Base):
    """
    One market-rank record per coin, per source, per ISO week. This is the
    history that lets us measure rank CHANGE week-over-week.
    """
    __tablename__ = "rank_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin_id = Column(Integer, ForeignKey("coins.id"), nullable=False, index=True)
    source = Column(String, nullable=False)             # "coingecko" / "cmc"
    week = Column(String, nullable=False, index=True)    # ISO year-week "2026-W39"
    rank = Column(Integer)                               # market rank that week
    recorded_date = Column(DateTime, default=utcnow)

    def __repr__(self):
        return f"<RankHistory coin_id={self.coin_id} {self.source} {self.week} #{self.rank}>"


class CoinQueue(Base):
    """
    The single rotation queue for the expensive filter steps (see rotation.py).

    One row per age-passing candidate we've ever seen, keyed by chain +
    contract (not coin_id - most candidates are never saved as coins). Queue
    position = last_checked, or first_seen while the coin has never been
    checked; `id` preserves insertion order for ties.
    """
    __tablename__ = "coin_queue"
    # The same address can exist on several EVM chains -> unique per chain.
    __table_args__ = (UniqueConstraint("chain", "contract"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain = Column(String, nullable=False)
    contract = Column(String, nullable=False, index=True)   # stored lowercased
    first_seen = Column(DateTime, nullable=False)            # when it joined the queue
    last_checked = Column(DateTime)                          # NULL until its first turn
    # Result of its last analysis: "saved" (entered active), "rejected"
    # (failed revenue / volume / honeypot / liquidity) or "deleted" (removed by
    # drop_logic after 7 unhealthy days). rejected / deleted coins get ONE
    # second chance through the entry filter after 15 days (rotation.py).
    outcome = Column(String)
    attempts = Column(Integer, nullable=False, default=0)    # times analyzed
    # DISPLAY ONLY (the dashboard's "Coming up" table) - copied each run from
    # the discovery data already in hand; never used to pick or filter.
    name = Column(String)
    symbol = Column(String)
    mcap = Column(Float)
    narrative = Column(String)         # from its CMC tags (queue_view.py)
    last_seen = Column(DateTime)       # last run it was discovered + passed age

    def __repr__(self):
        return f"<CoinQueue {self.chain}:{self.contract} seen={self.first_seen} checked={self.last_checked}>"


class AgeLookup(Base):
    """
    Persistent cache of the age FALLBACK (CoinMarketCap /info date_added) for
    coins DexScreener can't date - so each coin costs that call at most once,
    not every daily run (see age_check.py).
    listed_at NULL = CMC had no date; asked again after 30 days.
    """
    __tablename__ = "age_lookups"
    __table_args__ = (UniqueConstraint("chain", "contract"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain = Column(String, nullable=False)
    contract = Column(String, nullable=False, index=True)   # stored lowercased
    listed_at = Column(DateTime)                             # NULL = no date found
    checked_at = Column(DateTime, nullable=False)
