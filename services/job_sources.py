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
