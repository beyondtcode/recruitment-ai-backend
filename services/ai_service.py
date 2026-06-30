from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from core.config import settings
from models.candidate import CandidateSchema, ProgrammingLanguageExperience

logger = logging.getLogger(__name__)

TOOL_NAME = "extract_candidate_fields"
MAX_CV_CHARS = 80_000
MAX_TOKENS = 4096

SYSTEM_PROMPT = """You are an elite Israeli Tech Recruiter. Your task is to extract CV and ingestion-source details into a strict schema. Do not just extract text; use logical deduction:

## Professional experience (strict — applies to years_of_experience, job_category, ai_summary)
1. **Strict Professional Experience Only:** Completely IGNORE personal projects, academic/bootcamp projects, or self-study when calculating years_of_experience, selecting job_category, or writing the ai_summary.
2. Only count actual professional employment, paid work, or formal industry internships.
3. If a candidate only has academic projects and no real industry employment, years_of_experience must be 0, and the ai_summary must reflect that they are a graduate with no industry experience.
4. **Mixed Backgrounds:** If they have professional experience in multiple areas (e.g., 5 years QA, 2 years Backend), calculate the TOTAL years of relevant tech experience, but explicitly break down this timeline in the ai_summary (e.g., "סך הכל 7 שנות ניסיון. מתוכן 5 שנים ב-QA ו-2 שנים בפיתוח Backend").

## city (עיר מגורים — CRITICAL: explicit residence first, mandatory education fallback)
5. **Priority 1 — explicit residence:** Populate `city` when the CV explicitly states the candidate's city of residence — e.g. address line, "מגורים: תל אביב", "כתובת: בני ברק", "גר בחיפה", "עיר: רמת גן". Do not infer city from employer HQ, job sites, military base names, accelerators, placement programs, or training frameworks alone.
6. **Priority 2 — education institution fallback (MANDATORY when Priority 1 fails):** Applies ONLY when the candidate's city of residence is NOT explicitly mentioned AND the CV lists a real physical school — seminary, yeshiva, college, university, or accredited vocational campus (degree/diploma/matriculation from that institution). You MUST infer `city` from that institution's known primary campus city in Israel. Key examples:
   - **ירושלים:** סמינר בית יעקב, מרכז בית יעקב, מכון בית יעקב, בינת, האוניברסיטה העברית, סמינר בירושלים
   - **בני ברק:** מכון לב / Machon Lev (the college — NOT Kamatech)
   - **חיפה:** הטכניון / Technion, אוניברסיטת חיפה
   - **באר שבע:** אוניברסיטת בן גוריון / BGU
   - **תל אביב / רמת גן:** אוניברסיטת תל אביב; אונו → רמת גן
   - **Other:** מכללת תל חי → קרית שמונה; מכללת ספיר → שדרות; מכללת אחווה → אור עקיבא
   If a candidate studied at any Jerusalem-area seminary or institution listed above, set `city` to "ירושלים" unless explicit residence contradicts it.
7. **Never use for `city` (even if mentioned in education/training):** Tech hubs, accelerators, placement frameworks, bootcamps, and industry programs — including **"קאמטאק" / "קאמאטק" (Kamatech)**, **"אולטרה קוד" (Ultra Code)**, coding bootcamps, MOOCs, and employer-sponsored upskilling. Kamatech is a tech hub/accelerator/placement organization, NOT a school or campus — mentioning it must NEVER set or imply `city` (e.g. never infer "בני ברק" from Kamatech). These names may affect `is_haredi` only (see rule 10); they have zero influence on `city`.
8. **Remote/distributed exception:** Do NOT guess a city from fully remote or nationwide institutions (e.g., האוניברסיטה הפתוחה, online-only programs) unless the CV names a specific physical campus/branch (e.g., "האוניברסיטה הפתוחה — רעננה").
9. If neither explicit residence nor a deducible on-campus school/seminary/university is found, set `city` to `null`.

## Field deduction
10. **Haredi Sector (is_haredi) — independent of `city`:** Output 'כן' if ANY of these conditions apply:
   - "בס"ד" appears at the top of the CV.
   - There is no mention of military service, OR there is explicit mention of exemption (e.g. "אין צבא", "פטור").
   - Education or training at: "אולטרה קוד" (Ultra Code), "קאמטאק" / "קאמאטק" (Kamatech), "מרכז בית יעקב", "מכון בית יעקב", "תעודת מה"ט", "תעודת משרד החינוך". (Kamatech/Ultra Code: use for `is_haredi` only — never for `city`.)
   - Any degree/diploma where the institution is "המכללה למנהל" within the orthodox training frameworks above.
   Otherwise, default to 'לא / לא צוין'.
11. **Company Type (company_type):** Use ONLY one of: 'חברת מוצר' (product/tech/startup employers), 'חברת מוסד' (public sector, NGOs, institutional employers), or 'לא ידוע' when unclear.
12. **Job Category (job_category) — multi-select, inclusive:** Based on professional roles and responsibilities only (not academic/personal projects). Identify and list ALL relevant job categories that genuinely match the candidate. Examples: a fullstack developer with strong backend and frontend work may include 'fullstack', 'Backend', and 'Frontend' when each is substantiated by paid roles; a mixed QA-then-Backend career should include both relevant tags. Use exact Monday board dropdown labels (e.g. 'פיתוח ותוכנה', 'דאטה ואנליזה', 'QA', 'אוטומציה', 'Backend', 'Frontend', 'fullstack'). Do not artificially limit to a single tag when multiple clearly apply.
13. **Gender:** Deduce based on Hebrew grammar used in the CV (e.g., 'מפתחת' vs 'מפתח') or name. If ambiguous, use 'לא ידוע'.
14. **AI Summary:** Write a sharp, 2-sentence professional summary in Hebrew. Base it on professional employment only: highlight strongest tech skills from paid roles, total professional experience per the rules above, and include the mixed-background breakdown when applicable.

## languages (spoken languages — English rule)
15. Include 'אנגלית' in the languages array ONLY when the CV explicitly states native-level English ("שפת אם", "שפת אם: אנגלית", "native English", "mother tongue: English").
16. If English is described as fluent, good, working level, business level, or similar — do NOT include 'אנגלית'.
17. For other languages, include only when clearly stated in the CV with board labels: 'צרפתית', 'רוסית', 'ערבית', 'ספרדית'.

## interview_summaries (never from CV)
18. **Always null from CV:** When parsing a CV or any backend ingestion source, ALWAYS set `interview_summaries` to `null`. This field is reserved for manual recruiter updates and the Monday.com AI Agent only.
19. Do NOT extract interview notes, phone-screen outcomes, recruiter call summaries, or commentary from the CV body, email body, or attachments into `interview_summaries`.
20. Do NOT place CV commentary, AI meta-comments, or professional summaries into `interview_summaries` (use `ai_summary` for the professional CV summary only).

## programming_languages (employment timeline only — max 5)
21. **STRICT MAXIMUM of 5 entries.** Each entry is `{language, years}` where `years` is total professional tenure for that technology.
22. **Employment timeline only:** Include ONLY technologies the candidate used in paid employment or formal industry internships. Walk the employment history (ניסיון תעסוקתי / Professional Experience) role-by-role. DO NOT include languages listed only in generic "Skills", "Tools", "Knowledge", or "טכנולוגיות" sections unless the same technology also appears in a job's responsibilities or deliverables.
23. Ignore academic projects, bootcamp homework, personal projects, and self-study mentions.
24. **Years calculation:** Sum professional tenure per technology across employment entries (calendar months where the tech was actively used in that role). Output `years` as a float with at most one decimal place.
25. Sort by `years` descending; keep only the top 5. Drop minor tools (Git, Jira, Postman, peripheral libraries). Use Monday-compatible technology names (core languages and major frameworks only).
26. Example: If React appears in a Skills list but never in any job description under employment history, exclude it entirely.

## extraction_confidence (self-reflection on skills / languages / tech stack)
27. **Mandatory self-assessment:** After extracting `programming_languages` and related tech-stack fields, critically evaluate how reliable that extraction is and set `extraction_confidence` to exactly one of: `high`, `medium`, or `low`.
28. **`high`:** Technologies and years of experience are explicitly, clearly, and unambiguously stated in the CV without contradictions. Employment timelines support the extracted stack with minimal interpretation.
29. **`medium`:** Technologies are listed, but context or years of experience are partial, vague, or require slight inference (e.g. skills section partially aligned with jobs, approximate date ranges, implied but not stated tenure).
30. **`low`:** The CV is poorly formatted or messy, lacks crucial tech-stack details, or forces significant assumptions or complex interpretations to produce `programming_languages` (e.g. keyword soup, contradictory dates, no clear employment history, heavy guessing from generic skills lists).
31. **`confidence_reasoning` (mandatory):** Always populate with a short paragraph in English or Hebrew that justifies the chosen `extraction_confidence`. Explain exactly what in the CV drove the rating — e.g. clear dated job bullets vs. vague skills lists, contradictions, missing employment history, or messy layout. Do not repeat the enum label alone; cite concrete evidence from the document. This field is for server debug logging only and is never written to Monday.

## linkedin (adaptive URL extraction — including hidden hyperlinks)
34. Our CV parser inlines hyperlink targets next to anchor text using bracket notation, e.g. `LinkedIn [https://www.linkedin.com/in/jane-doe]`, `Profile [https://...]`, `קישור [https://...]`, or the candidate's name followed by `[https://...]`. ALWAYS parse these bracketed URLs — the absolute `https://...` inside `[...]` is the true destination even when the visible text is only "LinkedIn" or "פרופיל".
35. Search for LinkedIn profile URLs in: bracket pairs `[https://www.linkedin.com/in/...]`, plain text URLs, and lines like `LinkedIn [https://...]` appended by the PDF/DOCX extractor.
36. Normalize any valid personal profile to `https://www.linkedin.com/in/{handle}` (include `/in/` paths; ignore `/company/` unless no personal profile exists).
37. If multiple LinkedIn URLs appear, prioritize the personal `/in/` profile link over company pages or share links.
38. If a LinkedIn username/handle appears next to the word "LinkedIn" without a bracketed URL (e.g., "LinkedIn: johndoe"), reconstruct as `https://www.linkedin.com/in/{username}`.
39. Use `קיים בקובץ (לינק מוסתר)` ONLY when the CV mentions "LinkedIn" / "פרופיל" / "קישור" but there is neither a bracketed URL, nor a plain URL, nor a recoverable handle.
40. NEVER put GitHub, Netlify, Vercel, portfolio, or other non-LinkedIn URLs in `linkedin`. If only those links exist and no LinkedIn evidence is present, return `null`.

## recruiter_notes (city derivation note + red flags)
41. Leave `recruiter_notes` as `null` by default. Do NOT use this field to explain general reasoning, show calculations, or justify how you derived years_of_experience or any other field.
42. **Mandatory — city derived from education:** ONLY when `city` is a non-empty string that you set via rules 6/8 (not rule 5, not rule 7). Then set `recruiter_notes` to exactly: `העיר נגזרה אוטומטית ממוסד הלימודים ([שם המוסד])` (replace the bracketed part with the actual school name from the CV). If a red-flag note also applies (rule 44), prepend this city sentence and append the red-flag sentence separated by a space or newline.
42b. **Forbidden — no city, no city note:** If `city` is null — including when education is mentioned but campus city is unknown, remote-only, or excluded (rule 7) — leave `recruiter_notes` null unless rule 44 (red flag) applies. Do NOT write that city was removed, skipped, could not be inferred, or excluded because of the institution.
42c. **Consistency check:** Never output the derivation sentence from rule 42 unless `city` is also populated in the same tool response.
43. **Red flags:** Additionally populate `recruiter_notes` (alone, or appended after the mandatory city sentence from rule 42) when there is a critical red flag a recruiter must act on immediately (e.g., "פער של 3 שנים ללא תעסוקה", suspected resume inflation, visa/relocation blocker). Keep each note short and actionable in Hebrew.

## Contact (name, email, phone)
44. **Priority sources:** Extract contact fields from the earliest content in the CV text — especially the `--- DOCX_HEADER ---` block (if present), the first lines of the document, and top-of-page tables — before experience or education sections.
45. **`name` (English only):** MUST be in English (Latin script). If the CV name is in Hebrew (e.g. "יעל כהן"), transliterate/translate to English (e.g. "Yael Cohen"). Preserve standard English spelling for names already in English. Use the candidate's personal full name from the contact area. Do NOT use employer names, university names, or section headings as the candidate name.
46. **`email`:** Extract a valid email address from the contact area. Prefer explicit emails and `mailto:` hyperlinks; also parse bracketed URLs from the CV parser (e.g. `Email [mailto:user@example.com]`).
47. **`phone`:** Extract Israeli mobile or landline numbers from the contact area only. Do NOT use GPA, test scores, ID numbers, or years as phone values.
48. **Phone Number Cleanliness:** For the `phone` field, extract ONLY the raw digits. Strictly remove any dashes, spaces, parentheses, or special characters (e.g., convert "055-6722091" or "055 (672) 2091" into "0556722091").

## test_score (ציון מבחן — never from CV)
49. **Always null from CV:** When parsing a CV, ALWAYS set `test_score` to `null`. This field is for recruiter-entered test scores on Monday only.
50. **Never map academic grades:** Do NOT put GPA, ממוצע, degree grades, course scores, matriculation scores, or any numeric grade from education sections into `test_score`.

Use only exact enum labels defined in the tool schema. Use null for unknown optional fields and empty lists for unknown list fields."""

