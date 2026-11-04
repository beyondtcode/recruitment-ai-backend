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
Return ONLY a JSON object: {"candidates": [CONTACT, ...]}
where CONTACT = {"name": str, "title": str, "linkedin_url": str}: up to 4 different
qualifying people, best first (by the priority order below). An empty list if nobody qualifies.

Rules:
- Use only people that appear in the results. Copy linkedin_url exactly from a result link
  (a linkedin.com/in/ profile URL). Never invent people or URLs.
- The person must CURRENTLY work at the given company (the result title/snippet shows the
  company as their current employer). Skip former employees and other companies.
- The company hires in Israel. Beware of different companies with a similar name in other
  countries: when the result does not clearly match this company (name, industry, Israel),
  skip it. When in doubt, return null rather than a wrong person.
- Never pick recruiters, talent acquisition, HR, HRBP or people/HR roles.
- Priority order:
  {priority}
- "title" is the person's current role as shown (e.g. "VP R&D").
- Only the roles in the priority list qualify. Team leads, engineers, developers,
  architects, product managers and other non-executive roles never qualify.
- List every qualifying person you find, even lower in the priority order; return an
  empty list only when nobody at this company holds any listed role.
Do not include markdown or extra keys."""


_AT_COMPANY_RE = re.compile(r"\s+(?:(?:at|ב-?)\s+|@\s*)\S.*$", re.IGNORECASE)


def clean_title(title: str) -> str:
    """'Chief Technology Officer at Lumana' → 'Chief Technology Officer'."""
    return _AT_COMPANY_RE.sub("", title or "").strip(" -–|,")


# Titles that are the profile's first choice: once one is found, stop searching.
TOP_ROLE_RE: dict[str, re.Pattern] = {
    "dev": re.compile(
        r"\b(?:cto|chief technology|(?:vp|vice president)\b.*?(?:r&d|\bengineering|\btechnology|\bdevelopment))",
        re.IGNORECASE,
    ),
    "ai": re.compile(r"\b(?:cto|chief technology)\b", re.IGNORECASE),
    "systems": re.compile(
        r"\b(?:(?:vp|vice president)\b.*?\boperations|revops|revenue operations)\b", re.IGNORECASE,
    ),
}


def is_top_role(title: str, profile: str) -> bool:
    return bool(TOP_ROLE_RE.get(profile, TOP_ROLE_RE["dev"]).search(title or ""))


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


_NOT_THE_CTO_RE = re.compile(r"office of the cto|cto office|field cto", re.IGNORECASE)


def too_junior(title: str) -> bool:
    """'Engineering Team Leader' / 'Software Engineer' / 'Architect, CTO Office' are
    not decision makers."""
    if _NOT_THE_CTO_RE.search(title or ""):
        return True
    return bool(_JUNIOR_RE.search(title or "")) and not _SENIOR_RE.search(title or "")


_GENERIC_NAME_WORDS = {
    "ai", "the", "inc", "ltd", "llc", "corp", "co", "com", "io", "group", "company", "labs", "lab",
    "technologies", "technology", "tech", "security", "systems", "software", "solutions", "data",
    "cloud", "israel", "health", "robotics", "formerly", "global",
}


def _name_keys(company_name: str) -> set[str]:
    """Ways a company is written in a LinkedIn headline: 'Upwind Security' → upwind
    security / upwindsecurity / upwind; 'Alice (Formerly ActiveFence)' → alice / activefence."""
    short = short_company_name(company_name).lower()
    keys = {short, short.replace(" ", "")}
    for text in (short, (company_name or "").lower()):
        keys.update(t for t in re.findall(r"[a-z0-9&]+", text) if len(t) >= 3 and t not in _GENERIC_NAME_WORDS)
    return {k for k in keys if k}


def mentions_company(text: str, company_name: str) -> bool:
    """True when the result text names the company (guards against people at
    other companies, e.g. "VP R&D at Ryft" offered for Cyera)."""
    keys = _name_keys(company_name)
    if not keys or not all(k.isascii() for k in keys):
        return True  # Hebrew / unusual names: rely on the LLM
    low = (text or "").lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", low) for k in keys)


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
    Stops as soon as a first-choice role is found (e.g. CTO / VP R&D); a lower
    role found early is kept only if the remaining searches find nobody better.

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
            continue  # keep an earlier pick, if any
        best = pick  # later picks see all results so far, so they are better informed
        if is_top_role(pick[0].title, profile if profile in PROFILES else "dev"):
            return best  # first-choice role found: no more searches needed
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
    data: dict = {}
    for attempt in range(2):  # one retry: the model occasionally returns broken JSON
        try:
            data = await complete_json(system, user, max_tokens=700)
            break
        except Exception:
            logger.warning("contact_enrichment: LLM failed for %r (attempt %d)", company_name, attempt + 1,
                           exc_info=attempt == 1)
    candidates = data.get("candidates")
    if not isinstance(candidates, list):  # older answer shape
        candidates = [data.get("primary"), data.get("alternate")]

    # The code checks (other company, junior title, invented URL) may drop someone;
    # the next valid candidate then moves up.
    allowed = {r.link: f"{r.title} {r.snippet}" for r in results}
    picked: list[Contact] = []
    for raw in candidates:
        contact = _valid(raw, allowed, company_name)
        if contact is not None and all(c.linkedin_url != contact.linkedin_url for c in picked):
            picked.append(contact)
        if len(picked) == 2:
            break
    return (picked[0] if picked else None), (picked[1] if len(picked) > 1 else None)
