from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import requests

from models.candidate import CandidateSchema, ProgrammingLanguageExperience

logger = logging.getLogger(__name__)

MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_FILE_API_URL = "https://api.monday.com/v2/file"
MONDAY_API_VERSION = "2024-01"
MONDAY_FILE_API_VERSION = "2023-10"
MAIN_HUB_BOARD_ID = "5096673346"
BOARD_ID = MAIN_HUB_BOARD_ID  # backward-compat alias
# Monday board file column key — override via MONDAY_FILE_COLUMN_ID in .env if needed
FILE_COLUMN_ID = os.getenv("MONDAY_FILE_COLUMN_ID", "file_mm3gnkmj")
EMAIL_COLUMN_ID = "email_mm3ga25b"
PHONE_COLUMN_ID = "phone_mm3g4gvh"
ENTER_DATE_COLUMN_ID = "date_mm3grrc"
INTERVIEW_SUMMARIES_COLUMN_ID = "text_mm3nx5vz"
LANGUAGE_EXPERIENCE_COLUMN_ID = "text_mm3rr7tw"
PROGRAMMING_LANGUAGES_COLUMN_ID = "dropdown_mm3j8kby"
JOB_CATEGORY_DROPDOWN_COLUMN_ID = "dropdown_mm3fyv0t"
AI_SUMMARY_COLUMN_ID = "text_mm3gs8ec"
TEAM_LEAD_DROPDOWN_LABEL = "ראש צוות"
RECRUITER_NOTES_COLUMN_ID = "long_text_mm3g3yhw"

ECOSYSTEM_DOTNET_COLUMN_ID = "numeric_mm467xsd"
ECOSYSTEM_ANGULAR_COLUMN_ID = "numeric_mm46yqhz"
ECOSYSTEM_NODEJS_COLUMN_ID = "numeric_mm46atw8"
ECOSYSTEM_JAVA_COLUMN_ID = "numeric_mm46c6tt"
ECOSYSTEM_PYTHON_COLUMN_ID = "numeric_mm46mcx7"
ECOSYSTEM_REACT_COLUMN_ID = "numeric_mm46v1zt"
ECOSYSTEM_CPP_COLUMN_ID = "numeric_mm46cc84"

ECOSYSTEM_NUMERIC_COLUMN_IDS: tuple[str, ...] = (
    ECOSYSTEM_DOTNET_COLUMN_ID,
    ECOSYSTEM_ANGULAR_COLUMN_ID,
    ECOSYSTEM_NODEJS_COLUMN_ID,
    ECOSYSTEM_JAVA_COLUMN_ID,
    ECOSYSTEM_PYTHON_COLUMN_ID,
    ECOSYSTEM_REACT_COLUMN_ID,
    ECOSYSTEM_CPP_COLUMN_ID,
)

