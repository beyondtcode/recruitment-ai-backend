"""Lead Scraping Agent routes.

Env vars:
  LEADS_SCRAPE_URL — default job-board listing URL (overridable via ?url=)
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.enrichment import DEFAULT_CONTACT_INFO, enrich_jobs
from services.job_sources import DEFAULT_SEARCH_URLS
from services.lead_dedup import clear_seen_jobs
from services.scraper import LINKEDIN_DEFAULT_URL, scrape_jobs

logger = logging.getLogger(__name__)


class LeadOpportunity(BaseModel):
    """Excel-ready lead row. Serialized JSON keys match spreadsheet columns."""

    model_config = ConfigDict(populate_by_name=True)

    company_name: str = Field(serialization_alias="Company Name")
    job_title: str = Field(serialization_alias="Job Title")
    job_summary: str = Field(serialization_alias="Job Summary")
    job_link: str = Field(serialization_alias="Job Link")
    contact_info: str = Field(serialization_alias="Contact Info")
    track: Literal["A", "B"] = Field(serialization_alias="Track")
    publish_date: str = Field(serialization_alias="Publish Date")


router = APIRouter(prefix="/api/leads", tags=["leads"])


def _to_lead(job: dict) -> LeadOpportunity | None:
    try:
        return LeadOpportunity(
            company_name=job.get("company_name") or "Unknown",
            job_title=job.get("job_title") or "",
            job_summary=job.get("job_summary") or "",
            job_link=job.get("job_link") or "",
            contact_info=job.get("contact_info") or DEFAULT_CONTACT_INFO,
            track=job["track"],
            publish_date=job.get("publish_date") or "",
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
    Response JSON keys match Excel columns (Company Name, Job Title, …).
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
    Response JSON keys match Excel columns (Company Name, Job Title, …).
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
