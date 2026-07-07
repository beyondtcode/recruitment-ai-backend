from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any, Literal
from crm_integration.config import CrmSettings, get_crm_settings
from crm_integration.lookup import ContactMatch
from crm_integration.monday_client import (
    CREATE_ITEM_MUTATION,
    CREATE_SUBITEM_MUTATION,
    FIND_ITEMS_LIMIT,
    ITEMS_PAGE_BY_COLUMN_VALUES_QUERY,
    ITEMS_PAGE_WITH_COLUMNS_QUERY,
    ITEM_SUBITEMS_WITH_COLUMNS_QUERY,
    USERS_BY_EMAILS_QUERY,
    execute_graphql,
)
from crm_integration.schemas import NodeTakerWebhookPayload
from services.monday_service import get_mirly_reminder_by_title

logger = logging.getLogger(__name__)

MeetingTypeLabel = Literal[
    "מעקב",
    "מצגת",
    "משא מתן",
    "סגירה",
    "פגישת היכרות",
    "פגישת לקוח",
]

BoardKind = Literal["customer", "company", "subitem"]
ColumnTarget = Literal["board", "subitem"]

INTERNAL_EMAIL_DOMAIN = "@beyondtcode.com"

# Checked in order; first match wins. Default is "מעקב".
MEETING_TYPE_RULES: tuple[tuple[MeetingTypeLabel, tuple[str, ...]], ...] = (
    (
        "סגירה",
        (
            "סגירה",
            "closing",
            "חתימה",
            "sign",
            "חוזה",
            "contract",
        ),
    ),
    (
        "משא מתן",
        (
            'מו"מ',
            "מו״מ",
            "מומ",
            "משא מתן",
            "negotiation",
            "הצעת מחיר",
            "proposal",
            "תנאים",
        ),
    ),
    (
        "מצגת",
        (
            "demo",
            "דמו",
            "מצגת",
            "presentation",
            "pitch",
        ),
    ),
    (
        "פגישת היכרות",
        (
            "intro",
            "היכרות",
            "ראיון",
            "interview",
            "מועמד",
            "candidate",
            "first meeting",
        ),
    ),
    (
        "פגישת לקוח",
        (
            "לקוח",
            "client",
            "customer",
            "qbr",
        ),
    ),
)

DEFAULT_MEETING_TYPE: MeetingTypeLabel = "מעקב"

SUBITEM_MEETING_TYPE_INDEX: dict[MeetingTypeLabel, int] = {
    "פגישת היכרות": 1,
    "סגירה": 4,
    "מצגת": 2,
    "משא מתן": 2,
    "פגישת לקוח": 3,
    "מעקב": 3,
}
MEETING_SUMMARY_DECISIONS_MARKERS = (
    "### החלטות ותוצאות",
    "### Decisions",
)


def extract_meeting_summary_intro(summary: str) -> str:
    """Return only the introductory section before decisions/outcomes."""
    text = summary.strip()
    if not text:
        return ""

    split_at = len(text)
    for marker in MEETING_SUMMARY_DECISIONS_MARKERS:
        index = text.find(marker)
        if index != -1 and index < split_at:
            split_at = index

    return text[:split_at].rstrip()


def external_participant_emails(participant_emails: list[str]) -> list[str]:
    """Return participant emails that are not internal @beyondtcode.com addresses."""
    return [
        email
        for email in participant_emails
        if email and not email.strip().lower().endswith(INTERNAL_EMAIL_DOMAIN)
    ]


def is_internal_only_meeting(participant_emails: list[str]) -> bool:
    """Return True when every participant is an internal @beyondtcode.com address."""
    emails = [email.strip() for email in participant_emails if email and email.strip()]
    return bool(emails) and not external_participant_emails(emails)


def meeting_board_kind(payload: NodeTakerWebhookPayload) -> BoardKind:
    """Select the destination board for a meeting payload."""
    return "company" if is_internal_only_meeting(payload.participant_emails) else "customer"


def _meeting_target_board(settings: CrmSettings, board_kind: BoardKind) -> tuple[str, str]:
    if board_kind == "company":
        return (
            settings.monday_crm_company_meetings_board_id,
            settings.monday_crm_company_meetings_group_id,
        )
    if board_kind == "subitem":
        return settings.monday_crm_lead_subitems_board_id, ""
    return (
        settings.monday_crm_meeting_notes_board_id,
        settings.monday_crm_meeting_notes_group_id,
    )


