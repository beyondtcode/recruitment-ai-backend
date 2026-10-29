"""Job collection pipeline for the lead agent: scrape → filter → dedup → classify."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.enrichment import classify_lead_jobs
from services.job_sources import LEAD_SEARCHES, with_lookback
from services.lead_dedup import (
    filter_unseen_fingerprints,
    filter_unseen_jobs,
    mark_fingerprints_seen,
    mark_jobs_seen,
)
from services.lead_filters import (
    Blocklist,
    blocked_company,
    format_ddmmyyyy,
    is_hardware_role,
    job_fingerprint,
    passes_title_prefilter,
    source_display,
    to_ddmmyyyy,
    today_israel,
)
from services.scraper import LINKEDIN_MAX_JOBS_OVERRIDE, scrape_jobs

logger = logging.getLogger(__name__)

SEARCH_PAUSE_SECONDS = 3.0

# Category of jobs that were not sent to the LLM (company known not to fit).
UNCLASSIFIED = "לא סווג (חברה לא מתאימה)"


class LeadOpportunity(BaseModel):
    """Sheet-ready lead row. Serialized JSON keys match the sheet column headers."""

    model_config = ConfigDict(populate_by_name=True)

    company_name: str = Field(serialization_alias="Company Name")
    job_title: str = Field(serialization_alias="Job Title")
    category: str = Field(serialization_alias="Category")
    outreach_phrasing: str = Field(serialization_alias="Outreach Phrasing")
    job_summary: str = Field(serialization_alias="Job Summary")
    job_link: str = Field(serialization_alias="Job Link")
    publish_date: str = Field(serialization_alias="Publish Date")
    found_date: str = Field(serialization_alias="Found Date")
    source: str = Field(serialization_alias="Source")
    company_link: str = Field(default="", serialization_alias="Company LinkedIn")


def _to_lead(job: dict, found_date: str) -> LeadOpportunity | None:
    try:
        return LeadOpportunity(
            company_name=job.get("company_name") or "Unknown",
            job_title=job.get("job_title") or "",
            category=job["category"],
            outreach_phrasing=job.get("outreach_phrasing") or "",
            job_summary=job.get("job_summary") or "",
            job_link=job.get("job_link") or "",
            publish_date=to_ddmmyyyy(job.get("publish_date") or ""),
            found_date=found_date,
            source=source_display(job.get("source") or ""),
            company_link=job.get("company_link") or "",
        )
    except (ValidationError, KeyError, TypeError) as exc:
        logger.warning("Skipping invalid enriched job %r: %s", job, exc)
        return None


def _apply_lead_rules(
    jobs: list[dict], blocklist: Blocklist | None = None
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Blocklist + hardware filter + cross-board dedup.

    Returns ``(candidates, dropped, stats)``. ``dropped`` jobs are final
    rejections and can be marked seen right away.
    """
    stats = {"blocked": 0, "hardware": 0, "duplicates": 0}
    candidates: list[dict] = []
    dropped: list[dict] = []
    for job in jobs:
        company = job.get("company_name") or ""
        title = job.get("job_title") or ""
        hit = blocked_company(company, blocklist)
        if hit:
            stats["blocked"] += 1
            logger.info("leads: blocked company %r (%s): %s", company, hit, title)
            dropped.append(job)
            continue
        if is_hardware_role(title, job.get("snippet") or ""):
            stats["hardware"] += 1
            logger.info("leads: hardware role dropped %r: %s", company, title)
            dropped.append(job)
            continue
        job["fingerprint"] = job_fingerprint(company, title)
        candidates.append(job)

    unique = filter_unseen_fingerprints(candidates)
    stats["duplicates"] = len(candidates) - len(unique)
    kept_ids = {id(j) for j in unique}
    dropped.extend(j for j in candidates if id(j) not in kept_ids)
    return unique, dropped, stats