JOB_FIT_EVALUATION_APPENDIX = """

## job_fit_score / job_fit_reasoning — multi-layered gap analysis (only when Job requirements are provided)
51. When the user message includes a "Job requirements (דרישות משרה)" section below the CV, perform an explicit, multi-layered gap analysis between the JD and the parsed candidate profile. Evaluate **professional** skills and paid employment experience only — academic projects, bootcamp work, personal projects, and self-study do NOT count as professional years of experience.
52. When NO "Job requirements (דרישות משרה)" section is present, ALWAYS set both `job_fit_score` and `job_fit_reasoning` to `null` — never guess job fit without requirements context.

### Evaluation algorithm & weights
53. Compute `job_fit_score` (integer 1–10) using this strict hierarchy:
   - **Hard Requirements (תנאי חובה)** = **80%** of the weight.
   - **Nice-to-Haves / Advantages (יתרונות)** = **20%** of the weight.
54. **CRITICAL GATEKEEPING RULE:** If the candidate does NOT meet even **ONE** hard requirement (חובה), their **MAXIMUM** allowed `job_fit_score` is **3/10**. No exceptions. A partial degree, 0 professional years when 3+ are required, or academic-only tech knowledge all trigger this cap.
55. Only after all hard requirements are satisfied may the score exceed 3; then apply the 80/20 blend and use the full 1–10 scale (reserve 9–10 for strong matches on nearly all hard requirements plus most advantages).

### Dynamic mapping — four JD pillars
56. Extract requirements from the JD and map the candidate against these **four pillars** (adapt labels to the specific role):
   1. **Academic Degree** — e.g. BA / B.Sc / exact sciences vs. practical engineer / other.
   2. **Professional Experience** — years in industry for the relevant stack; exclude bootcamp time, degree study, and academic projects.
   3. **Primary Tech Stack** — e.g. C++, Linux, RT Embedded; production use in paid roles only.
   4. **Tools & Methodologies** — e.g. GIT, JIRA, OOD, Design Patterns; must be evidenced in employment, not skills lists alone.
57. For each pillar, classify the gap as **Pass**, **Partial**, or **Fail** internally before scoring. Any **Fail** on a hard-requirement pillar triggers rule 54.

### Output fields
58. Set `job_fit_reasoning` to a brief paragraph in **Hebrew** (preferred) citing pillar-by-pillar strengths and gaps relative to דרישות משרה — name specific hard requirements missed when applicable.
59. When a hard requirement fails, set `recruiter_notes` to a short, actionable Hebrew disqualification note (e.g. `פסילה אוטומטית - פער ניסיון קריטי: ...`) — prepend any mandatory city note from rule 42 if applicable.
60. When job-fit evaluation reveals thin or contradictory employment history, reflect that in `extraction_confidence` and `confidence_reasoning` (e.g. role starting in the current calendar year = 0 completed professional years).

### System example — Senior Real-Time C++ position (ground truth for scoring)
CURRENT SYSTEM YEAR: 2026

JOB DESCRIPTION INPUT:
"לחברה ביטחונית מובילה דרוש/ה מפתח/ת C++ למערכות Real Time.
דרישות:
• תואר ראשון במדעי המחשב / הנדסת תוכנה – חובה
• 3+ שנות ניסיון ב-Modern C++ ב-Linux – חובה
• ניסיון ב-RT Embedded ו-GIT/JIRA – חובה
• ניסיון ב-OOD ודיזיין פטרנס – חובה"

CANDIDATE CV INPUT (Sara Mauda):
{
  "name": "Sara Mauda",
  "education": "הנדסת תוכנה מעשיים",
  "years_of_experience": 0.0,
  "experience_details": "BeyondCode (2026 - Present): AI Automation Engineer. Built Claude API and Monday integrations. Academic projects in C++, Angular, Spring Boot."
}

EXPECTED OUTPUT EVALUATION:
- Degree Check: Partial/Fail (Practical Engineer vs. Required B.Sc/BA).
- Experience Check: CRITICAL FAIL (0 years professional C++ experience vs. 3+ years required. The 2026 position just started and is in AI Automation, not core RT/C++).
- Core Stack Check: Fail (Academic knowledge only, no production-grade Linux/C++ experience).

FINAL JSON OUTPUT FOR THIS CANDIDATE:
{
  "job_fit_score": 2,
  "extraction_confidence": "low",
  "confidence_reasoning": "Candidate claims a role starting in 2026 (current year), representing 0 actual years of professional history. Most experience is academic/projects.",
  "recruiter_notes": "פסילה אוטומטית - פער ניסיון קריטי: המשרה דורשת לפחות 3 שנות ניסיון תעסוקתי ב-C++ ו-Linux, בעוד שלמועמדת יש 0 שנות ניסיון מוכח בתעשייה ורק רקע אקדמי.",
  "job_fit_reasoning": "אי התאמה לתנאי הסף (חובה). המועמדת היא בוגרת טרייה ללא ניסיון תעסוקתי בפיתוח מערכות Real Time או Linux (0 שנים בפועל מול 3 נדרשות). בנוסף, התואר הוא הנדסאי ולא תואר אקדמי ראשון כפי שנדרש כחובה במשרה."
}"""

