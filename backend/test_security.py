"""Self-check for security_check rules (GoPlus EVM / RugCheck Solana), no network. Run: python test_security.py"""
import security_check as S

ok = {"is_honeypot": "0", "cannot_sell_all": "0", "is_open_source": "1", "is_mintable": "0",
      "owner_address": "", "creator_address": "0xCreator", "buy_tax": "0.05", "sell_tax": "0",
      "transfer_pausable": "0", "is_blacklisted": "0", "owner_change_balance": "0", "selfdestruct": "0"}
evm = lambda **kw: S.classify_evm({**ok, **kw})

r = evm()
assert r["verdict"] == "safe" and r["buy_tax"] == 5.0 and r["sell_tax"] == 0.0     # 5% tax is fine
assert r["renounced"] and r["mint_disabled"] and r["freeze_disabled"] and r["creator_address"] == "0xCreator"
assert r["owner_address"] is None and r["lp_locked_pct"] is None
assert evm(sell_tax="0.1")["verdict"] == "safe"                                      # exactly 10% passes
assert evm(sell_tax="0.15")["verdict"] == "unsafe" and "tax" in evm(sell_tax="0.15")["reason"]
assert evm(buy_tax="0.11")["verdict"] == "unsafe"
assert evm(sell_tax="", buy_tax="")["verdict"] == "safe"                             # unknown tax never rejects
assert evm(owner_change_balance="1")["verdict"] == "safe"                             # recorded only, never rejects
assert evm(selfdestruct="1")["verdict"] == "unsafe"
assert evm(is_honeypot="1")["reason"] == "honeypot"
assert evm(transfer_pausable="1")["freeze_disabled"] is False and evm(transfer_pausable="1")["verdict"] == "safe"
r = evm(owner_address="0xABC", lp_holders=[
    {"address": "0x000000000000000000000000000000000000dEaD", "percent": "0.6", "is_locked": 0},
    {"address": "0xlocker", "percent": "0.25", "is_locked": 1},
    {"address": "0xwhale", "percent": "0.15", "is_locked": 0}])
assert r["lp_locked_pct"] == 85.0 and r["owner_address"] == "0xABC" and r["renounced"] is False

good = {"rugged": False, "mintAuthority": None, "freezeAuthority": None, "creator": "Creat0r",
        "transferFee": {"pct": 0}, "tokenMeta": {"updateAuthority": "UpdAuth"}, "token_extensions": None,
        "markets": [{"lp": {"baseUSD": 300, "quoteUSD": 300, "lpLockedUSD": 600}},
                    {"lp": {"baseUSD": 200, "quoteUSD": 200, "lpLockedUSD": 0}}]}
sol = lambda **kw: S.classify_rugcheck({**good, **kw})
r = sol()
assert r["verdict"] == "safe" and r["lp_locked_pct"] == 60.0 and r["creator_address"] == "Creat0r"
assert r["renounced"] and r["mint_disabled"] and r["freeze_disabled"] and r["owner_address"] == "UpdAuth"
assert r["buy_tax"] == r["sell_tax"] == 0.0
assert sol(rugged=True)["reason"] == "rugged (RugCheck)"
r = sol(freezeAuthority="Fr33ze")                                                   # recorded only, never rejects
assert r["verdict"] == "safe" and r["freeze_disabled"] is False and r["renounced"] is False
r = sol(mintAuthority="M1nt")
assert r["verdict"] == "safe" and r["mint_disabled"] is False and r["owner_address"] == "M1nt"
assert sol(transferFee={"pct": 100})["verdict"] == "unsafe" and sol(transferFee={"pct": 11})["verdict"] == "unsafe"
assert sol(transferFee={"pct": 10})["verdict"] == "safe"
assert sol(transferFee={"pct": 3})["sell_tax"] == 3.0 and sol(transferFee={"pct": 3})["verdict"] == "safe"
assert sol(token_extensions={"nonTransferable": True})["verdict"] == "unsafe"
assert sol(token_extensions={"nonTransferable": False, "transferFeeConfig": None})["verdict"] == "safe"   # seen live
assert sol(markets=[])["lp_locked_pct"] is None
r = sol(mintAuthority={"lamports": 1, "owner": "1111"}, tokenMeta={"updateAuthority": {"lamports": 1}})  # seen live
assert r["mint_disabled"] is False and r["owner_address"] is None and r["verdict"] == "safe"
assert sol(freezeAuthority={"lamports": 1})["freeze_disabled"] is False
# unknown: rejected on a chain GoPlus / RugCheck cover, passes on an unsupported one
assert S.rejects(S._unknown("GoPlus API error")) and S.rejects(evm(is_honeypot="1"))
assert not S.rejects(S.check_token_security("0xabc", "sui")) and not S.rejects(evm())
print("security self-check: ok")
