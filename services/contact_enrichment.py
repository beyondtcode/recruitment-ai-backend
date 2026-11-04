"""Decision-maker contacts (primary + alternate) for target companies.

Only public professional data is collected: name, title and LinkedIn profile URL.
Requires a web search client; without one, contacts stay "לא נמצא".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from services.enrichment import complete_json
from services.lead_filters import AI_CATEGORIES
from services.search_client import BudgetExceeded, SearchClient, SearchResult, format_results

logger = logging.getLogger(__name__)

NOT_FOUND = "לא נמצא"

# Profile → (priority description for the LLM, role words for the searches in order).
# Each search is "<short company name> <roles>" on linkedin.com. Short, unquoted
# queries find the right people far more often than one long OR-list (checked
# on Cyera / Glow / Dream / AITAN, Nov 2026).
PROFILES: dict[str, tuple[str, list[str]]] = {
    "dev": (
        "1) CTO or VP R&D (also VP Engineering / Chief Technology Officer). "
        "2) Director of Engineering / Head of Engineering / R&D Manager, preferably in the "
        "job's domain ({domain}). "
        "3) If nobody from 1-2 appears in the results: the CEO or a Founder.",
        [
            "CTO",
            '"VP R&D" OR "VP Engineering" OR "Head of Engineering"',
            "CEO OR Founder",
        ],
    ),
    "ai": (
        "1) CTO. 2) COO. 3) CEO or Founder.",
        [
            "CTO",
            "COO",
            "CEO OR Founder",
        ],
    ),
    "systems": (
        "The person responsible for business systems (Salesforce / monday.com): "
        "1) VP Operations or RevOps / Revenue Operations lead. 2) COO. 3) CIO.",
        [
            '"VP Operations" OR RevOps OR "Revenue Operations"',
            "COO",
            'CIO OR "Head of Business Systems"',
        ],
    ),
}

_EXCLUDED_ROLE_RE = re.compile(
    r"recruit|talent|acquisition|\bhr\b|hrbp|human resources|people (?:partner|operations)|"
    r"גיוס|משאבי אנוש|מגייס",
    re.IGNORECASE,
)


@dataclass
class Contact:
    name: str
    title: str
    linkedin_url: str


_COUNTRY_SUBDOMAIN_RE = re.compile(r"^https?://([a-z]{2})\.linkedin\.com/", re.IGNORECASE)


def _foreign_profile(link: str) -> bool:
    """True for profiles on another country's LinkedIn subdomain (il / www are kept)."""
    m = _COUNTRY_SUBDOMAIN_RE.match(link or "")
    return bool(m) and m.group(1).lower() != "il"


def choose_profile(target_reason: str, latest_category: str) -> str:
    if target_reason == "systems":
        return "systems"
    if latest_category in AI_CATEGORIES:
        return "ai"
    return "dev"


_SYSTEM_PROMPT = """You pick outreach contacts at a company from LinkedIn search results.
Return ONLY a JSON object: {"primary": CONTACT|null, "alternate": CONTACT|null}
where CONTACT = {"name": str, "title": str, "linkedin_url": str}.

Rules:
- Use only people that appear in the results. Copy linkedin_url exactly from a result link
  (a linkedin.com/in/ profile URL). Never invent people or URLs.
- The person must CURRENTLY work at the given company (the result title/snippet shows the
  company as their current employer). Skip former employees and other companies.
- The company hires in Israel. Beware of different companies with a similar name in other
  countries: when the result does not clearly match this company (name, industry, Israel),
  skip it. When in doubt, return null rather than a wrong person.
- Never pick recruiters, talent acquisition, HR, HRBP or people/HR roles.
- Priority order (primary = best available, alternate = next best, a different person):
  {priority}
- "title" is the person's current role as shown (e.g. "VP R&D").
- Only the roles in the priority list qualify. Team leads, engineers, developers,
  architects, product managers and other non-executive roles never qualify.
- Among qualifying people, take the best available: a lower priority role is better
  than null. Return null only when nobody at this company holds any listed role.
Do not include markdown or extra keys."""


_AT_COMPANY_RE = re.compile(r"\s+(?:at|@|ב-?)\s+.+$", re.IGNORECASE)


def clean_title(title: str) -> str:
    """'Chief Technology Officer at Lumana' → 'Chief Technology Officer'."""
    return _AT_COMPANY_RE.sub("", title or "").strip(" -–|,")


_FALLBACK_ROLE_RE = re.compile(r"\b(?:ceo|chief executive|founder)\b", re.IGNORECASE)
_SENIOR_TECH_RE = re.compile(
    r"\b(?:cto|chief technology|vp|vice president|r&d|engineering|coo|chief operating|cio|operations|revops)\b",
    re.IGNORECASE,
)


def is_fallback_role(title: str) -> bool:
    """CEO / Founder without a technical or operations role (keep searching)."""
    return bool(_FALLBACK_ROLE_RE.search(title or "")) and not _SENIOR_TECH_RE.search(title or "")


_JUNIOR_RE = re.compile(
    r"\b(?:team ?lead(?:er)?|tech(?:nical)? lead|engineer|developer|architect|specialist|analyst|"
    r"intern|student|product manager)\b",
    re.IGNORECASE,
)
_SENIOR_RE = re.compile(
    r"\b(?:cto|ceo|coo|cio|chief|vp|vice president|director|head|founder|co-founder|"
    r"r&d manager|engineering manager|revops|revenue operations)\b",
    re.IGNORECASE,
)


