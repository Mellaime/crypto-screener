"""
daily_report.py
---------------
The DAILY TELEGRAM REPORT, sent at the end of every daily run (scheduler.py):
new coins, drops (by reason), recoveries, problems / data-quality flags, API
usage and a one-line summary - in Arabic (Darija).

Delivery: Telegram Bot API sendMessage (one HTTPS POST, no extra library),
to TELEGRAM_CHAT_ID with TELEGRAM_BOT_TOKEN (backend/.env locally, repo
Actions secrets in GitHub). No token / chat id -> the report is skipped.

ROBUST: send_report() never raises - any problem (Telegram down, bad token,
a bug here) is logged and the run carries on. The token is never printed:
errors are reported by type / HTTP status only (a requests error message
would contain the URL, which contains the token).

API usage: count_requests() counts every HTTP request the run makes, per
host (all of them go through requests' Session.request). Each run's counts
are added to the api_usage table; month-to-date = this month's rows (daily
job only - website page views aren't counted). CoinMarketCap's own credit
counter (/v1/key/info, free) is used for CMC.
"""

import os
import logging
from collections import Counter
from datetime import date, datetime, timezone
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
log = logging.getLogger("daily_report")

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4000              # Telegram's limit is 4096 characters
MAX_NAMES = 15              # names listed per section, then "+N"

# Monthly free-tier limits (CMC's comes from its own counter).
LIMITS = {"Codex": 10_000, "CoinGecko": 10_000, "CoinMarketCap": 15_000}
NEAR_LIMIT_SHARE = 0.8      # warn when the month is projected past 80% of a limit
HOSTS = {"graph.codex.io": "Codex", "api.coingecko.com": "CoinGecko",
         "pro-api.coinmarketcap.com": "CoinMarketCap", "api.dexscreener.com": "DexScreener",
         "api.llama.fi": "DefiLlama", "api.gopluslabs.io": "GoPlus"}
MASS_DROP_SHARE = 0.5       # more than half of the checked coins dropped -> flag

# ---------------------------------------------------------------- counting
CALLS = Counter()
_installed = False


def count_requests():
    """Count every outgoing HTTP request by API (once per process)."""
    global _installed
    if _installed:
        return
    original = requests.sessions.Session.request

    def counted(self, method, url, *args, **kwargs):
        try:
            source = HOSTS.get(urlparse(url).hostname or "")
            if source:
                CALLS[source] += 1
        except Exception:
            pass
        return original(self, method, url, *args, **kwargs)

    requests.sessions.Session.request = counted
    _installed = True


# ---------------------------------------------------------------- the run's facts
class RunReport:
    """Filled by scheduler.run_pipeline as the steps finish."""

    def __init__(self):
        self.started = datetime.now(timezone.utc).replace(tzinfo=None)
        self.failed = None          # "ErrorType: message" if the run crashed
        self.step_errors = []       # display steps that failed but were skipped
        self.save = None            # save_tokens() stats
        self.tracking = None        # run_daily_tracking() stats
        self.drop = None            # run_drop_logic() stats
        self.scored = None          # number of coins scored


def _new_coin_names(session, since):
    from models import Coin
    return [c.name for c in session.query(Coin).filter(Coin.first_seen >= since)
            .order_by(Coin.first_seen)]


def _names(names):
    names = [n.strip() for n in names if n]
    if not names:
        return ""
    shown = "، ".join(names[:MAX_NAMES])
    return shown + (f" و{len(names) - MAX_NAMES} أخرى" if len(names) > MAX_NAMES else "")


def _reason_kind(reason):
    r = reason or ""
    if r.startswith("Crash - "):
        return "انهيار السعر"
    if r.startswith("Liquidity crash"):
        return "انهيار السيولة"
    if r.startswith("Volume falling"):
        return "الفوليم"
    if r.startswith("Liquidity under"):
        return "سيولة أقل من $5K"
    if r.startswith("Liquidity down"):
        return "السيولة"
    if r.startswith("Holders down"):
        return "الحاملين"
    if r.startswith("Honeypot"):
        return "honeypot"
    return "أخرى"


