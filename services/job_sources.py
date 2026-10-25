"""Shared default job-board listing URLs for lead and matcher scrapers.

~3-day publish window where boards support it:
  LinkedIn: f_TPR=r259200 = last 3 days (259200s)
  AllJobs: duration=3 ≈ last 3 days
  Drushim: ssaen=3
  Glassdoor: fromAge=3
  Others use category/keyword searches.
"""

from __future__ import annotations

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
    # QA & automation
    ("LinkedIn", "QA", _LI + "QA%20Engineer"),
    ("LinkedIn", "Automation", _LI + "Automation%20Engineer"),
    ("Drushim", "QA", _DRUSHIM.format(q="QA")),
    ("JobMaster", "QA", _JOBMASTER.format(q="QA")),
    # Salesforce / monday.com implementation
    ("LinkedIn", "Salesforce", _LI + "Salesforce"),
    ("LinkedIn", "monday.com", _LI + "monday.com%20implementation"),
    ("Drushim", "Salesforce", _DRUSHIM.format(q="Salesforce")),
    # AI builders
    ("LinkedIn", "AI Builder", _LI + "AI%20Builder"),
    ("LinkedIn", "AI Automation", _LI + "AI%20Automation"),
    ("LinkedIn", "AI Agent", _LI + "AI%20Agent%20Developer"),
    ("LinkedIn", "AI Implementation", _LI + "AI%20Implementation"),
    ("LinkedIn", "AI Workflow", _LI + "AI%20Workflow"),
    ("Drushim", "AI Builder", _DRUSHIM.format(q="AI%20Builder")),
    ("JobMaster", "AI Builder", _JOBMASTER.format(q="AI+Builder")),
]
