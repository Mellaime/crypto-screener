"""
challenge.py
------------
Paper Trading Challenge (Favorites page): each favorites profile gets ONE
virtual $300 balance for 7 or 14 days. It buys Active coins with dollar
amounts and sells them; when the time is up everything still open is sold
automatically and the result is frozen. A leaderboard ranks every profile.

Rules:
  * $300, never topped up. Cash = $300 + realized P&L - money in open
    positions; a buy can't exceed the cash.
  * Buy: coins in Active only (a held coin that is later dropped or removed
    stays sellable). No fees, no slippage.
  * Price = the price the site shows: the hourly live price (CMC, then
    DexScreener, then Codex) while < 90 min old, else the daily price
    (hourly_prices.effective_price). No API calls here. Each open position
    keeps last_price (refreshed hourly) so a coin removed from our database
    can still be valued and sold.
  * End: at end_date every open position is closed at its price then
    ("auto_closed"), final_balance is stored and trading stops. Expired
    challenges are closed by the hourly job and also whenever a challenge
    is read or traded, so one never stays open past its end.
  * Reset (PIN): deletes the profile's challenge; favorites are untouched.
Changes need the profile's session token or PIN (favorites._authorize).
"""

from datetime import timedelta

from favorites import FavError, _authorize, _profile, _now

STARTING_CAPITAL = 300.0
DURATIONS = (7, 14)
MIN_TRADE_USD = 1.0


def _price(coin, position=None):
    """The coin's current price, else the position's last known one."""
    from hourly_prices import effective_price
    p = effective_price(coin)[0] if coin is not None else None
    if not p and position is not None:
        p = position.last_price or position.entry_price
    return p


def _close(pos, price, now, status):
    pos.exit_price, pos.exit_date, pos.status = price, now, status
    pos.pnl_usd = round(pos.entry_amount_usd * (price / pos.entry_price - 1), 2)
    pos.pnl_pct = round((price / pos.entry_price - 1) * 100, 2)


def _coins(session, positions):
    from models import Coin
    keys = {p.contract.lower() for p in positions}
    if not keys:
        return {}
    from sqlalchemy import func
    return {c.contract.lower(): c for c in session.query(Coin).filter(func.lower(Coin.contract).in_(keys))}


def close_expired(session):
    """End every challenge whose time is up: open positions auto-closed at
    their current price, final_balance stored. Returns challenges ended."""
    from models import Challenge, ChallengePosition
    now = _now()
    ended = session.query(Challenge).filter(Challenge.status == "active", Challenge.end_date <= now).all()
    for ch in ended:
        pos = session.query(ChallengePosition).filter_by(challenge_id=ch.id).all()
        coins = _coins(session, [p for p in pos if p.status == "open"])
        for p in pos:
            if p.status == "open":
                _close(p, _price(coins.get(p.contract.lower()), p), ch.end_date, "auto_closed")
        ch.final_balance = round(STARTING_CAPITAL + sum(p.pnl_usd or 0 for p in pos), 2)
        ch.status = "ended"
    if ended:
        session.commit()
    return len(ended)


def hourly_update(session):
    """Hourly job: refresh open positions' last_price, then end expired
    challenges. Uses the prices the hourly run just stored (no API calls)."""
    from models import ChallengePosition
    open_pos = session.query(ChallengePosition).filter_by(status="open").all()
    coins = _coins(session, open_pos)
    for p in open_pos:
        c = coins.get(p.contract.lower())
        price = _price(c) if c is not None else None
        if price:
            p.last_price, p.last_price_at = price, _now()
    session.commit()
    return close_expired(session)


def _challenge(session, profile, lock=False):
    from models import Challenge
    q = session.query(Challenge).filter_by(profile_id=profile.id)
    return (q.with_for_update() if lock else q).first()      # lock: no double-spend on parallel buys