async def run_lead_pipeline(
    raw: list[dict],
    *,
    blocklist: Blocklist | None = None,
    known_links: set[str] | None = None,
    skip_classify: Callable[[dict], bool] | None = None,
) -> list[LeadOpportunity]:
    """Filter, dedup and classify scraped jobs; returns new leads only.

    ``skip_classify(job)`` → True for jobs of companies already known not to
    fit whatever the category: they are kept (category UNCLASSIFIED) without
    an LLM call.
    """
    known = known_links or set()
    by_link: dict[str, dict] = {}
    for job in raw:
        link = (job.get("job_link") or "").strip()
        if link and link not in by_link and link not in known:
            by_link[link] = job
    fresh = filter_unseen_jobs(list(by_link.values()))

    candidates, dropped, stats = _apply_lead_rules(fresh, blocklist)
    # Optional cap for small test runs. Jobs over the cap are not marked seen,
    # so a later (uncapped) run still picks them up.
    try:
        max_new = int(os.environ.get("LEADS_MAX_NEW_JOBS") or 0)
    except ValueError:
        max_new = 0
    if max_new > 0 and len(candidates) > max_new:
        candidates = candidates[:max_new]
    skipped: list[dict] = []
    if skip_classify is not None:
        skipped = [j for j in candidates if skip_classify(j)]
        skipped_ids = {id(j) for j in skipped}
        candidates = [j for j in candidates if id(j) not in skipped_ids]
    for job in skipped:
        job["category"] = UNCLASSIFIED
    kept, settled = await classify_lead_jobs(candidates)
    kept = kept + skipped
    settled = settled + skipped

    # Only mark what got a definitive answer; LLM errors are retried next run.
    mark_jobs_seen(dropped + settled)
    mark_fingerprints_seen(settled)

    found_date = format_ddmmyyyy(today_israel())
    leads = [lead for job in kept if (lead := _to_lead(job, found_date)) is not None]
    logger.info(
        "leads: %d scraped → %d new → blocked %d, hardware %d, duplicates %d → "
        "%d classified (+%d of known non-fit companies, not classified) → %d leads",
        len(raw),
        len(fresh),
        stats["blocked"],
        stats["hardware"],
        stats["duplicates"],
        len(candidates),
        len(skipped),
        len(leads),
    )
    return leads


async def scrape_one(url: str, source: str) -> list[dict]:
    return await scrape_jobs(
        url,
        source=source,
        apply_lead_dedup=False,
        title_filter=passes_title_prefilter,
    )


# One-time backfill: LinkedIn jobs per search when looking further back.
BACKFILL_LINKEDIN_MAX_JOBS = 1000


async def scrape_all_raw(
    lookback_days: int | None = None, only: list[str] | None = None
) -> tuple[list[dict], list[str]]:
    """Run every search in ``LEAD_SEARCHES``.

    Each search looks back LEADS_LOOKBACK_DAYS (default 3: runs every 2 days,
    plus a day of overlap; set 5 for a twice-a-week schedule). ``lookback_days``
    (a one-time backfill) overrides it and lets LinkedIn page deeper.

    Returns (jobs, labels of searches that failed or returned nothing). A search
    with zero results usually means the board changed its layout or blocked us.
    """
    combined: list[dict] = []
    failed: list[str] = []
    searches = list(LEAD_SEARCHES)
    try:
        limit = int(os.environ.get("LEADS_SEARCH_LIMIT") or 0)
    except ValueError:
        limit = 0
    if limit > 0:  # short test runs
        searches = searches[:limit]
    if only:  # test runs: just these searches (label or "Board / Label", any case)
        wanted = {w.strip().casefold() for w in only if w.strip()}
        searches = [
            (b, l, u) for b, l, u in searches
            if l.casefold() in wanted or f"{b} / {l}".casefold() in wanted or b.casefold() in wanted
        ]
    if lookback_days:
        LINKEDIN_MAX_JOBS_OVERRIDE.set(BACKFILL_LINKEDIN_MAX_JOBS)
    try:
        window = lookback_days or int(os.environ.get("LEADS_LOOKBACK_DAYS") or 3)
    except ValueError:
        window = 3
    if window != 3:  # the search URLs are written with a 3-day window
        searches = [(b, l, with_lookback(u, window)) for b, l, u in searches]
    for i, (board, label, url) in enumerate(searches):
        if i:
            # Be gentle with the boards (LinkedIn rate-limits bursts of guest searches).
            await asyncio.sleep(SEARCH_PAUSE_SECONDS)
        try:
            logger.info("scrape_all: starting %s / %s (%s)", board, label, url)
            jobs = await scrape_one(url, board)
            combined.extend(jobs)
            if not jobs:
                failed.append(f"{board} / {label}")
            logger.info("scrape_all: %s / %s → %d jobs", board, label, len(jobs))
        except Exception:
            failed.append(f"{board} / {label}")
            logger.warning(
                "scrape_all: %s / %s failed; continuing with remaining searches",
                board,
                label,
                exc_info=True,
            )
    return combined, failed
