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

from fastapi import APIRouter, Header, HTTPException, Query

from services import lead_agent
from services.lead_dedup import clear_seen_jobs
from services.lead_pipeline import (
    LeadOpportunity,
    run_lead_pipeline,
    scrape_all_raw,
    scrape_one,
)
from services.job_sources import LEAD_SEARCHES
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
async def run_agent(
    x_leads_token: str | None = Header(default=None),
    lookback_days: int | None = Query(default=None, ge=1, le=30),
    mode: str | None = Query(default=None, pattern="^(quick|watch|searches)$"),
    search: str | None = Query(default=None),
) -> dict[str, Any]:
    """Full every-2-days run. Writes the sheet and returns a summary for Slack.

    ``?lookback_days=30`` = one-time backfill: searches look back that many days
    instead of 3 (and LinkedIn pages deeper). Leave it off for scheduled runs.

    Short test runs (same processing and sheet writes, less input; existing
    links are not re-checked and the weekly file is not built):
    ``?mode=quick``     the first 3 searches, no watch list (a few minutes);
    ``?mode=watch``     the watch list only;
    ``?mode=searches&search=Backend,QA``  only the named searches.
    """
    _check_token(x_leads_token)
    client = get_search_client()
    if client is None:
        logger.warning("leads/run: no TAVILY_API_KEY or SERPER_API_KEY — company data from LinkedIn pages / the LLM only, no contacts")
    if mode:
        summary = await _test_run(client, mode, search)
        summary["test_mode"] = mode
        return summary
    if lookback_days:
        logger.info("leads/run: one-time backfill, looking back %d days", lookback_days)
    summary = await lead_agent.run(_store(), client, lookback_days=lookback_days)
    if lookback_days:
        summary["lookback_days"] = lookback_days
    return summary


async def _no_check(urls: list[str]) -> dict[str, str]:
    return {}


async def _no_scrape() -> tuple[list[dict], list[str]]:
    return [], []


async def _no_watch(watch) -> tuple[list[dict], list[str]]:
    return [], []


async def _test_run(client, mode: str, names: str | None) -> dict[str, Any]:
    logger.info("leads/run: test run mode=%s search=%s", mode, names)
    if mode == "quick":
        labels = [f"{board} / {label}" for board, label, _u in LEAD_SEARCHES[:3]]
        scrape, watch = (lambda: scrape_all_raw(only=labels)), _no_watch
    elif mode == "watch":
        scrape, watch = _no_scrape, None
    else:
        wanted = [n for n in (names or "").split(",") if n.strip()]
        if not wanted:
            raise HTTPException(status_code=400, detail="mode=searches needs ?search=Label1,Label2")
        scrape, watch = (lambda: scrape_all_raw(only=wanted)), _no_watch
    return await lead_agent.run(_store(), client, scrape=scrape, check=_no_check,
                                scrape_watch=watch, weekly_file=False)


@router.api_route("/weekly", methods=["GET", "POST"])
async def weekly_file(x_leads_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Rebuild the Weekly campaign tab and return counts for the Slack message."""
    _check_token(x_leads_token)
    return await lead_agent.weekly(_store())


@router.get("/sheet-check")
async def sheet_check(token: str | None = None) -> dict[str, Any]:
    """Setup helper: confirms the server can reach the sheet. Pass ?token=LEADS_API_TOKEN."""
    _check_token(token)
    store = _store()
    try:
        info = store.ping() if hasattr(store, "ping") else {}
        jobs = store.read(lead_agent.jobs_tab())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Sheet check failed: {exc}") from exc
    return {
        "ok": True,
        "sheet": info.get("name", ""),
        "jobs_tab_found": jobs is not None,
        "job_rows": len(jobs.rows) if jobs else 0,
    }


@router.delete("/dedup")
async def clear_lead_dedup(x_leads_token: str | None = Header(default=None)) -> dict[str, int]:
    """Clear the SQLite seen-jobs tables so scrape runs treat all links as new."""
    _check_token(x_leads_token)
    deleted = clear_seen_jobs()
    return {"deleted": deleted}


@router.post("/reset")
async def reset_agent(
    clear_sheet: bool = False, x_leads_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """Start over: forget seen jobs, status history and retries.

    With ``?clear_sheet=true`` also empties Jobs, Companies, Weekly and the
    archives (headers, formatting, Blocked Companies and Do Not Contact stay).
    """
    _check_token(x_leads_token)
    store = _store() if clear_sheet else None
    async with lead_agent._agent_lock:
        seen = clear_seen_jobs()
        state = lead_agent.reset_state()
        cleared = lead_agent.clear_sheet(store) if store is not None else []
    return {"seen_jobs_cleared": seen, **state, "tabs_cleared": cleared}
