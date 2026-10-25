"""
favorites.py
------------
Favorites profiles: up to MAX_PROFILES named lists, each protected by a
4-digit PIN. Everyone can VIEW every profile; adding / removing coins needs
the PIN or a session token obtained with it (24 h); deleting a profile
always needs the PIN.

Security:
  * PINs are stored only as salted PBKDF2-SHA256 hashes.
  * Session tokens are random (32 chars); only their SHA-256 is stored, in
    the database - the API runs on Vercel, where an in-memory cache would be
    lost between requests.
  * PIN attempts are rate-limited per IP: at most 5 per minute, and 10
    failures within an hour block that IP for an hour (10,000 possible PINs
    can't be brute-forced).

Coins are stored with a snapshot (name, symbol, chain, price, market cap,
P&L since caught). The daily run refreshes it while the coin exists; when a
coin is deleted from our database the favorite stays, with its last known
data and delisted_at = the day it disappeared.

Portfolio: each coin keeps its ENTRY PRICE (the price when it was added); a
coin's P&L = (current price - entry price) / entry price - frozen at the
last known price once the coin is delisted. The profile's Portfolio
Performance = the average of those P&Ls (delisted coins included), since
tracking_since (creation or the last Reset). Coins are LOCKED: the only way
to remove them is Reset (wipes all coins, restarts tracking_since; always
needs the PIN).
"""

import re
import hmac
import hashlib
import secrets
from datetime import timedelta

MAX_PROFILES = 5
MAX_COINS = 50
SESSION_HOURS = 24
PBKDF2_ITERATIONS = 200_000
PER_MINUTE = 5                 # PIN attempts per IP per minute
FAILS_TO_BLOCK = 10            # failed attempts within an hour -> blocked
BLOCK_MINUTES = 60
NAME_RE = re.compile(r"^[A-Za-z0-9 ]{1,20}$")
PIN_RE = re.compile(r"^\d{4}$")