MEETING_BRIEF_MAX_TOKENS = 2048

MEETING_BRIEF_SYSTEM_PROMPT = """You are an expert recruitment and CRM assistant for an Israeli tech recruitment agency.

Your task is to prepare a concise, sharp pre-meeting briefing in fluent Hebrew for an upcoming client meeting.

You MUST return ONLY the following Markdown structure — no greeting, no preamble, no closing remarks, no conversational filler:

### סיכום פגישה אחרונה
[Extract and summarize only the most recent meeting details, decisions, and pending action items from the provided context. The first meeting in the context is the most recent one.]

### פרופיל לקוח כללי
[Provide a holistic summary of who this client is, their main goals, and ongoing business context based on ALL past meetings provided.]

### דגשים ודברים חשובים לפגישה
[Highlight critical patterns, recurring issues, specific requests, or warnings discovered across the entire meeting history that the recruiter/team must know before talking to them today.]

Rules:
- Write every section in clear, professional Hebrew.
- Be direct and actionable — recruiters should scan the brief in under a minute.
- Use the exact three heading lines above (### סיכום פגישה אחרונה, ### פרופיל לקוח כללי, ### דגשים ודברים חשובים לפגישה).
- Do not add any other headings, bullet labels, or text outside this structure.
- If no historical meeting notes are provided, keep the same three headings: under the first section state that no previous meetings were found; under the other two sections suggest what to cover in a first or follow-up meeting."""

