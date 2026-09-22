"""
monitor.py — Core monitoring logic: query → deduplicate → notify → record.
"""
import logging
from datetime import date, timedelta
from typing import Dict, List, Tuple

from history import get_history
from notifier import dispatch_notifications
from sam_api import run_all_searches

logger = logging.getLogger(__name__)

# The SAM.gov API rejects postedFrom/postedTo ranges wider than one year.
MAX_WINDOW_DAYS = 364


def _get_search_window(cfg: dict, history) -> Tuple[date, date]:
    """
    Decide which posted-date range to search.

    Three cases:
      1. No prior run recorded  → look back `sam.lookback_days`.
      2. Prior run recorded     → search from the day before that run, so a
                                  missed or failed run does not lose a day.
      3. Very stale history     → clamp to the API's one-year maximum.

    The one-day overlap in case 2 is intentional and harmless: anything already
    seen is filtered out by the history store anyway, and the overlap covers
    opportunities posted late on the previous run's day.
    """
    posted_to = date.today()
    last_run = history.get_last_run()

    if last_run is None:
        lookback = int(cfg.get("sam", {}).get("lookback_days", 7) or 7)
        posted_from = posted_to - timedelta(days=lookback)
        logger.info("No previous run recorded — looking back %d day(s).", lookback)
    else:
        posted_from = last_run - timedelta(days=1)
        gap = (posted_to - last_run).days
        if gap > 1:
            logger.info(
                "Last run was %s (%d days ago) — widening window to cover the gap.",
                last_run.isoformat(), gap,
            )
        else:
            logger.info("Last run was %s.", last_run.isoformat())

    # Clamp to the API maximum range.
    if (posted_to - posted_from).days > MAX_WINDOW_DAYS:
        posted_from = posted_to - timedelta(days=MAX_WINDOW_DAYS)
        logger.warning("Window exceeded API limit — clamped to %d days.", MAX_WINDOW_DAYS)

    logger.info("Search window: %s → %s", posted_from.isoformat(), posted_to.isoformat())
    return posted_from, posted_to


def run_monitor(cfg: dict, dry_run: bool = False) -> Dict:
    """
    Execute one full monitoring cycle.

      1. Determine the search window from stored run history
      2. Query SAM.gov for every configured keyword and NAICS code
      3. Deduplicate within the run, then against stored history
      4. Send notifications and record what was seen
      5. Stamp the run date so the next run knows where to resume

    Args:
        cfg: Loaded configuration dict.
        dry_run: If True, make no writes and send no notifications.

    Returns:
        Summary dict: total_fetched, total_unique, total_new, searches_run, failed_searches
    """
    sam_cfg = cfg.get("sam", {})
    filters = cfg.get("filters", {})

    history = get_history(cfg)
    prior = history.stats()
    logger.info("History: %d opportunit(ies) previously recorded.", prior["total_seen"])

    posted_from, posted_to = _get_search_window(cfg, history)

    search_results, failed_searches = run_all_searches(
        api_key=sam_cfg["api_key"],
        posted_from=posted_from,
        posted_to=posted_to,
        keywords=filters.get("keywords", []) or [],
        naics_codes=filters.get("naics_codes", []) or [],
        set_asides=filters.get("set_asides", []) or [],
        state=filters.get("state", "") or "",
        procurement_types=filters.get("procurement_types", []) or [],
        active_only=sam_cfg.get("active_only", True),
        page_size=sam_cfg.get("page_size", 100),
    )

    seen_this_run: set = set()
    total_fetched = 0
    total_unique = 0
    total_new = 0
    all_new: List[Dict] = []

    for matched_by, opportunities in search_results.items():
        total_fetched += len(opportunities)

        # Same notice often appears under several keywords / NAICS codes.
        unique: List[Dict] = []
        for op in opportunities:
            nid = op.get("noticeId", "")
            if nid and nid not in seen_this_run:
                seen_this_run.add(nid)
                unique.append(op)
        total_unique += len(unique)

        new_opps = history.filter_new(unique)
        total_new += len(new_opps)

        if not new_opps:
            logger.info("%-34s no new opportunities (%d fetched).", matched_by, len(opportunities))
            continue

        logger.info(
            "%-34s %d NEW (of %d fetched, %d unique this run)",
            matched_by, len(new_opps), len(opportunities), len(unique),
        )
        for op in new_opps:
            logger.info("    • %s", (op.get("title") or "Untitled")[:100])

        dispatch_notifications(cfg, new_opps, matched_by=matched_by, dry_run=dry_run)

        if not dry_run:
            # Record only after a delivery attempt, so a crash mid-run does not
            # mark opportunities as seen that were never reported.
            history.mark_seen(new_opps, matched_by=matched_by)

        all_new.extend(new_opps)

    if not dry_run:
        history.set_last_run(posted_to)

    if failed_searches:
        logger.warning(
            "%d search(es) failed and were skipped: %s",
            len(failed_searches), ", ".join(failed_searches),
        )

    summary = {
        "total_fetched": total_fetched,
        "total_unique": total_unique,
        "total_new": total_new,
        "searches_run": len(search_results),
        "failed_searches": failed_searches,
    }

    logger.info("─" * 50)
    logger.info(
        "Run complete — searches: %d | fetched: %d | unique: %d | NEW: %d",
        summary["searches_run"], total_fetched, total_unique, total_new,
    )
    return summary
