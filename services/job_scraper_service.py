"""Scrape Israeli tech job boards for the matcher pipeline.

Reuses ``services.scraper.scrape_jobs`` per board, applies matcher-specific
30-day dedup, and maps results to ``JobInput``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

from schemas.matcher import JobInput
from services.job_dedup import filter_new_jobs, get_window_days, job_id_for_url, mark_jobs_seen
from services.job_sources import DEFAULT_SEARCH_URLS
from services.scraper import DEFAULT_TIMEOUT_SECONDS, DEFAULT_USER_AGENT, scrape_jobs

logger = logging.getLogger(__name__)

_DESCRIPTION_FETCH_CONCURRENCY = 5
_DESCRIPTION_TIMEOUT_SECONDS = 15.0
_MIN_DESCRIPTION_CHARS = 80

_DESCRIPTION_SELECTORS = (
    ".job-description",
    "[class*='job-description']",
    "[class*='JobDescription']",
    "[data-test='jobDescription']",
    "[data-testid='job-description']",
    "article .description",
    ".description",
    "[class*='description']",
    "article",
    "main",
)

_NOISE_RE = re.compile(r"\s+")


@dataclass
class JobScrapeResult:
    total_found: int
    new_jobs_count: int
    jobs: list[JobInput] = field(default_factory=list)


def _dedupe_by_link(jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep first occurrence of each non-empty job_link; drop empty-link dupes last."""
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for job in jobs:
        link = (job.get("job_link") or "").strip()
        if not link:
            unique.append(job)
            continue
        if link in seen:
            continue
        seen.add(link)
        unique.append(job)
    return unique


def _normalize_text(text: str) -> str:
    return _NOISE_RE.sub(" ", (text or "").strip())


def _extract_description_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for selector in _DESCRIPTION_SELECTORS:
        node = soup.select_one(selector)
        if node is None:
            continue
        text = _normalize_text(node.get_text(" ", strip=True))
        if len(text) >= _MIN_DESCRIPTION_CHARS:
            return text
    return ""


async def _fetch_job_description(
    client: httpx.AsyncClient,
    url: str,
    *,
    semaphore: asyncio.Semaphore,
) -> str:
    async with semaphore:
        try:
            response = await client.get(url)
            response.raise_for_status()
            return _extract_description_from_html(response.text)
        except Exception:
            logger.debug("job_scraper: description fetch failed for %s", url, exc_info=True)
            return ""


async def _empty_description() -> str:
    return ""


async def _enrich_descriptions(jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Best-effort httpx detail-page fetch; falls back to listing snippet."""
    if not jobs:
        return jobs

    semaphore = asyncio.Semaphore(_DESCRIPTION_FETCH_CONCURRENCY)
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html",
    }

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=_DESCRIPTION_TIMEOUT_SECONDS,
        headers=headers,
    ) as client:
        tasks = []
        for job in jobs:
            link = (job.get("job_link") or "").strip()
            if link:
                tasks.append(
                    _fetch_job_description(client, link, semaphore=semaphore)
                )
            else:
                tasks.append(_empty_description())
        descriptions = await asyncio.gather(*tasks)

    enriched: list[dict[str, str]] = []
    for job, detail in zip(jobs, descriptions):
        row = dict(job)
        snippet = _normalize_text(job.get("snippet") or "")
        description = _normalize_text(detail) if detail else ""
        if not description or len(description) < _MIN_DESCRIPTION_CHARS:
            description = snippet
        row["description"] = description
        enriched.append(row)
    return enriched


def _to_job_input(job: dict[str, str]) -> JobInput | None:
    url = (job.get("job_link") or "").strip()
    title = _normalize_text(job.get("job_title") or "")
    if not url or not title:
        return None

    company = _normalize_text(job.get("company_name") or "") or "Unknown"
    description = _normalize_text(job.get("description") or job.get("snippet") or "")
    if not description:
        description = f"{title} at {company}"

    return JobInput(
        id=job_id_for_url(url),
        title=title,
        company=company,
        description=description,
        url=url,
    )


async def scrape_daily_jobs() -> JobScrapeResult:
    """Scrape all configured Israeli boards and return new (unseen) JobInput rows."""
    combined: list[dict[str, str]] = []

    for source, url in DEFAULT_SEARCH_URLS.items():
        try:
            jobs = await scrape_jobs(
                url,
                source=source,
                timeout=DEFAULT_TIMEOUT_SECONDS,
                apply_lead_dedup=False,
            )
            combined.extend(jobs)
            logger.info(
                "job_scraper: %s → %d keyword-filtered job(s)",
                source,
                len(jobs),
            )
        except Exception:
            logger.warning(
                "job_scraper: board %s failed; continuing with remaining sources",
                source,
                exc_info=True,
            )

    combined = _dedupe_by_link(combined)
    total_found = len(combined)

    days = get_window_days()
    new_raw = filter_new_jobs(combined, window_days=days)
    new_raw = await _enrich_descriptions(new_raw)

    jobs: list[JobInput] = []
    seen_rows: list[dict[str, str]] = []
    for row in new_raw:
        mapped = _to_job_input(row)
        if mapped is None:
            continue
        row["job_id"] = mapped.id
        jobs.append(mapped)
        seen_rows.append(row)

    if seen_rows:
        mark_jobs_seen(seen_rows)

    logger.info(
        "job_scraper: total_found=%d new_jobs_count=%d (window=%dd)",
        total_found,
        len(jobs),
        days,
    )
    return JobScrapeResult(
        total_found=total_found,
        new_jobs_count=len(jobs),
        jobs=jobs,
    )
