"""Watch list: specific companies whose open jobs are collected on every run.

The list lives in the sheet's "Watch List" tab (Company Name, Company LinkedIn,
Note) and is seeded with WATCH_SEED the first time. The list only widens the
search: jobs found this way go through the same checks as any other job
(blocklist, category, company size and type, Do Not Contact).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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


# LinkedIn's numeric company id lets the job search filter on the company
# itself (f_C=<id>) instead of a keyword search on its name, which misses
# jobs and needs filtering. Ids are cached in a small JSON file.
_ID_PATTERNS = [
    re.compile(r"f_C=(\d+)"),
    re.compile(r"urn:li:(?:fsd_)?(?:organization|company|normalized_company):(\d+)"),
    re.compile(r"urn%3Ali%3A(?:fsd_)?(?:organization|company)%3A(\d+)"),
    re.compile(r'"companyId"\s*:\s*"?(\d+)'),
    re.compile(r"companyId=(\d+)"),
]


def parse_company_id(html: str) -> str:
    """Numeric LinkedIn company id from a public company page, or ''.

    Patterns are tried in order of reliability (the page's own "see jobs" link
    first); the page also lists similar companies, so a later pattern is only
    used when the earlier ones find nothing.
    """
    for pattern in _ID_PATTERNS:
        counts: dict[str, int] = {}
        for m in pattern.finditer(html or ""):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        if counts:
            return max(counts, key=counts.get)
    return ""


def _ids_path() -> str:
    return os.environ.get("LEADS_WATCH_IDS", "data/watch_company_ids.json")


def _load_ids() -> dict[str, str]:
    try:
        with open(_ids_path(), encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_ids(ids: dict[str, str]) -> None:
    try:
        path = _ids_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ids, f, ensure_ascii=False, indent=1)
    except OSError:
        logger.warning("watch_list: could not save company ids", exc_info=True)


async def fetch_company_id(company: WatchCompany) -> str:
    """Open the company's public LinkedIn page and read its numeric id."""
    from services import company_enrichment as ce

    if not company.slug:
        return ""
    url = f"https://www.linkedin.com/company/{company.slug}/"
    async with ce._linkedin_lock:
        try:
            html = await asyncio.to_thread(ce._fetch_company_playwright_sync, url)
        except Exception:
            logger.info("watch_list: company page fetch failed for %s", url, exc_info=True)
            html = ""
        finally:
            await asyncio.sleep(ce.LINKEDIN_PAUSE_SECONDS)
    return parse_company_id(html)


def company_jobs_url(company_id: str) -> str:
    """All of the company's open jobs in Israel."""
    return f"https://www.linkedin.com/jobs/search?location=Israel&f_C={company_id}"


async def scrape_watch_list(watch: WatchList) -> tuple[list[dict], list[str]]:
    """Open jobs of every watch-list company. Returns (jobs, companies with none found)."""
    from services.lead_pipeline import scrape_one

    return await _scrape(watch, scrape_one, fetch_company_id)


async def _scrape(watch: WatchList, scrape_one, get_id) -> tuple[list[dict], list[str]]:
    from services.scraper import LINKEDIN_MAX_JOBS_OVERRIDE

    ids = _load_ids()
    ids_changed = False
    jobs: list[dict] = []
    empty: list[str] = []
    for i, company in enumerate(watch.companies):
        if i:
            await asyncio.sleep(SEARCH_PAUSE_SECONDS)
        cid = ids.get(company.slug, "")
        if not cid and company.slug:
            cid = await get_id(company)
            if cid:
                ids[company.slug] = cid
                ids_changed = True
        # Both searches, merged: the company filter (paged) and the name search
        # (first page; unrelated jobs follow the company's own). Neither one
        # alone finds everything (Cyera: 1 by id, 13 by name).
        by_id: list[dict] = []
        if cid:
            by_id = await _search(scrape_one, company_jobs_url(cid), company, None, by_id=True)
        by_name = await _search(scrape_one, search_url(company), company, 0, by_id=False)
        found, seen_links = [], set()
        for j in by_id + by_name:
            link = j.get("job_link", "")
            if link and link in seen_links:
                continue
            seen_links.add(link)
            found.append(j)
        how = f"id {cid}: {len(by_id)}, name: {len(by_name)}" if cid else f"name: {len(by_name)}"
        if cid and not by_id and by_name:
            # The id gave nothing but the name did: probably a wrong id; read it again next run.
            ids.pop(company.slug, None)
            ids_changed = True
        for j in found:
            j["watch"] = "1"
        jobs.extend(found)
        if not found:
            empty.append(company.name)
        logger.info("watch_list: %s (%s) → %d jobs", company.name, how, len(found))
    if ids_changed:
        _save_ids(ids)
    return jobs, empty


async def _search(scrape_one, url: str, company: WatchCompany, max_jobs: int | None, *, by_id: bool) -> list[dict]:
    from services.scraper import LINKEDIN_MAX_JOBS_OVERRIDE

    token = LINKEDIN_MAX_JOBS_OVERRIDE.set(max_jobs)
    try:
        jobs = await scrape_one(url, "LinkedIn")
    except Exception:
        logger.warning("watch_list: search failed for %s", company.name, exc_info=True)
        return []
    finally:
        LINKEDIN_MAX_JOBS_OVERRIDE.reset(token)
    if by_id:
        # Results of a company filter are the company's own jobs; keep a
        # sanity check on the company name when the card carries a link.
        return [j for j in jobs if belongs_to(j, company) or not j.get("company_link")]
    return [j for j in jobs if belongs_to(j, company)]