def map_meeting_type_to_subitem_index(meeting_type: MeetingTypeLabel) -> int:
    """Map board classifier output to the Leads subitem meeting-type dropdown index."""
    return SUBITEM_MEETING_TYPE_INDEX.get(meeting_type, SUBITEM_MEETING_TYPE_INDEX["מעקב"])


def _column_id(settings: CrmSettings, field: str, target: ColumnTarget) -> str:
    """Resolve a logical field name to the Monday column ID for board or subitem targets."""
    if target == "subitem":
        mapping = {
            "date": settings.monday_crm_lead_subitem_date_column_id,
            "summary": settings.monday_crm_lead_subitem_summary_column_id,
            "action_items": settings.monday_crm_lead_subitem_action_items_column_id,
            "external_participants": settings.monday_crm_lead_subitem_external_participants_column_id,
            "people": settings.monday_crm_lead_subitem_people_column_id,
            "reminder_date": settings.monday_crm_lead_subitem_reminder_date_column_id,
            "reminder_info": settings.monday_crm_lead_subitem_reminder_info_column_id,
        }
    else:
        mapping = {
            "date": settings.monday_crm_meeting_date_column_id,
            "summary": settings.monday_crm_meeting_summary_column_id,
            "action_items": settings.monday_crm_meeting_action_items_column_id,
            "meeting_type": settings.monday_crm_meeting_type_column_id,
            "external_participants": settings.monday_crm_meeting_external_participants_column_id,
            "people": settings.monday_crm_meeting_people_column_id,
            "reminder_date": settings.meeting_notes_reminder_date_column_id,
            "reminder_info": settings.meeting_notes_reminder_info_column_id,
        }
    return mapping[field]


def parse_comma_separated_emails(raw: str) -> list[str]:
    """Parse a comma- or semicolon-separated email list from a Monday text column."""
    return [part.strip() for part in re.split(r"[,;]", raw) if part.strip()]


def status_column_index(column: dict[str, Any]) -> int | None:
    """Extract the status index from a Monday status column value."""
    value = column.get("value")
    if not value:
        return None
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    index = parsed.get("index")
    return int(index) if index is not None else None


def column_text(column: dict[str, Any]) -> str:
    """Return human-readable text from a Monday column value."""
    text = str(column.get("text") or "").strip()
    if text:
        return text

    value = column.get("value")
    if not value:
        return ""

    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return ""

    if isinstance(parsed, dict):
        for key in ("text", "date", "email"):
            field_value = parsed.get(key)
            if field_value:
                return str(field_value).strip()
    if isinstance(parsed, str):
        return parsed.strip()
    return ""


def date_column_value(column: dict[str, Any]) -> date | None:
    """Parse a Monday date column into a date object."""
    text = column_text(column)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _column_by_id(item: dict[str, Any], column_id: str) -> dict[str, Any] | None:
    for column in item.get("column_values") or []:
        if str(column.get("id")) == column_id:
            return column
    return None


async def gather_past_meeting_context(
    lead_item_id: str,
    *,
    before_date: date,
    settings: CrmSettings | None = None,
) -> str:
    """Collect formatted summaries from past meeting subitems under a lead."""
    settings = settings or get_crm_settings()
    if not lead_item_id.strip():
        return ""

    date_column_id = settings.monday_crm_lead_subitem_date_column_id
    summary_column_id = settings.monday_crm_lead_subitem_summary_column_id

    body = await execute_graphql(
        ITEM_SUBITEMS_WITH_COLUMNS_QUERY,
        {
            "itemId": lead_item_id,
            "columnIds": [date_column_id, summary_column_id],
        },
        column_ids=[date_column_id, summary_column_id],
    )
    items = body.get("data", {}).get("items") or []
    if not items:
        return ""

    subitems = items[0].get("subitems") or []
    past_meetings: list[tuple[date, str, str]] = []
    for subitem in subitems:
        date_column = _column_by_id(subitem, date_column_id)
        summary_column = _column_by_id(subitem, summary_column_id)
        meeting_date = date_column_value(date_column or {})
        if meeting_date is None or meeting_date >= before_date:
            continue

        summary = extract_meeting_summary_intro(column_text(summary_column or {}))
        if not summary:
            continue

        title = str(subitem.get("name") or "").strip() or "פגישה ללא שם"
        past_meetings.append((meeting_date, title, summary))

    if not past_meetings:
        return ""

    past_meetings.sort(key=lambda entry: entry[0], reverse=True)
    sections = [
        f"### {title} ({meeting_date.isoformat()})\n{summary}"
        for meeting_date, title, summary in past_meetings
    ]
    return "\n\n".join(sections)