def _api_usage(session, today):
    """[(source, this run, month to date, limit, days of data)] - saves this
    run's counts first. "days of data" = days this month with counted runs
    (the month projection divides by it, not by the date - counting started
    mid-month)."""
    from models import ApiUsage
    from sqlalchemy import func
    rows = {r.source: r for r in session.query(ApiUsage).filter(ApiUsage.day == today)}
    for source, n in CALLS.items():
        row = rows.get(source)
        if row is None:
            row = ApiUsage(day=today, source=source, calls=0)
            session.add(row)
        row.calls = (row.calls or 0) + n
    session.commit()
    month_start = today.replace(day=1)
    month = dict(session.query(ApiUsage.source, func.sum(ApiUsage.calls))
                 .filter(ApiUsage.day >= month_start).group_by(ApiUsage.source).all())
    days = dict(session.query(ApiUsage.source, func.count(func.distinct(ApiUsage.day)))
                .filter(ApiUsage.day >= month_start).group_by(ApiUsage.source).all())
    out = []
    for source in ("Codex", "CoinGecko"):
        used = int(month.get(source) or 0)
        out.append((source, CALLS.get(source, 0), used, LIMITS[source], int(days.get(source) or 1)))
    # CoinMarketCap: its own exact credit counter (free).
    try:
        key = os.getenv("COINMARKETCAP_API_KEY")
        u = (requests.get("https://pro-api.coinmarketcap.com/v1/key/info",
                          headers={"X-CMC_PRO_API_KEY": key}, timeout=20).json()
             .get("data") or {}).get("usage") or {}
        out.append(("CoinMarketCap", (u.get("current_day") or {}).get("credits_used"),
                    (u.get("current_month") or {}).get("credits_used"), LIMITS["CoinMarketCap"],
                    today.day))
    except Exception:
        out.append(("CoinMarketCap", None, None, LIMITS["CoinMarketCap"], today.day))
    return out


