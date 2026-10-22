"""Job collection pipeline for the lead agent: scrape → filter → dedup → classify."""

from __future__ import annotations

import asyncio
import logging
import os

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.enrichment import classify_lead_jobs
from services.job_sources import LEAD_SEARCHES
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
from services.scraper import scrape_jobs

logger = logging.getLogger(__name__)

SEARCH_PAUSE_SECONDS = 3.0


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
) -> list[LeadOpportunity]:
    """Filter, dedup and classify scraped jobs; returns new leads only."""
    known = known_links or set()
    by_link: dict[str, dict] = {}
    for job in raw:
        link = (job.get("job_link") or "").strip()
        if link and link not in by_link and link not in known:
            by_link[link] = job
    fresh = filter_unseen_jobs(list(by_link.values()))

    candidates, dropped, stats = _apply_lead_rules(fresh, blocklist)
    kept, settled = await classify_lead_jobs(candidates)

    # Only mark what got a definitive answer; LLM errors are retried next run.
    mark_jobs_seen(dropped + settled)
    mark_fingerprints_seen(settled)

    found_date = format_ddmmyyyy(today_israel())
    leads = [lead for job in kept if (lead := _to_lead(job, found_date)) is not None]
    logger.info(
        "leads: %d scraped → %d new → blocked %d, hardware %d, duplicates %d → "
        "%d classified → %d leads",
        len(raw),
        len(fresh),
        stats["blocked"],
        stats["hardware"],
        stats["duplicates"],
        len(candidates),
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


async def scrape_all_raw() -> tuple[list[dict], list[str]]:
    """Run every search in ``LEAD_SEARCHES``.

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