def build_meeting_logs_for_profile(
    payload: NodeTakerWebhookPayload,
    past_context: str,
) -> str:
    """Assemble meeting logs for client profile AI (current meeting first, then history)."""
    title = payload.meeting_title.strip() or "פגישה ללא שם"
    summary = extract_meeting_summary_intro(payload.meeting_summary)
    sections: list[str] = [f"### {title} ({payload.meeting_date.isoformat()})\n{summary}"]

    action_items = payload.action_items.strip()
    if action_items:
        sections[-1] += f"\n\nAction Items:\n{action_items}"

    past = past_context.strip()
    if past:
        sections.append(past)

    return "\n\n".join(sections)


def internal_participant_emails(participant_emails: list[str]) -> list[str]:
    """Return participant emails that belong to internal @beyondtcode.com addresses."""
    return [
        email.strip()
        for email in participant_emails
        if email and email.strip().lower().endswith(INTERNAL_EMAIL_DOMAIN)
    ]


async def _find_lead_item_by_name(
    board_id: str,
    name: str,
) -> str | None:
    search_name = name.strip()
    if not search_name:
        return None

    body = await execute_graphql(
        ITEMS_PAGE_WITH_COLUMNS_QUERY,
        {
            "boardId": board_id,
            "limit": FIND_ITEMS_LIMIT,
            "columnIds": [],
            "queryParams": {
                "rules": [
                    {
                        "column_id": "name",
                        "compare_value": [search_name],
                        "operator": "contains_text",
                    }
                ],
            },
        },
    )
    boards = body.get("data", {}).get("boards") or []
    items: list[dict[str, Any]] = []
    if boards:
        items = (boards[0].get("items_page") or {}).get("items") or []

    name_key = search_name.casefold()
    for item in items:
        if str(item.get("name") or "").strip().casefold() == name_key:
            return str(item["id"])
    if items and items[0].get("id") is not None:
        return str(items[0]["id"])
    return None


async def resolve_beyondcode_client_match(
    settings: CrmSettings | None = None,
) -> ContactMatch:
    """Return the BeyondCode lead item used for company meeting board relations."""
    settings = settings or get_crm_settings()
    item_id = settings.beyondcode_company_client_item_id.strip()
    if not item_id:
        item_id = (
            await _find_lead_item_by_name(
                settings.monday_crm_leads_board_id,
                settings.beyondcode_company_client_name,
            )
            or ""
        )
    if not item_id:
        logger.warning(
            "BeyondCode lead item not found for company meeting relation (name=%r)",
            settings.beyondcode_company_client_name,
        )
    return ContactMatch(
        item_id=item_id,
        match_type="lead",
        matched_email="",
    )


def classify_meeting_type(title: str, summary: str) -> MeetingTypeLabel:
    """Classify a meeting based on title and summary text."""
    text = f"{title} {summary}".casefold()
    normalized = re.sub(r"\s+", " ", text)

    for label, keywords in MEETING_TYPE_RULES:
        if any(keyword.casefold() in normalized for keyword in keywords):
            return label
    return DEFAULT_MEETING_TYPE


def _plain_text_column_value(text: str) -> str:
    return text


def _long_text_column_value(text: str) -> dict[str, str]:
    return {"text": text}


def _dropdown_column_value(label: str) -> dict[str, list[str]]:
    return {"labels": [label]}


def _dropdown_index_column_value(index: int) -> dict[str, int]:
    return {"index": index}


def _status_index_column_value(index: int) -> dict[str, int]:
    return {"index": index}


def _date_column_value(meeting_date: date) -> dict[str, str]:
    return {"date": meeting_date.isoformat()}


def _board_relation_column_value(item_id: str) -> dict[str, list[int]]:
    return {"item_ids": [int(item_id)]}


