"""Self-check for the V3 scoring rules (pure functions, no database). Run: python test_scoring.py"""
from types import SimpleNamespace
import scoring as S

# tiers + gates
assert S.pts_turnover(25) == 20 and S.pts_turnover(0.29) == 0 and S.pts_turnover(None) is None
assert S.pts_absorption(85, 2, 5) == 10 and S.pts_absorption(50, 2, 5) == 5
assert S.pts_absorption(300, 2, 0.001) == 0                 # huge % jump on near-zero volume
assert S.pts_absorption(120, 12, 5) == 0                    # price outside the quiet band
assert S.pts_buyers(15_000, 4_000, 10, 5) == (None, None)   # under the $20K gate
assert S.pts_buyers(30_000, 10_000, 9, 5)[0] == 25          # ratio capped at 1.5x, still top tier
assert S.pts_buyers(30_000, 10_000, 5, 9)[0] == 12.5        # more sell transactions -> halved
assert S.pts_liquidity(500, 5_000) is None                  # low base (Neutrino-style)
assert S.pts_liquidity(500, 50_000) == 10 and S.pts_liquidity(-3, 50_000) == 0
assert S.pts_holders(2.5) == 15 and S.pts_holders(-0.1) == 0 and S.pts_holder_vel(0.4, 0.7) == 5

# anti-chasing only touches the 24h parts
p = {"absorption": 10, "holder_vel": 5, "turnover": 20}
assert S.anti_chase(p, 20) == {"absorption": 5, "holder_vel": 2.5, "turnover": 20}
assert S.anti_chase(p, 30)["absorption"] == 0 and S.anti_chase(p, 5) == p

# missing = cross-sectional median
blank = {c: None for c, *_ in S.PARTS}
pts = [{"pts": {**blank, "turnover": v}} for v in (0, 4, 16)]
med = S.part_medians(pts)
filled, missing = S.fill({**blank, "turnover": None}, med)
assert filled["turnover"] == 4 and "turnover" in missing and med["buyers"] == 0

# score change: only parts with real data on both days; pre-V3 rows not comparable
row = lambda miss, **v: SimpleNamespace(missing=miss, **{**{c: 0.0 for c, *_ in S.PARTS}, **v})
assert S._shared_change(row("", turnover=16), row("turnover", turnover=4))["buyers"] == 0
assert "turnover" not in S._shared_change(row("", turnover=16), row("turnover", turnover=4))
assert S._shared_change(row("", turnover=16), row(None)) is None
print("scoring self-check: ok")
