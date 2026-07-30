"""Async job-board scraper for the Lead Scraping Agent.

Fetches listing HTML with httpx (or Playwright for JS-heavy boards), parses
cards with BeautifulSoup, and filters out non-development roles. Importable
from ``routers.leads``:

    from services.scraper import scrape_jobs

JS-rendered boards (AllJobs, Glassdoor, Jobnet, SQ Link, LinkedIn) require a
real browser — install once with::

    python -m playwright install chromium

Add new boards by extending ``BOARD_CONFIGS`` (selectors + host match).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from services.lead_dedup import filter_unseen_jobs, mark_jobs_seen

logger = logging.getLogger(__name__)

# Titles containing any of these (case-insensitive) are dropped.
EXCLUDE_KEYWORDS: tuple[str, ...] = (
    "QA",
    "DevOps",
    "Support",
    "תמיכה",
    "Automation",
    "fashion",
    "textile",
    "apparel",
    "אופנה",
    "טקסטיל",
    "הלבשה",
)

# Titles must match at least one of these to be kept (Software Development / AI).
KEEP_KEYWORDS: tuple[str, ...] = (
    "software",
    "developer",
    "development",
    "engineer",
    "engineering",
    "full.?stack",
    "fullstack",
    "front.?end",
    "frontend",
    "back.?end",
    "backend",
    "ai",
    "artificial intelligence",
    "machine learning",
    "ml engineer",
    "llm",
    "data scientist",
    "python",
    "java",
    "react",
    "node",
    "golang",
    "מפתח",
    "פיתוח",
    "תוכנה",
    "בינה מלאכותית",
)

# Non-tech "product developer" (fashion/textile) — keep only if software context.
_NON_TECH_PRODUCT_RE = re.compile(
    r"product\s+developer|מפתח(?:ת)?\s+מוצר",
    re.IGNORECASE,
)
_SOFTWARE_CONTEXT_RE = re.compile(
    r"software|saas|tech|digital|platform|app(?:lication)?|"
    r"full.?stack|front.?end|back.?end|engineer|"
    r"תוכנה|דיגיטל|היי[\s\-]?טק|הייטק|פלטפורמה|אפליקציה",
    re.IGNORECASE,
)

# Relative / absolute dates often shown on AllJobs cards.
_PUBLISH_DATE_RE = re.compile(
    r"(?:לפני\s+\d+\s+(?:דקה|דקות|שעה|שעות|יום|ימים|שבוע|שבועות)|"
    r"אתמול|היום|"
    r"\d{1,2}[./]\d{1,2}[./]\d{2,4})",
    re.IGNORECASE,
)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; RecruitmentAI-LeadScraper/1.0; +https://beyondtcode.com)"
)
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

ALLJOBS_MAX_PAGES = 5
ALLJOBS_SPONSORED_CONTAINER_SELECTOR = (
    ".open-board-box, .job-highlight, "
    "[class*='Highlight'], [class*='highlight'], "
    "[class*='VIP'], [class*='vip'], "
    "[class*='Premium'], [class*='premium'], "
    "[class*='Sponsored'], [class*='sponsored'], "
    "[class*='Promoted'], [class*='promoted']"
)
_ALLJOBS_SPONSORED_CLASS_RE = re.compile(
    r"open-board|highlight|vip|premium|sponsored|promoted",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BoardConfig:
    """CSS selectors and fetch options for a job-board listing page."""

    url: str
    source: str
    job_card: str = "article, .job-card, .job-listing, li.job, tr.job"
    title: str = "h2 a, h3 a, a.job-title, .job-title a, .title a"
    company: str = ".company, .company-name, [data-company], .employer"
    link: str = "h2 a, h3 a, a.job-title, .job-title a, a[href]"
    publish_date: str = (
        "time, .date, .publish-date, .posted-date, [class*='date'], [datetime]"
    )
    hosts: tuple[str, ...] = ()
    use_playwright: bool = False
    max_pages: int = 1
    skip_sponsored: bool = False
    default_company: str = ""


# Modular registry — add boards by extending this map (selectors + host match).
BOARD_CONFIGS: dict[str, BoardConfig] = {
    "AllJobs": BoardConfig(
        url="",
        source="AllJobs",
        job_card=".job-content-top",
        title=".job-content-top-title a[title], .job-content-top-title a",
        company=(
            ".job-content-top .T14 a, .job-content-top-company, [class*='company']"
        ),
        link=(
            ".job-content-top-title a[title], .job-content-top-title a, "
            "a[href*='/Search/'], a[href*='/Job/']"
        ),
        publish_date=(
            ".job-content-top [class*='date'], .job-content-top .AR, "
            ".job-content-top .T12, time, [datetime]"
        ),
        hosts=("alljobs.co.il",),
        use_playwright=True,
        max_pages=ALLJOBS_MAX_PAGES,
        skip_sponsored=True,
    ),
    "Drushim": BoardConfig(
        url="",
        source="Drushim",
        job_card=".job-item, .jobList_item, article.job, .primary--list--job",
        title="h2 a, h3 a, .job-url, a.job-title, .job-item-title a",
        company=".job-item-company, .company-name, .companyName, [data-company]",
        link="h2 a, h3 a, .job-url, a.job-title, a[href*='/job/']",
        publish_date=".job-item-date, .date, time, [class*='date']",
        hosts=("drushim.co.il",),
    ),
    "JobMaster": BoardConfig(
        url="",
        source="JobMaster",
        job_card=".JobItem, .job-item, article.job, .results-list .item",
        title="h2 a, h3 a, .JobItem-title a, a.jobTitle, .title a",
        company=".JobItem-company, .company, .companyName, [class*='company']",
        link="h2 a, h3 a, .JobItem-title a, a.jobTitle, a[href]",
        publish_date=".JobItem-date, .date, time, [class*='date']",
        hosts=("jobmaster.co.il",),
    ),
    "Glassdoor": BoardConfig(
        url="",
        source="Glassdoor",
        job_card=(
            '[data-test="job-listing-first-item"], .JobsList_jobListItem__, .jobTile'
        ),
        title='[data-test="job-title"], .JobCard_seoLink__',
        company=(
            '[data-test="employer-name"], .EmployerProfile_compactEmployerName__'
        ),
        link='a[data-test="job-title"], a.JobCard_seoLink__',
        publish_date='[data-test="job-age"], .JobCard_listingAge__',
        hosts=("glassdoor.com", "glassdoor.co.il"),
        use_playwright=True,
    ),
    "Jobnet": BoardConfig(
        url="",
        source="Jobnet",
        job_card=".job-item, .JobCard, #JobList .item, tr.JobRow",
        title='.job-title, .JobName, a[id*="JobName"]',
        company=".company-name, .CompanyName",
        link='a[href*="Position"], a[href*="Job"]',
        publish_date=".job-date, .Date",
        hosts=("jobnet.co.il",),
        use_playwright=True,
    ),
    "SQ Link": BoardConfig(
        url="",
        source="SQ Link",
        job_card=".position-item, .job-box, .careers-list-item",
        title=".position-title, h3, h4",
        company=".company-name, .company, [data-company]",
        link='a[href*="position"], a[href*="job"]',
        publish_date=".date, .publish-date",
        hosts=("sqlink.com",),
        use_playwright=True,
        default_company="SQ Link",
    ),
    "LinkedIn": BoardConfig(
        url=(
            "https://www.linkedin.com/jobs/search?"
            "keywords=Software%20Developer&location=Israel&f_TPR=r86400"
        ),
        source="LinkedIn",
        job_card=(
            ".job-search-card, .base-search-card, ul.jobs-search__results-list > li"
        ),
        title=".base-search-card__title, h3.base-search-card__title",
        company=".base-search-card__subtitle, a.hidden-nested-link",
        link="a.base-card__full-link, a.base-search-card__full-link",
        publish_date="time.job-search-card__listdate, time",
        hosts=("linkedin.com",),
        use_playwright=True,
    ),
    "GenericJobBoard": BoardConfig(
        url="",
        source="GenericJobBoard",
    ),
}

LINKEDIN_DEFAULT_URL = BOARD_CONFIGS["LinkedIn"].url
LINKEDIN_WAIT_SELECTOR = ".job-search-card, .jobs-search__results-list"


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_alljobs_url(url: str) -> bool:
    return "alljobs.co.il" in _hostname(url)


def _is_linkedin_url(url: str) -> bool:
    return "linkedin.com" in _hostname(url)


def _is_linkedin_board(config: BoardConfig) -> bool:
    return config.source == "LinkedIn" or _is_linkedin_url(config.url)


def _board_template_for_url(url: str) -> BoardConfig | None:
    host = _hostname(url)
    if not host:
        return None
    for board in BOARD_CONFIGS.values():
        if any(h in host for h in board.hosts):
            return board
    return None


def board_config_for_url(url: str, source: str = "GenericJobBoard") -> BoardConfig:
    """Resolve selectors from URL host, then ``BOARD_CONFIGS[source]``, else generic."""
    by_host = _board_template_for_url(url)
    if by_host is not None:
        # Keep caller source if they overrode it; otherwise use board default.
        resolved_source = source if source and source != "GenericJobBoard" else by_host.source
        logger.info(
            "scrape_jobs: auto-selected board config '%s' for host '%s' (url=%s)",
            resolved_source,
            _hostname(url),
            url,
        )
        return replace(by_host, url=url, source=resolved_source)

    template = BOARD_CONFIGS.get(source) or BOARD_CONFIGS["GenericJobBoard"]
    return replace(template, url=url, source=source or template.source)


def _alljobs_url_with_page(url: str, page: int) -> str:
    """Set or replace the ``page`` query parameter on an AllJobs listing URL."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))


