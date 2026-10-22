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
- Never pick recruiters, talent acquisition, HR, HRBP or people/HR roles.
- Priority order (primary = best available, alternate = next best, a different person):
  {priority}
- "title" is the person's current role as shown (e.g. "VP R&D").
- If nobody qualifies, return null for that slot.
Do not include markdown or extra keys."""


def _valid(raw: object, allowed_links: set[str]) -> Contact | None:
    if not isinstance(raw, dict):
        return None
    name = " ".join(str(raw.get("name") or "").split())
    title = " ".join(str(raw.get("title") or "").split())
    url = str(raw.get("linkedin_url") or "").strip().split("?")[0].rstrip("/")
    if not name or "linkedin.com/in/" not in url:
        return None
    if url not in allowed_links:
        logger.warning("contact_enrichment: dropping URL not in results: %s", url)
        return None
    if _EXCLUDED_ROLE_RE.search(title):
        return None
    return Contact(name=name, title=title, linkedin_url=url)


async def find_contacts(
    company_name: str,
    profile: str,
    domain: str,
    search: SearchClient | None,
) -> tuple[Contact | None, Contact | None]:
    """Return (primary, alternate).

    Raises ``BudgetExceeded`` only when the run's search budget ran out before
    anything was searched for this company. Other errors are swallowed.
    """
    if search is None:
        return None, None
    priority, clauses = PROFILES.get(profile, PROFILES["dev"])
    results: list[SearchResult] = []
    seen: set[str] = set()
    for i, clause in enumerate(clauses):
        # The last clause is a fallback (e.g. CEO/Founder): skip it when the
        # earlier searches already found at least two people.
        if i == len(clauses) - 1 and i >= 2 and len(results) >= 2:
            break
        try:
            batch = await search.search(f'"{company_name}" {clause}', num=10, domain="linkedin.com")
        except BudgetExceeded:
            if not results:
                raise  # nothing searched for this company: caller retries next run
            break
        except Exception:
            logger.warning("contact_enrichment: search failed for %r", company_name, exc_info=True)
            continue
        for r in batch:
            link = r.link.split("?")[0].rstrip("/")
            if "linkedin.com/in/" in link and link not in seen:
                seen.add(link)
                results.append(SearchResult(title=r.title, link=link, snippet=r.snippet))

    if not results:
        return None, None

    system = _SYSTEM_PROMPT.replace("{priority}", priority.format(domain=domain or "software"))
    user = (
        f"Company: {company_name}\n\nSearch results:\n"
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
