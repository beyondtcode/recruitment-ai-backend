from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class CrmSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    monday_crm_active_clients_board_id: str
    monday_crm_active_clients_email_column_id: str
    monday_crm_leads_board_id: str
    monday_crm_leads_email_column_id: str
    monday_crm_meeting_notes_board_id: str
    monday_crm_meeting_notes_group_id: str = "topics"
    monday_crm_meeting_date_column_id: str
    monday_crm_meeting_client_relation_column_id: str
    monday_crm_meeting_lead_relation_column_id: str
    monday_crm_meeting_doc_column_id: str
    monday_crm_meeting_summary_column_id: str
    monday_crm_meeting_external_participants_column_id: str = "text_mm4dmn71"
    monday_crm_meeting_action_items_column_id: str = "long_text_mm4dh8vv"
    monday_crm_meeting_type_column_id: str = "dropdown_mm4dpky"
    monday_crm_meeting_people_column_id: str = "multiple_person_mm4de6qm"
    future_meetings_board_id: str = "5098793829"
    future_meetings_date_column_id: str = "date4"
    future_meetings_status_column_id: str = "status"
    future_meetings_participants_column_id: str = "text_mm4e3rd9"
    future_meetings_brief_column_id: str = "text_mm4eda8z"
    batch_secret: str = ""
    monday_notetaker_api_keys: str = ""


@lru_cache
def get_crm_settings() -> CrmSettings:
    return CrmSettings()


def get_notetaker_api_keys(settings: CrmSettings | None = None) -> list[str]:
    """
    API keys used to pull Notetaker meetings.

    Monday scopes Notetaker results to the authenticated user, so provide one token
    per internal team member (comma-separated) to cover organization-wide meetings.
    Falls back to ``MONDAY_API_KEY`` when unset.
    """
    from services.monday_service import _get_api_key

    settings = settings or get_crm_settings()
    raw = settings.monday_notetaker_api_keys.strip()
    if raw:
        keys = [part.strip() for part in raw.split(",") if part.strip()]
        if keys:
            return keys
    return [_get_api_key()]