def _class_list(element: Any) -> str:
    raw = getattr(element, "get", lambda *_: None)("class")
    if isinstance(raw, list):
        return " ".join(raw)
    return str(raw or "")


def _is_sponsored_alljobs_card(card: Any) -> bool:
    """True if the card itself or an ancestor looks like sponsored/VIP noise."""
    current = card
    while current is not None and getattr(current, "name", None) is not None:
        if _ALLJOBS_SPONSORED_CLASS_RE.search(_class_list(current)):
            return True
        current = getattr(current, "parent", None)
    return False


def _keyword_to_pattern(keyword: str) -> str:
    """Build a regex fragment; short tokens use word boundaries to avoid false hits."""
    if ".?" in keyword:
        return rf"(?<!\w)(?:{keyword})(?!\w)"
    escaped = re.escape(keyword)
    # ASCII tokens (e.g. "AI", "QA", "Support") must not match inside other words.
    if re.fullmatch(r"[A-Za-z]+", keyword):
        return rf"\b{escaped}\b"
    return escaped


def _compile_keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(_keyword_to_pattern(kw) for kw in keywords), re.IGNORECASE)


_EXCLUDE_RE = _compile_keyword_pattern(EXCLUDE_KEYWORDS)
_KEEP_RE = _compile_keyword_pattern(KEEP_KEYWORDS)


