"""Lead-agent rules: categories, outreach phrasing, blocklist, hardware filter,
title prefilter, cross-board fingerprints and DD/MM/YYYY dates.

Kept separate from ``services.scraper`` so the CV matcher keeps its own
title filter and search list.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# ---------------------------------------------------------------------------
# Categories (spec: 12 role types) and outreach phrasing
# ---------------------------------------------------------------------------

CATEGORIES: tuple[str, ...] = (
    "Backend",
    "Frontend",
    "Full Stack",
    "QA",
    "פיתוח אוטומציה",
    "יישום Salesforce",
    "יישום monday.com",
    "AI Builder",
    "AI Workflow Builder",
    "AI Automation Specialist",
    "AI Implementation Specialist",
    "AI Agent Developer",
)

AI_CATEGORIES: frozenset[str] = frozenset(
    {
        "AI Builder",
        "AI Workflow Builder",
        "AI Automation Specialist",
        "AI Implementation Specialist",
        "AI Agent Developer",
    }
)

_PHRASING: dict[str, str] = {
    "Backend": "מפתחות",
    "Frontend": "מפתחות",
    "Full Stack": "מפתחות",
    "QA": "בודקות תוכנה ומפתחות אוטומציה",
    "פיתוח אוטומציה": "בודקות תוכנה ומפתחות אוטומציה",
    "יישום Salesforce": "מיישמות Salesforce",
    "יישום monday.com": "מיישמות monday.com",
    **{c: "AI Builders" for c in AI_CATEGORIES},
}

_CATEGORY_LOOKUP: dict[str, str] = {c.casefold(): c for c in CATEGORIES}
_CATEGORY_ALIASES: dict[str, str] = {
    "fullstack": "Full Stack",
    "full-stack": "Full Stack",
    "automation": "פיתוח אוטומציה",
    "qa automation": "פיתוח אוטומציה",
    "salesforce": "יישום Salesforce",
    "monday": "יישום monday.com",
    "monday.com": "יישום monday.com",
}


def normalize_category(raw: object) -> str | None:
    """Map an LLM answer to one of the 12 categories, or None (= not collected)."""
    if raw is None:
        return None
    text = " ".join(str(raw).split()).strip()
    if not text or text.casefold() in ("none", "null", "other", "n/a", "-"):
        return None
    key = text.casefold()
    return _CATEGORY_LOOKUP.get(key) or _CATEGORY_ALIASES.get(key)


def outreach_phrasing(category: str) -> str:
    return _PHRASING.get(category, "")


# ---------------------------------------------------------------------------
# Blocked companies (spec table; phase 2 will read the sheet tab instead)
# ---------------------------------------------------------------------------

BLOCKED_COMPANIES: dict[str, tuple[str, ...]] = {
    "מטריקס": ("Matrix", "Matrix IT", "מטריקס"),
    "NVIDIA": ("NVIDIA", "NVIDIA AI", "Nvidia", "אנבידיה", "אינבידיה"),
    "ONE": ("ONE Technologies", "One Technologies Israel", "ONE Tech", "One1", "וואן טכנולוגיות"),
    "SQLink": ("SQLink", "SQLink Group", "אס.קיו.לינק", "SQ Link"),
    "התעשייה האווירית": (
        "IAI",
        "IAI - Israel Aerospace Industries",
        "Israel Aerospace Industries",
        "התעשייה האווירית",
        "תעשייה אווירית",
    ),
    "אלעד מערכות": ("Elad Software", "Elad Software Systems", "Elad Systems", "אלעד מערכות"),
    "קבוצת יעל": ("Yael Group", "Yael Software", "קבוצת יעל"),
    "Commit": ("Commit", "Commit Software", "קומיט"),
    "בינת": ("Bynet", "Bynet Data Communications", "בינת", "בינת סמך"),
    "Intel": ("Intel", "Intel Corporation", "אינטל"),
    "Comblack": ("Comblack", "ComBlack", "קומבלק"),
}

# Added to the Blocked Companies tab once (Beyond Code, Oct 2026): software
# houses / staffing companies that must not be processed at all.
BLOCKED_ADDITIONS: dict[str, tuple[str, ...]] = {
    "מלם תים": ("Malam Team", "Malam-Team", "MalamTeam", "מלם תים", "מל\"ם תים"),
    "Ness": ("Ness", "Ness Technologies", "Ness Digital Engineering", "Ness Israel", "נס טכנולוגיות"),
    "דנאל": ("Danel", "Danel Adir", "דנאל", "דנאל אדיר"),
    "לין ביכלר": ("לין ביכלר", "Lin Bichler", "Lin Bikhler"),
    "Quicklizard": ("Quicklizard", "Quick Lizard"),
    # Extra spellings for an existing row.
    "ONE": ("ONE Technologies", "One Technologies Israel", "ONE Tech", "One1", "וואן טכנולוגיות"),
}


def _variant_pattern(variant: str) -> str:
    # Whole-word match so "Intel" does not hit "Intellinx".
    escaped = re.escape(variant.strip()).replace(r"\ ", r"[\s\-]+")
    return rf"(?<![\w]){escaped}(?![\w])"


class Blocklist:
    """Company-name matcher built from {canonical name: name variants}."""

    def __init__(self, entries: dict[str, tuple[str, ...] | list[str]]) -> None:
        self._entries: list[tuple[str, re.Pattern[str]]] = []
        for canonical, variants in entries.items():
            # Variants are authoritative (e.g. "ONE" alone would block "One Zero");
            # the canonical name is used only when no variants are given.
            names = [v for v in variants if v and v.strip()] or [canonical]
            if not names:
                continue
            pattern = re.compile("|".join(_variant_pattern(v) for v in names), re.IGNORECASE)
            self._entries.append((canonical, pattern))

    def match(self, company_name: str) -> str | None:
        name = " ".join((company_name or "").split())
        if not name:
            return None
        for canonical, pattern in self._entries:
            if pattern.search(name):
                return canonical
        return None

    def __len__(self) -> int:
        return len(self._entries)


DEFAULT_BLOCKLIST = Blocklist(BLOCKED_COMPANIES)


def blocked_company(company_name: str, blocklist: Blocklist | None = None) -> str | None:
    """Return the blocklist entry that matches ``company_name``, if any."""
    return (blocklist or DEFAULT_BLOCKLIST).match(company_name)


# ---------------------------------------------------------------------------
# Hardware roles (spec: title or description)
# ---------------------------------------------------------------------------

HARDWARE_KEYWORDS: tuple[str, ...] = (
    "hardware",
    "chip design",
    "firmware",
    "קושחה",
    "embedded",
    "משובץ",
    "driver",
    "kernel",
    "BSP",
)

_HARDWARE_RE = re.compile(
    "|".join(rf"(?<!\w){re.escape(k)}(?:s)?(?!\w)" for k in HARDWARE_KEYWORDS),
    re.IGNORECASE,
)


def is_hardware_role(title: str, description: str = "") -> bool:
    return bool(_HARDWARE_RE.search(f"{title or ''}\n{description or ''}"))


# ---------------------------------------------------------------------------
# Cheap title prefilter (before the LLM). The LLM makes the final call.
# ---------------------------------------------------------------------------

_LEAD_KEEP_RE = re.compile(
    r"(?<!\w)(?:"
    r"software|developer|development|engineer|engineering|programmer|"
    r"full[\s\-]?stack|front[\s\-]?end|back[\s\-]?end|server[\s\-]?side|"
    r"python|java|node(?:\.?js)?|\.net|c#|golang|go developer|react|angular|vue|"
    r"qa|sdet|tester|testing|automation|"
    r"salesforce|monday(?:\.com)?|"
    r"ai|genai|llm|agentic|agents?|builder|implementation|"
    r"מפתח|מפתחת|מפתח/ת|פיתוח|תוכנה|בודק|בודקת|בודק/ת|בדיקות|אוטומציה|אוטומציות|"
    r"מיישם|מיישמת|מיישם/ת|מאנדיי|בינה מלאכותית|צד שרת|צד לקוח|פול סטאק"
    r")(?!\w)",
    re.IGNORECASE,
)

_LEAD_EXCLUDE_RE = re.compile(
    r"(?<!\w)(?:"
    r"devops|sre|site reliability|"
    r"data scientist|data science|"
    r"product manager|product owner|מנהל/ת מוצר|מנהל מוצר|מנהלת מוצר|"
    r"cyber|סייבר|security researcher|penetration|pentester|soc analyst|"
    r"support|תמיכה|helpdesk|help desk|"
    r"fashion|textile|apparel|אופנה|טקסטיל|הלבשה"
    r")(?!\w)",
    re.IGNORECASE,
)


def passes_title_prefilter(title: str) -> bool:
    """Keep titles that may belong to one of the 12 categories."""
    cleaned = (title or "").strip()
    if not cleaned:
        return False
    if _LEAD_EXCLUDE_RE.search(cleaned):
        return False
    return bool(_LEAD_KEEP_RE.search(cleaned))


# ---------------------------------------------------------------------------
# Cross-board duplicate fingerprint (same job on LinkedIn + Drushim, etc.)
# ---------------------------------------------------------------------------

_COMPANY_SUFFIX_RE = re.compile(
    r"(?<!\w)(?:ltd|ltd\.|inc|inc\.|llc|corp|corporation|co|group|israel|"
    r"technologies|technology|בע\"מ|בעמ|בע״מ|ישראל)(?!\w)",
    re.IGNORECASE,
)
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def normalize_company(name: str) -> str:
    text = (name or "").casefold()
    text = _COMPANY_SUFFIX_RE.sub(" ", text)
    return " ".join(_NON_WORD_RE.sub(" ", text).split())


def normalize_title(title: str) -> str:
    text = (title or "").casefold()
    text = re.sub(r"/ת|/ית|\(ית\)|\(ת\)", "", text)
    return " ".join(_NON_WORD_RE.sub(" ", text).split())


def job_fingerprint(company_name: str, title: str) -> str:
    company = normalize_company(company_name)
    if not company or company == "unknown":
        return ""
    return f"{company}|{normalize_title(title)}"


# ---------------------------------------------------------------------------
# Dates → DD/MM/YYYY
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DMY_RE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})")
_REL_HE_RE = re.compile(r"לפני\s+(\d+)\s+(דקה|דקות|שעה|שעות|יום|ימים|שבוע|שבועות|חודש|חודשים)")
_REL_EN_RE = re.compile(
    r"(\d+)\s*\+?\s*(minute|min|hour|hr|h|day|d|week|w|month|mo)s?\b(?:\s+ago)?",
    re.IGNORECASE,
)
_HE_UNIT_DAYS = {
    "דקה": 0, "דקות": 0, "שעה": 0, "שעות": 0,
    "יום": 1, "ימים": 1, "שבוע": 7, "שבועות": 7, "חודש": 30, "חודשים": 30,
}
_EN_UNIT_DAYS = {
    "minute": 0, "min": 0, "hour": 0, "hr": 0, "h": 0,
    "day": 1, "d": 1, "week": 7, "w": 7, "month": 30, "mo": 30,
}


def today_israel() -> date:
    return datetime.now(ISRAEL_TZ).date()


def format_ddmmyyyy(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def to_ddmmyyyy(raw: str, *, today: date | None = None) -> str:
    """Convert board publish strings to DD/MM/YYYY. Unknown formats → ""."""
    text = " ".join((raw or "").split()).strip()
    if not text:
        return ""
    ref = today or today_israel()

    try:
        m = _ISO_RE.search(text)
        if m:
            return format_ddmmyyyy(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        m = _DMY_RE.search(text)
        if m:
            year = int(m.group(3))
            if year < 100:
                year += 2000
            return format_ddmmyyyy(date(year, int(m.group(2)), int(m.group(1))))
    except ValueError:
        return ""

    lowered = text.casefold()
    if "היום" in text or "today" in lowered or "just now" in lowered or "עכשיו" in text:
        return format_ddmmyyyy(ref)
    if "אתמול" in text or "yesterday" in lowered:
        return format_ddmmyyyy(ref - timedelta(days=1))

    m = _REL_HE_RE.search(text)
    if m:
        return format_ddmmyyyy(ref - timedelta(days=int(m.group(1)) * _HE_UNIT_DAYS[m.group(2)]))
    m = _REL_EN_RE.search(text)
    if m:
        unit = m.group(2).casefold()
        return format_ddmmyyyy(ref - timedelta(days=int(m.group(1)) * _EN_UNIT_DAYS[unit]))
    return ""


# ---------------------------------------------------------------------------
# Source display name for the "מקור" column
# ---------------------------------------------------------------------------

_SOURCE_DISPLAY = {
    "LinkedIn": "לינקדאין",
    "Drushim": "דרושים",
    "AllJobs": "AllJobs",
    "JobMaster": "JobMaster",
    "Glassdoor": "Glassdoor",
    "Jobnet": "Jobnet",
}


def source_display(source: str) -> str:
    return _SOURCE_DISPLAY.get((source or "").strip(), (source or "").strip() or "לוח משרות")