# Normalized lookup keys (lowercase, collapsed whitespace) → ecosystem numeric column IDs.
TARGET_MAPPING: dict[str, list[str]] = {
    ".net": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "c#": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "asp.net": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "wcf": [ECOSYSTEM_DOTNET_COLUMN_ID],
    ".net core": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "c# .net": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "asp.net core": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "asp.net mvc": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "asp.net web api": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "c#/.net core": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "c# / .net core": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "c# / asp.net": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "wcf / asp.net": [ECOSYSTEM_DOTNET_COLUMN_ID],
    ".net / mvc": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "asp .net": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "vb.net": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "asp.net / .net core": [ECOSYSTEM_DOTNET_COLUMN_ID],
    ".net c#": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "unity / c#": [ECOSYSTEM_DOTNET_COLUMN_ID],
    ".net/c#": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "wpf": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "c#/.net": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "c# / .net": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "c# / asp.net core": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "c# .net core": [ECOSYSTEM_DOTNET_COLUMN_ID],
    ".net core / c#": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "wpf/winforms": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "c# / wpf": [ECOSYSTEM_DOTNET_COLUMN_ID],
    ".net / .net core": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "linq": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "wpf/.net": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "c# selenium": [ECOSYSTEM_DOTNET_COLUMN_ID],
    ".net core / asp.net": [ECOSYSTEM_DOTNET_COLUMN_ID],
    ".net core / microservices": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "c#.net": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "winforms": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "asp.net / vb.net / c#": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "entity framework": [ECOSYSTEM_DOTNET_COLUMN_ID],
    "angular": [ECOSYSTEM_ANGULAR_COLUMN_ID],
    "angularjs / angular": [ECOSYSTEM_ANGULAR_COLUMN_ID],
    "angular.js": [ECOSYSTEM_ANGULAR_COLUMN_ID],
    "angular js": [ECOSYSTEM_ANGULAR_COLUMN_ID],
    "angularjs": [ECOSYSTEM_ANGULAR_COLUMN_ID],
    "angular / typescript": [ECOSYSTEM_ANGULAR_COLUMN_ID],
    "node.js": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "nestjs": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "express": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "nodejs": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "node js": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "express.js": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "node": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "nest.js": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "node.js / nestjs": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "javascript/nodejs": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "javascript / node.js": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "nestjs / node.js": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "node.js / nest.js": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "node.js (nestjs)": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "javascript/node.js": [ECOSYSTEM_NODEJS_COLUMN_ID],
    "java": [ECOSYSTEM_JAVA_COLUMN_ID],
    "spring": [ECOSYSTEM_JAVA_COLUMN_ID],
    "j2ee": [ECOSYSTEM_JAVA_COLUMN_ID],
    "spring boot": [ECOSYSTEM_JAVA_COLUMN_ID],
    "hibernate": [ECOSYSTEM_JAVA_COLUMN_ID],
    "java spring": [ECOSYSTEM_JAVA_COLUMN_ID],
    "java spring boot": [ECOSYSTEM_JAVA_COLUMN_ID],
    "java spring & spring boot": [ECOSYSTEM_JAVA_COLUMN_ID],
    "java / springboot": [ECOSYSTEM_JAVA_COLUMN_ID],
    "java / spring boot": [ECOSYSTEM_JAVA_COLUMN_ID],
    "java (android)": [ECOSYSTEM_JAVA_COLUMN_ID],
    "java (spring)": [ECOSYSTEM_JAVA_COLUMN_ID],
    "java (j2ee/jsf)": [ECOSYSTEM_JAVA_COLUMN_ID],
    "java selenium": [ECOSYSTEM_JAVA_COLUMN_ID],
    "springboot": [ECOSYSTEM_JAVA_COLUMN_ID],
    "spring framework": [ECOSYSTEM_JAVA_COLUMN_ID],
    "python": [ECOSYSTEM_PYTHON_COLUMN_ID],
    "fastapi": [ECOSYSTEM_PYTHON_COLUMN_ID],
    "django": [ECOSYSTEM_PYTHON_COLUMN_ID],
    "flask": [ECOSYSTEM_PYTHON_COLUMN_ID],
    "python/scripting": [ECOSYSTEM_PYTHON_COLUMN_ID],
    "python (flask)": [ECOSYSTEM_PYTHON_COLUMN_ID],
    "python (django)": [ECOSYSTEM_PYTHON_COLUMN_ID],
    "react": [ECOSYSTEM_REACT_COLUMN_ID],
    "react native": [ECOSYSTEM_REACT_COLUMN_ID],
    "reactjs": [ECOSYSTEM_REACT_COLUMN_ID],
    "react.js": [ECOSYSTEM_REACT_COLUMN_ID],
    "react-native": [ECOSYSTEM_REACT_COLUMN_ID],
    "react redux": [ECOSYSTEM_REACT_COLUMN_ID],
    "next.js / react": [ECOSYSTEM_REACT_COLUMN_ID],
    "redux": [ECOSYSTEM_REACT_COLUMN_ID],
    "react / next.js": [ECOSYSTEM_REACT_COLUMN_ID],
    "react / react native": [ECOSYSTEM_REACT_COLUMN_ID],
    "react js": [ECOSYSTEM_REACT_COLUMN_ID],
    "react / nextjs": [ECOSYSTEM_REACT_COLUMN_ID],
    "react (typescript)": [ECOSYSTEM_REACT_COLUMN_ID],
    "redux toolkit": [ECOSYSTEM_REACT_COLUMN_ID],
    "next.js": [ECOSYSTEM_REACT_COLUMN_ID],
    "c++": [ECOSYSTEM_CPP_COLUMN_ID],
    "c/c++": [ECOSYSTEM_CPP_COLUMN_ID],
    "c/c++/c#/.net": [ECOSYSTEM_CPP_COLUMN_ID, ECOSYSTEM_DOTNET_COLUMN_ID],
    "react / node.js": [ECOSYSTEM_REACT_COLUMN_ID, ECOSYSTEM_NODEJS_COLUMN_ID],
    "javascript / react / express": [
        ECOSYSTEM_REACT_COLUMN_ID,
        ECOSYSTEM_NODEJS_COLUMN_ID,
    ],
    "angular / react / node.js": [
        ECOSYSTEM_ANGULAR_COLUMN_ID,
        ECOSYSTEM_REACT_COLUMN_ID,
        ECOSYSTEM_NODEJS_COLUMN_ID,
    ],
    "angular/react": [ECOSYSTEM_ANGULAR_COLUMN_ID, ECOSYSTEM_REACT_COLUMN_ID],
    "react/angular": [ECOSYSTEM_ANGULAR_COLUMN_ID, ECOSYSTEM_REACT_COLUMN_ID],
    "react / angular": [ECOSYSTEM_ANGULAR_COLUMN_ID, ECOSYSTEM_REACT_COLUMN_ID],
}

_LANGUAGE_EXPERIENCE_ENTRY_RE = re.compile(
    r"^\s*(.+?)\s*\((\d+(?:\.\d+)?)y?\)\s*$",
    re.IGNORECASE,
)
NAME_COLUMN_ID = "name"
FIND_ITEMS_LIMIT = 25

# Exact Monday board labels for dropdown_mm3j8kby (case-sensitive on the API).
MONDAY_PROGRAMMING_LANGUAGE_LABELS: tuple[str, ...] = (
    "C#",
    ".NET",
    "Python",
    "REST APIs",
    "Microservices",
    "React",
    "Angular",
    "Java",
    "Node.js",
    "AWS",
    "ASP.NET",
    "SQL Server",
    "WCF",
    "SQL",
    "JavaScript",
    "MySQL",
    "TypeScript",
    "C++",
    ".NET Core",
    "Magic XPA",
    "VB",
    "SSIS",
    "C# .NET",
    "NestJS",
    "GraphQL",
    "Express",
    "MongoDB",
    "C",
    "Matlab",
    "Perl",
    "React Native",
    "Spring",
    "Swift",
    "Kotlin",
    "PHP Laravel",
    "Vue.js",
    "Oracle/SQL",
    "Hibernate",
    "J2EE",
    "NodeJS",
    "Machine Learning",
    "Spring Boot",
    "PL/SQL",
    "MSSQL",
    "Oracle",
    "PowerShell",
    "Priority (ERP)",
    "Web API",
    "ReactJS",
    "React.js",
    ".NET / C#",
    "JavaScript / TypeScript",
    "QlikView / QlikSense",
    "Linux",
    "PHP",
    "Power BI",
    "Vue",
    "JQuery",
    "CSS",
    "Splunk",
    "Metasploit",
    "Wireshark",
    "VMware",
    "Azure",
    "AI",
    "Laravel (PHP)",
    "ASP.NET Core",
)