CLIENT_MEETING_PROFILE_MAX_TOKENS = 1024

CLIENT_MEETING_PROFILE_SYSTEM_PROMPT = """You are an expert CRM data analyst. Analyze the provided company meeting logs and return a STRICTLY valid raw JSON object.
Do not include any markdown styling, no backticks (```json), and no text outside the JSON object.
The JSON must contain exactly two keys: 'profile' and 'latest_date'.

For the 'profile' key, generate a concise, exactly 5-line executive summary profile about this client in Hebrew.

CRITICAL STYLE GUIDELINES FOR THE PROFILE:
1. Focus strictly on the COMPANY itself, its core business operations, capabilities, and industrial metrics. Always start the profile directly with 'החברה היא...'.
2. Do NOT write it as a chronological narrative of individuals reaching out (AVOID starting with names like 'X פנה בשם...'). Integrate key people (like the CEO or HR) naturally as part of the corporate structure.
3. Explicitly extract and highlight the core business needs, technical requirements, and pain points mentioned.
4. Clearly mention the specific commercial arrangement, proposals, or next steps concluded between us and the client (e.g., specific terms discussed, or the decision to proceed with standard vendor-customer relations instead of partnerships).

For the 'latest_date' key: Identify the single most recent meeting date from the logs and convert it strictly into 'YYYY-MM-DD' format. The data is from the year 2022, so if you see shorthand dates like '4.8' (August 4th) or '5.9' (September 5th), assume the year is 2022 (e.g., '2022-09-05')."""