def is_excluded_title(title: str) -> bool:
    """Return True if the title contains a blocked keyword (QA, fashion, etc.)."""
    return bool(title and _EXCLUDE_RE.search(title))


def is_software_or_ai_title(title: str) -> bool:
    """Return True if the title looks like Software Development or AI."""
    return bool(title and _KEEP_RE.search(title))


def is_non_tech_product_developer(title: str) -> bool:
    """Fashion/textile-style product developers (not software)."""
    cleaned = (title or "").strip()
    if not cleaned or not _NON_TECH_PRODUCT_RE.search(cleaned):
        return False
    return not _SOFTWARE_CONTEXT_RE.search(cleaned)


def filter_job_title(title: str) -> bool:
    """Keep titles that are Software/AI related and not in the exclude list."""
    cleaned = (title or "").strip()
    if not cleaned:
        return False
    if is_excluded_title(cleaned):
        return False
    if is_non_tech_product_developer(cleaned):
        return False
    return is_software_or_ai_title(cleaned)


def _text_or_empty(element: Any) -> str:
    if element is None:
        return ""
    return " ".join(element.get_text(" ", strip=True).split())


def _first_href(element: Any, base_url: str) -> str:
    if element is None:
        return ""
    anchor = element if getattr(element, "name", None) == "a" else element.find("a", href=True)
    if anchor is None:
        return ""
    href = (anchor.get("href") or "").strip()
    if not href or href.startswith("#") or href.lower().startswith("javascript:"):
        return ""
    return urljoin(base_url, href)