_PROGRAMMING_LANGUAGE_BY_LOWER: dict[str, str] = {
    label.casefold(): label for label in MONDAY_PROGRAMMING_LANGUAGE_LABELS
}

CREATE_ITEM_MUTATION = """
mutation ($boardId: ID!, $groupId: String!, $itemName: String!, $columnValues: JSON!) {
  create_item (
    board_id: $boardId,
    group_id: $groupId,
    item_name: $itemName,
    column_values: $columnValues,
    create_labels_if_missing: true
  ) {
    id
  }
}
"""

CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION = """
mutation ($boardId: ID!, $itemId: ID!, $columnValues: JSON!) {
  change_multiple_column_values(
    board_id: $boardId,
    item_id: $itemId,
    column_values: $columnValues,
    create_labels_if_missing: true
  ) {
    id
  }
}
"""

BOARD_DROPDOWN_COLUMN_QUERY = """
query ($boardId: ID!, $columnIds: [String!]!) {
  boards(ids: [$boardId]) {
    columns(ids: $columnIds) {
      id
      settings_str
    }
  }
}
"""

CHANGE_COLUMN_METADATA_MUTATION = """
mutation ($boardId: ID!, $columnId: String!, $value: JSON!) {
  change_column_metadata(
    board_id: $boardId,
    column_id: $columnId,
    column_property: settings,
    value: $value
  ) {
    id
  }
}
"""

CHANGE_ITEM_NAME_MUTATION = """
mutation ($boardId: ID!, $itemId: ID!, $name: String!) {
  change_simple_column_value(
    board_id: $boardId,
    item_id: $itemId,
    column_id: "name",
    value: $name
  ) {
    id
    name
  }
}
"""

ITEMS_PAGE_BY_COLUMN_VALUES_QUERY = """
query ($boardId: ID!, $limit: Int!, $columns: [ItemsPageByColumnValuesQuery!]!) {
  items_page_by_column_values(board_id: $boardId, limit: $limit, columns: $columns) {
    items {
      id
      name
    }
  }
}
"""

ITEMS_BY_IDS_QUERY = """
query ($ids: [ID!]!, $columnIds: [String!]!) {
  items(ids: $ids) {
    id
    name
    column_values(ids: $columnIds) {
      id
      text
      value
    }
  }
}
"""