def _people_column_value(user_ids: list[str]) -> dict[str, list[dict[str, str | int]]]:
    return {
        "personsAndTeams": [
            {"id": int(user_id), "kind": "person"}
            for user_id in user_ids
        ]
    }


async def resolve_monday_user_ids_by_emails(emails: list[str]) -> list[str]:
    """Map email addresses to Monday.com user IDs via the users(emails: ...) query."""
    normalized = [email.strip().lower() for email in emails if email and email.strip()]
    if not normalized:
        return []

    body = await execute_graphql(
        USERS_BY_EMAILS_QUERY,
        {"emails": normalized},
    )
    users = body.get("data", {}).get("users") or []

    email_to_id: dict[str, str] = {}
    for user in users:
        user_email = str(user.get("email") or "").strip().lower()
        user_id = user.get("id")
        if user_email and user_id is not None:
            email_to_id[user_email] = str(user_id)

    resolved: list[str] = []
    for email in normalized:
        user_id = email_to_id.get(email)
        if user_id:
            resolved.append(user_id)
        else:
            logger.warning("No Monday user found for internal email: %s", email)

    return resolved


def parse_board_relation_lead_id(column: dict[str, Any] | None) -> str | None:
    """Extract the first linked lead item ID from a board-relation column value."""
    if not column:
        return None

    linked_item_ids = column.get("linked_item_ids")
    if linked_item_ids:
        first_id = linked_item_ids[0]
        return str(first_id) if first_id is not None else None

    value = column.get("value")
    if not value:
        return None

    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(parsed, dict):
        return None

    linked_ids = parsed.get("linkedPulseIds") or parsed.get("item_ids") or []
    if not linked_ids:
        return None

    first_id = linked_ids[0]
    return str(first_id) if first_id is not None else None


def parse_meeting_type_label(text: str) -> MeetingTypeLabel | None:
    """Return a known meeting type label when legacy dropdown text matches exactly."""
    normalized = text.strip()
    if normalized in SUBITEM_MEETING_TYPE_INDEX:
        return normalized  # type: ignore[return-value]
    return None


def resolve_meeting_type_label(
    legacy_type_text: str,
    title: str,
    summary: str,
) -> MeetingTypeLabel:
    """Prefer legacy dropdown text; fall back to title/summary classification."""
    parsed = parse_meeting_type_label(legacy_type_text)
    if parsed:
        return parsed
    return classify_meeting_type(title, summary)


def _build_column_values(
    payload: NodeTakerWebhookPayload,
    match: ContactMatch | None,
    settings: CrmSettings,
    *,
    internal_user_ids: list[str] | None = None,
    board_kind: BoardKind = "customer",
    target: ColumnTarget = "board",
    meeting_type_override: MeetingTypeLabel | None = None,
) -> dict[str, Any]:
    date_col = _column_id(settings, "date", target)
    column_values: dict[str, Any] = {
        date_col: _date_column_value(payload.meeting_date),
    }

    if target != "subitem":
        meeting_type = meeting_type_override or classify_meeting_type(
            payload.meeting_title, payload.meeting_summary
        )
        if board_kind == "customer":
            column_values[_column_id(settings, "meeting_type", target)] = _dropdown_column_value(
                meeting_type
            )

    overview = extract_meeting_summary_intro(payload.meeting_summary)
    if overview:
        column_values[_column_id(settings, "summary", target)] = _long_text_column_value(overview)

    external_emails = external_participant_emails([str(email) for email in payload.participant_emails])
    if external_emails:
        column_values[_column_id(settings, "external_participants", target)] = (
            _plain_text_column_value(", ".join(external_emails))
        )

    action_items = payload.action_items.strip()
    if action_items:
        column_values[_column_id(settings, "action_items", target)] = _long_text_column_value(
            action_items
        )

    if target == "board" and match and match.item_id and settings.monday_crm_meeting_lead_relation_column_id:
        column_values[settings.monday_crm_meeting_lead_relation_column_id] = (
            _board_relation_column_value(match.item_id)
        )

    if internal_user_ids:
        column_values[_column_id(settings, "people", target)] = _people_column_value(
            internal_user_ids
        )

    return column_values