def too_junior(title: str) -> bool:
    """'Engineering Team Leader' / 'Software Engineer' are not decision makers."""
    return bool(_JUNIOR_RE.search(title or "")) and not _SENIOR_RE.search(title or "")


def mentions_company(text: str, company_name: str) -> bool:
    """True when the result text names the company (guards against people at
    other companies, e.g. "VP R&D at Ryft" offered for Cyera)."""
    short = short_company_name(company_name).lower()
    if not short or not short.isascii():
        return True  # Hebrew / unusual names: rely on the LLM
    return re.search(rf"(?<![a-z0-9]){re.escape(short)}(?![a-z0-9])", (text or "").lower()) is not None


def _valid(raw: object, allowed_links: dict[str, str], company_name: str = "") -> Contact | None:
    if not isinstance(raw, dict):
        return None
    name = " ".join(str(raw.get("name") or "").split())
    title = clean_title(" ".join(str(raw.get("title") or "").split()))
    url = str(raw.get("linkedin_url") or "").strip().split("?")[0].rstrip("/")
    if not name or "linkedin.com/in/" not in url:
        return None
    if url not in allowed_links:
        logger.warning("contact_enrichment: dropping URL not in results: %s", url)
        return None
    if company_name and not mentions_company(allowed_links[url], company_name):
        logger.info("contact_enrichment: %s does not show %r as employer; skipped", url, company_name)
        return None
    if _EXCLUDED_ROLE_RE.search(title):
        return None
    if too_junior(title):
        logger.info("contact_enrichment: %r (%s) is not a decision maker; skipped", name, title)
        return None
    return Contact(name=name, title=title, linkedin_url=url)


_NAME_TAIL_RE = re.compile(r"\s*(?:[|(–—]|\s-\s).*$")
_NAME_SUFFIX_RE = re.compile(r"[,.]?\s+(?:ltd|inc|llc|corp|corporation|group|technologies|בע\"?מ)\.?$", re.IGNORECASE)


def short_company_name(name: str) -> str:
    """'PayPlus - The Future of Payments' → 'PayPlus'; 'AppCard, Inc.' → 'AppCard'."""
    text = _NAME_TAIL_RE.sub("", name or "").strip()
    text = _NAME_SUFFIX_RE.sub("", text).strip()
    return text or (name or "").strip()


async def find_contacts(
    company_name: str,
    profile: str,
    domain: str,
    search: SearchClient | None,
    company_hint: str = "",
) -> tuple[Contact | None, Contact | None]:
    """Return (primary, alternate).

    Up to three searches, one per role group of the profile, in priority order.
    Stops when a suitable person is found; a CEO / Founder found early is kept
    only if the remaining searches find nobody in a higher-priority role.

    Raises ``BudgetExceeded`` only when the run's search budget ran out before
    anything was searched for this company. Other errors are swallowed.
    """
    if search is None:
        return None, None
    priority, clauses = PROFILES.get(profile, PROFILES["dev"])
    results: list[SearchResult] = []
    seen: set[str] = set()
    searched = False

    async def collect(query: str) -> bool:
        """Run one query; returns False when the budget ran out."""
        nonlocal searched
        try:
            batch = await search.search(query, num=10, domain="linkedin.com")
        except BudgetExceeded:
            if not searched and not results:
                raise  # nothing searched for this company: caller retries next run
            return False
        except Exception:
            logger.warning("contact_enrichment: search failed for %r", company_name, exc_info=True)
            return True
        searched = True
        for r in batch:
            link = r.link.split("?")[0].rstrip("/")
            if _foreign_profile(link):
                continue  # e.g. pk.linkedin.com — usually a similarly named company abroad
            if "linkedin.com/in/" in link and link not in seen:
                seen.add(link)
                results.append(SearchResult(title=r.title, link=link, snippet=r.snippet))
        return True

    short = short_company_name(company_name)
    best: tuple[Contact | None, Contact | None] = (None, None)
    for roles in clauses:
        if not await collect(f"{short} {roles}"):
            break
        pick = await _pick(company_name, priority, domain, company_hint, results)
        if pick[0] is None:
            continue  # keep an earlier CEO / Founder pick, if any
        best = pick
        if not is_fallback_role(pick[0].title):
            return best
    return best


async def _pick(
    company_name: str,
    priority: str,
    domain: str,
    company_hint: str,
    results: list[SearchResult],
) -> tuple[Contact | None, Contact | None]:
    if not results:
        return None, None
    system = _SYSTEM_PROMPT.replace("{priority}", priority.format(domain=domain or "software"))
    hint = f"Company LinkedIn page: {company_hint}\n" if company_hint else ""
    user = (
        f"Company: {company_name}\n{hint}\nSearch results:\n"
        f"{format_results(results, limit=25)}\n\nRespond with JSON only."
    )
    try:
        data = await complete_json(system, user, max_tokens=400)
    except Exception:
        logger.warning("contact_enrichment: LLM failed for %r", company_name, exc_info=True)
        return None, None

    allowed = {r.link: f"{r.title} {r.snippet}" for r in results}
    primary = _valid(data.get("primary"), allowed, company_name)
    alternate = _valid(data.get("alternate"), allowed, company_name)
    if primary is None and alternate is not None:
        primary, alternate = alternate, None
    if alternate is not None and primary is not None and alternate.linkedin_url == primary.linkedin_url:
        alternate = None
    return primary, alternate
