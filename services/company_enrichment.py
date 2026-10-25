"""Company size / type enrichment and target-audience rules for the lead agent.

Size comes from the company's LinkedIn page (via web search snippets) when a
search client is configured; otherwise the LLM estimates from its own
knowledge and marks low confidence as unknown.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

from services.enrichment import complete_json
from services.search_client import BudgetExceeded, SearchClient, format_results

logger = logging.getLogger(__name__)

# LinkedIn company-size buckets
SIZE_BUCKETS: tuple[str, ...] = (
    "1-10",
    "11-50",
    "51-200",
    "201-500",
    "501-1000",
    "1001-5000",
    "5001-10000",
    "10001+",
)

UNKNOWN = "לא ידוע"
YES = "כן"
NO = "לא"

# internal key → sheet label
COMPANY_TYPES: dict[str, str] = {
    "product": "מוצר",
    "bank": "בנק",
    "insurance": "ביטוח",
    "telecom": "תקשורת",
    "retail": "קמעונאות",
    "government": "ציבורי/ממשלתי",
    "services": "חברת שירות",
    "staffing": "השמה",
    "outsourcing": "מיקור חוץ",
    "dev_center": "מרכז פיתוח בינלאומי",
    "other": "אחר",
}
_TYPE_BY_LABEL = {v: k for k, v in COMPANY_TYPES.items()}
EXCLUDED_ALWAYS = {"staffing", "outsourcing"}

SYSTEMS_CATEGORIES = frozenset({"יישום Salesforce", "יישום monday.com"})

_SIZE_IN_TEXT_RE = re.compile(
    r"(\d{1,2},?\d{0,3})\s*[-–]\s*(\d{1,2},?\d{0,3})\s*employees|(10,?001)\+?\s*employees",
    re.IGNORECASE,
)


@dataclass
class CompanyInfo:
    size: str = UNKNOWN          # LinkedIn bucket, e.g. "51-200", or UNKNOWN
    company_type: str = UNKNOWN  # Hebrew label from COMPANY_TYPES, or UNKNOWN
    linkedin_url: str = ""


def normalize_size(raw: object) -> str:
    text = " ".join(str(raw or "").split()).replace(",", "").replace("–", "-")
    if not text or text.lower() in ("null", "none", "unknown"):
        return UNKNOWN
    if text in SIZE_BUCKETS:
        return text
    if text.startswith("10001") or text.endswith("+"):
        return "10001+"
    nums = [int(n) for n in re.findall(r"\d+", text)]
    if not nums:
        return UNKNOWN
    n = max(nums) if len(nums) > 1 else nums[0]
    for bucket in SIZE_BUCKETS[:-1]:
        lo, hi = (int(x) for x in bucket.split("-"))
        if n <= hi:
            return bucket
    return "10001+"


def size_from_snippets(text: str) -> str:
    """Pull a LinkedIn size bucket like '51-200 employees' out of search snippets."""
    m = _SIZE_IN_TEXT_RE.search(text or "")
    if not m:
        return UNKNOWN
    if m.group(3):
        return "10001+"
    return normalize_size(f"{m.group(1)}-{m.group(2)}")


def normalize_type(raw: object) -> str:
    key = str(raw or "").strip().lower().replace(" ", "_")
    if key in COMPANY_TYPES:
        return COMPANY_TYPES[key]
    label = str(raw or "").strip()
    if label in _TYPE_BY_LABEL:
        return label
    return UNKNOWN


def _size_bounds(size: str) -> tuple[int, int] | None:
    if size == "10001+":
        return 10001, 10**9
    if size in SIZE_BUCKETS:
        lo, hi = (int(x) for x in size.split("-"))
        return lo, hi
    return None


def compute_target(
    info: CompanyInfo,
    categories: set[str],
    *,
    do_not_contact: bool = False,
) -> tuple[str, str]:
    """Return (target, reason). target is כן / לא / לא ידוע.

    reason is "product" (product company 50–200) or "systems" (Salesforce /
    monday.com exception) when target is כן; it selects the contact profile.
    """
    type_key = _TYPE_BY_LABEL.get(info.company_type)
    if do_not_contact or type_key in EXCLUDED_ALWAYS:
        return NO, ""
    bounds = _size_bounds(info.size)
    if bounds is None:
        return UNKNOWN, ""
    lo, _hi = bounds
    if info.size == "51-200" and type_key == "product":
        return YES, "product"
    if categories & SYSTEMS_CATEGORIES and lo >= 51:
        return YES, "systems"
    if type_key is None:
        return UNKNOWN, ""
    return NO, ""


_SYSTEM_PROMPT = """You profile Israeli employers for a B2B outreach campaign.
Given a company name, a few of its job titles, and data from its LinkedIn company page or web
search results, return ONLY a JSON object:
- "linkedin_url": the company's LinkedIn company page URL from the results, or null
- "size": the LinkedIn employee-count bucket, exactly one of
  "1-10","11-50","51-200","201-500","501-1000","1001-5000","5001-10000","10001+", or null.
  Prefer the number shown in the results ("51-200 employees"). Count employees worldwide
  as LinkedIn does. If the results do not show it and you are not confident, return null.
- "company_type": exactly one of
  "product"     — develops and sells its own software product (incl. SaaS / tech startups)
  "bank", "insurance", "telecom", "retail", "government" (also public bodies, municipalities,
  hospitals, universities, defense bodies)
  "services"    — IT / consulting / integration / service company
  "staffing"    — recruitment / placement agency
  "outsourcing" — dev outsourcing / body shop
  "dev_center"  — Israeli R&D center of a large international company
  "other"
  or null if you cannot tell.