def _inject_mirly_reminder_columns(
    column_values: dict[str, Any],
    reminder: dict[str, str] | None,
    settings: CrmSettings,
    *,
    target: ColumnTarget,
) -> None:
    if not reminder:
        return
    if reminder.get("date"):
        column_values[_column_id(settings, "reminder_date", target)] = _date_column_value(
            date.fromisoformat(reminder["date"])
        )
    if reminder.get("info"):
        column_values[_column_id(settings, "reminder_info", target)] = _long_text_column_value(
            reminder["info"]
        )


async def meeting_already_exists(
    payload: NodeTakerWebhookPayload,
    settings: CrmSettings | None = None,
) -> bool:
    """Return True if a company-meeting board item with the same title and date already exists."""
    settings = settings or get_crm_settings()
    if meeting_board_kind(payload) != "company":
        return False

    title = payload.meeting_title.strip()
    board_id, _ = _meeting_target_board(settings, "company")

    body = await execute_graphql(
        ITEMS_PAGE_BY_COLUMN_VALUES_QUERY,
        {
            "boardId": board_id,
            "limit": FIND_ITEMS_LIMIT,
            "columns": [
                {
                    "column_id": settings.monday_crm_meeting_date_column_id,
                    "column_values": [payload.meeting_date.isoformat()],
                }
            ],
        },
        column_ids=[settings.monday_crm_meeting_date_column_id],
    )
    items = body.get("data", {}).get("items_page_by_column_values", {}).get("items") or []
    for item in items:
        if str(item.get("name") or "").strip() == title:
            logger.info(
                "Meeting already exists on board %s: title=%r date=%s item_id=%s",
                board_id,
                title,
                payload.meeting_date.isoformat(),
                item.get("id"),
            )
            return True
    return False


async def find_existing_meeting_subitem_id(
    payload: NodeTakerWebhookPayload,
    lead_item_id: str,
    settings: CrmSettings | None = None,
) -> str | None:
    """Return the subitem ID when a meeting with the same title and date exists under the lead."""
    settings = settings or get_crm_settings()
    title = payload.meeting_title.strip()
    date_column_id = settings.monday_crm_lead_subitem_date_column_id

    body = await execute_graphql(
        ITEM_SUBITEMS_WITH_COLUMNS_QUERY,
        {
            "itemId": lead_item_id,
            "columnIds": [date_column_id],
        },
        column_ids=[date_column_id],
    )
    items = body.get("data", {}).get("items") or []
    if not items:
        return None

    for subitem in items[0].get("subitems") or []:
        if str(subitem.get("name") or "").strip() != title:
            continue
        date_column = _column_by_id(subitem, date_column_id)
        meeting_date = date_column_value(date_column or {})
        if meeting_date == payload.meeting_date:
            subitem_id = subitem.get("id")
            return str(subitem_id) if subitem_id is not None else None
    return None


async def meeting_subitem_already_exists(
    payload: NodeTakerWebhookPayload,
    lead_item_id: str,
    settings: CrmSettings | None = None,
) -> bool:
    """Return True if a meeting subitem with the same title and date already exists under the lead."""
    existing_id = await find_existing_meeting_subitem_id(
        payload,
        lead_item_id,
        settings=settings,
    )
    if existing_id:
        logger.info(
            "Meeting subitem already exists under lead %s: title=%r date=%s subitem_id=%s",
            lead_item_id,
            payload.meeting_title.strip(),
            payload.meeting_date.isoformat(),
            existing_id,
        )
        return True
    return False


async def _verify_subitem_nested(parent_item_id: str, subitem_id: str) -> None:
    """Confirm that Monday nested the subitem under the parent lead."""
    body = await execute_graphql(
        ITEM_SUBITEMS_WITH_COLUMNS_QUERY,
        {
            "itemId": parent_item_id,
            "columnIds": [],
        },
        column_ids=[],
    )
    items = body.get("data", {}).get("items") or []
    if not items:
        logger.error(
            "Subitem nesting verification failed: parent lead %s not found when verifying subitem %s",
            parent_item_id,
            subitem_id,
        )
        raise RuntimeError(
            f"Subitem {subitem_id} was created but parent lead {parent_item_id} could not be verified"
        )

    nested_ids = [
        str(subitem.get("id"))
        for subitem in items[0].get("subitems") or []
        if subitem.get("id") is not None
    ]
    if subitem_id not in nested_ids:
        logger.error(
            "Subitem nesting verification failed: subitem %s is not nested under lead %s; observed_subitems=%s",
            subitem_id,
            parent_item_id,
            nested_ids,
        )
        raise RuntimeError(
            f"Subitem {subitem_id} was created but is not nested under lead {parent_item_id}"
        )

    logger.info(
        "Verified meeting subitem %s is nested under lead %s",
        subitem_id,
        parent_item_id,
    )


