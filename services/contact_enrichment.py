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

# Profile → (priority description for the LLM, search role clauses in order)
PROFILES: dict[str, tuple[str, list[str]]] = {
    "dev": (
        "1) CTO or VP R&D (also VP Engineering / Chief Technology Officer). "
        "2) Director of Engineering / Head of Engineering / R&D Manager, preferably in the "
        "job's domain ({domain}). "
        "3) Only if none of the above exist: CEO or Founder.",
        [
            '(CTO OR "VP R&D" OR "VP Engineering" OR "Chief Technology Officer")',
            '("Director of Engineering" OR "Head of Engineering" OR "R&D Manager" OR "Engineering Manager")',
            '(CEO OR Founder OR "Co-Founder")',
        ],
    ),
    "ai": (
        "1) CTO. 2) COO. 3) CEO or Founder.",
        [
            '(CTO OR "Chief Technology Officer")',
            '(COO OR "Chief Operating Officer")',
            '(CEO OR Founder OR "Co-Founder")',
        ],
    ),
    "systems": (
        "The person responsible for business systems (Salesforce / monday.com): "
        "1) VP Operations or RevOps / Revenue Operations lead. 2) COO. 3) CIO.",
        [
            '("VP Operations" OR "RevOps" OR "Revenue Operations")',
            '(COO OR "Chief Operating Officer" OR CIO OR "Chief Information Officer")',
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
- If nobody qualifies, return null for that slot.
Do not include markdown or extra keys."""


_AT_COMPANY_RE = re.compile(r"\s+(?:at|@|ב-?)\s+.+$", re.IGNORECASE)


def clean_title(title: str) -> str:
    """'Chief Technology Officer at Lumana' → 'Chief Technology Officer'."""
    return _AT_COMPANY_RE.sub("", title or "").strip(" -–|,")


def _valid(raw: object, allowed_links: set[str]) -> Contact | None:
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
    if _EXCLUDED_ROLE_RE.search(title):
        return None
    return Contact(name=name, title=title, linkedin_url=url)


# Simpler second-pass queries (no quotes / boolean operators), used only when
# the first pass found nobody suitable.
LOOSE_ROLES: dict[str, list[str]] = {
    "dev": ["CTO", "VP R&D", "Head of Engineering", "Founder"],
    "ai": ["CTO", "COO", "CEO"],
    "systems": ["VP Operations", "COO", "CIO"],
}

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

    Pass 1: precise queries. Pass 2 (only if pass 1 found nobody suitable):
    simpler queries with the short company name.

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

    # Pass 1 — precise.
    for i, clause in enumerate(clauses):
        # The last clause is a fallback (e.g. CEO/Founder): skip it when the
        # earlier searches already found at least two people.
        if i == len(clauses) - 1 and i >= 2 and len(results) >= 2:
            break
        if not await collect(f'"{company_name}" {clause} Israel'):
            break
    primary, alternate = await _pick(company_name, priority, domain, company_hint, results)
    if primary is not None:
        return primary, alternate

    # Pass 2 — looser, with the short company name.
    short = short_company_name(company_name)
    for role in LOOSE_ROLES.get(profile, LOOSE_ROLES["dev"]):
        if not await collect(f"{short} {role}"):
            break
        if len(results) >= 6:
            break
    return await _pick(company_name, priority, domain, company_hint, results)


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

    allowed = {r.link for r in results}
    primary = _valid(data.get("primary"), allowed)
    alternate = _valid(data.get("alternate"), allowed)
    if primary is None and alternate is not None:
        primary, alternate = alternate, None
    if alternate is not None and primary is not None and alternate.linkedin_url == primary.linkedin_url:
        alternate = None
    return primary, alternate