# ---------------------------------------------------------------- the message
def build_message(rep, session):
    """The report text (Arabic). Never raises on missing parts."""
    from calendar import monthrange
    today = datetime.now(timezone.utc).date()
    drop = rep.drop or {}
    det = drop.get("details") or {}
    trk = rep.tracking or {}
    lines = [f"🤖 تقرير التشغيل اليومي — {today:%Y-%m-%d}"]
    if rep.failed:
        lines.append("🔴 التشغيل فشل! " + rep.failed[:200])

    # 📊 new coins
    try:
        new = _new_coin_names(session, rep.started)
    except Exception:
        session.rollback()
        new = []
    lines.append(f"\n📊 عملات جداد دخلو: {len(new)}" + (f"\n{_names(new)}" if new else ""))

    # ❌ dropped (+ crash-blocked + deleted after 7 days)
    dropped = det.get("dropped") or []
    kinds = Counter(_reason_kind(r) for _, r in dropped)
    line = f"\n❌ محذوفين: {len(dropped)}"
    if kinds:
        line += " (" + "، ".join(f"{k} {n}" for k, n in kinds.most_common()) + ")"
    lines.append(line)
    if det.get("blocked"):
        lines.append(f"⛔ انهيار سعر — تحيدو وتبلوكاو 15 يوم: {_names([n for n, _ in det['blocked']])}")
    if det.get("deleted"):
        lines.append(f"🗑️ تمسحو (7 أيام بلا تحسن): {len(det['deleted'])}")

    # ✅ recovered
    rec = det.get("recovered") or []
    lines.append(f"\n✅ راجعين لـActive: {len(rec)}" + (f"\n{_names(rec)}" if rec else ""))

    # ⚠️ problems / data-quality flags
    issues = []
    if rep.failed:
        issues.append("التشغيل وقف بخطأ — الأرقام ناقصة")
    for step in rep.step_errors:
        issues.append(f"خطوة {step} فشلات (تخطيناها)")
    if trk.get("codex_failed_batches"):
        issues.append(f"Codex: {trk['codex_failed_batches']} دفعات فشلات")
    try:
        from discovery import DISCOVERY_ISSUES
        if DISCOVERY_ISSUES.get("coingecko_failed_pages"):
            issues.append(f"CoinGecko: صفحات فشلات {DISCOVERY_ISSUES['coingecko_failed_pages']}")
        if DISCOVERY_ISSUES.get("cmc_failed"):
            issues.append("CoinMarketCap listings فشل")
    except Exception:
        pass
    checked = drop.get("checked") or 0
    if checked and len(dropped) > MASS_DROP_SHARE * checked:
        issues.append(f"بزاف ديال الحذف: {len(dropped)} من {checked} — خاصها مراجعة")
    flags = (f"🧪 جودة البيانات: dust pool {trk.get('dust_refetched', 0)} · سعر مصحّح "
             f"{trk.get('price_replaced', 0)} · fix-4 {drop.get('liq_source_mismatch', 0)} · "
             f"انهيار مصدر واحد {drop.get('crash_suspects', 0)}")
    lines.append("\n⚠️ مشاكل مهمة: " + ("ما كاين والو" if not issues else ""))
    lines.extend(f"• {x}" for x in issues)
    lines.append(flags)

    # 🔌 APIs
    lines.append("\n🔌 الـAPIs (هاد التشغيل / الشهر / الحد):")
    near = []
    try:
        usage = _api_usage(session, today)
    except Exception:
        session.rollback()
        usage = []
    days_in_month = monthrange(today.year, today.month)[1]
    for source, run_n, month, limit, days in usage:
        if month is None:
            lines.append(f"• {source}: ما قدرناش نقراو الاستهلاك")
            continue
        projected = month / max(days, 1) * days_in_month      # daily average x month
        flag = " ⚠️ قريب من الحد" if projected > NEAR_LIMIT_SHARE * limit else ""
        if flag:
            near.append(source)
        note = " (التشغيل اليومي فقط)" if source != "CoinMarketCap" else ""
        lines.append(f"• {source}: {run_n if run_n is not None else '—'} / {month:,} / {limit:,}{note}{flag}")

    # خلاصة
    active = drop.get("active_remaining")
    if rep.failed:
        summary = "التشغيل ما كملش — خاص نشوفو اللوغ ف GitHub Actions."
    else:
        summary = (f"التشغيل داز{' مزيان' if not issues else ' ولكن كاين ملاحظات'}: "
                   f"{len(new)} دخلو، {len(dropped)} تحيدو، {len(rec)} رجعو"
                   + (f" — دابا {active} عملة ف Active." if active is not None else "."))
        if near:
            summary += f" انتبه للاستهلاك ديال {'، '.join(near)}."
    lines.append(f"\n📝 خلاصة: {summary}")
    text = "\n".join(lines)
    return text if len(text) <= MAX_LEN else text[:MAX_LEN - 3] + "..."


# ---------------------------------------------------------------- sending
def send_message(text):
    """POST to Telegram. Returns True on success; never raises, never logs the token."""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.warning("Telegram report skipped - TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return False
    for attempt in (1, 2):
        try:
            r = requests.post(TELEGRAM_URL.format(token=token),
                              json={"chat_id": chat, "text": text,
                                    "disable_web_page_preview": True}, timeout=20)
            if r.status_code == 200 and (r.json() or {}).get("ok"):
                return True
            log.warning("Telegram report not sent (HTTP %s, attempt %d)", r.status_code, attempt)
        except Exception as e:                 # message would contain the URL -> type only
            log.warning("Telegram report not sent (%s, attempt %d)", type(e).__name__, attempt)
    return False


def send_report(rep):
    """Build + send the run's report. NEVER raises (the run must not fail
    because of the report)."""
    try:
        from database import SessionLocal
        session = SessionLocal()
        try:
            text = build_message(rep, session)
        finally:
            session.close()
        if send_message(text):
            log.info("Telegram report sent (%d characters)", len(text))
    except Exception as e:
        log.warning("Telegram report failed - run unaffected (%s)", type(e).__name__)
