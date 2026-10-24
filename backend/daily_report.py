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
LIMITS = {"Codex": 10_000, "CoinGecko": 10_000, "CoinMarketCap": 15_000, "DropsTab": 500_000,
          "Birdeye": 30_000}      # Birdeye: compute units (3 CU per call), not calls
NEAR_LIMIT_SHARE = 0.8      # warn when the month is projected past 80% of a limit
HOSTS = {"graph.codex.io": "Codex", "api.coingecko.com": "CoinGecko", "public-api.dropstab.com": "DropsTab", "api.coinalyze.net": "Coinalyze",
         "pro-api.coinmarketcap.com": "CoinMarketCap", "api.dexscreener.com": "DexScreener",
         "api.llama.fi": "DefiLlama", "api.gopluslabs.io": "GoPlus", "public-api.birdeye.so": "Birdeye"}
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
        self.listings = None        # listings.run_listings_update() stats
        self.volume = None          # volume_anomaly.run_volume_anomaly_detection() stats
        self.price_check = None     # price_sources.run_price_sanity() stats
        self.birdeye = None         # liquidity_verify.run_birdeye_fetch() stats


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
    if r.startswith("Low liquidity confirmed by only 1 source"):
        return "سيولة: مصدر واحد فوق $5K"
    if r.startswith("Liquidity between $3K-"):
        return "سيولة borderline ($3K–$5K)"
    if r.startswith("Liquidity down"):
        return "السيولة"
    if r.startswith("Holders down"):
        return "الحاملين"
    if r.startswith("Honeypot"):
        return "honeypot"
    return "أخرى"


def liquidity_reason_ar(reason):
    """The 3-source liquidity removal / drop reason in Darija."""
    r = reason or ""
    out = r
    for en, ar in (("No liquidity data available", "ما كاين حتى مصدر فيه داتا ديال السيولة"),
                   ("Insufficient liquidity sources (chain not supported)",
                    "غير مصدر واحد فيه السيولة (الشبكة ما مدعوماش)"),
                   ("Liquidity below $3K (dead)", "السيولة تحت $3K (ميتة)"),
                   ("Liquidity between $3K-", "السيولة borderline (بين $3K والحد الأدنى)"),
                   ("Low liquidity confirmed by only 1 source", "غير مصدر واحد فوق $5K")):
        if r.startswith(en):
            out = ar
            break
    if "(abandoned:" in r:
        out += " — متروكة (Birdeye ما تحدّثش 48 ساعة+)"
    return out