def _state(session, profile, ch):
    """The challenge as the page shows it (cash, invested, P&L, positions)."""
    from models import ChallengePosition
    if ch is None:
        return {"profile": profile.name, "challenge": None, "starting_capital": STARTING_CAPITAL}
    pos = (session.query(ChallengePosition).filter_by(challenge_id=ch.id)
           .order_by(ChallengePosition.entry_date.desc()).all())
    coins = _coins(session, pos)
    rows, invested, open_value, realized = [], 0.0, 0.0, 0.0
    for p in pos:
        c = coins.get(p.contract.lower())
        r = {"id": p.id, "coin_name": p.coin_name, "contract": p.contract, "chain": p.chain, "status": p.status,
             "entry_price": p.entry_price, "entry_amount_usd": p.entry_amount_usd, "entry_date": p.entry_date.isoformat(),
             "exit_price": p.exit_price, "exit_date": p.exit_date.isoformat() if p.exit_date else None,
             "pnl_usd": p.pnl_usd, "pnl_pct": p.pnl_pct, "coin_status": c.status if c is not None else "removed"}
        if p.status == "open":
            price = _price(c, p)
            value = p.entry_amount_usd * price / p.entry_price
            invested += p.entry_amount_usd
            open_value += value
            r.update(current_price=price, value=round(value, 2), pnl_usd=round(value - p.entry_amount_usd, 2),
                     pnl_pct=round((price / p.entry_price - 1) * 100, 2))
        else:
            realized += p.pnl_usd or 0
        rows.append(r)
    cash = STARTING_CAPITAL + realized - invested
    balance = ch.final_balance if ch.status == "ended" else cash + open_value
    return {"profile": profile.name, "starting_capital": STARTING_CAPITAL, "challenge": {
        "id": ch.id, "status": ch.status, "duration_days": ch.duration_days, "start_date": ch.start_date.isoformat(),
        "end_date": ch.end_date.isoformat(), "final_balance": ch.final_balance,
        "cash": round(cash, 2), "invested": round(invested, 2), "open_value": round(open_value, 2),
        "unrealized_pnl": round(open_value - invested, 2), "realized_pnl": round(realized, 2),
        "balance": round(balance, 2), "pnl_pct": round((balance / STARTING_CAPITAL - 1) * 100, 2),
        "trades": len(pos), "positions": rows}}


def get_state(session, name):
    close_expired(session)
    p = _profile(session, name)
    return _state(session, p, _challenge(session, p))


def start(session, name, duration_days, token=None, pin=None, ip="?"):
    from models import Challenge
    p = _profile(session, name)
    _authorize(session, p, token, pin, ip)
    try:
        days = int(duration_days)
    except (TypeError, ValueError):
        days = None
    if days not in DURATIONS:
        raise FavError(400, "Pick 7 or 14 days.")
    if _challenge(session, p) is not None:
        raise FavError(409, "This profile already has a challenge. Reset it to start a new one.")
    now = _now()
    ch = Challenge(profile_id=p.id, start_date=now, duration_days=days, end_date=now + timedelta(days=days),
                   status="active", starting_capital=STARTING_CAPITAL)
    session.add(ch)
    session.commit()
    return _state(session, p, ch)


def buy(session, name, contract, amount_usd, token=None, pin=None, ip="?"):
    from sqlalchemy import func
    from models import Coin, ChallengePosition
    close_expired(session)
    p = _profile(session, name)
    _authorize(session, p, token, pin, ip)
    ch = _challenge(session, p, lock=True)
    if ch is None or ch.status != "active":
        raise FavError(409, "No running challenge - start one first." if ch is None else "The challenge has ended.")
    try:
        amount = round(float(amount_usd), 2)
    except (TypeError, ValueError):
        raise FavError(400, "Enter a dollar amount.")
    coin = session.query(Coin).filter(func.lower(Coin.contract) == (contract or "").strip().lower()).first()
    if coin is None or coin.status != "active":
        raise FavError(400, "You can only buy coins in the Active list.")
    price = _price(coin)
    if not price:
        raise FavError(409, f"No price for {coin.name} right now - try again later.")
    cash = _state(session, p, ch)["challenge"]["cash"]
    if amount < MIN_TRADE_USD or amount > cash + 1e-9:
        raise FavError(400, f"Amount must be between ${MIN_TRADE_USD:.0f} and your available ${cash:,.2f}.")
    session.add(ChallengePosition(challenge_id=ch.id, contract=coin.contract, chain=coin.chain, coin_name=coin.name,
                                  entry_price=price, entry_amount_usd=amount, entry_date=_now(), status="open",
                                  last_price=price, last_price_at=_now()))
    session.commit()
    return _state(session, p, ch)