_LATEST_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY") or settings.anthropic_api_key
        _client = AsyncAnthropic(api_key=api_key)
    return _client


def _tool_input_schema() -> dict[str, Any]:
    schema = CandidateSchema.model_json_schema()
    schema.pop("title", None)
    return schema


def _truncate_cv(cv_text: str) -> str:
    if len(cv_text) <= MAX_CV_CHARS:
        return cv_text
    logger.warning("CV text truncated from %d to %d characters", len(cv_text), MAX_CV_CHARS)
    return cv_text[:MAX_CV_CHARS]


def _parse_tool_input(response: Any) -> dict[str, Any]:
    for block in response.content:
        if block.type == "tool_use" and block.name == TOOL_NAME:
            return block.input
    raise ValueError("Claude response did not include the expected tool_use block")


_CITY_DERIVATION_SENTENCE_RE = re.compile(
    r"העיר נגזרה אוטומטית ממוסד הלימודים\s*\([^)]*\)\s*\.?",
    re.UNICODE,
)
_CITY_EDUCATION_NOTE_MARKERS = ("נגזרה", "מוסד הלימודים")


def _strip_city_education_notes(notes: str) -> str | None:
    """Remove city-from-education notes when city is empty; keep unrelated red flags."""
    text = _CITY_DERIVATION_SENTENCE_RE.sub("", notes).strip()
    if not text:
        return None

    kept: list[str] = []
    for part in re.split(r"\n+", text):
        part = part.strip()
        if not part:
            continue
        if all(marker in part for marker in _CITY_EDUCATION_NOTE_MARKERS):
            continue
        if "העיר" in part and "מוסד הלימודים" in part:
            continue
        kept.append(part)

    return "\n".join(kept).strip() or None