def _api_usage(session, today):
    """[(source, this run, month to date, limit, days of data)] - saves this
    run's counts first. "days of data" = days this month with counted runs
    (the month projection divides by it, not by the date - counting started
    mid-month)."""
    from models import ApiUsage
    from sqlalchemy import func
    rows = {r.source: r for r in session.query(ApiUsage).filter(ApiUsage.day == today)}
    # Birdeye: billable calls only (a 429 retry isn't charged) = CU / 3.
    import birdeye
    if birdeye.CU_USED or CALLS.get("Birdeye"):
        CALLS["Birdeye"] = birdeye.CU_USED // birdeye.CU_PER_CALL
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
    for source in ("Codex", "CoinGecko", "DropsTab"):
        used = int(month.get(source) or 0)
        out.append((source, CALLS.get(source, 0), used, LIMITS[source], int(days.get(source) or 1)))
    # Birdeye: compute units = calls x 3 (the daily job's calls only).
    from birdeye import CU_PER_CALL
    out.append(("Birdeye (CU)", CALLS.get("Birdeye", 0) * CU_PER_CALL,
                int(month.get("Birdeye") or 0) * CU_PER_CALL, LIMITS["Birdeye"], int(days.get("Birdeye") or 1)))
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

    # 🧹 / 🗑️ removed for good by the 3-source liquidity check: too few
    # sources (presence rule) and effective liquidity under $3K, separately
    gone = det.get("removed_liquidity") or []
    dead = [g for g in gone if (g[1] or "").startswith("Liquidity below $3K")]
    few = [g for g in gone if g not in dead]
    if few:
        lines.append(f"\n🧹 تحيدو نهائياً (مصادر السيولة ما كافياش): {len(few)}")
        lines.extend(f"• {n} — {liquidity_reason_ar(r)}\n  {readings}" for n, r, readings, _ in few[:MAX_NAMES])
        if len(few) > MAX_NAMES:
            lines.append(f"و{len(few) - MAX_NAMES} أخرى")
    if dead:
        lines.append(f"\n🗑️ عملات حيدات نهائياً (سيولة < $3K): {len(dead)}")
        lines.extend(f"• {n} — ${v or 0:,.0f}" + (" (متروكة)" if "(abandoned:" in (r or "") else "")
                     for n, r, _, v in dead[:MAX_NAMES])
        if len(dead) > MAX_NAMES:
            lines.append(f"و{len(dead) - MAX_NAMES} أخرى")
    # 💥 24h crash (2 liquidity sources -15%+ / price -30%+) - removed for good
    c24 = det.get("crash_24h") or []
    if c24:
        def crash_line(n, liq, price):
            bits = []
            if liq:
                bits.append("Liquidity " + " + ".join(f"{v:.0f}%" for v in sorted(liq.values())) + " (مصدرين)")
            if price:
                bits.append(f"السعر طاح {price[0]:.0f}%")
            return f"• {n} — " + " · ".join(bits)
        lines.append("\n💥 عملات حيدات بسبب crash:")
        lines.extend(crash_line(*x) for x in c24[:MAX_NAMES])
        if len(c24) > MAX_NAMES:
            lines.append(f"و{len(c24) - MAX_NAMES} أخرى")
    # 🚫 price mismatch (> 2x between two sources) - removed for good
    pmm = det.get("price_mismatch") or []
    if pmm:
        lines.append("\n🚫 عملات حيدات بسبب اختلاف السعر:")
        lines.extend(f"• {n} — {a} ${pa:.4g} / {b} ${pb:.4g} ({r:.1f}x فرق)" for n, a, pa, b, pb, r in pmm[:MAX_NAMES])
        if len(pmm) > MAX_NAMES:
            lines.append(f"و{len(pmm) - MAX_NAMES} أخرى")
    liq_drops = [(n, r) for n, r in dropped
                 if (r or "").startswith(("Low liquidity confirmed by only 1 source", "Liquidity between $3K-"))]
    if liq_drops:
        lines.append("⚠️ تمشات لـ Dropped (السيولة، عندهم 7 أيام): " + "، ".join(
            f"{n} ({'borderline liquidity' if r.startswith('Liquidity between') else 'مصدر واحد فوق $5K'})"
            for n, r in liq_drops[:MAX_NAMES]))
    # 🔄 back in active after a liquidity drop (each also sent as its own alert)
    back = det.get("liq_recovered") or []
    if back:
        lines.append(f"\n🔄 عملات رجعت (السيولة طلعت فوق $5K): {_names([n for n, _ in back])}")

    # 🏦 exchange listings (new / scheduled / delisted), only when there's news
    lst = rep.listings or {}
    if lst.get("new"):
        lines.append(f"\n🏦 إدراج جديد ف بورصة: {len(lst['new'])}")
        lines.extend(f"• 🆕 {n} — {ex} ({pair})" for n, ex, pair in lst["new"][:MAX_NAMES])
    if lst.get("scheduled"):
        lines.append("\n📅 إدراج مبرمج:")
        lines.extend(f"• {n} — {ex}" + (f" نهار {when:%Y-%m-%d %H:%M} UTC" if when else "")
                     for n, ex, when in lst["scheduled"][:MAX_NAMES])
    if lst.get("delisted"):
        lines.append("📉 تحيدات من بورصة: " + "، ".join(f"{n} ({ex})" for n, ex in lst["delisted"][:MAX_NAMES]))

    # 📊 unusual volume (big / medium spikes, each coin at most once per 3 days)
    vol = rep.volume or {}
    if vol.get("alerts"):
        def usd(v):
            return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"
        lines.append(f"\n📊 فوليوم غير عادي: {len(vol['alerts'])}")
        lines.extend(f"• {'🚨' if sev == 'big' else '⚠️'} {n} — {ratio:.1f}x ({usd(today)} مقابل متوسط {usd(avg)})"
                     for n, sev, ratio, today, avg in vol["alerts"][:MAX_NAMES])

    # ⚠️ borderline price disagreement, 1.5x-2x (warning only; > 2x = removed)
    pc = rep.price_check or {}
    if pc.get("mismatches"):
        def px(v):
            return f"${v:.6g}"
        lines.append("\n⚠️ تحذير: اختلاف في السعر (1.5x–2x)")
        lines.extend(f"• {n} — {a} {px(pa)} / {b} {px(pb)} ({r:,.1f}x فرق)"
                     for n, a, pa, b, pb, r in pc["mismatches"][:MAX_NAMES])

    # ⚠️ problems / data-quality flags
    issues = []
    if rep.failed:
        issues.append("التشغيل وقف بخطأ — الأرقام ناقصة")
    for step in rep.step_errors:
        issues.append(f"خطوة {step} فشلات (تخطيناها)")
    if lst.get("exchanges_failed"):
        issues.append("Listings: ما تقراوش " + "، ".join(lst["exchanges_failed"]))
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
    be = rep.birdeye or {}
    if be.get("error") or be.get("skipped_cap"):
        issues.append(f"Birdeye: {be.get('error', 0) + be.get('skipped_cap', 0)} طلبات فشلات — ما تقررش فيهم")
    if drop.get("liq_unknown"):
        issues.append(f"السيولة (3 مصادر): {drop['liq_unknown']} عملات بلا قرار (مصدر فشل)")
    flags = (f"🧪 جودة البيانات: dust pool {trk.get('dust_refetched', 0)} · سعر مصحّح "
             f"{trk.get('price_replaced', 0)} · fix-4 {drop.get('liq_source_mismatch', 0)} · "
             f"انهيار مصدر واحد {drop.get('crash_suspects', 0)}")
    lines.append("\n⚠️ مشاكل مهمة: " + ("ما كاين والو" if not issues else ""))
    lines.extend(f"• {x}" for x in issues)
    lines.append(flags)
    lines.append("🗄️ توفير الـegress: كاش Vercel شغال (API ‏1–10 دقايق) · تحديث الصفحة كل 30 دقيقة")

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
        if source.startswith("Birdeye"):
            note = " (التشغيل اليومي فقط، بلا Check a Coin)"
        lines.append(f"• {source}: {run_n if run_n is not None else '—'} / {month:,} / {limit:,}{note}{flag}")

    # خلاصة
    active = drop.get("active_remaining")
    if rep.failed:
        summary = "التشغيل ما كملش — خاص نشوفو اللوغ ف GitHub Actions."
    else:
        summary = (f"التشغيل داز{' مزيان' if not issues else ' ولكن كاين ملاحظات'}: "
                   f"{len(new)} دخلو، {len(dropped)} تحيدو، {len(rec)} رجعو"
                   + (f"، {len(gone) + len(c24) + len(pmm)} تحيدو نهائياً (السيولة / crash / السعر)"
                      if gone or c24 or pmm else "")
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
        # 🔄 one alert per coin back in Active after a liquidity drop.
        for name, alert in ((rep.drop or {}).get("details") or {}).get("liq_recovered") or []:
            if send_message(alert):
                log.info("Telegram recovery alert sent (%s)", name)
    except Exception as e:
        log.warning("Telegram report failed - run unaffected (%s)", type(e).__name__)