def _extract_publish_date(card: Any, config: BoardConfig) -> str:
    """Pull published date/text from a card via selector, then regex fallback."""
    if config.publish_date and hasattr(card, "select"):
        for el in card.select(config.publish_date):
            dt = (el.get("datetime") or "").strip()
            if dt:
                return dt
            text = _text_or_empty(el)
            if text and (_PUBLISH_DATE_RE.search(text) or len(text) <= 40):
                # Prefer strings that look like dates when several nodes match.
                if _PUBLISH_DATE_RE.search(text):
                    return text
    card_text = _text_or_empty(card)
    match = _PUBLISH_DATE_RE.search(card_text)
    if match:
        return match.group(0).strip()
    return ""


def parse_job_cards(html: str, config: BoardConfig) -> list[dict[str, str]]:
    """Parse raw HTML into job dicts (unfiltered)."""
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict[str, str]] = []
    seen_links: set[str] = set()

    cards = soup.select(config.job_card)
    if not cards:
        # Fallback: treat title anchors as standalone listings.
        cards = soup.select(config.title)

    for card in cards:
        if config.skip_sponsored and _is_sponsored_alljobs_card(card):
            continue

        title_el = card.select_one(config.title) if hasattr(card, "select_one") else None
        if title_el is None and getattr(card, "name", None) == "a":
            title_el = card

        title = _text_or_empty(title_el)
        if not title:
            continue

        company_el = card.select_one(config.company) if hasattr(card, "select_one") else None
        company = _text_or_empty(company_el) or config.default_company or "Unknown"

        link_el = card.select_one(config.link) if hasattr(card, "select_one") else title_el
        job_link = _first_href(link_el, config.url) or _first_href(title_el, config.url)

        if job_link and job_link in seen_links:
            continue
        if job_link:
            seen_links.add(job_link)

        jobs.append(
            {
                "job_title": title,
                "company_name": company,
                "job_link": job_link,
                "publish_date": _extract_publish_date(card, config),
                "source": config.source,
            }
        )

    return jobs


