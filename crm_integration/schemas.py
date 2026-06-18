from __future__ import annotations

from datetime import date

from pydantic import BaseModel, EmailStr, Field


class NodeTakerWebhookPayload(BaseModel):
    meeting_title: str = Field(..., min_length=1)
    meeting_date: date
    participant_emails: list[EmailStr] = Field(default_factory=list)
    meeting_summary: str = ""
    action_items: str = ""


class NodeTakerWebhookResult(BaseModel):
    status: str
    meeting_item_id: str | None = None
    match_type: str | None = None
    matched_email: str | None = None
    doc_id: str | None = None
    doc_created: bool = False
    warnings: list[str] = Field(default_factory=list)
