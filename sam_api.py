"""
sam_api.py — SAM.gov Opportunities API v2 client.

API reference: https://open.gsa.gov/api/get-opportunities-public-api/
"""
import logging
import time
from datetime import date
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.sam.gov/opportunities/v2/search"
OPPORTUNITY_UI_BASE = "https://sam.gov/opp/"

# Raised conditions that should abort the whole run rather than be retried.
FATAL_STATUSES = {401, 403}


class SamAuthError(RuntimeError):
    """The API key was rejected — retrying will not help."""


def build_ui_link(notice_id: str) -> str:
    """Construct the direct SAM.gov UI link for an opportunity."""
    return f"{OPPORTUNITY_UI_BASE}{notice_id}/view"


def _fetch_page(
    api_key: str,
    params: dict,
    offset: int,
    page_size: int,
    retries: int = 3,
    backoff: float = 3.0,
) -> Optional[dict]:
    """
    Fetch a single page, retrying transient failures with linear backoff.

    Returns the parsed JSON body, or None if the page could not be retrieved.
    Raises SamAuthError on an authentication failure.
    """
    query = {**params, "limit": page_size, "offset": offset, "api_key": api_key}

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(BASE_URL, params=query, timeout=45)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code in FATAL_STATUSES:
                raise SamAuthError(
                    f"SAM.gov rejected the API key (HTTP {resp.status_code}). "
                    "Verify sam.api_key / the SAM_API_KEY secret is correct and active."
                )

            if resp.status_code == 429:
                wait = backoff * attempt
                logger.warning(
                    "Rate limited by SAM.gov. Waiting %.0fs (attempt %d/%d).",
                    wait, attempt, retries,
                )
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                wait = backoff * attempt
                logger.warning(
                    "SAM.gov server error %d. Retrying in %.0fs (attempt %d/%d).",
                    resp.status_code, wait, attempt, retries,
                )
                time.sleep(wait)
                continue

            # 4xx other than auth/rate-limit means a malformed query — do not retry.
            logger.error("SAM.gov returned HTTP %d: %s", resp.status_code, resp.text[:400])
            return None

        except requests.exceptions.Timeout:
            wait = backoff * attempt
            logger.warning("Request timed out. Retrying in %.0fs (attempt %d/%d).", wait, attempt, retries)
            time.sleep(wait)
        except requests.exceptions.RequestException as exc:
            wait = backoff * attempt
            logger.warning("Network error: %s. Retrying in %.0fs (attempt %d/%d).", exc, wait, attempt, retries)
            time.sleep(wait)
        except ValueError as exc:
            logger.error("Could not parse SAM.gov response as JSON: %s", exc)
            return None

    logger.error("Giving up on page offset=%d after %d attempts.", offset, retries)
    return None


def fetch_opportunities(
    api_key: str,
    posted_from: date,
    posted_to: date,
    keyword: Optional[str] = None,
    naics_code: Optional[str] = None,
    set_aside: Optional[str] = None,
    state: Optional[str] = None,
    procurement_types: Optional[List[str]] = None,
    active_only: bool = True,
    page_size: int = 100,
    max_pages: int = 50,
) -> Tuple[List[Dict], bool]:
    """
    Fetch all pages of opportunities matching the given criteria.

    Returns (opportunities, ok) where ok is False if any page failed, so the
    caller can avoid treating a partial result as authoritative.
    """
    params: dict = {
        # The API requires MM/dd/yyyy for these two parameters.
        "postedFrom": posted_from.strftime("%m/%d/%Y"),
        "postedTo": posted_to.strftime("%m/%d/%Y"),
    }
    if keyword:
        params["title"] = keyword
    if naics_code:
        params["ncode"] = naics_code
    if set_aside:
        params["typeOfSetAside"] = set_aside
    if state:
        params["state"] = state
    if procurement_types:
        params["ptype"] = ",".join(procurement_types)
    if active_only:
        params["status"] = "active"

    all_results: List[Dict] = []
    offset = 0
    pages = 0
    ok = True

    while pages < max_pages:
        data = _fetch_page(api_key, params, offset, page_size)
        if data is None:
            ok = False
            break

        opportunities = data.get("opportunitiesData") or []
        total = int(data.get("totalRecords") or 0)

        for op in opportunities:
            # Guarantee a usable link even if the API omits uiLink.
            if not op.get("uiLink") and op.get("noticeId"):
                op["uiLink"] = build_ui_link(op["noticeId"])

        all_results.extend(opportunities)
        pages += 1
        offset += page_size

        logger.debug(
            "  page %d: %d records (running total %d of %d)",
            pages, len(opportunities), len(all_results), total,
        )

        if not opportunities or offset >= total:
            break

        time.sleep(0.5)  # be polite between pages

    if pages >= max_pages:
        logger.warning(
            "Hit max_pages=%d — results may be truncated. Narrow your filters "
            "or raise sam.page_size.", max_pages,
        )

    return all_results, ok


def run_all_searches(
    api_key: str,
    posted_from: date,
    posted_to: date,
    keywords: List[str],
    naics_codes: List[str],
    set_asides: List[str],
    state: str,
    procurement_types: List[str],
    active_only: bool,
    page_size: int,
) -> Tuple[Dict[str, List[Dict]], List[str]]:
    """
    Run every configured keyword and NAICS search.

    The API has no OR-style multi-term search, so each term is its own query.
    Cross-search deduplication happens in monitor.py.

    Returns (results_by_label, failed_labels).
    """
    results: Dict[str, List[Dict]] = {}
    failed: List[str] = []

    # A single set-aside filter is applied across all searches when configured.
    set_aside = set_asides[0] if set_asides else None
    if set_asides and len(set_asides) > 1:
        logger.warning(
            "Multiple set_asides configured; the API accepts one per query. Using '%s'.",
            set_aside,
        )

    jobs = [("keyword", kw) for kw in keywords] + [("naics", code) for code in naics_codes]
    if not jobs:
        logger.error("No keywords or NAICS codes configured — nothing to search.")
        return results, failed

    for kind, term in jobs:
        label = f"{kind}:{term}"
        logger.info("Searching %s = %s", kind, term)

        opportunities, ok = fetch_opportunities(
            api_key=api_key,
            posted_from=posted_from,
            posted_to=posted_to,
            keyword=term if kind == "keyword" else None,
            naics_code=term if kind == "naics" else None,
            set_aside=set_aside,
            state=state or None,
            procurement_types=procurement_types or None,
            active_only=active_only,
            page_size=page_size,
        )

        if not ok:
            failed.append(label)
            logger.warning("  → search failed or returned partial results; skipping.")
            # Still keep whatever came back is unsafe for dedup bookkeeping,
            # so drop partial results entirely.
            continue

        logger.info("  → %d result(s)", len(opportunities))
        results[label] = opportunities
        time.sleep(1)  # spacing between distinct queries

    return results, failed