def filter_jobs(jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Apply text-based Software/AI keep + exclude-keyword filters."""
    filtered: list[dict[str, str]] = []
    for job in jobs:
        title = job.get("job_title", "")
        if filter_job_title(title):
            filtered.append(job)
        else:
            logger.debug("Filtered out job title: %s", title)
    return filtered


async def fetch_listing_html(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    headers: dict[str, str] | None = None,
) -> str:
    """Download listing page HTML. Raises httpx.HTTPError on transport/HTTP failure."""
    request_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html"}
    if headers:
        request_headers.update(headers)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers=request_headers,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


def _import_playwright_sync():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error(
            "scrape_jobs: playwright is not installed; "
            "pip install playwright && python -m playwright install chromium"
        )
        raise
    return sync_playwright


def _is_alljobs_board(config: BoardConfig) -> bool:
    return config.source == "AllJobs" or config.skip_sponsored or config.max_pages > 1


def _fetch_alljobs_sync(url: str, timeout_ms: int = 30000) -> tuple[str, int]:
    """Sync Playwright fetch of AllJobs pages 1..N — runs via ``asyncio.to_thread``.

    Returns ``(combined_card_html, pages_scraped)``. Sponsored/VIP cards are omitted.
    """
    sync_playwright = _import_playwright_sync()

    card_selector = BOARD_CONFIGS["AllJobs"].job_card
    card_htmls: list[str] = []
    pages_scraped = 0
    max_pages = BOARD_CONFIGS["AllJobs"].max_pages or ALLJOBS_MAX_PAGES

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(
                user_agent=BROWSER_USER_AGENT,
                locale="he-IL",
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()

            for page_num in range(1, max_pages + 1):
                page_url = _alljobs_url_with_page(url, page_num)
                page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_selector(card_selector, timeout=timeout_ms)
                except Exception:
                    logger.warning(
                        "scrape_jobs: Playwright timed out waiting for job cards "
                        "on page %d (%s)",
                        page_num,
                        page_url,
                    )
                    break

                page_cards: list[str] = page.eval_on_selector_all(
                    card_selector,
                    """(els, sponsoredSel) => els
                        .filter(el => !el.closest(sponsoredSel))
                        .filter(el => {
                          const cls = (el.className || '').toString();
                          return !/open-board|highlight|vip|premium|sponsored|promoted/i.test(cls);
                        })
                        .map(el => el.outerHTML)
                    """,
                    ALLJOBS_SPONSORED_CONTAINER_SELECTOR,
                )

                if not page_cards:
                    logger.info(
                        "scrape_jobs: AllJobs page %d had 0 organic cards; stopping",
                        page_num,
                    )
                    break

                card_htmls.extend(page_cards)
                pages_scraped += 1
                logger.info(
                    "scrape_jobs: AllJobs page %d → %d organic cards",
                    page_num,
                    len(page_cards),
                )
        finally:
            browser.close()

    combined = (
        '<div id="alljobs-scrape-root">' + "".join(card_htmls) + "</div>"
        if card_htmls
        else ""
    )
    logger.info(
        "scrape_jobs: AllJobs scraped %d pages, %d card HTML fragments accumulated",
        pages_scraped,
        len(card_htmls),
    )
    return combined, pages_scraped


def _fetch_listing_playwright_sync(
    url: str,
    config: BoardConfig,
    timeout_ms: int = 30000,
) -> tuple[str, int]:
    """Sync Playwright single-page fetch for JS-heavy boards (non-AllJobs)."""
    sync_playwright = _import_playwright_sync()
    is_linkedin = _is_linkedin_board(config) or _is_linkedin_url(url)
    wait_selector = LINKEDIN_WAIT_SELECTOR if is_linkedin else config.job_card

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(
                user_agent=BROWSER_USER_AGENT,
                locale="he-IL",
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)
            try:
                page.wait_for_selector(wait_selector, timeout=timeout_ms)
            except Exception:
                logger.warning(
                    "scrape_jobs: Playwright timed out waiting for job cards "
                    "on %s (%s)",
                    config.source,
                    url,
                )
                return "", 0

            if is_linkedin:
                # Gentle scroll to activate LinkedIn Guest Mode lazy-loading.
                page.evaluate("window.scrollBy(0, 1000)")
                page.wait_for_timeout(1500)

            html = page.content()
            logger.info(
                "scrape_jobs: %s Playwright rendered listing page (%d chars)",
                config.source,
                len(html),
            )
            return html, 1
        finally:
            browser.close()


async def fetch_listing_html_playwright(
    url: str,
    *,
    config: BoardConfig | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, int]:
    """Render a JS-heavy listing page with headless Chromium.

    AllJobs uses multi-page pagination (pages 1–N). Other boards (Glassdoor,
    Jobnet, SQ Link, LinkedIn, etc.) render a single page. LinkedIn Guest Mode
    also scrolls once to trigger lazy-loaded job cards.

    Uses sync Playwright in a thread so Windows/Uvicorn SelectorEventLoop does not
    hit ``NotImplementedError`` on subprocess transport.

    Returns ``(html, pages_scraped)``.

    Requires ``playwright`` and a one-time ``python -m playwright install chromium``.
    """
    timeout_ms = int(timeout * 1000)
    board = config or board_config_for_url(url)
    if _is_alljobs_board(board) or _is_alljobs_url(url):
        return await asyncio.to_thread(_fetch_alljobs_sync, url, timeout_ms)
    return await asyncio.to_thread(
        _fetch_listing_playwright_sync, url, board, timeout_ms
    )


async def scrape_jobs(
    url: str | None = None,
    *,
    source: str = "GenericJobBoard",
    config: BoardConfig | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, str]]:
    """Scrape a job board and return filtered Software Development / AI listings.

    Each dict has keys: ``job_title``, ``company_name``, ``job_link``,
    ``publish_date``, ``source``.

    Previously seen ``job_link`` values (SQLite dedup) are excluded so daily
    automation only returns new leads.

    Parameters
    ----------
    url:
        Listing page URL. Required unless ``config.url`` is provided.
    source:
        Board key in ``BOARD_CONFIGS`` / value written to each job's ``source``
        field when host-based detection does not apply.
    config:
        Optional board-specific selectors. When omitted, selectors are chosen from
        the URL host or ``BOARD_CONFIGS``.
    timeout:
        Fetch timeout in seconds (httpx or Playwright).

    Returns an empty list on network/parse failures (errors are logged).
    """
    if config is not None:
        board = config
    else:
        resolved = (url or "").strip()
        if not resolved and source == "LinkedIn":
            resolved = LINKEDIN_DEFAULT_URL
        if not resolved:
            logger.error("scrape_jobs: no URL provided")
            return []
        board = board_config_for_url(resolved, source=source)

    if not board.url:
        fallback = (url or "").strip()
        if not fallback and (board.source == "LinkedIn" or source == "LinkedIn"):
            fallback = LINKEDIN_DEFAULT_URL
        if not fallback:
            logger.error("scrape_jobs: no URL provided")
            return []
        board = board_config_for_url(fallback, source=board.source or source)

    pages_scraped = 0
    try:
        if board.use_playwright or _is_alljobs_url(board.url):
            html, pages_scraped = await fetch_listing_html_playwright(
                board.url, config=board, timeout=timeout
            )
        else:
            html = await fetch_listing_html(board.url, timeout=timeout)
    except ImportError:
        return []
    except httpx.TimeoutException:
        logger.exception("scrape_jobs: timeout fetching %s", board.url)
        return []
    except httpx.HTTPStatusError as exc:
        logger.exception(
            "scrape_jobs: HTTP %s for %s",
            exc.response.status_code,
            board.url,
        )
        return []
    except httpx.HTTPError:
        logger.exception("scrape_jobs: request failed for %s", board.url)
        return []
    except Exception:
        logger.exception("scrape_jobs: failed to fetch %s", board.url)
        return []

    try:
        raw_jobs = parse_job_cards(html, board)
    except Exception:
        logger.exception("scrape_jobs: failed to parse HTML from %s", board.url)
        return []

    if pages_scraped:
        logger.info(
            "scrape_jobs: %s scraped %d pages → %d raw jobs before filter",
            board.source,
            pages_scraped,
            len(raw_jobs),
        )

    titles = [j.get("job_title", "") for j in raw_jobs]
    logger.info("scrape_jobs: extracted %d titles: %s", len(titles), titles)

    filtered = filter_jobs(raw_jobs)
    new_jobs = filter_unseen_jobs(filtered)
    if new_jobs:
        mark_jobs_seen(new_jobs)

    logger.info(
        "scrape_jobs: %s → %d raw, %d after filter, %d new after dedup (source=%s)",
        board.url,
        len(raw_jobs),
        len(filtered),
        len(new_jobs),
        board.source,
    )
    return new_jobs
