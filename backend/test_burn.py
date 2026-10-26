"""Self-check for burn_detector classification / percentage math (no network). Run: python test_burn.py"""
import burn_detector as B

C, PAIR, NOW, DAY = "0xToken", "0xpair", 1_800_000_000, 86400
DEAD, ZERO = B.BURN_WALLETS[0], B.ZERO
tr = lambda *days, frm="0xuser", v=10**18: [{"from": frm, "ts": NOW - d * DAY, "value": v} for d in days]
s = lambda bals, supply, transfers, **kw: B._summarize(C, 1, "etherscan", {DEAD: 0, ZERO: 0, B.BURN_WALLETS[2]: 0, **bals},
                                                         supply, 18, transfers, kw.get("pair"), kw.get("created"), NOW)

r = s({}, 10**24, [])
assert (r["burn_status"], r["burn_frequency"], r["burned_percentage"], r["is_burnable"]) == ("NO_BURN", "NONE", 0.0, False)

r = s({DEAD: 25 * 10**22}, 10**24, [])                         # 25% sitting in 0x...dEaD, no transfers seen
assert r["burned_percentage"] == 25.0 and r["burn_status"] == "STATIC_PRE_BURN" and r["total_burned_amount"] == 250000
assert r["burn_addresses_with_balance"] == [{"address": DEAD, "amount": 250000.0}]

r = s({}, 9 * 10**23, tr(1, 8, 15, v=10**23))                   # _burn(): balance 0, supply already reduced
assert r["burned_percentage"] == 25.0 and r["burn_status"] == "ACTIVE_BURN" and r["burn_frequency"] == "WEEKLY"

assert B.classify(tr(1, 2, 3), 1, C, now=NOW) == ("ACTIVE_BURN", "DAILY")
assert B.classify(tr(40, 70, 100), 1, C, now=NOW) == ("STATIC_PRE_BURN", "ONE_TIME")              # last burn > 30 days ago
assert B.classify(tr(1, 5), 1, C, now=NOW) == ("STATIC_PRE_BURN", "ONE_TIME")                     # only 2 burns
assert B.classify(tr(1, 3, 6), 1, C, pair_created_at=NOW - 7 * DAY, now=NOW) == ("ACTIVE_BURN", "WEEKLY")
assert B.classify(tr(5, 5.5, 6), 1, C, pair_created_at=NOW - 6 * DAY - 60, now=NOW) == ("STATIC_PRE_BURN", "ONE_TIME")  # day-1 pre-burn
assert B.classify(tr(3, frm=PAIR) + tr(9, 30), 1, C, PAIR)[0] == "BUYBACK_AND_BURN"
assert B.classify(tr(3, frm=C.lower()), 1, C)[0] == "BUYBACK_AND_BURN"
assert B.classify(tr(20, 45, 70), 1, C, now=NOW) == ("ACTIVE_BURN", "MONTHLY")                    # 15-30 d, recurring monthly
assert B.classify(tr(20, 120, 300), 1, C, now=NOW) == ("STATIC_PRE_BURN", "ONE_TIME")             # 15-30 d, irregular
assert s({DEAD: 2 * 10**24}, 10**24, [])["burned_percentage"] == 100.0                  # clamped
r = s({DEAD: 28 * 10**18}, 10**27, tr(59))                                                # dust: 28 of 1B = 0.0000028%
assert (r["burn_status"], r["total_burned_amount"], r["burned_percentage"]) == ("NO_BURN", 0.0, 0.0)
assert s({DEAD: 10**22}, 10**24, [])["burn_status"] == "STATIC_PRE_BURN"                 # 1% is kept
assert B.chain_id("Ethereum") == 1 and B.chain_id("solana") is None and B.chain_id("hyperevm") == 999

# ---- Solana (Helius): mint filter, cases A / B / C, dust, micro-burns, buyback
M = "MintAAA"
tx = lambda ts, amt, mint=M, to="", swap=False, extra=(): {"timestamp": ts, "feePayer": "payer", "events": {"swap": {"tokenInputs": [1]}} if swap else {},
     "tokenTransfers": [{"mint": mint, "tokenAmount": amt, "fromUserAccount": "payer", "toUserAccount": to, "toTokenAccount": "" if not to else "acc"}, *extra]}
b = B._solana_burns(M, [tx(NOW, 5), tx(NOW, 9, mint="LPmint"), tx(NOW, 3, to=B.INCINERATOR), tx(NOW, 4, to="someone")])
assert [x["value"] for x in b] == [5, 3]                                              # other mint + plain transfer ignored
assert B._solana_burns(M, [tx(NOW, 5, swap=True)])[0]["buyback"]
sol = lambda burns, raw, mx=None, mint_off=True, created=None: B._solana_summary(M, 1_000_000.0, burns, raw, mx, mint_off, True, created, NOW)
burn = lambda days, v: {"ts": NOW - days * DAY, "value": v, "buyback": False}

r = sol([burn(1, 1000), burn(4, 1000), burn(8, 1000)], 3)                              # case A: sum of the page
assert r["total_burned_amount"] == 3000 and r["burned_percentage"] == round(3000 / 1_003_000 * 100, 4)
assert (r["burn_status"], r["burn_frequency"]) == ("ACTIVE_BURN", "WEEKLY") and r["is_safe_from_manipulation"]
r = sol([burn(1, 1000)], 1, mx=1_500_000.0)                                            # A, older burn via max - current
assert r["total_burned_amount"] == 500_000 and r["burned_percentage"] == round(500_000 / 1_500_000 * 100, 4)
r = sol([], 0, mx=1_500_000.0)                                                         # case B, nothing in the window
assert (r["total_burned_amount"], r["burn_status"], r["burn_frequency"]) == (500_000, "STATIC_PRE_BURN", "ONE_TIME")
assert sol([], 0, mx=1_500_000.0, mint_off=False)["burn_status"] == "NO_BURN"           # mint still on: gap = unminted
r = sol([burn(d / 10, 50) for d in range(100)], 100)                                   # case C: full page, no max
assert r["total_burned_amount"] == r["burned_percentage"] == "Unknown" and r["recent_sampled_burned_amount"] == 5000
assert r["burn_status"] == "ACTIVE_BURN" and r["burn_frequency"] == "DAILY"
assert sol([burn(d / 10, 0.5) for d in range(100)], 100)["burn_status"] == "NO_BURN"   # 100 account-close dust burns
assert sol([burn(1, 50)], 1)["burn_status"] == "NO_BURN"                               # 0.005% < 0.01% dust
r = sol([burn(1, 500)] + [burn(d, 0.1) for d in range(2, 9)], 8)                        # micro-burns ignored for status
assert (r["burn_status"], r["burn_frequency"]) == ("STATIC_PRE_BURN", "ONE_TIME")
assert sol([{**burn(1, 500), "buyback": True}, burn(3, 500)], 2)["burn_status"] == "BUYBACK_AND_BURN"
assert B.supported("solana") and not B.supported("sui")
print("burn self-check: ok")
