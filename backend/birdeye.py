"""
birdeye.py
----------
Birdeye - the THIRD liquidity source (with DexScreener and Codex) for the
3-source liquidity verification (liquidity_verify.py).

Endpoint: GET https://public-api.birdeye.so/defi/price
              ?address=<contract>&include_liquidity=true   (header x-chain)
  3 CU per call (Birdeye's "Compute Unit Cost" table), on the free Standard
  plan (30,000 CU / month, 1 request / second, no batch endpoints). Returns
  {value (price), liquidity (USD), updateUnixTime (Birdeye's last price
  update)} - the same liquidity figure as /defi/v3/token/market-data, which
  costs 10 CU.

lookup(chain, contract) -> {"status", "liquidity", "price", "updated_at"}
  status "ok"          - liquidity is a positive USD number
         "no_data"     - Birdeye answered but has no market for the token
                         (empty data, null / zero liquidity)
         "unsupported" - chain not on Birdeye (TON, Tron, Cronos, ...)
         "error"       - request failed (network, 429 after retries, 5xx,
                         no API key): NOT a "no data" answer - callers must
                         not remove a coin on it
Never raises. Paced to 1 request / second; CU_USED counts this process's CU.
"""

import os
import time
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
log = logging.getLogger("birdeye")

BASE = "https://public-api.birdeye.so"
CU_PER_CALL = 3            # /defi/price
MONTHLY_CU = 30_000        # free Standard plan
MIN_INTERVAL = 1.25        # seconds between request starts (plan: 1 request / second)
RETRY_WAITS = [3, 10]      # on 429 / 5xx / network errors

# Our chain name -> Birdeye x-chain (Birdeye's /defi/networks, Oct 2026).
CHAINS = {"ethereum": "ethereum", "bsc": "bsc", "base": "base", "solana": "solana",
          "polygon": "polygon", "arbitrum": "arbitrum", "optimism": "optimism",
          "hyperevm": "hyperevm", "sui": "sui", "avalanche": "avalanche",
          "zksync": "zksync", "mantle": "mantle", "aptos": "aptos", "monad": "monad",
          "robinhood": "robinhood"}

CU_USED = 0                # this process's CU (a 429 "too many requests" isn't counted)
CALLS = 0                  # coins looked up
RETRIES = {}               # retry reason -> count (429 / 5xx / network)
_last = 0.0


def _key():
    return os.getenv("BIRDEYE_API_KEY")


def supported(chain):
    return (chain or "").lower() in CHAINS


def _result(status, liquidity=None, price=None, updated_at=None):
    return {"status": status, "liquidity": liquidity, "price": price, "updated_at": updated_at}


def lookup(chain, contract):
    """One token's Birdeye liquidity (see module docstring). Never raises."""
    global CU_USED, CALLS, _last
    x_chain = CHAINS.get((chain or "").lower())
    if not x_chain:
        return _result("unsupported")
    if not contract or not _key():
        return _result("error")
    CALLS += 1
    for wait in [0] + RETRY_WAITS:
        if wait:
            time.sleep(wait)
        gap = MIN_INTERVAL - (time.monotonic() - _last)
        if gap > 0:
            time.sleep(gap)
        _last = time.monotonic()
        try:
            r = requests.get(f"{BASE}/defi/price", params={"address": contract, "include_liquidity": "true"},
                             headers={"X-API-KEY": _key(), "x-chain": x_chain, "accept": "application/json"},
                             timeout=20)
        except requests.exceptions.RequestException as e:
            log.warning("Birdeye request failed (%s)", type(e).__name__)
            RETRIES["network"] = RETRIES.get("network", 0) + 1
            continue
        if r.status_code == 429:
            RETRIES["429"] = RETRIES.get("429", 0) + 1
            continue
        CU_USED += CU_PER_CALL
        if r.status_code >= 500:
            RETRIES["5xx"] = RETRIES.get("5xx", 0) + 1
            continue
        try:
            body = r.json()
        except ValueError:
            continue
        if r.status_code == 400 and "not supported" in str(body.get("message", "")).lower():
            return _result("unsupported")
        if r.status_code != 200:
            log.warning("Birdeye HTTP %s for %s", r.status_code, x_chain)
            return _result("error")
        d = body.get("data") or {}
        try:
            liq = float(d.get("liquidity")) if d.get("liquidity") is not None else None
        except (TypeError, ValueError):
            liq = None
        ts = d.get("updateUnixTime")
        updated = (datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
                   if isinstance(ts, (int, float)) and ts > 0 else None)
        price = d.get("value") if isinstance(d.get("value"), (int, float)) else None
        if not liq or liq <= 0:
            return _result("no_data", None, price, updated)
        return _result("ok", liq, price, updated)
    return _result("error")


if __name__ == "__main__":
    for name, ch, ct in [("edeXa", "polygon", "0xC114678C6e4654d041B2006C90f08478b444c4E2"),
                         ("TON coin", "ton", "whatever")]:
        print(name, lookup(ch, ct))
    print("CU used", CU_USED)