def _sanitize_recruiter_notes(candidate: CandidateSchema) -> CandidateSchema:
    """Drop city-education recruiter_notes when city was not populated."""
    if (candidate.city or "").strip():
        return candidate

    notes = (candidate.recruiter_notes or "").strip()
    if not notes:
        return candidate

    if not any(marker in notes for marker in _CITY_EDUCATION_NOTE_MARKERS):
        return candidate

    cleaned = _strip_city_education_notes(notes)
    if cleaned == notes and not all(marker in notes for marker in _CITY_EDUCATION_NOTE_MARKERS):
        return candidate

    if cleaned:
        logger.info("Stripped city-education recruiter_notes because city is empty")
    else:
        logger.info("Cleared recruiter_notes (city-education only) because city is empty")

    return candidate.model_copy(update={"recruiter_notes": cleaned})


def _sanitize_test_score(candidate: CandidateSchema) -> CandidateSchema:
    """CV pipeline must never persist resume-derived values into test_score."""
    if candidate.test_score is None:
        return candidate
    logger.info("Cleared test_score from CV parse (not extracted from resumes)")
    return candidate.model_copy(update={"test_score": None})


def _sanitize_interview_summaries(candidate: CandidateSchema) -> CandidateSchema:
    """CV pipeline must never populate interview summaries on Monday."""
    if not (candidate.interview_summaries or "").strip():
        return candidate
    logger.info("Cleared interview_summaries from CV parse (recruiter/Agent column only)")
    return candidate.model_copy(update={"interview_summaries": None})


_MAX_PROGRAMMING_LANGUAGES = 5


def _sanitize_programming_languages(candidate: CandidateSchema) -> CandidateSchema:
    """Drop invalid entries, sort by years descending, cap at 5."""
    filtered: list[ProgrammingLanguageExperience] = []
    for entry in candidate.programming_languages:
        language = (entry.language or "").strip()
        if not language or entry.years <= 0:
            continue
        filtered.append(
            ProgrammingLanguageExperience(language=language, years=entry.years)
        )

    filtered.sort(key=lambda e: e.years, reverse=True)
    if len(filtered) > _MAX_PROGRAMMING_LANGUAGES:
        logger.info(
            "Truncated programming_languages from %d to %d entries",
            len(filtered),
            _MAX_PROGRAMMING_LANGUAGES,
        )
        filtered = filtered[:_MAX_PROGRAMMING_LANGUAGES]

    if filtered == candidate.programming_languages:
        return candidate
    return candidate.model_copy(update={"programming_languages": filtered})


def _sanitize_job_fit(
    candidate: CandidateSchema,
    *,
    job_requirements: str | None,
) -> CandidateSchema:
    """Clear job-fit fields when no דרישות משרה context was supplied."""
    if (job_requirements or "").strip():
        return candidate
    if candidate.job_fit_score is None and not (candidate.job_fit_reasoning or "").strip():
        return candidate
    logger.info("Cleared job_fit fields (no job requirements context)")
    return candidate.model_copy(update={"job_fit_score": None, "job_fit_reasoning": None})


def _sanitize_candidate(
    candidate: CandidateSchema,
    *,
    job_requirements: str | None = None,
) -> CandidateSchema:
    """Apply post-validation fixes before Monday upsert."""
    candidate = _sanitize_recruiter_notes(candidate)
    candidate = _sanitize_test_score(candidate)
    candidate = _sanitize_interview_summaries(candidate)
    candidate = _sanitize_job_fit(candidate, job_requirements=job_requirements)
    return _sanitize_programming_languages(candidate)


def _log_extraction_confidence_debug(candidate: CandidateSchema) -> None:
    print("\n" + "=" * 40)
    print(f"DEBUG - CANDIDATE: {candidate.name}")
    print(f"CONFIDENCE LEVEL: {candidate.extraction_confidence.upper()}")
    print(f"REASONING: {candidate.confidence_reasoning}")
    print("=" * 40 + "\n")


