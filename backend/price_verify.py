"""
price_verify.py
---------------
PRICE MISMATCH REMOVAL: a coin whose price sources disagree by more than
REMOVE_X (2x) is removed for good - a sign of a dead / scam token, a broken
pool or price confusion we can't trust.

Sources (all fetched in the SAME daily run, by price_sources.run_price_sanity,
before the drop logic - never stale stored values):
  CMC        - CoinMarketCap quotes/latest by coins.cmc_id
  CoinGecko  - /simple/token_price by contract
  Daily      - today's daily price (coins.price_usd, tracking.py: DexScreener,
               checked against Codex / CoinGecko)
Zero / missing = no price from that source. Fewer than 2 prices -> skipped.

  any pair more than REMOVE_X apart (max / min)  -> removed for good
      "Price mismatch: CMC $X vs CoinGecko $Y (ratio Nx)"
  worst pair between WARN_X and REMOVE_X          -> Telegram warning only
  all within WARN_X                               -> fine

Applied by drop_logic (after the liquidity rules) to ACTIVE coins every run
and to DROPPED coins on their 3-day re-check day (same cadence as Birdeye).
If the price step didn't run / failed this run, nothing is decided.
"""

REMOVE_X = 2.0
WARN_X = 1.5

# {coin_id: {"CMC": price or None, "CoinGecko": price or None}} - this run's
# fresh lookups (run_price_sanity). Empty = the price step didn't run.
FRESH = {}


def readings(coin):
    """This run's prices for a coin: {source: USD > 0} (missing sources left out)."""
    fresh = FRESH.get(coin.id) or {}
    vals = {"CMC": fresh.get("CMC"), "CoinGecko": fresh.get("CoinGecko"), "Daily": coin.price_usd}
    return {k: float(v) for k, v in vals.items() if v and float(v) > 0}


def worst_pair(prices):
    """(source_a, price_a, source_b, price_b, ratio) for the two prices
    furthest apart (a = the higher), or None with fewer than 2 prices."""
    if len(prices) < 2:
        return None
    hi = max(prices, key=prices.get)
    lo = min(prices, key=prices.get)
    return hi, prices[hi], lo, prices[lo], prices[hi] / prices[lo]


def usd(v):
    return f"${v:.4g}" if v < 1 else f"${v:,.2f}"


def check(coin):
    """{"pair": worst_pair, "reason": text} when the coin must be removed
    (sources more than REMOVE_X apart), else None. None too when this run
    has no fresh price lookup for the coin."""
    if coin.id not in FRESH:
        return None
    wp = worst_pair(readings(coin))
    if not wp or wp[4] <= REMOVE_X:
        return None
    a, pa, b, pb, r = wp
    return {"pair": wp, "reason": f"Price mismatch: {a} {usd(pa)} vs {b} {usd(pb)} (ratio {r:.1f}x)"}
