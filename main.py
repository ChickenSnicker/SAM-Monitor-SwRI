#!/usr/bin/env python3
"""
main.py — Entry point for the SAM.gov Contract Opportunity Monitor.

Usage:
    python main.py                  # run continuously on a daily schedule
    python main.py --run-once       # run a single cycle and exit (used by CI/cron)
    python main.py --dry-run        # query and report, but write nothing and send nothing
    python main.py --stats          # print history statistics and exit
    python main.py --config PATH    # use an alternate config file

Exit codes:
    0  success
    1  configuration or fatal runtime error
    2  completed, but one or more individual searches failed
"""
import argparse
import logging
import logging.handlers
import os
import sys
import time

from config_loader import load_config
from monitor import run_monitor
from sam_api import SamAuthError


# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────

def setup_logging(cfg: dict) -> None:
    log_cfg = cfg.get("logging", {})
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    log_file = log_cfg.get("log_file", "")
    # Writing a log file inside a CI runner is pointless — the run log is the record.
    if log_file and not os.environ.get("GITHUB_ACTIONS"):
        if not os.path.isabs(log_file):
            log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), log_file)
        handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=int(log_cfg.get("max_mb", 5)) * 1024 * 1024,
            backupCount=int(log_cfg.get("backup_count", 3)),
            encoding="utf-8",
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)


# ─────────────────────────────────────────────
#  GitHub Actions integration
# ─────────────────────────────────────────────

def write_job_summary(summary: dict) -> None:
    """
    Append a short report to the GitHub Actions run summary panel, so each
    scheduled run shows its outcome without opening the raw logs.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        new = summary.get("total_new", 0)
        icon = "🔔" if new else "✅"
        lines = [
            "## SAM.gov Monitor",
            "",
            f"{icon} **{new}** new opportunit{'y' if new == 1 else 'ies'} found",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| Searches run | {summary.get('searches_run', 0)} |",
            f"| Records fetched | {summary.get('total_fetched', 0)} |",
            f"| Unique this run | {summary.get('total_unique', 0)} |",
            f"| New (alerted) | {new} |",
        ]
        failed = summary.get("failed_searches") or []
        if failed:
            lines += ["", f"⚠️ **{len(failed)} search(es) failed:** " + ", ".join(f"`{f}`" for f in failed)]
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass  # never let reporting break the run


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="SAM.gov Contract Opportunity Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    p.add_argument("--run-once", action="store_true", help="run one cycle and exit")
    p.add_argument("--dry-run", action="store_true", help="no writes, no notifications")
    p.add_argument("--stats", action="store_true", help="print history stats and exit")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # --stats only reads local history, so skip strict validation for it.
    cfg = load_config(args.config, validate=not args.stats)
    setup_logging(cfg)
    log = logging.getLogger(__name__)

    if args.stats:
        from history import get_history
        stats = get_history(cfg).stats()
        log.info("History statistics:")
        for key, value in stats.items():
            log.info("  %-12s %s", key, value if value is not None else "—")
        return 0

    log.info("SAM.gov Contract Monitor starting")
    if args.dry_run:
        log.info("DRY RUN — nothing will be written or sent.")

    in_ci = bool(os.environ.get("GITHUB_ACTIONS"))
    run_once = args.run_once or in_ci
    if in_ci and not args.run_once:
        log.info("GitHub Actions detected — running a single cycle.")

    def cycle() -> dict:
        log.info("─" * 50)
        return run_monitor(cfg, dry_run=args.dry_run)

    # ---- single run ----
    if run_once:
        try:
            summary = cycle()
        except SamAuthError as exc:
            log.error("%s", exc)
            return 1
        except Exception:
            log.exception("Fatal error during run.")
            return 1
        write_job_summary(summary)
        return 2 if summary.get("failed_searches") else 0

    # ---- scheduler mode ----
    import schedule  # imported lazily; unnecessary for --run-once / CI

    sched_cfg = cfg.get("schedule", {})
    run_time = sched_cfg.get("run_time", "07:00")

    def safe_cycle() -> None:
        try:
            summary = cycle()
            log.info("Next run at %s.", run_time)
            write_job_summary(summary)
        except SamAuthError as exc:
            log.error("%s", exc)
        except Exception:
            # Keep the daemon alive through transient failures.
            log.exception("Run failed; will try again at the next scheduled time.")

    schedule.every().day.at(run_time).do(safe_cycle)
    log.info("Scheduled daily run at %s local time.", run_time)

    if sched_cfg.get("run_on_startup", True):
        safe_cycle()

    log.info("Scheduler active — press Ctrl+C to stop.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