def _format_retry_message(errors: list[dict[str, Any]]) -> str:
    lines = [
        "Validation failed. Fix invalid fields and call the tool again.",
        "Use only exact allowed enum labels from the schema.",
    ]
    for err in errors:
        loc = ".".join(str(part) for part in err.get("loc", ()))
        lines.append(f"- {loc}: {err.get('msg', 'invalid value')}")
    return "\n".join(lines)


async def _call_claude(
    cv_text: str,
    *,
    retry_message: str | None = None,
    job_requirements: str | None = None,
) -> dict[str, Any]:
    content = _truncate_cv(cv_text)
    requirements = (job_requirements or "").strip()
    if requirements:
        content = f"{content}\n\n---\nJob requirements (דרישות משרה):\n{requirements}"
    if retry_message:
        content = f"{content}\n\n---\n{retry_message}"

    system_prompt = SYSTEM_PROMPT + JOB_FIT_EVALUATION_APPENDIX if requirements else SYSTEM_PROMPT
    tool_description = (
        "Extract structured candidate fields from CV text. "
        "Always set extraction_confidence and confidence_reasoning after assessing "
        "programming_languages and tech-stack reliability (high / medium / low)."
    )
    if requirements:
        tool_description += (
            " When job requirements are provided, perform four-pillar gap analysis "
            "(degree, experience, tech stack, tools/methodologies) and set job_fit_score "
            "(1-10, max 3 if any hard requirement fails) and job_fit_reasoning in Hebrew."
        )

    response = await _get_client().messages.create(
        model=settings.anthropic_model,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
        tools=[
            {
                "name": TOOL_NAME,
                "description": tool_description,
                "input_schema": _tool_input_schema(),
            }
        ],
        tool_choice={"type": "tool", "name": TOOL_NAME},
    )
    return _parse_tool_input(response)


def _extract_text_response(response: Any) -> str:
    parts: list[str] = []
    for block in response.content:
        if block.type == "text":
            parts.append(block.text)
    text = "\n".join(parts).strip()
    if not text:
        raise ValueError("Claude response did not include text content")
    return text