Be careful: the result must be about this exact company, not a similarly named one.
Do not include markdown or extra keys."""


@dataclass
class LinkedInPage:
    size: str = UNKNOWN
    industry: str = ""
    description: str = ""


_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def parse_linkedin_company_page(html: str) -> LinkedInPage | None:
    """Read size / industry / description from a public LinkedIn company page."""
    soup = BeautifulSoup(html or "", "html.parser")

    def field(test_id: str) -> str:
        el = soup.select_one(f'[data-test-id="{test_id}"] dd') or soup.select_one(
            f'[data-test-id="{test_id}"]'
        )
        return " ".join(el.get_text(" ", strip=True).split()) if el else ""

    size_text = field("about-us__size")
    industry = field("about-us__industry")
    description = field("about-us__description")
    if not description:
        meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
            "meta", attrs={"property": "og:description"}
        )
        description = (meta.get("content") or "").strip() if meta else ""
    size = size_from_snippets(size_text) if size_text else UNKNOWN
    if size == UNKNOWN:
        size = size_from_snippets(soup.get_text(" ", strip=True)[:20000])
    if size == UNKNOWN and not industry and not description:
        return None
    return LinkedInPage(size=size, industry=industry, description=description[:800])


_linkedin_lock = asyncio.Lock()
LINKEDIN_PAUSE_SECONDS = 1.5


def _fetch_company_playwright_sync(url: str, timeout_ms: int = 30000) -> str:
    """Render the public company page in headless Chromium (LinkedIn answers
    plain HTTP clients with 403 but usually serves the guest page to a browser)."""
    from services.scraper import BROWSER_USER_AGENT, _import_playwright_sync

    sync_playwright = _import_playwright_sync()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        try:
            context = browser.new_context(user_agent=BROWSER_USER_AGENT, locale="en-US")
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2000)
            if response is not None and response.status >= 400:
                logger.info("company_enrichment: browser got %s for %s", response.status, url)
                return ""
            if "authwall" in page.url or "/login" in page.url:
                logger.info("company_enrichment: LinkedIn auth wall for %s", url)
                return ""
            return page.content()
        finally:
            browser.close()


async def fetch_linkedin_company(url: str) -> LinkedInPage | None:
    """Fetch the public company page. None when LinkedIn blocks or the page is empty.

    Tries a plain HTTP request first, then a headless browser. Requests are
    serialized with a short pause to avoid LinkedIn rate limits.
    """
    if "linkedin.com/company/" not in (url or ""):
        return None
    async with _linkedin_lock:
        try:
            html = ""
            try:
                async with httpx.AsyncClient(
                    headers=_PAGE_HEADERS, timeout=20.0, follow_redirects=True
                ) as client:
                    response = await client.get(url)
                if response.status_code == 200 and "authwall" not in str(response.url):
                    html = response.text
            except Exception:
                logger.debug("company_enrichment: http fetch failed for %s", url, exc_info=True)
            page = parse_linkedin_company_page(html) if html else None
            if page is None or page.size == UNKNOWN:
                try:
                    html = await asyncio.to_thread(_fetch_company_playwright_sync, url)
                except Exception:
                    logger.info("company_enrichment: browser fetch failed for %s", url, exc_info=True)
                    html = ""
                page = parse_linkedin_company_page(html) if html else page
            if page is None:
                logger.info("company_enrichment: LinkedIn page not readable %s", url)
            return page
        finally:
            await asyncio.sleep(LINKEDIN_PAUSE_SECONDS)


async def enrich_company(
    company_name: str,
    job_titles: list[str],
    search: SearchClient | None,
    company_link: str = "",
) -> CompanyInfo:
    """Find size / type for one company. Never raises; returns UNKNOWN on failure.

    Order: public LinkedIn company page (free) → web search (counts against the
    run budget) → LLM knowledge only.
    """
    evidence = ""
    page_size = UNKNOWN
    linkedin_url = company_link if "linkedin.com/company/" in (company_link or "") else ""

    page = await fetch_linkedin_company(linkedin_url) if linkedin_url else None
    if page is not None:
        page_size = page.size
        evidence = (
            f"LinkedIn company page ({linkedin_url}):\n"
            f"  Company size: {page.size}\n  Industry: {page.industry or '?'}\n"
            f"  About: {page.description or '?'}"
        )
    # No page, or the page did not show the size → also try a web search.
    if (page is None or page_size == UNKNOWN) and search is not None:
        try:
            results = await search.search(
                f'"{company_name}" company size employees', num=6, domain="linkedin.com"
            )
            evidence = (evidence + "\n\n" if evidence else "") + "Web search results:\n" + format_results(results)
            if page_size == UNKNOWN:
                page_size = size_from_snippets(" ".join(r.snippet for r in results))
        except BudgetExceeded:
            pass
        except Exception:
            logger.warning("company_enrichment: search failed for %r", company_name, exc_info=True)
    if not evidence:
        evidence = "(no web data available — use your own knowledge only; return null when unsure)"

    user = (
        f"Company: {company_name}\n"
        f"Job titles: {', '.join(job_titles[:5]) or '(none)'}\n\n"
        f"{evidence}\n\nRespond with JSON only."
    )
    try:
        data = await complete_json(_SYSTEM_PROMPT, user, max_tokens=300)
    except Exception:
        logger.warning("company_enrichment: LLM failed for %r", company_name, exc_info=True)
        return CompanyInfo(size=page_size, linkedin_url=linkedin_url)

    size = page_size if page_size != UNKNOWN else normalize_size(data.get("size"))
    url = linkedin_url or str(data.get("linkedin_url") or "").strip()
    if "linkedin.com/company" not in url:
        url = ""
    return CompanyInfo(size=size, company_type=normalize_type(data.get("company_type")), linkedin_url=url)
