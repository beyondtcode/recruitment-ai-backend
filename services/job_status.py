"""Check whether a job posting is still open.

closed  — HTTP 404/410, or the page says the job is no longer available
open    — page loads (200) with no closed marker
unknown — blocked / rate-limited / network error; caller must not treat as closed
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

Status = Literal["open", "closed", "unknown"]

CLOSED_MARKERS: tuple[str, ...] = (
    "no longer accepting applications",
    "this job is no longer available",
    "this job has expired",
    "job is no longer active",
    "המשרה אינה רלוונטית",
    "המשרה אינה פעילה",
    "המשרה לא פעילה",
    "המשרה הוסרה",
    "המשרה כבר לא רלוונטית",
    "משרה זו אינה זמינה",
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
}


def classify_response(status_code: int, body: str) -> Status:
    if status_code in (404, 410):
        return "closed"
    if status_code != 200:
        return "unknown"
    text = (body or "").lower()
    if any(marker in text for marker in CLOSED_MARKERS):
        return "closed"
    return "open"


async def check_job(client: httpx.AsyncClient, url: str) -> Status:
    if not url.startswith("http"):
        return "unknown"
    try:
        response = await client.get(url)
    except Exception:
        logger.debug("job_status: request failed for %s", url, exc_info=True)
        return "unknown"
    return classify_response(response.status_code, response.text)


async def check_jobs(urls: list[str], *, concurrency: int = 4) -> dict[str, Status]:
    """Check many links. LinkedIn links run one at a time with a pause."""
    results: dict[str, Status] = {}
    linkedin = [u for u in urls if "linkedin.com" in urlparse(u).netloc]
    other = [u for u in urls if u not in set(linkedin)]
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        headers=_HEADERS, timeout=20.0, follow_redirects=True
    ) as client:

        async def one(url: str) -> None:
            async with sem:
                results[url] = await check_job(client, url)

        async def linkedin_serial() -> None:
            for url in linkedin:
                results[url] = await check_job(client, url)
                await asyncio.sleep(1.5)

        await asyncio.gather(linkedin_serial(), *(one(u) for u in other))

    counts = {s: sum(1 for v in results.values() if v == s) for s in ("open", "closed", "unknown")}
    logger.info("job_status: checked %d links → %s", len(urls), counts)
    return results
