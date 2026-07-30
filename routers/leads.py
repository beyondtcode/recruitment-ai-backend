"""Lead Scraping Agent routes.

Env vars:
  LEADS_SCRAPE_URL — default job-board listing URL (overridable via ?url=)
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ValidationError

from services.enrichment import enrich_jobs
from services.lead_dedup import clear_seen_jobs
from services.scraper import LINKEDIN_DEFAULT_URL, scrape_jobs

logger = logging.getLogger(__name__)

# Default listing URLs for GET /scrape-all (~3-day publish window where supported).
# LinkedIn: f_TPR=r259200 = last 3 days (259200s). AllJobs: duration=3 ≈ last 3 days.
# Drushim: ssaen=3. Glassdoor: fromAge=3. Others use category/keyword searches.
DEFAULT_SEARCH_URLS: dict[str, str] = {
    "LinkedIn": (
        "https://www.linkedin.com/jobs/search?"
        "keywords=Software%20Developer&location=Israel&f_TPR=r259200"
    ),
    "AllJobs": (
        "https://www.alljobs.co.il/SearchResultsGuest.aspx?"
        "page=1&position=235&type=&city=&region=&duration=3"
    ),
    "Drushim": (
        "https://www.drushim.co.il/jobs/search/Software%20Developer/?ssaen=3"
    ),
    "JobMaster": (
        "https://www.jobmaster.co.il/jobs/?q=Software+Developer"
    ),
    "Glassdoor": (
        "https://www.glassdoor.com/Job/israel-software-developer-jobs-"
        "SRCH_IL.0,6_IN119_KO7,26.htm?fromAge=3"
    ),
    "Jobnet": "https://www.jobnet.co.il/jobs?subprofid=819",
    "SQ Link": "https://www.sqlink.com/careers/",
}


class LeadOpportunity(BaseModel):
    company_name: str
    job_title: str
    track: Literal["A", "B"]
    company_size: str
    publish_date: str
    job_link: str
    source: str


router = APIRouter(prefix="/api/leads", tags=["leads"])


def _to_lead(job: dict) -> LeadOpportunity | None:
    try:
        return LeadOpportunity(
            company_name=job.get("company_name") or "Unknown",
            job_title=job.get("job_title") or "",
            track=job["track"],
            company_size=job.get("company_size") or "",
            publish_date=job.get("publish_date") or "",
            job_link=job.get("job_link") or "",
            source=job.get("source") or "",
        )
    except (ValidationError, KeyError, TypeError) as exc:
        logger.warning("Skipping invalid enriched job %r: %s", job, exc)
        return None


def _leads_from_enriched(enriched: list[dict]) -> list[LeadOpportunity]:
    leads: list[LeadOpportunity] = []
    for job in enriched:
        lead = _to_lead(job)
        if lead is not None:
            leads.append(lead)
    return leads


@router.get("/scrape", response_model=list[LeadOpportunity])
async def scrape_leads(
    url: str | None = None,
    source: str = "GenericJobBoard",
) -> list[LeadOpportunity]:
    """Scrape a job board, enrich with LLM (size + track), return 100+ companies only.

    Uses ``LEADS_SCRAPE_URL`` when ``url`` is omitted.
    """
    resolved_url = (url or os.environ.get("LEADS_SCRAPE_URL") or "").strip()
    if not resolved_url and source == "LinkedIn":
        resolved_url = LINKEDIN_DEFAULT_URL
    if not resolved_url:
        logger.warning(
            "scrape_leads: no URL (pass ?url= or set LEADS_SCRAPE_URL); returning []"
        )
        return []

    raw = await scrape_jobs(resolved_url, source=source)
    enriched = await enrich_jobs(raw)
    return _leads_from_enriched(enriched)


@router.get("/scrape-all", response_model=list[LeadOpportunity])
async def scrape_all() -> list[LeadOpportunity]:
    """Scrape all supported boards sequentially (~3-day window) and combine new leads.

    Each board is scraped independently: a failure on one site is logged as a
    warning and does not abort the remaining boards.
    """
    combined_raw: list[dict] = []

    for source, url in DEFAULT_SEARCH_URLS.items():
        try:
            logger.info("scrape_all: starting %s (%s)", source, url)
            jobs = await scrape_jobs(url, source=source)
            combined_raw.extend(jobs)
            logger.info("scrape_all: %s → %d new jobs", source, len(jobs))
        except Exception:
            logger.warning(
                "scrape_all: %s failed; continuing with remaining boards",
                source,
                exc_info=True,
            )

    enriched = await enrich_jobs(combined_raw)
    leads = _leads_from_enriched(enriched)
    logger.info(
        "scrape_all: done — %d raw new jobs → %d leads after enrichment",
        len(combined_raw),
        len(leads),
    )
    return leads


@router.delete("/dedup")
async def clear_lead_dedup() -> dict[str, int]:
    """Clear the SQLite seen_jobs table so scrape runs treat all links as new."""
    deleted = clear_seen_jobs()
    return {"deleted": deleted}