async def generate_meeting_brief(
    past_meetings_context: str,
    current_meeting_title: str,
    *,
    participant_emails: list[str] | None = None,
) -> str:
    """Generate a Hebrew preparation brief for an upcoming meeting."""
    title = current_meeting_title.strip()
    if not title:
        raise ValueError("Meeting title is empty.")

    context = past_meetings_context.strip()
    if context:
        user_content = (
            f"Upcoming meeting title: {title}\n\n"
            f"Historical meeting notes (sorted newest first — the first entry is the most recent meeting):\n"
            f"{context}\n\n"
            "Return ONLY the required Hebrew Markdown brief with exactly these three sections:\n"
            "### סיכום פגישה אחרונה\n"
            "### פרופיל לקוח כללי\n"
            "### דגשים ודברים חשובים לפגישה"
        )
    else:
        emails_text = ", ".join(participant_emails or []) or "לא צוינו"
        user_content = (
            f"Upcoming meeting title: {title}\n\n"
            f"No previous meeting notes were found for these participants: {emails_text}.\n\n"
            "Return ONLY the required Hebrew Markdown brief with exactly these three sections:\n"
            "### סיכום פגישה אחרונה\n"
            "### פרופיל לקוח כללי\n"
            "### דגשים ודברים חשובים לפגישה\n"
            "Under the first section, note that no previous meetings were found. "
            "Use the other sections to suggest what to cover in a first or follow-up meeting."
        )

    response = await _get_client().messages.create(
        model=settings.anthropic_model,
        max_tokens=MEETING_BRIEF_MAX_TOKENS,
        system=MEETING_BRIEF_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return _extract_text_response(response)


def _strip_json_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


_JSON_STRING_VALUE_END_RE = re.compile(r'((?:[^"\\]|\\.)*")\s+("[\w_]+"\s*:)')
_JSON_OBJECT_KEY_RE = re.compile(r'"([\w_]+)"\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL)
_ISO_DATE_IN_TEXT_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _fix_common_json_typos(text: str) -> str:
    """Apply regex fixes for common LLM JSON formatting mistakes."""
    fixed = text.strip()
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    fixed = _JSON_STRING_VALUE_END_RE.sub(r"\1, \2", fixed)
    fixed = re.sub(r"([}\]0-9eE.+-])\s+(?=\"[\w_]+\"\s*:)", r"\1, ", fixed)
    fixed = re.sub(r"(true|false|null)\s+(?=\"[\w_]+\"\s*:)", r"\1, ", fixed, flags=re.IGNORECASE)
    return fixed


def _unescape_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return (
            value.replace("\\n", "\n")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )


def _extract_client_profile_fields(text: str) -> dict[str, str]:
    """Best-effort regex extraction when JSON parsing fails."""
    fields: dict[str, str] = {}
    for match in _JSON_OBJECT_KEY_RE.finditer(text):
        key, raw_value = match.group(1), match.group(2)
        if key in ("profile", "latest_date"):
            fields[key] = _unescape_json_string(raw_value).strip()
    if not fields.get("latest_date") or not _LATEST_DATE_RE.fullmatch(fields["latest_date"]):
        date_match = _ISO_DATE_IN_TEXT_RE.search(text)
        if date_match:
            fields["latest_date"] = date_match.group(0)
    return fields


def _load_client_meeting_profile_data(raw: str) -> dict[str, str]:
    """Parse client profile JSON with typo repair and regex fallback."""
    for attempt_index, candidate in enumerate((raw, _fix_common_json_typos(raw))):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return {
                "profile": str(data.get("profile") or "").strip(),
                "latest_date": str(data.get("latest_date") or "").strip(),
            }
        logger.warning(
            "Client profile response parsed as non-object JSON (attempt %d)",
            attempt_index + 1,
        )

    extracted = _extract_client_profile_fields(raw)
    if extracted:
        logger.warning(
            "Client profile JSON parse failed; recovered fields via regex extraction. "
            "Raw response: %s",
            raw,
        )
        return {
            "profile": extracted.get("profile", ""),
            "latest_date": extracted.get("latest_date", ""),
        }

    logger.error(
        "Client profile JSON parse failed; returning empty defaults. Raw response: %s",
        raw,
    )
    return {"profile": "", "latest_date": ""}


def _parse_client_meeting_profile_response(text: str) -> tuple[str, str]:
    """Parse Claude JSON response into (profile, latest_date).

    Always returns a tuple. Malformed JSON is repaired or partially extracted;
    completely unparseable responses yield empty defaults instead of raising.
    """
    raw = _strip_json_fences(text)
    data = _load_client_meeting_profile_data(raw)

    profile = data["profile"]
    latest_date = data["latest_date"]

    if not profile:
        logger.warning("Client profile response missing non-empty 'profile'")
    if not _LATEST_DATE_RE.fullmatch(latest_date):
        date_match = _ISO_DATE_IN_TEXT_RE.search(raw)
        if date_match:
            latest_date = date_match.group(0)
            logger.warning(
                "Client profile response had invalid 'latest_date' %r; "
                "recovered ISO date from raw text: %s",
                data["latest_date"],
                latest_date,
            )
        elif data["latest_date"]:
            logger.warning(
                "Client profile response 'latest_date' must be YYYY-MM-DD, got: %r",
                data["latest_date"],
            )
            latest_date = ""

    return profile, latest_date


async def extract_client_meeting_profile(meeting_logs: str) -> tuple[str, str]:
    """Extract Hebrew client profile and latest meeting date from meeting logs."""
    logs = meeting_logs.strip()
    if not logs:
        raise ValueError("Meeting logs are empty.")

    user_content = (
        "Company meeting logs (newest meeting first):\n\n"
        f"{logs}"
    )

    response = await _get_client().messages.create(
        model=settings.anthropic_model,
        max_tokens=CLIENT_MEETING_PROFILE_MAX_TOKENS,
        system=CLIENT_MEETING_PROFILE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return _parse_client_meeting_profile_response(_extract_text_response(response))


async def analyze_cv_with_claude(
    cv_text: str,
    *,
    job_requirements: str | None = None,
) -> CandidateSchema:
    """Extract and validate structured candidate fields from CV text."""
    if not cv_text or not cv_text.strip():
        raise ValueError("CV text is empty.")

    raw = await _call_claude(cv_text, job_requirements=job_requirements)
    try:
        result = _sanitize_candidate(
            CandidateSchema.model_validate(raw),
            job_requirements=job_requirements,
        )
        _log_extraction_confidence_debug(result)
        return result
    except ValidationError as first_error:
        logger.warning(
            "CV analysis validation failed, retrying: %s",
            json.dumps(first_error.errors(), ensure_ascii=False),
        )
        raw = await _call_claude(
            cv_text,
            retry_message=_format_retry_message(list(first_error.errors())),
            job_requirements=job_requirements,
        )
        try:
            result = _sanitize_candidate(
                CandidateSchema.model_validate(raw),
                job_requirements=job_requirements,
            )
            _log_extraction_confidence_debug(result)
            return result
        except ValidationError as second_error:
            fields = [".".join(str(p) for p in err.get("loc", ())) for err in second_error.errors()]
            raise ValueError(f"CV parse failed after retry. Invalid fields: {', '.join(fields)}") from second_error
