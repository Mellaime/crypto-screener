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
    # Global supply (CoinGecko / CMC, from discovery - tracking.py keeps it
    # fresh). Display only: the detail page's Supply section.
    total_supply = Column(Float)
    circulating_supply = Column(Float)
    max_supply = Column(Float)
    supply_uncapped = Column(Boolean)       # CMC infinite_supply; None = not said
    supply_updated_at = Column(DateTime)
    # X / Twitter handle from CoinGecko (socials.py) - display only.
    twitter_handle = Column(String)         # None = no handle on CoinGecko
    twitter_checked_at = Column(DateTime)   # None = never checked yet
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


class TxBarsCache(Base):
    """
    Dashboard detail page: a coin's daily Codex getBars data (15-day bars,
    7d + total volume; tx_bars.py), cached for one UTC day - so opening a coin costs at
    most 1 Codex request per coin per day. Display only.
    """
    __tablename__ = "tx_bars_cache"
    __table_args__ = (UniqueConstraint("chain", "contract"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain = Column(String, nullable=False)
    contract = Column(String, nullable=False, index=True)   # stored lowercased
    day = Column(Date, nullable=False)                       # UTC day it was fetched
    bars = Column(String, nullable=False)                    # JSON list of daily bars
    fetched_at = Column(DateTime)


class VolStatsCache(Base):
    """
    Dashboard detail page: a coin's live 4h / 24h volume from Codex
    getDetailedTokenStats (tx_bars.py), cached for an hour - display only.
    """
    __tablename__ = "vol_stats_cache"
    __table_args__ = (UniqueConstraint("chain", "contract"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain = Column(String, nullable=False)
    contract = Column(String, nullable=False, index=True)   # stored lowercased
    vol_4h = Column(Float)
    vol_24h = Column(Float)
    liq_chg_4h = Column(Float)        # liquidity change over 4h, %
    liq_chg_24h = Column(Float)       # liquidity change over 24h, %
    fetched_at = Column(DateTime, nullable=False)


class RevenueCache(Base):
    """
    Dashboard detail page: a coin's DefiLlama Revenue section data (daily
    revenue bars, annualized revenue, TVL now vs a week ago; revenue_detail.py)
    as JSON, cached for one UTC day. Display only.
    """
    __tablename__ = "revenue_cache"
    __table_args__ = (UniqueConstraint("chain", "contract"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain = Column(String, nullable=False)
    contract = Column(String, nullable=False, index=True)   # stored lowercased
    day = Column(Date, nullable=False)                       # UTC day it was fetched
    data = Column(String, nullable=False)                    # JSON


class CoinCheckCache(Base):
    """
    "Check a Coin" page: the finished analysis of a manually checked contract
    (coin_check.py) as JSON, cached for one UTC day. The checked coin itself
    is never added to coins / snapshots / the queue.
    """
    __tablename__ = "coin_check_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contract = Column(String, nullable=False, unique=True, index=True)   # lowercased
    day = Column(Date, nullable=False)                                   # UTC day checked
    data = Column(String, nullable=False)                                # JSON


class RunLog(Base):
    """
    One row per daily filter run: its funnel numbers, for the dashboard's
    "Last run" summary on the Coming Up page. Written by save_to_db at the
    end of each run; display only.
    """
    __tablename__ = "run_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime, nullable=False)
    discovered = Column(Integer)       # unique coins after discovery
    passed_age = Column(Integer)       # arrived in the entry queue
    analyzed = Column(Integer)         # went through steps 4-7 this run
    saved = Column(Integer)            # passed all 7 steps -> active
    rejected = Column(Integer)         # failed one of steps 4-7
    retry = Column(Integer)            # CoinGecko hiccup -> next run
    waiting = Column(Integer)          # still in the queue after the run


class PriceRangeCache(Base):
    """
    Dashboard detail page: a coin's all-time price range (CoinGecko ATH /
    ATL / current price; price_range.py) as JSON, cached for one UTC day.
    Display only - the filter and drop_logic never read it.
    """
    __tablename__ = "price_range_cache"
    __table_args__ = (UniqueConstraint("chain", "contract"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain = Column(String, nullable=False)
    contract = Column(String, nullable=False, index=True)   # stored lowercased
    day = Column(Date, nullable=False)                       # UTC day it was fetched
    data = Column(String, nullable=False)                    # JSON


class RelStrengthCache(Base):
    """
    Dashboard detail page: a coin's "Relative Strength" section (its 24h
    change vs Bitcoin's and its CoinGecko sector's; relative_strength.py) as
    JSON, cached for one UTC day. Display only - the filter and drop_logic
    never read it.
    """
    __tablename__ = "rel_strength_cache"
    __table_args__ = (UniqueConstraint("chain", "contract"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain = Column(String, nullable=False)
    contract = Column(String, nullable=False, index=True)   # stored lowercased
    day = Column(Date, nullable=False)                       # UTC day it was fetched
    data = Column(String, nullable=False)                    # JSON


class NarrativeSnapshot(Base):
    """
    "Narratives" tab: one CoinGecko category's numbers on one UTC day
    (narratives.py). Saved once a day by the daily job from a single
    /coins/categories call; rows older than 7 days are deleted. Display only.
    """
    __tablename__ = "narrative_snapshots"
    __table_args__ = (UniqueConstraint("date", "category_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, index=True)          # UTC day
    category_id = Column(String, nullable=False, index=True)  # CoinGecko category id
    category_name = Column(String, nullable=False)
    market_cap = Column(Float)
    volume_24h = Column(Float)
    market_cap_change_24h = Column(Float)                     # % (CoinGecko)


class EntryZoneCache(Base):
    """
    A tracked coin's latest Entry Zone evaluation (zone, price bands, details,
    change vs the previous evaluation; entry_zone.py) as JSON. `day` = the day
    it was evaluated; it is shown as-is until it is re-evaluated 3 days later.
    Display only.
    """
    __tablename__ = "entry_zone_cache"
    __table_args__ = (UniqueConstraint("chain", "contract"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain = Column(String, nullable=False)
    contract = Column(String, nullable=False, index=True)   # stored lowercased
    day = Column(Date, nullable=False)                       # UTC day it was evaluated
    data = Column(String, nullable=False)                    # JSON


class EntryZoneHistory(Base):
    """
    One coin's Entry Zone on one UTC day (entry_zone.py) - saved daily by the
    daily job (and when a detail page computes a fresh zone), so the section
    can show how the zone changed vs 3 days ago. Display only.
    """
    __tablename__ = "entry_zone_history"
    __table_args__ = (UniqueConstraint("chain", "contract", "day"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain = Column(String, nullable=False)
    contract = Column(String, nullable=False, index=True)   # stored lowercased
    day = Column(Date, nullable=False, index=True)           # UTC day
    zone = Column(String, nullable=False)                    # BEST / GOOD / ... / RISK
    position_pct = Column(Float)                             # price position in its range


class EntryZoneBars(Base):
    """
    A tracked coin's daily history for the Entry Zone (entry_zone.py) as JSON:
    [date, close, volume, primary-chain liquidity, all-chains liquidity] per
    day (last 90 days) + the coin's chains (DexScreener liquidity per chain).
    Filled ONCE from Codex when the coin is first tracked, then extended daily
    by the tracking step from stored readings. Display only.
    """
    __tablename__ = "entry_zone_bars"
    __table_args__ = (UniqueConstraint("chain", "contract"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain = Column(String, nullable=False)
    contract = Column(String, nullable=False, index=True)   # stored lowercased
    updated_day = Column(Date)                               # latest day stored
    data = Column(String, nullable=False)                    # JSON


class TradeFlow(Base):
    """
    One row per tracked coin per day: Codex's rolling-24h BUY and SELL volume
    (and counts) at the daily run - from the bulk filterTokens request the
    tracking step already makes (no extra calls). The score's "Buyers volume
    (15-day)" component sums the last 15 days. Kept 30 days.
    """
    __tablename__ = "trade_flows"
    __table_args__ = (UniqueConstraint("chain", "contract", "day"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain = Column(String, nullable=False)
    contract = Column(String, nullable=False, index=True)   # stored lowercased
    day = Column(Date, nullable=False, index=True)           # UTC day of the run
    buy_volume = Column(Float)        # Codex buyVolume24 (USD)
    sell_volume = Column(Float)       # Codex sellVolume24 (USD)
    buy_count = Column(Integer)       # Codex buyCount24
    sell_count = Column(Integer)      # Codex sellCount24


class ApiUsage(Base):
    """
    Requests the DAILY JOB made to each outside API, per UTC day (counted by
    daily_report.py). Month-to-date usage in the Telegram report is the sum of
    these rows - it covers the daily job only, not website page views.
    """
    __tablename__ = "api_usage"
    __table_args__ = (UniqueConstraint("day", "source"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    day = Column(Date, nullable=False, index=True)
    source = Column(String, nullable=False)       # "Codex" / "CoinGecko" / "CoinMarketCap" / ...
    calls = Column(Integer, nullable=False, default=0)


class DropstabCoin(Base):
    """
    Our coin (chain + contract) -> its DropsTab coin id, looked up ONCE via
    DropsTab's /coins/contract/{contract} (dropstab.py) and reused forever.
    dropstab_id NULL = DropsTab doesn't know this contract (asked again after
    dropstab.RECHECK_DAYS).
    """
    __tablename__ = "dropstab_coins"
    __table_args__ = (UniqueConstraint("chain", "contract"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain = Column(String, nullable=False)
    contract = Column(String, nullable=False, index=True)   # stored lowercased
    dropstab_id = Column(Integer, index=True)                # NULL = not on DropsTab
    slug = Column(String)
    symbol = Column(String)
    checked_at = Column(DateTime, nullable=False)


class UnlockSnapshot(Base):
    """
    One row per DropsTab coin we track that has token-unlock data: its entry
    from DropsTab's /tokenUnlocks overview (JSON, as returned), refreshed by
    the daily run. prev_next = each allocation's nextTokenUnlockDate from the
    PREVIOUS day's entry (detects daily / linear vesting). The detail page's
    "Upcoming Unlocks" section is computed from this - no DropsTab call on a
    page view.
    """
    __tablename__ = "unlock_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dropstab_id = Column(Integer, nullable=False, unique=True, index=True)
    day = Column(Date, nullable=False)                        # UTC day fetched
    data = Column(Text, nullable=False)                       # JSON overview entry
    prev_next = Column(Text)                                  # JSON {allocation id: date}
    updated_at = Column(DateTime, nullable=False)


class ScoreHistory(Base):
    """
    Each ACTIVE coin's score (0-100) per daily run - the Active table's
    delta arrow (vs the previous recorded day) and the detail page's "All
    Score" table. Written by scoring.record_scores() after score_all; one
    row per coin per day (a re-run the same day updates it). Kept 90 days.
    coin_id = coins.id (no foreign key: a deleted coin's rows simply age out).
    """
    __tablename__ = "score_history"
    __table_args__ = (UniqueConstraint("coin_id", "recorded_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin_id = Column(Integer, nullable=False, index=True)
    score = Column(Float, nullable=False)
    # The score's 6 parts that day (NULL on rows saved before they existed).
    vol_liq = Column(Float)        # Volume / Liquidity (max 25)
    vol_mcap = Column(Float)       # Volume / Market cap (max 15)
    liq_growth = Column(Float)     # Liquidity growth 7d (max 15)
    cheapness = Column(Float)      # Price cheapness (max 20)
    buyers = Column(Float)         # Buyers volume (max 20)
    rank_change = Column(Float)    # Rank change (max 5)
    recorded_at = Column(Date, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class CoinalyzeCoin(Base):
    """
    Our coin (coins.id) -> its main Coinalyze perpetual market (e.g.
    LDOUSDT_PERP.A = Binance), found by ticker and confirmed by price
    (within 30%, so a ticker clash like META isn't matched). Coins without a
    match (coinalyze_symbol NULL) are checked again after 30 days.
    """
    __tablename__ = "coinalyze_coins"
    __table_args__ = (UniqueConstraint("coin_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin_id = Column(Integer, nullable=False, index=True)
    ticker = Column(String)
    coinalyze_symbol = Column(String, index=True)            # NULL = no futures match
    checked_at = Column(DateTime, nullable=False)


class FuturesSnapshot(Base):
    """
    One row per matched coin per day: Coinalyze open interest (USD), funding
    rate (%), and long / short %. Written by the daily job; the detail page's
    "Futures Data" table reads these. Kept 90 days. Display only.
    """
    __tablename__ = "futures_snapshots"
    __table_args__ = (UniqueConstraint("coin_id", "day"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin_id = Column(Integer, nullable=False, index=True)
    day = Column(Date, nullable=False, index=True)
    open_interest_usd = Column(Float)
    funding_rate = Column(Float)        # in % (0.01 = 0.01%)
    long_pct = Column(Float)
    short_pct = Column(Float)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class DropstabUnlockAll(Base):
    """
    EVERY coin in DropsTab's token-unlock overview (~1,000), not just ours -
    the "Token Unlocks" page. data = the overview entry as returned (same
    shape as UnlockSnapshot.data). Refreshed by the daily run from the same
    overview request (no extra calls); rows not refreshed for 2 days are
    removed (coins DropsTab dropped). Display only.
    """
    __tablename__ = "dropstab_unlocks_all"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dropstab_id = Column(Integer, nullable=False, unique=True, index=True)
    slug = Column(String)
    symbol = Column(String)
    name = Column(String)
    data = Column(Text, nullable=False)                        # JSON overview entry
    day = Column(Date, nullable=False)                         # UTC day fetched
    updated_at = Column(DateTime, nullable=False)


class ExchangePair(Base):
    """
    The latest full trading-pair list of each monitored exchange (DropsTab
    /exchanges/{slug}/pairs; ~25K rows). An exchange's rows are replaced
    whenever all its pages were fetched. Used to find our coins' listings
    and, for Check a Coin, any coin's listings by ticker (0 API calls).
    """
    __tablename__ = "exchange_pairs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    exchange = Column(String, nullable=False, index=True)       # DropsTab slug
    base = Column(String, nullable=False, index=True)           # upper-case ticker
    quote = Column(String)
    pair_type = Column(String)                                  # SPOT / DERIVATIVE
    price_usd = Column(Float)
    volume_24h = Column(Float)
    link = Column(String)
    day = Column(Date, nullable=False)


class ExchangeWatch(Base):
    """Coins (active / dropped) the listing check has looked at, and since
    when: listings found on a coin's FIRST check are its starting point
    (baseline), not new listings."""
    __tablename__ = "exchange_watch"

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin_id = Column(Integer, nullable=False, unique=True, index=True)
    since = Column(Date, nullable=False)


class ExchangeListing(Base):
    """
    One row per coin per monitored exchange it trades on (or traded on):
    first_seen = the day our check first saw it; baseline = it was already
    listed when we started watching (so not a "new" listing); delisted_on =
    the day it disappeared (NULL while listed). Top pair / volume / price =
    the coin's biggest pair there on the latest check.
    """
    __tablename__ = "exchange_listings"
    __table_args__ = (UniqueConstraint("coin_id", "exchange"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin_id = Column(Integer, nullable=False, index=True)
    exchange = Column(String, nullable=False)                   # DropsTab slug
    first_seen = Column(Date, nullable=False)
    last_seen = Column(Date, nullable=False)
    delisted_on = Column(Date)
    baseline = Column(Boolean, nullable=False, default=False)
    top_pair = Column(String)
    volume_24h = Column(Float)
    price_usd = Column(Float)
    link = Column(String)


class ExchangeAnnouncement(Base):
    """Upcoming-listing announcements ("Binance Will List X (ABC)") with the
    trading start time read from the article. Matched to coins by ticker."""
    __tablename__ = "exchange_announcements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    exchange = Column(String, nullable=False)
    code = Column(String, nullable=False, unique=True)          # the exchange's article id
    title = Column(String)
    tickers = Column(Text)                                      # JSON list, upper-case
    listing_at = Column(DateTime)                               # UTC; NULL = not found in the article
    released_at = Column(DateTime)
    url = Column(String)
    checked_at = Column(DateTime, nullable=False)


class VolumeSpike(Base):
    """
    A coin's unusual-volume day: today's DexScreener 24h volume vs its own
    average over the previous 30 days (daily_snapshots). Only days at 2x or
    more are stored: severity "big" (>= 5x), "medium" (3-5x), "elevated"
    (2-3x). alerted = sent in the Telegram report (big / medium, at most once
    per coin every 3 days). Kept 90 days. Display / alert only.
    """
    __tablename__ = "volume_spikes"
    __table_args__ = (UniqueConstraint("coin_id", "day"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    coin_id = Column(Integer, ForeignKey("coins.id"), nullable=False, index=True)
    day = Column(Date, nullable=False, index=True)
    volume_today = Column(Float)
    volume_avg_30d = Column(Float)
    spike_ratio = Column(Float)
    severity = Column(String)                 # big / medium / elevated
    alerted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
