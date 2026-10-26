"""Self-check for favorites star / unstar / Reset (in-memory SQLite, no production DB). Run: python test_favorites.py"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import database
import models as M
import favorites as F

eng = create_engine("sqlite://")
database.Base.metadata.create_all(eng)
s = sessionmaker(bind=eng)()
for i in range(F.MAX_COINS + 1):
    s.add(M.Coin(contract=f"0x{i}", chain="ethereum", name=f"C{i}", status="active", price_usd=1.0))
s.commit()
tok = F.create_profile(s, "Tester", "1234")["session_token"]
coins = lambda: {c["contract"] for c in F.get_profile(s, "Tester")["coins"]}

F.add_coin(s, "Tester", "0x0", token=tok)
assert coins() == {"0x0"}
F.remove_coin(s, "Tester", "0x0", token=tok)                              # unstar: no lock, no PIN
assert coins() == set()
F.add_coin(s, "Tester", "0x0", token=tok)                                 # re-star
assert coins() == {"0x0"}

for i in range(1, F.MAX_COINS):
    F.add_coin(s, "Tester", f"0x{i}", token=tok)
try:
    F.add_coin(s, "Tester", f"0x{F.MAX_COINS}", token=tok)
    raise AssertionError("51st coin should fail")
except F.FavError as e:
    assert e.status == 409

try:
    F.reset_profile(s, "Tester", "0000", "1.2.3.4")
    raise AssertionError("reset with a wrong PIN should fail")
except F.FavError:
    pass
assert F.reset_profile(s, "Tester", "1234", "1.2.3.4")["removed"] == F.MAX_COINS
assert coins() == set()
print("favorites self-check: ok")