async def create_meeting_subitem(
    payload: NodeTakerWebhookPayload,
    match: ContactMatch,
    settings: CrmSettings | None = None,
    *,
    meeting_type_override: MeetingTypeLabel | None = None,
    reminder: dict[str, str] | None = None,
    fetch_mirly_reminder: bool = True,
) -> str:
    """Create a meeting summary subitem under the matched lead and return the new subitem ID."""
    settings = settings or get_crm_settings()
    if not match.item_id:
        raise ValueError("Lead item_id is required to create a meeting subitem")

    internal_emails = internal_participant_emails(
        [str(email) for email in payload.participant_emails]
    )
    internal_user_ids = (
        await resolve_monday_user_ids_by_emails(internal_emails) if internal_emails else []
    )
    column_values = _build_column_values(
        payload,
        match,
        settings,
        internal_user_ids=internal_user_ids,
        target="subitem",
        meeting_type_override=meeting_type_override,
    )

    if fetch_mirly_reminder and reminder is None:
        reminder = await get_mirly_reminder_by_title(payload.meeting_title)
    _inject_mirly_reminder_columns(column_values, reminder, settings, target="subitem")

    item_name = payload.meeting_title.strip()
    mutation_variables = {
        "parentItemId": match.item_id,
        "itemName": item_name,
        "columnValues": json.dumps(column_values),
    }
    logger.info(
        "Creating meeting subitem via create_subitem: parent_item_id=%s item_name=%r column_keys=%s",
        match.item_id,
        item_name,
        list(column_values.keys()),
    )

    body = await execute_graphql(
        CREATE_SUBITEM_MUTATION,
        mutation_variables,
        column_ids=list(column_values.keys()),
    )
    logger.info(
        "Monday create_subitem response: parent_item_id=%s data=%s errors=%s",
        match.item_id,
        body.get("data"),
        body.get("errors"),
    )

    subitem_id = body.get("data", {}).get("create_subitem", {}).get("id")
    if not subitem_id:
        raise RuntimeError("Monday create_subitem did not return an item id")

    subitem_id = str(subitem_id)
    await _verify_subitem_nested(match.item_id, subitem_id)
    logger.info(
        "Meeting subitem created with ID %s under lead %s",
        subitem_id,
        match.item_id,
    )
    return subitem_id


async def create_meeting_item(
    payload: NodeTakerWebhookPayload,
    match: ContactMatch | None,
    settings: CrmSettings | None = None,
    *,
    board_kind: BoardKind = "customer",
) -> str:
    """Create a meeting board item and return the new item ID."""
    settings = settings or get_crm_settings()
    board_id, group_id = _meeting_target_board(settings, board_kind)
    internal_emails = internal_participant_emails(
        [str(email) for email in payload.participant_emails]
    )
    internal_user_ids = (
        await resolve_monday_user_ids_by_emails(internal_emails) if internal_emails else []
    )
    column_values = _build_column_values(
        payload,
        match,
        settings,
        internal_user_ids=internal_user_ids,
        board_kind=board_kind,
    )

    reminder = await get_mirly_reminder_by_title(payload.meeting_title)
    _inject_mirly_reminder_columns(column_values, reminder, settings, target="board")

    if not match or not match.item_id:
        logger.warning(
            "No contact match; meeting item will be created without board relation"
        )

    body = await execute_graphql(
        CREATE_ITEM_MUTATION,
        {
            "boardId": board_id,
            "groupId": group_id,
            "itemName": payload.meeting_title.strip(),
            "columnValues": json.dumps(column_values),
        },
        column_ids=list(column_values.keys()),
    )
    item_id = body.get("data", {}).get("create_item", {}).get("id")
    if not item_id:
        raise RuntimeError("Monday create_item did not return an item id")

    item_id = str(item_id)
    logger.info("Meeting item created with ID %s on board %s", item_id, board_id)
    return item_id
