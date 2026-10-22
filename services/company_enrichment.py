"""Company size / type enrichment and target-audience rules for the lead agent.

Size comes from the company's LinkedIn page (via web search snippets) when a
search client is configured; otherwise the LLM estimates from its own
knowledge and marks low confidence as unknown.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from services.enrichment import complete_json
from services.search_client import SearchClient, format_results

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
Given a company name, a few of its job titles, and web search results (usually its LinkedIn
company page), return ONLY a JSON object:
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


async def enrich_company(
    company_name: str,
    job_titles: list[str],
    search: SearchClient | None,
) -> CompanyInfo:
    """Find size / type for one company. Never raises; returns UNKNOWN on failure."""
    results_text = "(web search not configured — use your own knowledge only)"
    snippet_size = UNKNOWN
    if search is not None:
        try:
            results = await search.search(f'"{company_name}" site:linkedin.com/company', num=8)
        except Exception:
            logger.warning("company_enrichment: search failed for %r", company_name, exc_info=True)
            results = []
        results_text = format_results(results)
        snippet_size = size_from_snippets(" ".join(r.snippet for r in results))

    user = (
        f"Company: {company_name}\n"
        f"Job titles: {', '.join(job_titles[:5]) or '(none)'}\n\n"
        f"Search results:\n{results_text}\n\nRespond with JSON only."
    )
    try:
        data = await complete_json(_SYSTEM_PROMPT, user, max_tokens=300)
    except Exception:
        logger.warning("company_enrichment: LLM failed for %r", company_name, exc_info=True)
        return CompanyInfo(size=snippet_size)

    size = normalize_size(data.get("size"))
    if size == UNKNOWN:
        size = snippet_size
    url = str(data.get("linkedin_url") or "").strip()
    if "linkedin.com/company" not in url:
        url = ""
    return CompanyInfo(size=size, company_type=normalize_type(data.get("company_type")), linkedin_url=url)
