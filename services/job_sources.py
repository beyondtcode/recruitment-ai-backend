"""Shared default job-board listing URLs for lead and matcher scrapers.

~3-day publish window where boards support it:
  LinkedIn: f_TPR=r259200 = last 3 days (259200s)
  AllJobs: duration=3 ≈ last 3 days
  Drushim: ssaen=3
  Glassdoor: fromAge=3
  Others use category/keyword searches.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

DEFAULT_SEARCH_URLS: dict[str, str] = {
    "LinkedIn": (
        "https://www.linkedin.com/jobs/search?"
        "keywords=Software%20Developer&location=Israel&f_TPR=r259200"
    ),
    "AllJobs": (
        "https://www.alljobs.co.il/SearchResultsGuest.aspx?"
        "page=1&position=235&type=&city=&region=&duration=3"
    ),
    "Drushim": (
        "https://www.drushim.co.il/jobs/search/Software%20Developer/?ssaen=3"
    ),
    "JobMaster": (
        "https://www.jobmaster.co.il/jobs/?q=Software+Developer"
    ),
    "Glassdoor": (
        "https://www.glassdoor.com/Job/israel-software-developer-jobs-"
        "SRCH_IL.0,6_IN119_KO7,26.htm?fromAge=3"
    ),
    "Jobnet": "https://www.jobnet.co.il/jobs?subprofid=819",
    "SQ Link": "https://www.sqlink.com/careers/",
}


# ---------------------------------------------------------------------------
# Lead agent searches (``/api/leads/scrape-all``).
# Separate from DEFAULT_SEARCH_URLS so the CV matcher is unaffected.
# Each entry: (board, label, url). ``board`` selects the parser config.
# SQLink is intentionally excluded (it is on the blocked-companies list).
# ---------------------------------------------------------------------------

_LI = "https://www.linkedin.com/jobs/search?location=Israel&f_TPR=r259200&keywords="
_DRUSHIM = "https://www.drushim.co.il/jobs/search/{q}/?ssaen=3"
_JOBMASTER = "https://www.jobmaster.co.il/jobs/?q={q}"

LEAD_SEARCHES: list[tuple[str, str, str]] = [
    # Development
    ("LinkedIn", "Software Developer", _LI + "Software%20Developer"),
    ("LinkedIn", "Full Stack", _LI + "Full%20Stack%20Developer"),
    ("LinkedIn", "Backend", _LI + "Backend%20Developer"),
    ("LinkedIn", "Frontend", _LI + "Frontend%20Developer"),
    ("AllJobs", "Software", DEFAULT_SEARCH_URLS["AllJobs"]),
    ("Drushim", "Software Developer", _DRUSHIM.format(q="Software%20Developer")),
    ("JobMaster", "Software Developer", _JOBMASTER.format(q="Software+Developer")),
    ("Glassdoor", "Software Developer", DEFAULT_SEARCH_URLS["Glassdoor"]),
    ("Jobnet", "Software", DEFAULT_SEARCH_URLS["Jobnet"]),
    # More titles for the same roles (LinkedIn matches words, so a
    # "Software Engineer" posting may not show up for "Software Developer").
    ("LinkedIn", "Software Engineer", _LI + "Software%20Engineer"),
    ("LinkedIn", "Backend Engineer", _LI + "Backend%20Engineer"),
    ("LinkedIn", "Fullstack Engineer", _LI + "Fullstack%20Engineer"),
    ("LinkedIn", "Frontend Engineer", _LI + "Frontend%20Engineer"),
    ("LinkedIn", "React", _LI + "React%20Developer"),
    ("LinkedIn", "Node.js", _LI + "Node.js%20Developer"),
    ("LinkedIn", "Python", _LI + "Python%20Developer"),
    ("LinkedIn", "Java", _LI + "Java%20Developer"),
    ("LinkedIn", ".NET", _LI + ".NET%20Developer"),
    ("LinkedIn", "Software Team Lead", _LI + "Software%20Team%20Lead"),
    ("LinkedIn", "מפתח תוכנה", _LI + "%D7%9E%D7%A4%D7%AA%D7%97%20%D7%AA%D7%95%D7%9B%D7%A0%D7%94"),
    ("LinkedIn", "מהנדס תוכנה", _LI + "%D7%9E%D7%94%D7%A0%D7%93%D7%A1%20%D7%AA%D7%95%D7%9B%D7%A0%D7%94"),
    ("Drushim", "Full Stack", _DRUSHIM.format(q="Full%20Stack")),
    ("Drushim", "Backend", _DRUSHIM.format(q="Backend")),
    ("Drushim", "Frontend", _DRUSHIM.format(q="Frontend")),
    # QA & automation
    ("LinkedIn", "QA", _LI + "QA%20Engineer"),
    ("LinkedIn", "Automation", _LI + "Automation%20Engineer"),
    ("Drushim", "QA", _DRUSHIM.format(q="QA")),
    ("JobMaster", "QA", _JOBMASTER.format(q="QA")),
    ("LinkedIn", "QA Automation", _LI + "QA%20Automation"),
    ("LinkedIn", "SDET", _LI + "SDET"),
    ("LinkedIn", "Test Automation", _LI + "Test%20Automation%20Engineer"),
    ("Drushim", "QA Automation", _DRUSHIM.format(q="QA%20Automation")),
    # Salesforce / monday.com implementation
    ("LinkedIn", "Salesforce", _LI + "Salesforce"),
    ("LinkedIn", "monday.com", _LI + "monday.com%20implementation"),
    ("Drushim", "Salesforce", _DRUSHIM.format(q="Salesforce")),
    ("LinkedIn", "Salesforce Developer", _LI + "Salesforce%20Developer"),
    ("LinkedIn", "Salesforce Admin", _LI + "Salesforce%20Administrator"),
    ("LinkedIn", "CRM Developer", _LI + "CRM%20Developer"),
    ("LinkedIn", "monday.com Expert", _LI + "monday.com"),
    ("Drushim", "monday", _DRUSHIM.format(q="monday")),
    # AI builders
    ("LinkedIn", "AI Builder", _LI + "AI%20Builder"),
    ("LinkedIn", "AI Automation", _LI + "AI%20Automation"),
    ("LinkedIn", "AI Agent", _LI + "AI%20Agent%20Developer"),
    ("LinkedIn", "AI Implementation", _LI + "AI%20Implementation"),
    ("LinkedIn", "AI Workflow", _LI + "AI%20Workflow"),
    ("Drushim", "AI Builder", _DRUSHIM.format(q="AI%20Builder")),
    ("JobMaster", "AI Builder", _JOBMASTER.format(q="AI+Builder")),
    ("LinkedIn", "AI Engineer", _LI + "AI%20Engineer"),
    ("LinkedIn", "AI Developer", _LI + "AI%20Developer"),
    ("LinkedIn", "LLM Engineer", _LI + "LLM%20Engineer"),
    ("LinkedIn", "GenAI", _LI + "Generative%20AI"),
    ("LinkedIn", "Forward Deployed Engineer", _LI + "Forward%20Deployed%20Engineer"),
    ("LinkedIn", "Solutions Engineer AI", _LI + "AI%20Solutions%20Engineer"),
    ("Drushim", "AI", _DRUSHIM.format(q="AI")),
]


# Date-window query parameters per board (the 3-day defaults above).
_DATE_PARAMS = {"alljobs.co.il": "duration", "drushim.co.il": "ssaen", "glassdoor.": "fromAge"}


def with_lookback(url: str, days: int) -> str:
    """Widen a search URL's publish-date window to ``days`` (one-time backfill).

    LinkedIn gets ``f_TPR=r<seconds>``. Boards whose date filter only accepts
    fixed values (AllJobs, Drushim, Glassdoor) get the filter removed, i.e. all
    open jobs. URLs without a date filter are returned unchanged.
    """
    host = (urlparse(url).hostname or "").lower()
    if "linkedin.com" in host:
        return re.sub(r"f_TPR=r\d+", f"f_TPR=r{days * 86400}", url)
    param = next((p for h, p in _DATE_PARAMS.items() if h in host), None)
    if param is None:
        return url
    base, _, query = url.partition("?")
    kept = [kv for kv in query.split("&") if kv and not kv.startswith(param + "=")]
    return base + ("?" + "&".join(kept) if kept else "")
