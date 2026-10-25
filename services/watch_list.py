"""Watch list: specific companies whose open jobs are collected on every run.

The list lives in the sheet's "Watch List" tab (Company Name, Company LinkedIn,
Note) and is seeded with WATCH_SEED the first time. The list only widens the
search: jobs found this way go through the same checks as any other job
(blocklist, category, company size and type, Do Not Contact).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

from services.lead_filters import normalize_company

logger = logging.getLogger(__name__)

WATCH_HEADERS = ["Company Name", "Company LinkedIn", "Note"]
SEARCH_PAUSE_SECONDS = 3.0

# Funded Israeli startups that are hiring (supplied by Beyond Code, Oct 2026).
WATCH_SEED: list[tuple[str, str, str]] = [
    ("Wonderful", "https://www.linkedin.com/company/wonderfulai/", "גייסו $550M בספטמבר · ת\"א"),
    ("Cyera", "https://www.linkedin.com/company/cyera/", "גייסו $400M בספטמבר · ת\"א"),
    ("Island", "https://www.linkedin.com/company/island-io/", "גייסו $400M בספטמבר · ת\"א"),
    ("Upwind Security", "https://www.linkedin.com/company/upwindsecurity/", "גייסו $300M בספטמבר · רמת גן"),
    ("Xsight Labs", "https://www.linkedin.com/company/xsightlabs/", "גייסו $300M ביולי · גבעתיים / קריית גת"),
    ("Dream", "https://www.linkedin.com/company/dreamsecurity/", "גייסו $260M ביוני · ת\"א"),
    ("HiBob", "https://www.linkedin.com/company/hibob/", "גייסו $166M בספטמבר · ת\"א"),
    ("Alice (Formerly ActiveFence)", "https://www.linkedin.com/company/alice-io/", "גייסו $140M באוגוסט · רמת גן"),
    ("Buildots", "https://www.linkedin.com/company/buildots/", "גייסו $130M בספטמבר · ת\"א"),
    ("Zenity", "https://www.linkedin.com/company/zenitysec/", "גייסו $125M באוגוסט · ת\"א"),
    ("Onyx Security", "https://www.linkedin.com/company/onyx-sec/", "גייסו $113M ביולי · ת\"א"),
    ("Glow", "https://www.linkedin.com/company/glowsecurity/", "גייסו $100M ביולי · ת\"א"),
    ("Neo", "https://www.linkedin.com/company/neo-ai-security/", "גייסו $100M ביולי · ת\"א"),
    ("groundcover", "https://www.linkedin.com/company/groundcover-com/", "גייסו $100M ביולי · ת\"א"),
    ("MIND", "https://www.linkedin.com/company/mindsecurity/", "גייסו $72M בספטמבר · ת\"א"),
    ("OLIGO Security", "https://www.linkedin.com/company/oligo-security/", "גייסו $60M באוגוסט · ת\"א"),
    ("Faye", "https://www.linkedin.com/company/fayetravelinsurance/", "גייסו $50M באוגוסט · ת\"א"),
    ("AITAN", "https://www.linkedin.com/company/aitansystems/", "גייסו $41M בספטמבר · ת\"א"),
    ("Guardio", "https://www.linkedin.com/company/guardiosec/", "גייסו $40M בספטמבר · ת\"א"),
    ("Remepy", "https://www.linkedin.com/company/remepy/", "גייסו $36M באוגוסט · רמת גן"),
    ("Huskeys", "https://www.linkedin.com/company/huskeys/", "גייסו $27M בספטמבר · ת\"א"),
    ("Loora", "https://www.linkedin.com/company/loora/", "גייסו $22M בספטמבר · ת\"א"),
    ("enso", "https://www.linkedin.com/company/enso-bot/", "גייסו $15M באוקטובר · ת\"א"),
    ("Zafran Security", "https://www.linkedin.com/company/zafran-security/", "השקעה מ-Cisco ביולי · ת\"א"),
]

_SLUG_RE = re.compile(r"linkedin\.com/company/([^/?#]+)", re.IGNORECASE)


def linkedin_slug(url: str) -> str:
    m = _SLUG_RE.search(url or "")
    return m.group(1).lower() if m else ""


@dataclass(frozen=True)
class WatchCompany:
    name: str
    linkedin: str
    note: str = ""

    @property
    def slug(self) -> str:
        return linkedin_slug(self.linkedin)

    @property
    def search_name(self) -> str:
        # "Alice (Formerly ActiveFence)" → "Alice"
        return re.split(r"\s*[(|–-]\s*", self.name, maxsplit=1)[0].strip() or self.name


class WatchList:
    def __init__(self, companies: list[WatchCompany]) -> None:
        self.companies = companies
        self._slugs = {c.slug for c in companies if c.slug}
        self._names = {normalize_company(c.name) for c in companies} | {
            normalize_company(c.search_name) for c in companies
        }

    def __len__(self) -> int:
        return len(self.companies)

    def matches(self, company_name: str = "", company_linkedin: str = "") -> bool:
        slug = linkedin_slug(company_linkedin)
        if slug and slug in self._slugs:
            return True
        name = normalize_company(company_name)
        return bool(name) and name in self._names


def search_url(company: WatchCompany) -> str:
    # All current openings in Israel that mention the company (no date filter).
    return (
        "https://www.linkedin.com/jobs/search?location=Israel&keywords="
        + quote(f'"{company.search_name}"')
    )


def belongs_to(job: dict, company: WatchCompany) -> bool:
    """Keep only cards posted by the company itself (search also returns others)."""
    slug = linkedin_slug(job.get("company_link") or "")
    if slug and company.slug:
        return slug == company.slug
    return normalize_company(job.get("company_name") or "") in {
        normalize_company(company.name),
        normalize_company(company.search_name),
    }


async def scrape_watch_list(watch: WatchList) -> tuple[list[dict], list[str]]:
    """Open jobs of every watch-list company. Returns (jobs, companies with none found)."""
    from services.lead_pipeline import scrape_one
    from services.scraper import LINKEDIN_MAX_JOBS_OVERRIDE

    # A company-name search also matches unrelated jobs further down; the
    # company's own jobs rank at the top, so the first page is enough.
    token = LINKEDIN_MAX_JOBS_OVERRIDE.set(0)
    try:
        return await _scrape(watch, scrape_one)
    finally:
        LINKEDIN_MAX_JOBS_OVERRIDE.reset(token)


async def _scrape(watch: WatchList, scrape_one) -> tuple[list[dict], list[str]]:
    jobs: list[dict] = []
    empty: list[str] = []
    for i, company in enumerate(watch.companies):
        if i:
            await asyncio.sleep(SEARCH_PAUSE_SECONDS)
        try:
            found = [j for j in await scrape_one(search_url(company), "LinkedIn") if belongs_to(j, company)]
        except Exception:
            logger.warning("watch_list: search failed for %s", company.name, exc_info=True)
            found = []
        for j in found:
            j["watch"] = "1"
        jobs.extend(found)
        if not found:
            empty.append(company.name)
        logger.info("watch_list: %s → %d jobs", company.name, len(found))
    return jobs, empty
