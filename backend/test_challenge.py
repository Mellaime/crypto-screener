"""Self-check for the Paper Trading Challenge (in-memory SQLite, no production DB). Run: python test_challenge.py"""
from datetime import timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import database
import models as M
import favorites, challenge as C

eng = create_engine("sqlite://")
database.Base.metadata.create_all(eng)
s = sessionmaker(bind=eng)()
now = database.utcnow()
for i, (name, status, price) in enumerate([("A", "active", 1.0), ("B", "active", 2.0), ("D", "dropped", 3.0)]):
    s.add(M.Coin(contract=f"0x{i}", chain="ethereum", name=name, status=status, live_price=price, live_price_at=now))
s.commit()
tok = favorites.create_profile(s, "Tester", "1234")["session_token"]

st = C.start(s, "Tester", 7, token=tok)["challenge"]
assert st["cash"] == 300 and st["balance"] == 300
C.buy(s, "Tester", "0x0", 100, token=tok)
st = C.buy(s, "Tester", "0x1", 150, token=tok)["challenge"]
assert st["cash"] == 50 and st["invested"] == 250
for bad in [("0x0", 60), ("0x2", 10), ("0x0", 0.5)]:                      # over cash, dropped coin, under $1
    try:
        C.buy(s, "Tester", *bad, token=tok)
        raise AssertionError(f"buy {bad} should fail")
    except favorites.FavError:
        pass

s.query(M.Coin).filter_by(contract="0x0").update({"live_price": 1.2})       # A +20%
s.query(M.Coin).filter_by(contract="0x1").update({"live_price": 1.8})       # B -10%
s.commit()
st = C.get_state(s, "Tester")["challenge"]
assert st["unrealized_pnl"] == 5 and st["balance"] == 305                   # +20 - 15
pos_a = next(p for p in st["positions"] if p["contract"] == "0x0")
st = C.sell(s, "Tester", pos_a["id"], token=tok)["challenge"]
assert st["realized_pnl"] == 20 and st["cash"] == 170

ch = s.query(M.Challenge).one()
ch.end_date = now - timedelta(minutes=1)
s.commit()
assert C.hourly_update(s) == 1
st = C.get_state(s, "Tester")["challenge"]
assert st["status"] == "ended" and st["final_balance"] == 305
assert {p["status"] for p in st["positions"]} == {"closed", "auto_closed"}
board = C.leaderboard(s)
assert board["rows"][0]["balance"] == 305 and board["best_trade"]["pnl_pct"] == 20 and board["worst_trade"]["pnl_pct"] == -10

assert C.reset(s, "Tester", "1234")["challenge"] is None
assert s.query(M.ChallengePosition).count() == 0
print("challenge self-check: ok")