ITEM_CV_FILE_QUERY = """
query ($ids: [ID!]!, $columnId: String!) {
  items(ids: $ids) {
    column_values(ids: [$columnId]) {
      ... on FileValue {
        files {
          ... on FileAssetValue {
            name
            asset {
              public_url
              file_extension
            }
          }
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class FoundItem:
    item_id: str
    name: str


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, list) and len(value) == 0:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _dropdown_labels(labels: list[str]) -> dict[str, list[str]]:
    """Monday API v2 multi-select dropdown payload."""
    return {"labels": labels}


def _resolve_programming_language_label(raw: str) -> str | None:
    """Map known aliases to canonical labels; preserve unknown extracted labels."""
    text = str(raw).strip()
    if not text:
        return None
    canonical = _PROGRAMMING_LANGUAGE_BY_LOWER.get(text.casefold())
    if canonical is not None:
        return canonical
    logger.info("New programming language label for Monday dropdown: %r", text)
    return text


def resolve_programming_language_labels(languages: list[str]) -> list[str]:
    """
    Resolve Claude-extracted language tags to Monday dropdown labels.

    Known aliases use canonical board spelling; unknown labels are kept as-is
    so Monday can create them dynamically via create_labels_if_missing.
    """
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in languages:
        label = _resolve_programming_language_label(raw)
        if label is None:
            continue
        dedupe_key = label.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        resolved.append(label)
    return resolved


def normalize_programming_languages(languages: list[str]) -> list[str]:
    """Backward-compatible alias for resolve_programming_language_labels."""
    return resolve_programming_language_labels(languages)


def _format_years_compact(years: float) -> str:
    """Format tenure as compact suffix, e.g. 7y or 3.5y."""
    rounded = round(years, 1)
    if rounded == int(rounded):
        return f"{int(rounded)}y"
    return f"{rounded:.1f}y"


def _normalize_lookup_key(text: str) -> str:
    """Lowercase and collapse whitespace for ecosystem alias lookup."""
    return re.sub(r"\s+", " ", text.strip().casefold())


def _format_ecosystem_numeric_value(years: float) -> str:
    """Format experience years as Monday numeric column string (e.g. '4' or '2.5')."""
    rounded = round(years, 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return str(rounded)


def parse_language_experience_entries(language_experience_text: str) -> list[tuple[str, float]]:
    """
    Parse compact language tenure text into (language, years) pairs.

    Supports patterns like ``Java (7y)``, ``React (3.5y)``, and ``Python (2.5)``.
    """
    entries: list[tuple[str, float]] = []
    for part in language_experience_text.split(","):
        part = part.strip()
        if not part:
            continue
        match = _LANGUAGE_EXPERIENCE_ENTRY_RE.match(part)
        if not match:
            continue
        language = match.group(1).strip()
        years = float(match.group(2))
        entries.append((language, years))
    return entries


def map_language_experience_to_ecosystem_columns(
    language_experience_text: str,
) -> dict[str, float]:
    """
    Map parsed language tenure text to ecosystem numeric column IDs.

    When multiple entries resolve to the same column, the maximum years value wins.
    """
    column_years: dict[str, float] = {}
    for language, years in parse_language_experience_entries(language_experience_text):
        lookup_key = _normalize_lookup_key(language)
        target_columns = TARGET_MAPPING.get(lookup_key)
        if not target_columns:
            continue
        for column_id in target_columns:
            column_years[column_id] = max(column_years.get(column_id, 0.0), years)
    return column_years


def build_job_category_dropdown_labels(
    job_categories: list[str] | None,
    raw_cv_text: str | None = None,
) -> dict[str, list[str]] | None:
    """
    Build multi-select dropdown payload for job category + team-lead detection.

    Preserves Claude-extracted job categories and appends ``ראש צוות`` when the
    exact Hebrew phrase appears anywhere in the raw CV plain text.
    """
    labels: list[str] = []
    seen: set[str] = set()
    for raw in job_categories or []:
        label = str(raw).strip()
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)

    cv_text = (raw_cv_text or "").strip()
    if cv_text and TEAM_LEAD_DROPDOWN_LABEL in cv_text:
        if TEAM_LEAD_DROPDOWN_LABEL not in seen:
            labels.append(TEAM_LEAD_DROPDOWN_LABEL)

    if not labels:
        return None
    return _dropdown_labels(labels)


def apply_ecosystem_column_enrichment(
    column_values: dict[str, Any],
    candidate: CandidateSchema,
    raw_cv_text: str = "",
) -> None:
    """
    Enrich Monday payload with ecosystem numeric columns and team-lead dropdown.

    Mutates ``column_values`` in place immediately before API sync.
    """
    language_experience_text = ""
    if candidate.programming_languages:
        language_experience_text = format_language_experience_compact(
            candidate.programming_languages
        )
    if not language_experience_text:
        language_experience_text = str(
            column_values.get(LANGUAGE_EXPERIENCE_COLUMN_ID) or ""
        ).strip()

    column_years = map_language_experience_to_ecosystem_columns(language_experience_text)
    for column_id in ECOSYSTEM_NUMERIC_COLUMN_IDS:
        years = column_years.get(column_id)
        column_values[column_id] = (
            _format_ecosystem_numeric_value(years) if years is not None else ""
        )

    job_category_payload = build_job_category_dropdown_labels(
        candidate.job_category,
        raw_cv_text,
    )
    if job_category_payload is not None:
        column_values[JOB_CATEGORY_DROPDOWN_COLUMN_ID] = job_category_payload
    elif JOB_CATEGORY_DROPDOWN_COLUMN_ID in column_values:
        del column_values[JOB_CATEGORY_DROPDOWN_COLUMN_ID]


def format_language_experience_compact(
    experiences: list[ProgrammingLanguageExperience],
) -> str:
    """
    Build compact language tenure string for Monday text column.

    Example: "Java (7y), React (3.5y), Node.js (2y)"
    """
    parts: list[str] = []
    seen: set[str] = set()
    for entry in experiences:
        label = _resolve_programming_language_label(entry.language)
        if label is None:
            continue
        dedupe_key = label.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        parts.append(f"{label} ({_format_years_compact(entry.years)})")
    return ", ".join(parts)


def _status_label(label: str) -> dict[str, str]:
    """Monday API v2 status column payload."""
    return {"label": label}


def _append_note_field(existing: str | None, new: str, *, stamp: str) -> str:
    existing_text = (existing or "").strip()
    new_text = new.strip()
    if not new_text:
        return existing_text
    if not existing_text:
        return new_text
    return f"{existing_text}\n\n--- {stamp} ---\n{new_text}"


def _phone_digits_from_column(column: dict[str, Any]) -> str:
    text = column.get("text") or ""
    value = column.get("value")
    if value:
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
            if isinstance(parsed, dict):
                phone_value = parsed.get("phone") or parsed.get("text") or ""
                text = f"{text} {phone_value}"
        except (json.JSONDecodeError, TypeError):
            pass
    return normalize_phone(str(text))


def build_column_values(
    candidate: CandidateSchema,
    raw_cv_text: str = "",
) -> dict[str, Any]:
    """
    Build Monday.com column_values JSON for create_item / update.

    CV files (FILE_COLUMN_ID) are intentionally excluded — Monday does not accept
    raw file bytes in column_values. Upload via upload_file_to_item() instead.
    """
    column_values: dict[str, Any] = {}

    if not _is_empty(candidate.email):
        email = normalize_email(candidate.email)
        column_values[EMAIL_COLUMN_ID] = {
            "email": email,
            "text": email,
        }

    if not _is_empty(candidate.phone):
        phone = normalize_phone(candidate.phone)
        column_values[PHONE_COLUMN_ID] = {
            "phone": phone,
            "countryShortName": "IL",
        }

    if not _is_empty(candidate.linkedin):
        column_values["link_mm3g89t8"] = {
            "url": candidate.linkedin,
            "text": "LinkedIn Profile",
        }

    if not _is_empty(candidate.is_haredi):
        column_values["color_mm3g9c8r"] = _status_label(candidate.is_haredi)

    if not _is_empty(candidate.gender):
        column_values["color_mm3gywqs"] = _status_label(candidate.gender)

    if not _is_empty(candidate.education):
        column_values["dropdown_mm3fxr2k"] = _dropdown_labels([candidate.education])

    if not _is_empty(candidate.company_type):
        column_values["color_mm3n87tr"] = _status_label(candidate.company_type)

    if not _is_empty(candidate.ai_summary):
        column_values[AI_SUMMARY_COLUMN_ID] = candidate.ai_summary

    if candidate.programming_languages:
        language_names = [e.language for e in candidate.programming_languages][:5]
        if language_names:
            column_values[PROGRAMMING_LANGUAGES_COLUMN_ID] = {"labels": language_names}
        compact = format_language_experience_compact(candidate.programming_languages)
        if compact:
            column_values[LANGUAGE_EXPERIENCE_COLUMN_ID] = compact

    if candidate.years_of_experience is not None:
        column_values["numeric_mm3fc9k3"] = str(candidate.years_of_experience)

    if not _is_empty(candidate.languages):
        column_values["dropdown_mm3g8c14"] = _dropdown_labels(list(candidate.languages))

    if not _is_empty(candidate.city):
        column_values["text_mm3g8epc"] = candidate.city.strip()

    if not _is_empty(candidate.test_score):
        column_values["numeric_mm3fzf1d"] = str(candidate.test_score)

    column_values[ENTER_DATE_COLUMN_ID] = {"date": date.today().strftime("%Y-%m-%d")}

    if not _is_empty(candidate.recruiter_notes):
        column_values[RECRUITER_NOTES_COLUMN_ID] = {"text": candidate.recruiter_notes}

    if not _is_empty(candidate.salary_expectations):
        column_values["text_mm46zd0t"] = candidate.salary_expectations.strip()

    apply_ecosystem_column_enrichment(column_values, candidate, raw_cv_text)
    return column_values


def build_update_column_values(
    candidate: CandidateSchema,
    existing_notes: dict[str, str | None],
    raw_cv_text: str = "",
) -> dict[str, Any]:
    """Build column_values for update, appending notes and refreshing enter date."""
    column_values = build_column_values(candidate, raw_cv_text)
    stamp = date.today().strftime("%Y-%m-%d")

    if not _is_empty(candidate.recruiter_notes):
        merged = _append_note_field(
            existing_notes.get(RECRUITER_NOTES_COLUMN_ID),
            candidate.recruiter_notes,
            stamp=stamp,
        )
        column_values[RECRUITER_NOTES_COLUMN_ID] = {"text": merged}

    column_values[ENTER_DATE_COLUMN_ID] = {"date": stamp}
    return column_values


def _monday_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": api_key,
        "Content-Type": "application/json",
        "API-Version": MONDAY_API_VERSION,
    }


def _get_api_key() -> str:
    api_key = os.getenv("MONDAY_API_KEY")
    if not api_key:
        raise ValueError("MONDAY_API_KEY environment variable is not set")
    return api_key


def _print_monday_errors(
    errors: list[Any],
    *,
    http_status: int | None = None,
    response_text: str | None = None,
    column_ids: list[str] | None = None,
) -> None:
    """Print Monday.com error payload and flag any column IDs mentioned in messages."""
    if http_status is not None:
        print(f"Monday.com HTTP status: {http_status}")
    if response_text:
        print(f"Monday.com raw response: {response_text}")

    for index, err in enumerate(errors):
        print(f"Monday.com error [{index}]: {json.dumps(err, ensure_ascii=False, default=str)}")

        if not isinstance(err, dict):
            continue

        message = err.get("message", "")
        extensions = err.get("extensions") or {}
        if extensions:
            print(
                f"Monday.com error [{index}] extensions: "
                f"{json.dumps(extensions, ensure_ascii=False, default=str)}"
            )

        search_text = f"{message} {json.dumps(extensions, default=str)}"
        if column_ids:
            for column_id in column_ids:
                if column_id in search_text:
                    print(f"Monday.com error [{index}] mentions column ID: {column_id}")


def _raise_for_monday_errors(
    body: dict[str, Any],
    *,
    column_ids: list[str] | None = None,
) -> None:
    if errors := body.get("errors"):
        _print_monday_errors(errors, column_ids=column_ids)
        messages = "; ".join(
            err.get("message", str(err)) if isinstance(err, dict) else str(err)
            for err in errors
        )
        logger.error("Monday.com GraphQL errors: %s", messages)
        raise Exception(f"Monday.com API error: {messages}")


async def _post_graphql(
    query: str,
    variables: dict[str, Any],
    *,
    column_ids: list[str] | None = None,
) -> dict[str, Any]:
    api_key = _get_api_key()
    payload = {"query": query, "variables": variables}

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            MONDAY_API_URL,
            json=payload,
            headers=_monday_headers(api_key),
        )

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        response_text = exc.response.text
        try:
            http_errors = exc.response.json().get("errors", [])
        except (json.JSONDecodeError, AttributeError):
            http_errors = [{"message": response_text}]
        _print_monday_errors(
            http_errors,
            http_status=exc.response.status_code,
            response_text=response_text,
            column_ids=column_ids,
        )
        raise Exception(
            f"Monday.com HTTP error {exc.response.status_code}: {response_text}"
        ) from exc

    body = response.json()
    _raise_for_monday_errors(body, column_ids=column_ids)
    return body


def _looks_like_dropdown_label_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    keywords = ("label", "dropdown", "invalid", "does not exist", "missing")
    return any(keyword in message for keyword in keywords)


async def ensure_dropdown_labels_exist(
    labels: list[str],
    *,
    board_id: str = MAIN_HUB_BOARD_ID,
) -> None:
    """
    Best-effort fallback: append missing labels to the board dropdown column settings.

    Used when create_labels_if_missing fails (e.g. insufficient token permissions).
    """
    if not labels:
        return

    body = await _post_graphql(
        BOARD_DROPDOWN_COLUMN_QUERY,
        {"boardId": board_id, "columnIds": [PROGRAMMING_LANGUAGES_COLUMN_ID]},
    )
    boards = body.get("data", {}).get("boards") or []
    columns = (boards[0].get("columns") if boards else None) or []
    if not columns:
        logger.warning("Could not fetch dropdown column settings for label provisioning")
        return

    settings_raw = columns[0].get("settings_str") or "{}"
    settings = json.loads(settings_raw) if isinstance(settings_raw, str) else dict(settings_raw)
    existing_labels: list[dict[str, Any]] = list(settings.get("labels") or [])

    existing_names: set[str] = set()
    max_id = 0
    for item in existing_labels:
        name = str(item.get("name") or item.get("label") or "").strip()
        if name:
            existing_names.add(name.casefold())
        label_id = item.get("id")
        if isinstance(label_id, int):
            max_id = max(max_id, label_id)
        elif isinstance(label_id, str) and label_id.isdigit():
            max_id = max(max_id, int(label_id))

    new_label_rows: list[dict[str, Any]] = []
    for label in labels:
        text = label.strip()
        if not text or text.casefold() in existing_names:
            continue
        max_id += 1
        new_label_rows.append({"id": max_id, "name": text})
        existing_names.add(text.casefold())

    if not new_label_rows:
        return

    settings["labels"] = existing_labels + new_label_rows
    await _post_graphql(
        CHANGE_COLUMN_METADATA_MUTATION,
        {
            "boardId": board_id,
            "columnId": PROGRAMMING_LANGUAGES_COLUMN_ID,
            "value": settings,
        },
    )
    for row in new_label_rows:
        name = str(row["name"])
        _PROGRAMMING_LANGUAGE_BY_LOWER[name.casefold()] = name
    logger.info(
        "Provisioned dropdown labels on Monday board: %r",
        [row["name"] for row in new_label_rows],
    )


async def _post_column_values_with_dropdown_fallback(
    *,
    mutation: str,
    variables: dict[str, Any],
    column_values: dict[str, Any],
) -> dict[str, Any]:
    """
    Post column values to Monday; on dropdown failure retry without dropdown but keep text column.
    """
    column_ids = list(column_values.keys())
    try:
        return await _post_graphql(mutation, variables, column_ids=column_ids)
    except Exception as exc:
        if PROGRAMMING_LANGUAGES_COLUMN_ID not in column_values:
            raise

        dropdown_payload = column_values[PROGRAMMING_LANGUAGES_COLUMN_ID]
        labels = dropdown_payload.get("labels", []) if isinstance(dropdown_payload, dict) else []
        logger.warning(
            "Monday write failed with programming languages %r; attempting recovery: %s",
            labels,
            exc,
        )

        if _looks_like_dropdown_label_error(exc):
            try:
                board_id = str(variables.get("boardId", MAIN_HUB_BOARD_ID))
                await ensure_dropdown_labels_exist(labels, board_id=board_id)
            except Exception as provision_exc:
                logger.warning("Dropdown label provisioning failed: %s", provision_exc)

        if LANGUAGE_EXPERIENCE_COLUMN_ID not in column_values:
            raise

        retry_column_values = {
            key: value
            for key, value in column_values.items()
            if key != PROGRAMMING_LANGUAGES_COLUMN_ID
        }
        retry_variables = dict(variables)
        retry_variables["columnValues"] = json.dumps(retry_column_values)
        retry_column_ids = [
            key for key in column_ids if key != PROGRAMMING_LANGUAGES_COLUMN_ID
        ]
        logger.warning(
            "Retrying Monday write without %s; preserving %s",
            PROGRAMMING_LANGUAGES_COLUMN_ID,
            LANGUAGE_EXPERIENCE_COLUMN_ID,
        )
        return await _post_graphql(
            mutation,
            retry_variables,
            column_ids=retry_column_ids,
        )


async def _query_items_by_column(
    column_id: str,
    column_values: list[str],
    *,
    board_id: str = MAIN_HUB_BOARD_ID,
) -> list[FoundItem]:
    body = await _post_graphql(
        ITEMS_PAGE_BY_COLUMN_VALUES_QUERY,
        {
            "boardId": board_id,
            "limit": FIND_ITEMS_LIMIT,
            "columns": [{"column_id": column_id, "column_values": column_values}],
        },
    )
    items = body.get("data", {}).get("items_page_by_column_values", {}).get("items") or []
    return [
        FoundItem(item_id=str(item["id"]), name=str(item.get("name") or ""))
        for item in items
        if item.get("id") is not None
    ]


async def _disambiguate_phone_matches(
    items: list[FoundItem],
    normalized_phone: str,
) -> FoundItem | None:
    if not items:
        return None
    if len(items) == 1:
        item = items[0]
        item_phones = await _fetch_item_phone_digits(item.item_id)
        if item_phones == normalized_phone or not item_phones:
            return item
        logger.warning(
            "Monday phone lookup: single item %s phone %s does not exactly match %s",
            item.item_id,
            item_phones,
            normalized_phone,
        )
        return item

    item_ids = [item.item_id for item in items]
    body = await _post_graphql(
        ITEMS_BY_IDS_QUERY,
        {
            "ids": item_ids,
            "columnIds": [PHONE_COLUMN_ID],
        },
    )
    monday_items = body.get("data", {}).get("items") or []
    exact_matches: list[FoundItem] = []
    for monday_item in monday_items:
        item_id = str(monday_item["id"])
        name = str(monday_item.get("name") or "")
        phone_column = next(
            (col for col in monday_item.get("column_values") or [] if col.get("id") == PHONE_COLUMN_ID),
            None,
        )
        digits = _phone_digits_from_column(phone_column or {})
        if digits == normalized_phone:
            exact_matches.append(FoundItem(item_id=item_id, name=name))

    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        logger.error(
            "Monday phone lookup: multiple exact matches for %s: %s",
            normalized_phone,
            [item.item_id for item in exact_matches],
        )
        return None

    logger.warning(
        "Monday phone lookup: no exact phone match for %s among %d candidates",
        normalized_phone,
        len(items),
    )
    return None


async def _fetch_item_phone_digits(item_id: str) -> str:
    body = await _post_graphql(
        ITEMS_BY_IDS_QUERY,
        {"ids": [item_id], "columnIds": [PHONE_COLUMN_ID]},
    )
    items = body.get("data", {}).get("items") or []
    if not items:
        return ""
    phone_column = next(
        (col for col in items[0].get("column_values") or [] if col.get("id") == PHONE_COLUMN_ID),
        None,
    )
    return _phone_digits_from_column(phone_column or {})


async def find_existing_item_by_contact(
    candidate: CandidateSchema,
    *,
    board_id: str = MAIN_HUB_BOARD_ID,
) -> FoundItem | None:
    """Find an existing board item by normalized email or phone."""
    email = normalize_email(candidate.email) if not _is_empty(candidate.email) else ""
    phone = normalize_phone(candidate.phone) if not _is_empty(candidate.phone) else ""

    email_match: FoundItem | None = None
    phone_match: FoundItem | None = None

    if email:
        email_items = await _query_items_by_column(
            EMAIL_COLUMN_ID, [email], board_id=board_id
        )
        if email_items:
            email_match = email_items[0]
            if len(email_items) > 1:
                logger.warning(
                    "Monday email lookup: multiple items for %s, using %s",
                    email,
                    email_match.item_id,
                )

    if phone:
        phone_items = await _query_items_by_column(
            PHONE_COLUMN_ID, [phone], board_id=board_id
        )
        phone_match = await _disambiguate_phone_matches(phone_items, phone)

    if email_match and phone_match and email_match.item_id != phone_match.item_id:
        logger.warning(
            "Monday contact conflict: email match %s vs phone match %s; using email match",
            email_match.item_id,
            phone_match.item_id,
        )

    if email_match:
        return email_match
    return phone_match


async def _fetch_existing_notes(item_id: str) -> dict[str, str | None]:
    body = await _post_graphql(
        ITEMS_BY_IDS_QUERY,
        {
            "ids": [item_id],
            "columnIds": [RECRUITER_NOTES_COLUMN_ID],
        },
    )
    items = body.get("data", {}).get("items") or []
    if not items:
        return {RECRUITER_NOTES_COLUMN_ID: None}

    notes: dict[str, str | None] = {RECRUITER_NOTES_COLUMN_ID: None}
    for column in items[0].get("column_values") or []:
        column_id = column.get("id")
        if column_id in notes:
            text = column.get("text")
            notes[column_id] = text if text else None
    return notes


async def change_item_name(
    item_id: str,
    new_name: str,
    *,
    board_id: str = MAIN_HUB_BOARD_ID,
) -> None:
    """Rename board row via Monday Name column."""
    name = new_name.strip()
    if not name:
        logger.warning("Monday change_item_name skipped: empty name for item %s", item_id)
        return

    await _post_graphql(
        CHANGE_ITEM_NAME_MUTATION,
        {"boardId": board_id, "itemId": item_id, "name": name},
    )
    logger.info("Monday change_item_name: item %s renamed to %r", item_id, name)


async def update_candidate_item(
    item_id: str,
    candidate: CandidateSchema,
    *,
    existing_name: str,
    raw_cv_text: str = "",
    board_id: str = MAIN_HUB_BOARD_ID,
) -> str:
    """Update an existing Monday.com item and return its ID."""
    existing_notes = await _fetch_existing_notes(item_id)
    column_values = build_update_column_values(candidate, existing_notes, raw_cv_text)

    # Ensure programming language dropdown + text columns are always included on updates.
    if candidate.programming_languages:
        language_names = [e.language for e in candidate.programming_languages][:5]
        if language_names:
            column_values[PROGRAMMING_LANGUAGES_COLUMN_ID] = {"labels": language_names}
        compact = format_language_experience_compact(candidate.programming_languages)
        if compact:
            column_values[LANGUAGE_EXPERIENCE_COLUMN_ID] = compact

    column_values_json = json.dumps(column_values)
    column_ids = list(column_values.keys())

    logger.info(
        "Monday.com change_multiple_column_values keys for item %s: %s",
        item_id,
        column_ids,
    )

    await _post_column_values_with_dropdown_fallback(
        mutation=CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
        variables={
            "boardId": board_id,
            "itemId": item_id,
            "columnValues": column_values_json,
        },
        column_values=column_values,
    )

    new_name = candidate.name.strip()
    if new_name and new_name != existing_name.strip():
        await change_item_name(item_id, new_name, board_id=board_id)
    elif not new_name:
        logger.warning(
            "Monday update: parsed name empty for item %s; keeping %r",
            item_id,
            existing_name,
        )
    else:
        logger.debug("Monday update: item %s name unchanged (%r)", item_id, existing_name)

    return item_id


async def create_candidate_item(
    candidate: CandidateSchema,
    *,
    cv_file_path: str | Path | None = None,
    raw_cv_text: str = "",
    board_id: str = MAIN_HUB_BOARD_ID,
) -> str:
    """Create a Monday.com board item and return the new item ID."""
    column_values = build_column_values(candidate, raw_cv_text)
    column_values_json = json.dumps(column_values)
    group_id = os.getenv("MONDAY_GROUP_ID", "topics")

    logger.info("Monday.com create_item column_values keys: %s", list(column_values.keys()))
    print(f"Monday.com column_values JSON: {column_values_json}")

    body = await _post_column_values_with_dropdown_fallback(
        mutation=CREATE_ITEM_MUTATION,
        variables={
            "boardId": board_id,
            "groupId": group_id,
            "itemName": candidate.name,
            "columnValues": column_values_json,
        },
        column_values=column_values,
    )

    try:
        item_id = body["data"]["create_item"]["id"]
    except (KeyError, TypeError) as exc:
        raise Exception(f"Unexpected Monday.com response structure: {body}") from exc

    item_id = str(item_id)
    if cv_file_path:
        upload_file_to_item(item_id, str(cv_file_path))
    return item_id


async def upsert_candidate_item(
    candidate: CandidateSchema,
    *,
    cv_file_path: str | Path | None = None,
    raw_cv_text: str = "",
    board_id: str = MAIN_HUB_BOARD_ID,
) -> tuple[str, bool]:
    """
    Create or update a Monday.com item keyed by email/phone.

    When ``cv_file_path`` is set, uploads the CV only for newly created items.
    For matched existing items, update column values/name and skip file upload
    to prevent duplicate attachments during QA reprocessing.

    Returns (item_id, created) where created is True for a new row.
    """
    has_email = not _is_empty(candidate.email)
    has_phone = not _is_empty(candidate.phone)
    cv_path = str(cv_file_path) if cv_file_path else None

    if not has_email and not has_phone:
        logger.warning(
            "Monday upsert: no email or phone for %r; creating new item (duplicates possible)",
            candidate.name,
        )
        item_id = await create_candidate_item(
            candidate,
            cv_file_path=cv_path,
            raw_cv_text=raw_cv_text,
            board_id=board_id,
        )
        logger.info("Monday upsert: created item %s (no contact identifier)", item_id)
        return item_id, True

    existing = await find_existing_item_by_contact(candidate, board_id=board_id)
    if existing is None:
        item_id = await create_candidate_item(
            candidate,
            cv_file_path=cv_path,
            raw_cv_text=raw_cv_text,
            board_id=board_id,
        )
        contact = normalize_email(candidate.email) if has_email else normalize_phone(candidate.phone)
        logger.info("Monday upsert: created item %s (no match for %s)", item_id, contact)
        return item_id, True

    item_id = await update_candidate_item(
        existing.item_id,
        candidate,
        existing_name=existing.name,
        raw_cv_text=raw_cv_text,
        board_id=board_id,
    )
    if cv_path:
        logger.info(
            "Candidate item updated with new extracted data. "
            "Skipping file upload to prevent duplicates during QA."
        )
    contact = normalize_email(candidate.email) if has_email else normalize_phone(candidate.phone)
    logger.info("Monday upsert: updated item %s (matched %s)", item_id, contact)
    return item_id, False


async def get_item_cv_file_url(item_id: str) -> tuple[str, str]:
    """
    Fetch the public download URL and filename for the most recent CV file on an item.

    Returns:
        (public_url, filename) — URL is valid for ~1 hour per Monday API docs.

    Raises:
        ValueError: If no file is attached or the file type is unsupported.
    """
    body = await _post_graphql(
        ITEM_CV_FILE_QUERY,
        {"ids": [item_id], "columnId": FILE_COLUMN_ID},
    )
    items = body.get("data", {}).get("items") or []
    if not items:
        raise ValueError(f"Monday item {item_id} not found.")

    column_values = items[0].get("column_values") or []
    if not column_values:
        raise ValueError(f"No CV file on Monday item {item_id}.")

    files = column_values[0].get("files") or []
    if not files:
        raise ValueError(f"No CV file on Monday item {item_id}.")

    file_entry = files[-1]
    name = str(file_entry.get("name") or "").strip()
    asset = file_entry.get("asset") or {}
    public_url = str(asset.get("public_url") or "").strip()
    extension = str(asset.get("file_extension") or "").strip().lower()

    if not public_url:
        raise ValueError(f"CV file on item {item_id} has no public_url.")

    if not name and extension:
        name = f"cv.{extension}"

    if not name:
        raise ValueError(f"CV file on item {item_id} has no filename.")

    suffix = Path(name).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        if extension in {"pdf", "docx"}:
            name = f"{Path(name).stem}.{extension}" if Path(name).stem else f"cv.{extension}"
            suffix = f".{extension}"
        else:
            raise ValueError(
                f"Unsupported CV file type on item {item_id}: {name!r} (extension {extension!r})"
            )

    logger.info("Fetched CV file URL for item %s: %s", item_id, name)
    return public_url, name


def _cv_mime_type(file_path: str) -> str:
    mime, _ = mimetypes.guess_type(file_path)
    if mime:
        return mime
    suffix = Path(file_path).suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if suffix == ".doc":
        return "application/msword"
    return "application/octet-stream"


def upload_file_to_item(item_id: str, file_path: str) -> dict:
    """
    Attach a CV file to a Monday item via POST https://api.monday.com/v2/file.

    Uses multipart/form-data with the ``add_file_to_column`` mutation (see FILE_COLUMN_ID).
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"CV file not found at {file_path}")

    api_key = _get_api_key()
    column_id = FILE_COLUMN_ID
    query = (
        "mutation ($file: File!) { "
        "add_file_to_column ("
        f"item_id: {item_id}, "
        f'column_id: "{column_id}", '
        "file: $file"
        ") { id } }"
    )
    mime_type = _cv_mime_type(file_path)
    headers = {
        "Authorization": api_key,
        "API-Version": MONDAY_FILE_API_VERSION,
    }
    multipart_data = {
        "query": query,
        "map": json.dumps({"file": "variables.file"}),
    }

    with path.open("rb") as file_handle:
        response = requests.post(
            MONDAY_FILE_API_URL,
            headers=headers,
            data=multipart_data,
            files={"file": (path.name, file_handle, mime_type)},
            timeout=120,
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise Exception(
            f"Monday.com file upload HTTP error {response.status_code}: {response.text}"
        ) from exc

    try:
        response_json = response.json()
    except json.JSONDecodeError as exc:
        raise Exception(
            f"Monday.com file upload returned non-JSON body: {response.text[:500]}"
        ) from exc

    print(
        "Monday file upload response JSON:",
        json.dumps(response_json, ensure_ascii=False, default=str),
    )

    if response_json.get("errors"):
        print(f"❌ Monday File Upload Error: {response_json['errors']}")
        _print_monday_errors(response_json["errors"], column_ids=[FILE_COLUMN_ID])
        raise Exception(f"Monday File Upload Error: {response_json['errors']}")

    if response_json.get("error_message"):
        err_data = response_json.get("error_data") or {}
        print(f"❌ Monday File Upload Error: {response_json['error_message']}")
        raise Exception(
            f"Monday File Upload Error: {response_json['error_message']} "
            f"(column_id={err_data.get('column_id', column_id)})"
        )

    if not response_json.get("data"):
        raise Exception(
            f"Monday File Upload Error: unexpected response shape: {response_json}"
        )

    print("📎 File attached successfully to Monday item for candidate.")
    logger.info(
        "Monday file upload: item %s column %s file %s",
        item_id,
        FILE_COLUMN_ID,
        path.name,
    )
    return response_json
