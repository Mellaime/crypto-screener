"""
scheduler.py
------------
Phase 2 - the SCHEDULER. This is what makes the screener run on its own.

What it does:
  * Runs the full pipeline ONCE right now (so you can confirm it works
    without waiting a day). The daily sequence is:
        discover -> filter -> save -> drop -> rank -> holders -> score
    (scoring ranks the ACTIVE coins so the dashboard is always up to date).
  * Then runs it automatically every 24 hours, forever.
  * Logs a timestamped funnel summary each run.
  * Is crash-safe: if one run errors out, it logs the error and the
    schedule stays alive for the next run.

Why it matters:
  Every daily run stores today's numbers as a new snapshot. Over several
  days this builds the history that time-based rules need (like the real
  "volume rising over 5 days" check in is_volume_rising()).

Run it (real use - runs forever, stop with Ctrl+C):
    python scheduler.py

Run a quick demo (does the immediate run, shows the next scheduled time,
then exits instead of blocking):
    python scheduler.py --demo
"""

import sys
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.background import BackgroundScheduler

from database import init_db
from fetch_dexscreener import discover_tokens
from save_to_db import save_tokens, print_summary
from drop_logic import run_drop_logic
from rank_check import run_rank_update
from holders_check import run_holders_update
from scoring import score_all, print_ranking

# How often the pipeline runs automatically.
RUN_EVERY_HOURS = 24

# --- Logging setup ---------------------------------------------------------
# Every log line is automatically stamped with the date and time.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("scheduler")
# Let APScheduler's own INFO messages through too (shows job scheduling).
logging.getLogger("apscheduler").setLevel(logging.INFO)


def run_pipeline():
    """
    One full run of the screener. This is the function the scheduler calls.

    It is wrapped in try/except so that a failure (e.g. the API is down)
    is logged but does NOT stop the scheduler - the next run still happens.
    """
    log.info("=" * 55)
    log.info("PIPELINE RUN START")
    try:
        tokens = discover_tokens()
        if not tokens:
            log.warning("No tokens fetched this run - nothing to save.")
            return

        stats = save_tokens(tokens)

        # Compact one-line funnel (easy to scan in logs)...
        log.info(
            "FUNNEL  discovered=%d | mcap=%d | age=%d | volume=%d | liquidity=%d "
            "|| new=%d existing=%d snapshots(ins=%d, upd=%d)",
            stats["discovered"], stats["passed_mcap"], stats["passed_age"],
            stats["passed_volume"], stats["passed_liquidity"],
            stats["new_coins"], stats["existing_coins"],
            stats["snapshots_inserted"], stats["snapshots_updated"],
        )
        # ...and the full readable block.
        print_summary(stats)

        # After saving today's data, re-check existing coins and prune the list.
        log.info("Running drop/recovery/cleanup pass...")
        drop_stats = run_drop_logic()

        # Update market ranks (CoinGecko + CoinMarketCap) before scoring, so
        # the rank-change component of the score is fresh.
        log.info("Updating market ranks (CoinGecko + CoinMarketCap)...")
        run_rank_update()

        # Refresh holder counts for all active coins (builds growth history).
        log.info("Updating holder counts (CMC DEX)...")
        run_holders_update()

        # Final step: score the ACTIVE coins so the dashboard ranking is fresh.
        log.info("Scoring active coins...")
        results = score_all()
        print_ranking(results)

        # Consolidated pipeline summary.
        log.info("=" * 55)
        log.info("PIPELINE COMPLETE")
        log.info("  saved new coins ..... %d", stats["new_coins"])
        log.info("  active coins ........ %d", drop_stats["active_remaining"])
        log.info("  newly dropped ....... %d", drop_stats["newly_dropped"])
        log.info("  coins scored ........ %d", len(results))
        log.info("=" * 55)

        log.info("PIPELINE RUN OK")
    except Exception:
        # Log the full traceback but swallow the error so the schedule lives on.
        log.exception("PIPELINE RUN FAILED - error logged, schedule stays alive.")


def main():
    # --once: run the pipeline a single time then exit. This is what GitHub
    # Actions (or any external scheduler / cron) calls - no long-running
    # APScheduler loop, no time limit, exits cleanly when the run finishes.
    if "--once" in sys.argv:
        init_db()
        run_pipeline()
        return

    demo = "--demo" in sys.argv

    # Make sure the database + tables exist before we start.
    init_db()

    # In demo mode we use a Background scheduler (start() returns immediately)
    # so we can print the next run time and exit. In real use we use a Blocking
    # scheduler that keeps the process alive to run every 24 hours.
    scheduler = BackgroundScheduler() if demo else BlockingScheduler()

    # First AUTOMATIC run is 24h from now (the immediate run below is separate).
    first_auto_run = datetime.now() + timedelta(hours=RUN_EVERY_HOURS)
    scheduler.add_job(
        run_pipeline,
        trigger="interval",
        hours=RUN_EVERY_HOURS,
        id="daily_pipeline",
        name="Daily crypto screener pipeline",
        next_run_time=first_auto_run,
        max_instances=1,          # never overlap two runs
        coalesce=True,            # if we missed runs, just do one catch-up
    )

    # --- Run once immediately so you can see it work right now ------------
    log.info("Startup: running the pipeline immediately once...")
    run_pipeline()

    if demo:
        scheduler.start()
        job = scheduler.get_job("daily_pipeline")
        log.info(
            "[SCHEDULED] '%s' -> every %dh. Next automatic run: %s",
            job.name, RUN_EVERY_HOURS, job.next_run_time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        scheduler.shutdown(wait=False)
        log.info("DEMO complete. For real use, run without --demo to keep it running 24/7.")
        return

    log.info(
        "[SCHEDULED] every %dh. First automatic run: %s. Press Ctrl+C to stop.",
        RUN_EVERY_HOURS, first_auto_run.strftime("%Y-%m-%d %H:%M:%S"),
    )
    try:
        scheduler.start()  # blocks here forever, waking every 24h
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped by user. Goodbye.")


if __name__ == "__main__":
    main()
