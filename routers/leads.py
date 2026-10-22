"""Lead agent routes.

- ``/api/leads/run``     — every-2-days run: archive closed jobs, collect new jobs,
                           enrich companies + contacts, write the sheet. Returns a summary.
- ``/api/leads/weekly``  — rebuild the Weekly campaign tab. Returns counts.
- ``/api/leads/scrape-all`` / ``/scrape`` — collection only (no sheet access), returns rows.

Env vars:
  LEADS_SCRAPE_URL — default job-board listing URL for ``/scrape`` (overridable via ?url=)
  See services/lead_agent.py, services/lead_sheet.py, services/search_client.py.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from services import lead_agent
from services.lead_dedup import clear_seen_jobs
from services.lead_pipeline import (
    LeadOpportunity,
    run_lead_pipeline,
    scrape_all_raw,
    scrape_one,
)
from services.lead_sheet import get_sheet_store
from services.scraper import LINKEDIN_DEFAULT_URL
from services.search_client import get_search_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/leads", tags=["leads"])

# Kept for tests / callers that used the router-level name.
_run_lead_pipeline = run_lead_pipeline


@router.get("/scrape", response_model=list[LeadOpportunity])
async def scrape_leads(
    url: str | None = None,
    source: str = "GenericJobBoard",
) -> list[LeadOpportunity]:
    """Scrape one job board URL through the lead pipeline (no sheet access)."""
    resolved_url = (url or os.environ.get("LEADS_SCRAPE_URL") or "").strip()
    if not resolved_url and source == "LinkedIn":
        resolved_url = LINKEDIN_DEFAULT_URL
    if not resolved_url:
        logger.warning("scrape_leads: no URL (pass ?url= or set LEADS_SCRAPE_URL); returning []")
        return []
    return await run_lead_pipeline(await scrape_one(resolved_url, source))


@router.get("/scrape-all", response_model=list[LeadOpportunity])
async def scrape_all() -> list[LeadOpportunity]:
    """Collection only: run every lead search and return new rows (no sheet access)."""
    raw, _ = await scrape_all_raw()
    return await run_lead_pipeline(raw)


def _store():
    try:
        return get_sheet_store()
    except Exception as exc:  # missing credentials / sheet not shared
        logger.exception("leads: cannot open the leads sheet")
        raise HTTPException(status_code=503, detail=f"Leads sheet unavailable: {exc}") from exc


def _check_token(token: str | None) -> None:
    """Optional shared secret: when LEADS_API_TOKEN is set, require X-Leads-Token."""
    expected = (os.environ.get("LEADS_API_TOKEN") or "").strip()
    if expected and (token or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Leads-Token")


@router.api_route("/run", methods=["GET", "POST"])
async def run_agent(x_leads_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Full every-2-days run. Writes the sheet and returns a summary for Slack."""
    _check_token(x_leads_token)
    search = get_search_client()
    if search is None:
        logger.warning("leads/run: no TAVILY_API_KEY or SERPER_API_KEY — company data from LinkedIn pages / the LLM only, no contacts")
    return await lead_agent.run(_store(), search)


@router.api_route("/weekly", methods=["GET", "POST"])
async def weekly_file(x_leads_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Rebuild the Weekly campaign tab and return counts for the Slack message."""
    _check_token(x_leads_token)
    return await lead_agent.weekly(_store())


@router.delete("/dedup")
async def clear_lead_dedup() -> dict[str, int]:
    """Clear the SQLite seen-jobs tables so scrape runs treat all links as new."""
    deleted = clear_seen_jobs()
    return {"deleted": deleted}