class FavError(Exception):
    """An API error: HTTP status + message for the user."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _now():
    from database import utcnow
    return utcnow()


# ---------------------------------------------------------------- PIN + tokens
def hash_pin(pin):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt.hex()}${h.hex()}"


def pin_ok(pin, stored):
    try:
        _, it, salt, want = stored.split("$")
        h = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(h.hex(), want)
    except (ValueError, AttributeError):
        return False


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _new_session(session, profile):
    from models import FavoriteSession
    token = secrets.token_urlsafe(24)                         # 32 characters
    now = _now()
    session.query(FavoriteSession).filter(FavoriteSession.expires_at < now).delete(synchronize_session=False)
    session.add(FavoriteSession(token_hash=_token_hash(token), profile_id=profile.id,
                                expires_at=now + timedelta(hours=SESSION_HOURS)))
    return token, now + timedelta(hours=SESSION_HOURS)


# ---------------------------------------------------------------- rate limit
def _check_rate(session, ip):
    from models import FavoritePinAttempt
    now = _now()
    q = session.query(FavoritePinAttempt).filter(FavoritePinAttempt.ip == ip)
    fails = (q.filter(FavoritePinAttempt.ok.is_(False), FavoritePinAttempt.at >= now - timedelta(hours=1))
             .order_by(FavoritePinAttempt.at).all())
    if len(fails) >= FAILS_TO_BLOCK:
        until = fails[FAILS_TO_BLOCK - 1].at + timedelta(minutes=BLOCK_MINUTES)
        if until > now:
            mins = max(1, int((until - now).total_seconds() // 60) + 1)
            raise FavError(429, f"Too many wrong PINs. Try again in {mins} minute{'s' if mins != 1 else ''}.")
    if q.filter(FavoritePinAttempt.at >= now - timedelta(minutes=1)).count() >= PER_MINUTE:
        raise FavError(429, "Too many attempts. Wait a minute and try again.")


def _record(session, ip, ok):
    from models import FavoritePinAttempt
    now = _now()
    session.add(FavoritePinAttempt(ip=ip, ok=ok, at=now))
    session.query(FavoritePinAttempt).filter(FavoritePinAttempt.at < now - timedelta(days=1)).delete(
        synchronize_session=False)
    session.commit()


def _check_pin(session, profile, pin, ip):
    """Rate-limited PIN check; raises FavError on a wrong PIN."""
    if not PIN_RE.match(str(pin or "")):
        raise FavError(400, "The PIN must be exactly 4 digits.")
    _check_rate(session, ip)
    ok = pin_ok(str(pin), profile.pin_hash)
    _record(session, ip, ok)
    if not ok:
        raise FavError(401, "Wrong PIN.")


# ---------------------------------------------------------------- profiles
def _profile(session, name):
    from models import FavoriteProfile
    p = session.query(FavoriteProfile).filter_by(name_key=(name or "").strip().lower()).first()
    if p is None:
        raise FavError(404, f"No profile named '{name}'.")
    return p


def _authorize(session, profile, token=None, pin=None, ip="?"):
    """A valid session token for this profile, or the right PIN."""
    from models import FavoriteSession
    if token:
        s = session.query(FavoriteSession).filter_by(token_hash=_token_hash(token)).first()
        if s is not None and s.profile_id == profile.id and s.expires_at > _now():
            return
        if not pin:
            raise FavError(401, "Your session expired. Enter the PIN again.")
    if pin is None:
        raise FavError(401, "PIN required.")
    _check_pin(session, profile, pin, ip)


def list_profiles(session):
    from sqlalchemy import func
    from models import FavoriteProfile, FavoriteProfileCoin
    counts = dict(session.query(FavoriteProfileCoin.profile_id, func.count()).group_by(FavoriteProfileCoin.profile_id).all())
    last = dict(session.query(FavoriteProfileCoin.profile_id, func.max(FavoriteProfileCoin.added_at))
                .group_by(FavoriteProfileCoin.profile_id).all())
    # Leaderboard: each profile's average P&L since added (current prices;
    # delisted coins at their last known price).
    from models import Coin
    rows = session.query(FavoriteProfileCoin).all()
    contracts = list({r.coin_contract for r in rows})
    price = {(c.contract or "").lower(): c.price_usd for c in
             session.query(Coin).filter(func.lower(Coin.contract).in_(contracts))} if contracts else {}
    pnls = {}
    for r in rows:
        now_p = price.get(r.coin_contract) if price.get(r.coin_contract) is not None else r.last_known_price
        if now_p is not None and r.entry_price:
            pnls.setdefault(r.profile_id, []).append((now_p - r.entry_price) / r.entry_price * 100)
    out = []
    for p in session.query(FavoriteProfile).order_by(FavoriteProfile.created_at):
        upd = max([d for d in (p.updated_at, last.get(p.id)) if d is not None], default=None)
        v = pnls.get(p.id) or []
        out.append({"name": p.name, "coin_count": counts.get(p.id, 0),
                    "avg_pnl": round(sum(v) / len(v), 2) if v else None,
                    "tracking_since": (p.tracking_since or p.created_at).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "created_at": p.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "last_updated": upd.strftime("%Y-%m-%dT%H:%M:%SZ") if upd else None})
    return {"profiles": out, "max": MAX_PROFILES, "max_coins": MAX_COINS}


def create_profile(session, name, pin):
    from models import FavoriteProfile
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not NAME_RE.match(name):
        raise FavError(400, "Name: 1-20 letters, digits or spaces.")
    if not PIN_RE.match(str(pin or "")):
        raise FavError(400, "The PIN must be exactly 4 digits.")
    if session.query(FavoriteProfile).count() >= MAX_PROFILES:
        raise FavError(409, "Max 5 profiles reached.")
    if session.query(FavoriteProfile).filter_by(name_key=name.lower()).first():
        raise FavError(409, f"The name '{name}' is already taken.")
    now = _now()
    p = FavoriteProfile(name=name, name_key=name.lower(), pin_hash=hash_pin(str(pin)), created_at=now, updated_at=now,
                        tracking_since=now)
    session.add(p)
    session.flush()
    token, exp = _new_session(session, p)
    session.commit()
    return {"name": p.name, "session_token": token, "expires_at": exp.strftime("%Y-%m-%dT%H:%M:%SZ")}


def verify_pin(session, name, pin, ip):
    p = _profile(session, name)
    _check_pin(session, p, pin, ip)
    token, exp = _new_session(session, p)
    session.commit()
    return {"name": p.name, "session_token": token, "expires_at": exp.strftime("%Y-%m-%dT%H:%M:%SZ")}


def delete_profile(session, name, pin, ip):
    from models import FavoriteProfileCoin, FavoriteSession
    p = _profile(session, name)
    _check_pin(session, p, pin, ip)                           # always the PIN, never a token
    session.query(FavoriteProfileCoin).filter_by(profile_id=p.id).delete(synchronize_session=False)
    session.query(FavoriteSession).filter_by(profile_id=p.id).delete(synchronize_session=False)
    from challenge import delete_for_profile
    delete_for_profile(session, p.id)                         # its paper trading challenge too
    session.delete(p)
    session.commit()
    return {"deleted": p.name}


# ---------------------------------------------------------------- coins
def _snapshot(session, row, coin, ticker=None, perf=False):
    """Copy the coin's current data into the favorite row (ticker / perf can
    be passed in when the caller already has them for many coins)."""
    from main import latest_snapshot
    from listings import _tickers
    from performance import safe_compute
    snap = latest_snapshot(session, coin.id)
    if perf is False:
        perf = safe_compute(session, [coin]).get(coin.id)
    row.coin_id, row.coin_name, row.coin_chain = coin.id, coin.name, coin.chain
    if ticker is None:
        ticker = _tickers(session, [coin.id]).get(coin.id)
    row.coin_symbol = ticker or row.coin_symbol
    row.last_known_price = coin.price_usd if coin.price_usd is not None else row.last_known_price
    row.last_known_mcap = (snap.mcap if snap and snap.mcap is not None else row.last_known_mcap)
    row.last_known_pnl = perf["change_pct"] if perf else row.last_known_pnl
    row.last_known_status, row.delisted_at, row.updated_at = coin.status, None, _now()


def add_coin(session, name, contract, token=None, pin=None, ip="?"):
    from sqlalchemy import func
    from models import Coin, FavoriteProfileCoin
    p = _profile(session, name)
    _authorize(session, p, token, pin, ip)
    contract = (contract or "").strip().lower()
    coin = session.query(Coin).filter(func.lower(Coin.contract) == contract).first() if contract else None
    if coin is None:
        raise FavError(404, "That coin isn't in our database.")
    row = session.query(FavoriteProfileCoin).filter_by(profile_id=p.id, coin_contract=contract).first()
    if row is None:
        if session.query(FavoriteProfileCoin).filter_by(profile_id=p.id).count() >= MAX_COINS:
            raise FavError(409, f"Max {MAX_COINS} favorites per profile.")
        row = FavoriteProfileCoin(profile_id=p.id, coin_contract=contract, added_at=_now(), entry_price=coin.price_usd)
        session.add(row)
    _snapshot(session, row, coin)
    if row.entry_price is None:
        row.entry_price = row.last_known_price
    p.updated_at = _now()
    session.commit()
    return {"added": contract, "coin_id": coin.id}


def remove_coin(session, name, coin_ref, token=None, pin=None, ip="?"):
    """Coins are locked in the portfolio - only Reset removes them."""
    raise FavError(409, "This coin is locked in your portfolio. Use Reset to start fresh.")


def reset_profile(session, name, pin, ip):
    """Wipe all coins and restart the portfolio; always needs the PIN."""
    from models import FavoriteProfileCoin
    p = _profile(session, name)
    _check_pin(session, p, pin, ip)                           # never a session token
    n = session.query(FavoriteProfileCoin).filter_by(profile_id=p.id).delete(synchronize_session=False)
    p.tracking_since = p.updated_at = _now()
    session.commit()
    return {"reset": p.name, "removed": n, "tracking_since": p.tracking_since.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _remove_coin_unlocked(session, name, coin_ref, token=None, pin=None, ip="?"):
    """Individual removal from before coins were locked (not exposed)."""
    from models import FavoriteProfileCoin
    p = _profile(session, name)
    _authorize(session, p, token, pin, ip)
    q = session.query(FavoriteProfileCoin).filter_by(profile_id=p.id)
    ref = str(coin_ref).strip().lower()
    row = (q.filter_by(coin_id=int(ref)).first() if ref.isdigit() else None) or q.filter_by(coin_contract=ref).first()
    if row is None:
        raise FavError(404, "That coin isn't in this profile.")
    session.delete(row)
    p.updated_at = _now()
    session.commit()
    return {"removed": row.coin_contract}


def get_profile(session, name):
    """Public: the profile's coins - current data for coins still in our
    database, the last snapshot (delisted: true) for deleted ones."""
    from sqlalchemy import func
    from models import Coin, FavoriteProfileCoin
    p = _profile(session, name)
    rows = session.query(FavoriteProfileCoin).filter_by(profile_id=p.id).order_by(FavoriteProfileCoin.added_at).all()
    contracts = [r.coin_contract for r in rows]
    alive = {(c.contract or "").lower(): c for c in
             session.query(Coin).filter(func.lower(Coin.contract).in_(contracts))} if contracts else {}
    coins = []
    today = _now().date()
    for r in rows:
        c = alive.get(r.coin_contract)
        now_price = c.price_usd if c is not None and c.price_usd is not None else r.last_known_price
        pnl = ((now_price - r.entry_price) / r.entry_price * 100
               if now_price is not None and r.entry_price else None)
        coins.append({
            "entry_price": r.entry_price, "current_price": now_price,
            "pnl": round(pnl, 2) if pnl is not None else None,
            "days_held": max(0, (today - r.added_at.date()).days),
            "coin_id": r.coin_id, "contract": c.contract if c else r.coin_contract,
            "name": c.name if c else r.coin_name, "symbol": r.coin_symbol, "chain": c.chain if c else r.coin_chain,
            "status": c.status if c else "delisted", "delisted": c is None,
            "delisted_at": r.delisted_at.isoformat() if r.delisted_at else None,
            "price": (c.price_usd if c and c.price_usd is not None else r.last_known_price),
            "last_known_price": r.last_known_price, "last_known_mcap": r.last_known_mcap,
            "last_known_pnl": r.last_known_pnl,
            "added_at": r.added_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "snapshot_at": r.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.updated_at else None,
        })
    return {"name": p.name, "coin_count": len(coins), "max_coins": MAX_COINS, "coins": coins,
            "portfolio": portfolio_summary(coins, p.tracking_since or p.created_at)}


def portfolio_summary(coins, since):
    """Average P&L since added (delisted coins included, frozen), counts,
    best / worst pick, tracking start."""
    vals = [c for c in coins if c.get("pnl") is not None]
    out = {"tracking_since": since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else None,
           "days": max(0, (_now().date() - since.date()).days) if since else None,
           "coins": len(coins), "with_pnl": len(vals), "avg_pnl": None,
           "up": 0, "down": 0, "flat": 0, "best": None, "worst": None}
    if not vals:
        return out
    out["avg_pnl"] = round(sum(c["pnl"] for c in vals) / len(vals), 2)
    out["up"] = sum(1 for c in vals if c["pnl"] >= 1)
    out["down"] = sum(1 for c in vals if c["pnl"] <= -1)
    out["flat"] = len(vals) - out["up"] - out["down"]

    def pick(c):
        return {"name": c["name"], "contract": c["contract"], "pnl": c["pnl"], "delisted": c["delisted"]}
    out["best"] = pick(max(vals, key=lambda c: c["pnl"]))
    out["worst"] = pick(min(vals, key=lambda c: c["pnl"]))
    return out


def refresh_snapshots():
    """Daily job step: refresh every favorite's snapshot from current data;
    a coin no longer in our database keeps its last snapshot and gets
    delisted_at = today (once)."""
    from sqlalchemy import func
    from database import SessionLocal
    from models import Coin, FavoriteProfileCoin
    session = SessionLocal()
    stats = {"favorites": 0, "refreshed": 0, "delisted_now": 0}
    try:
        rows = session.query(FavoriteProfileCoin).all()
        stats["favorites"] = len(rows)
        contracts = list({r.coin_contract for r in rows})
        alive = {(c.contract or "").lower(): c for c in
                 session.query(Coin).filter(func.lower(Coin.contract).in_(contracts))} if contracts else {}
        today = _now().date()
        from listings import _tickers
        from performance import safe_compute
        coins = list({c.id: c for c in alive.values()}.values())
        tick = _tickers(session, [c.id for c in coins]) if coins else {}
        perfs = safe_compute(session, coins) if coins else {}
        for r in rows:
            c = alive.get(r.coin_contract)
            if c is not None:
                _snapshot(session, r, c, ticker=tick.get(c.id) or "", perf=perfs.get(c.id))
                stats["refreshed"] += 1
            elif r.delisted_at is None:
                r.delisted_at = today
                stats["delisted_now"] += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return stats