def sell(session, name, position_id, token=None, pin=None, ip="?"):
    from models import ChallengePosition
    close_expired(session)
    p = _profile(session, name)
    _authorize(session, p, token, pin, ip)
    ch = _challenge(session, p, lock=True)
    if ch is None or ch.status != "active":
        raise FavError(409, "The challenge has ended." if ch else "No running challenge.")
    pos = session.query(ChallengePosition).filter_by(id=position_id, challenge_id=ch.id).first()
    if pos is None or pos.status != "open":
        raise FavError(404, "That position is not open.")
    _close(pos, _price(_coins(session, [pos]).get(pos.contract.lower()), pos), _now(), "closed")
    session.commit()
    return _state(session, p, ch)


def reset(session, name, pin, ip="?"):
    """Delete the profile's challenge (always needs the PIN)."""
    from favorites import _check_pin
    from models import Challenge, ChallengePosition
    p = _profile(session, name)
    _check_pin(session, p, pin, ip)
    ch = _challenge(session, p)
    if ch is not None:
        session.query(ChallengePosition).filter_by(challenge_id=ch.id).delete(synchronize_session=False)
        session.delete(ch)
        session.commit()
    return _state(session, p, None)


def delete_for_profile(session, profile_id):
    """Remove a profile's challenge rows (favorites.delete_profile)."""
    from models import Challenge, ChallengePosition
    for ch in session.query(Challenge).filter_by(profile_id=profile_id).all():
        session.query(ChallengePosition).filter_by(challenge_id=ch.id).delete(synchronize_session=False)
        session.delete(ch)


def leaderboard(session):
    """Every profile's challenge ranked by balance (final if ended, current
    if running) + the best and worst single closed trade."""
    from models import Challenge, ChallengePosition, FavoriteProfile
    close_expired(session)
    rows = []
    for ch, prof in session.query(Challenge, FavoriteProfile).join(FavoriteProfile, FavoriteProfile.id == Challenge.profile_id):
        st = _state(session, prof, ch)["challenge"]
        rows.append({"profile": prof.name, "challenge_id": ch.id, "starting_capital": STARTING_CAPITAL,
                     "balance": st["balance"], "pnl_pct": st["pnl_pct"], "trades": st["trades"], "status": ch.status,
                     "duration_days": ch.duration_days, "end_date": st["end_date"]})
    rows.sort(key=lambda r: -r["balance"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    trades = (session.query(ChallengePosition, FavoriteProfile.name)
              .join(Challenge, Challenge.id == ChallengePosition.challenge_id)
              .join(FavoriteProfile, FavoriteProfile.id == Challenge.profile_id)
              .filter(ChallengePosition.status != "open").all())

    def trade(t):
        pos, prof = t
        return {"profile": prof, "coin_name": pos.coin_name, "pnl_pct": pos.pnl_pct, "pnl_usd": pos.pnl_usd,
                "entry_price": pos.entry_price, "exit_price": pos.exit_price}
    best = max(trades, key=lambda t: t[0].pnl_pct, default=None)
    worst = min(trades, key=lambda t: t[0].pnl_pct, default=None)
    return {"rows": rows, "best_trade": trade(best) if best else None,
            "worst_trade": trade(worst) if worst and worst is not best else None}
