from pydantic import BaseModel, Field
from typing import List, Optional, Literal


class ProgrammingLanguageExperience(BaseModel):
    language: str = Field(
        ...,
        description="Core programming language or major framework from paid employment only",
    )
    years: float = Field(
        ...,
        ge=0,
        description="Total professional years using this technology across employment history",
    )


class CandidateSchema(BaseModel):
    name: str = Field(..., description="Full Name", json_schema_extra={"monday_id": "name"})
    salary_expectations: Optional[str] = Field(
        None,
        description="ציפיות שכר — free text (e.g. '20,000 ₪', '15K–18K', 'לפי שוק')",
        json_schema_extra={"monday_id": "text_mm46zd0t"},
    )
    company_type: Optional[Literal["חברת מוצר", "חברת מוסד", "לא ידוע"]] = Field(
        None,
        description=(
            "סוג חברה — deduce from past employers: product/tech companies → 'חברת מוצר', "
            "institutional/public-sector employers → 'חברת מוסד', unclear → 'לא ידוע'."
        ),
        json_schema_extra={"monday_id": "color_mm3n87tr"},
    )
    job_category: List[str] = Field(
        default_factory=list,
        description=(
            "סוג משרה (קבוצה) — multi-select. List ALL relevant job categories that match "
            "the candidate's professional experience (e.g. fullstack role may include "
            "'fullstack', 'Backend', 'Frontend' when each is substantiated). "
            "Use only labels that exist on the Monday board dropdown."
        ),
        json_schema_extra={"monday_id": "dropdown_mm3fyv0t"},
    )
    test_score: Optional[int] = Field(
        None,
        description=(
            "ציון מבחן — recruiter-entered only. Always null when parsing a CV; never extract GPA, "
            "ממוצע, degree grades, or course scores from the resume."
        ),
        json_schema_extra={"monday_id": "numeric_mm3fzf1d"},
    )
    ai_summary: Optional[str] = Field(
        None, description="תקציר AI של המועמד", json_schema_extra={"monday_id": "text_mm3gs8ec"}
    )
    years_of_experience: Optional[float] = Field(
        None, description="שנות ניסיון", json_schema_extra={"monday_id": "numeric_mm3fc9k3"}
    )
    programming_languages: List[ProgrammingLanguageExperience] = Field(
        default_factory=list,
        description=(
            "Top programming languages/frameworks from paid employment only (max 5), "
            "each with total professional years. Sorted by years descending. "
            "Exclude skills listed only in generic Skills/Tools sections without job proof."
        ),
        json_schema_extra={"monday_id": "dropdown_mm3j8kby"},
    )
    extraction_confidence: Literal["high", "medium", "low"] = Field(
        ...,
        description=(
            "Self-assessed confidence in programming_languages and tech-stack extraction. "
            "'high' — technologies and years explicitly stated without contradictions; "
            "'medium' — technologies listed but context/years partial or require slight inference; "
            "'low' — poorly formatted CV, missing tech details, or significant assumptions required."
        ),
        json_schema_extra={"monday_id": "color_mm4nx466"},
    )
    confidence_reasoning: str = Field(
        ...,
        description=(
            "Detailed reasoning for the selected extraction_confidence level based on CV clarity. "
            "Short paragraph (English or Hebrew) explaining what made the tech-stack extraction "
            "high, medium, or low — cite specific CV evidence (formatting, explicit dates, "
            "contradictions, missing employment detail, etc.). Server logging only; not synced to Monday."
        ),
    )
    education: Optional[
        Literal["תואר ראשון", "תואר שני ומעלה", "תעודת הנדסאי", "קורס מקצועי", "בגרות מלאה"]
    ] = Field(None, json_schema_extra={"monday_id": "dropdown_mm3fxr2k"})
    languages: List[Literal["אנגלית", "צרפתית", "רוסית", "ערבית", "ספרדית"]] = Field(
        default_factory=list, json_schema_extra={"monday_id": "dropdown_mm3g8c14"}
    )
    city: Optional[str] = Field(
        None,
        description=(
            "עיר מגורים. Priority 1 — explicit residence only: address line, 'מגורים: תל אביב', "
            "'כתובת: בני ברק', 'גר ב...', 'עיר: ...'. "
            "Priority 2 (mandatory fallback when Priority 1 is absent): MUST infer from the educational "
            "institution's physical campus — e.g. סמינר בית יעקב, מרכז בית יעקב, מכון בית יעקב, בינת, "
            "האוניברסיטה העברית → ירושלים; הטכניון→חיפה; בן גוריון→באר שבע; תל אביב→תל אביב. "
            "Do not guess from remote/nationwide institutions (e.g. האוניברסיטה הפתוחה) unless a specific "
            "campus/branch is named. null only when neither explicit residence nor a deducible campus applies."
        ),
        json_schema_extra={"monday_id": "text_mm3g8epc"},
    )
    is_haredi: Optional[Literal["כן", "לא / לא צוין"]] = Field(
        None, description="מגזר חרדי", json_schema_extra={"monday_id": "color_mm3g9c8r"}
    )
    gender: Optional[Literal["זכר", "נקבה", "לא ידוע"]] = Field(
        None, json_schema_extra={"monday_id": "color_mm3gywqs"}
    )
    linkedin: Optional[str] = Field(None, json_schema_extra={"monday_id": "link_mm3g89t8"})
    email: Optional[str] = Field(None, json_schema_extra={"monday_id": "email_mm3ga25b"})
    phone: Optional[str] = Field(None, json_schema_extra={"monday_id": "phone_mm3g4gvh"})
    recruiter_notes: Optional[str] = Field(
        None,
        description=(
            "הערות למגייסת. null by default. ONLY when city is a non-empty string deduced from education "
            "(not explicit residence, not excluded programs): include exactly "
            "'העיר נגזרה אוטומטית ממוסד הלימודים ([שם המוסד])'. "
            "If city is null, do NOT mention city or education-inference (no removal/skip/failure notes). "
            "May include a short Hebrew red-flag note (employment gaps, visa blockers, etc.). "
            "Never output the derivation sentence without a populated city in the same response."
        ),
        json_schema_extra={"monday_id": "long_text_mm3g3yhw"},
    )
    interview_summaries: Optional[str] = Field(
        None,
        description=(
            "סיכומי ראיונות — reserved for manual recruiter updates and the Monday.com AI Agent only. "
            "Always null when parsing a CV via the backend pipeline; never extract from the resume."
        ),
        json_schema_extra={"monday_id": "text_mm3nx5vz"},
    )
    job_fit_score: Optional[int] = Field(
        None,
        ge=1,
        le=10,
        description=(
            "מדד התאמה למשרה — only when job requirements (דרישות משרה) context was provided. "
            "Integer 1–10 from four-pillar gap analysis: hard requirements (חובה) 80%, "
            "advantages (יתרונות) 20%. MAX 3 if any single hard requirement is not met."
        ),
        json_schema_extra={"monday_id": "numeric_mm4np5tt"},
    )
    job_fit_reasoning: Optional[str] = Field(
        None,
        description=(
            "נימוק התאמה למשרה — brief Hebrew paragraph with pillar-by-pillar gaps "
            "(degree, experience, tech stack, tools) vs. דרישות משרה. "
            "null when no job requirements context was supplied."
        ),
    )
