from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Hardcoded Monday column ID for the rolling meeting analysis Workdoc on lead rows.
MEETING_ANALYSIS_DOC_COLUMN_ID = "doc_mm4wb1bc"


class CrmSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    monday_crm_leads_board_id: str
    monday_crm_leads_email_column_id: str
    monday_crm_meeting_notes_board_id: str
    monday_crm_meeting_notes_group_id: str = "topics"
    monday_crm_company_meetings_board_id: str = "5099503871"
    monday_crm_company_meetings_group_id: str = "topics"
    beyondcode_company_client_item_id: str = "3018755375"
    beyondcode_company_client_name: str = 'ביונד קוד בע"מ'
    monday_crm_meeting_date_column_id: str
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
    mirly_reminders_board_id: str = "5099196766"
    mirly_reminders_date_column_id: str = "date4"
    mirly_reminders_info_column_id: str = "long_text_mm4nf3z8"
    mirly_reminders_creation_log_column_id: str = "pulse_log_mm4nf1xc"
    meeting_notes_reminder_date_column_id: str = "date_mm4nd4dm"
    meeting_notes_reminder_info_column_id: str = "long_text_mm4ncaga"
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
