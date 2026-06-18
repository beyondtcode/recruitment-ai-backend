"""Tests for the NodeTaker CRM webhook and Monday integration helpers."""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import app
from crm_integration.batch import process_morning_briefs
from crm_integration.config import CrmSettings
from crm_integration.lookup import ContactMatch, find_contact_by_emails
from crm_integration.meeting import (
    _build_column_values,
    _people_column_value,
    classify_meeting_type,
    column_text,
    extract_meeting_summary_intro,
    external_participant_emails,
    internal_participant_emails,
    meeting_already_exists,
    parse_comma_separated_emails,
    resolve_monday_user_ids_by_emails,
    status_column_index,
)
from crm_integration.monday_fetcher import (
    _format_action_items,
    _meeting_matches_participants,
    fetch_meeting_by_participants,
    meeting_to_payload,
)
from crm_integration.schemas import NodeTakerWebhookPayload
from crm_integration.workdoc import build_meeting_details_markdown, build_meeting_doc_blocks

client = TestClient(app)

TEST_CRM_SETTINGS = CrmSettings(
    monday_crm_active_clients_board_id="5098750813",
    monday_crm_active_clients_email_column_id="email_mm4d35ds",
    monday_crm_leads_board_id="5098750810",
    monday_crm_leads_email_column_id="email_mm4dy27s",
    monday_crm_meeting_notes_board_id="5098750811",
    monday_crm_meeting_notes_group_id="topics",
    monday_crm_meeting_date_column_id="date_mm4dk1jc",
    monday_crm_meeting_client_relation_column_id="board_relation_mm4der92",
    monday_crm_meeting_lead_relation_column_id="board_relation_mm4dbkv3",
    monday_crm_meeting_doc_column_id="doc_mm4dvm4h",
    monday_crm_meeting_summary_column_id="long_text_mm4dd982",
    monday_crm_meeting_external_participants_column_id="text_mm4dmn71",
    monday_crm_meeting_action_items_column_id="long_text_mm4dh8vv",
    monday_crm_meeting_type_column_id="dropdown_mm4dpky",
    monday_crm_meeting_people_column_id="multiple_person_mm4de6qm",
    future_meetings_board_id="5098793829",
    future_meetings_date_column_id="date4",
    future_meetings_status_column_id="status",
    future_meetings_participants_column_id="text_mm4dmn71",
    future_meetings_brief_column_id="text_mm4eda8z",
)

VALID_PAYLOAD = {
    "meeting_title": "Q1 Planning",
    "meeting_date": "2026-06-17",
    "participant_emails": ["client@example.com", "colleague@example.com"],
    "meeting_summary": "Discussed roadmap priorities.",
    "action_items": "- Send proposal\n- Schedule follow-up",
}


class MeetingColumnMappingTests(unittest.TestCase):
    def test_external_participant_emails_excludes_internal_domain(self):
        emails = external_participant_emails(
            ["dev@beyondtcode.com", "client@example.com", "saramauda06@gmail.com"]
        )
        self.assertEqual(emails, ["client@example.com", "saramauda06@gmail.com"])

    def test_internal_participant_emails_includes_only_internal_domain(self):
        emails = internal_participant_emails(
            ["dev@beyondtcode.com", "client@example.com", "  Admin@BeyondTCode.com  "]
        )
        self.assertEqual(emails, ["dev@beyondtcode.com", "Admin@BeyondTCode.com"])

    def test_people_column_value_format(self):
        self.assertEqual(
            _people_column_value(["12345", "67890"]),
            {
                "personsAndTeams": [
                    {"id": 12345, "kind": "person"},
                    {"id": 67890, "kind": "person"},
                ]
            },
        )

    def test_classify_meeting_type_intro(self):
        self.assertEqual(
            classify_meeting_type("Technical Interview", "Discussed candidate experience"),
            "פגישת היכרות",
        )

    def test_classify_meeting_type_presentation(self):
        self.assertEqual(
            classify_meeting_type("Product Demo", "Walked through the platform"),
            "מצגת",
        )

    def test_classify_meeting_type_negotiation(self):
        self.assertEqual(
            classify_meeting_type("Pricing Call", "Reviewed הצעת מחיר and תנאים"),
            "משא מתן",
        )

    def test_classify_meeting_type_closing(self):
        self.assertEqual(
            classify_meeting_type("Final Steps", "Discussed חוזה and signing"),
            "סגירה",
        )

    def test_classify_meeting_type_client(self):
        self.assertEqual(
            classify_meeting_type("Weekly Sync", "Reviewed client onboarding status"),
            "פגישת לקוח",
        )

    def test_classify_meeting_type_default_followup(self):
        self.assertEqual(
            classify_meeting_type("Team Standup", "Discussed sprint progress"),
            "מעקב",
        )

    def test_extract_meeting_summary_intro_splits_before_decisions_hebrew(self):
        summary = (
            "סיכום כללי של הפגישה.\n\n"
            "### החלטות ותוצאות\n\n"
            "- החלטה ראשונה"
        )
        self.assertEqual(
            extract_meeting_summary_intro(summary),
            "סיכום כללי של הפגישה.",
        )

    def test_extract_meeting_summary_intro_splits_before_decisions_english(self):
        summary = (
            "General meeting overview.\n\n"
            "### Decisions\n\n"
            "- First decision"
        )
        self.assertEqual(
            extract_meeting_summary_intro(summary),
            "General meeting overview.",
        )

    def test_extract_meeting_summary_intro_returns_full_text_without_marker(self):
        summary = "Short summary with no decisions section."
        self.assertEqual(extract_meeting_summary_intro(summary), summary)

    def test_build_column_values_summary_column_contains_intro_only(self):
        payload = NodeTakerWebhookPayload.model_validate(
            {
                "meeting_title": "Client Kickoff",
                "meeting_date": "2026-06-17",
                "participant_emails": ["client@example.com"],
                "meeting_summary": (
                    "Overview of the kickoff.\n\n"
                    "### Decisions\n\n"
                    "- Approve budget"
                ),
                "action_items": "- Send proposal",
            }
        )
        column_values = _build_column_values(payload, None, TEST_CRM_SETTINGS)

        self.assertEqual(
            column_values["long_text_mm4dd982"],
            {"text": "Overview of the kickoff."},
        )

    def test_build_column_values_includes_external_action_items_and_type(self):
        payload = NodeTakerWebhookPayload.model_validate(
            {
                "meeting_title": "Client Kickoff",
                "meeting_date": "2026-06-17",
                "participant_emails": ["dev@beyondtcode.com", "client@example.com"],
                "meeting_summary": "Discussed client roadmap.",
                "action_items": "- Send proposal",
            }
        )
        column_values = _build_column_values(payload, None, TEST_CRM_SETTINGS)

        self.assertEqual(
            column_values["text_mm4dmn71"],
            "client@example.com",
        )
        self.assertEqual(
            column_values["long_text_mm4dh8vv"],
            {"text": "- Send proposal"},
        )
        self.assertEqual(
            column_values["dropdown_mm4dpky"],
            {"labels": ["פגישת לקוח"]},
        )

    def test_build_column_values_includes_people_when_internal_user_ids_provided(self):
        payload = NodeTakerWebhookPayload.model_validate(
            {
                "meeting_title": "Internal Sync",
                "meeting_date": "2026-06-17",
                "participant_emails": ["dev@beyondtcode.com", "client@example.com"],
                "meeting_summary": "Team sync.",
                "action_items": "",
            }
        )
        column_values = _build_column_values(
            payload,
            None,
            TEST_CRM_SETTINGS,
            internal_user_ids=["111", "222"],
        )

        self.assertEqual(
            column_values["multiple_person_mm4de6qm"],
            {
                "personsAndTeams": [
                    {"id": 111, "kind": "person"},
                    {"id": 222, "kind": "person"},
                ]
            },
        )

    def test_build_column_values_omits_people_when_no_internal_users(self):
        payload = NodeTakerWebhookPayload.model_validate(
            {
                "meeting_title": "Client Only",
                "meeting_date": "2026-06-17",
                "participant_emails": ["client@example.com"],
                "meeting_summary": "External meeting.",
                "action_items": "",
            }
        )
        column_values = _build_column_values(payload, None, TEST_CRM_SETTINGS)

        self.assertNotIn("multiple_person_mm4de6qm", column_values)


class ResolveMondayUserIdsTests(unittest.IsolatedAsyncioTestCase):
    @patch("crm_integration.meeting.execute_graphql", new_callable=AsyncMock)
    async def test_maps_emails_to_user_ids(self, mock_graphql):
        mock_graphql.return_value = {
            "data": {
                "users": [
                    {"id": "111", "email": "dev@beyondtcode.com"},
                    {"id": "222", "email": "admin@beyondtcode.com"},
                ]
            }
        }

        user_ids = await resolve_monday_user_ids_by_emails(
            ["dev@beyondtcode.com", "admin@beyondtcode.com"]
        )

        self.assertEqual(user_ids, ["111", "222"])
        mock_graphql.assert_awaited_once()

    @patch("crm_integration.meeting.execute_graphql", new_callable=AsyncMock)
    async def test_returns_empty_list_for_no_emails(self, mock_graphql):
        user_ids = await resolve_monday_user_ids_by_emails([])

        self.assertEqual(user_ids, [])
        mock_graphql.assert_not_awaited()

    @patch("crm_integration.meeting.execute_graphql", new_callable=AsyncMock)
    async def test_skips_unresolved_emails(self, mock_graphql):
        mock_graphql.return_value = {
            "data": {
                "users": [{"id": "111", "email": "dev@beyondtcode.com"}],
            }
        }

        user_ids = await resolve_monday_user_ids_by_emails(
            ["dev@beyondtcode.com", "unknown@beyondtcode.com"]
        )

        self.assertEqual(user_ids, ["111"])


class ProcessRecentNotetakerMeetingsTests(unittest.IsolatedAsyncioTestCase):
    @patch("crm_integration.batch.process_nodetaker_webhook", new_callable=AsyncMock)
    @patch("crm_integration.batch.meeting_already_exists", new_callable=AsyncMock)
    @patch("crm_integration.batch.fetch_notetaker_meetings_since", new_callable=AsyncMock)
    async def test_skips_existing_and_processes_new_meetings(
        self,
        mock_fetch,
        mock_exists,
        mock_process,
    ):
        from crm_integration.batch import process_recent_notetaker_meetings
        from crm_integration.schemas import NodeTakerWebhookResult

        payload_new = NodeTakerWebhookPayload.model_validate(VALID_PAYLOAD)
        payload_existing = NodeTakerWebhookPayload.model_validate(
            {
                **VALID_PAYLOAD,
                "meeting_title": "Already Logged",
            }
        )
        mock_fetch.return_value = [payload_existing, payload_new]
        mock_exists.side_effect = [True, False]
        mock_process.return_value = NodeTakerWebhookResult(
            status="success",
            meeting_item_id="999",
            doc_created=True,
        )

        summary = await process_recent_notetaker_meetings(hours=24, settings=TEST_CRM_SETTINGS)

        self.assertEqual(summary["fetched"], 2)
        self.assertEqual(summary["processed_count"], 1)
        self.assertEqual(summary["skipped_count"], 1)
        mock_process.assert_awaited_once()


class MondayFetcherTests(unittest.TestCase):
    def test_meeting_matches_participants_on_same_date(self):
        meeting = {
            "start_time": "2026-06-17T14:30:00Z",
            "participants": [
                {"email": "dev@beyondtcode.com"},
                {"email": "saramauda06@gmail.com"},
            ],
        }
        self.assertTrue(
            _meeting_matches_participants(
                meeting,
                "dev@beyondtcode.com",
                "saramauda06@gmail.com",
                date(2026, 6, 17),
            )
        )

    def test_meeting_does_not_match_wrong_date(self):
        meeting = {
            "start_time": "2026-06-16T14:30:00Z",
            "participants": [
                {"email": "dev@beyondtcode.com"},
                {"email": "saramauda06@gmail.com"},
            ],
        }
        self.assertFalse(
            _meeting_matches_participants(
                meeting,
                "dev@beyondtcode.com",
                "saramauda06@gmail.com",
                date(2026, 6, 17),
            )
        )

    def test_meeting_to_payload_maps_summary_and_action_items(self):
        payload = meeting_to_payload(
            {
                "title": "Intro Call",
                "start_time": "2026-06-17T09:00:00+00:00",
                "summary": "Discussed hiring needs.",
                "participants": [
                    {"email": "dev@beyondtcode.com"},
                    {"email": "saramauda06@gmail.com"},
                ],
                "action_items": [
                    {"content": "Send CV", "owner": "Sarah", "due_date": "2026-06-20"},
                ],
            }
        )
        self.assertEqual(payload.meeting_title, "Intro Call")
        self.assertEqual(payload.meeting_date, date(2026, 6, 17))
        self.assertEqual(payload.meeting_summary, "Discussed hiring needs.")
        self.assertIn("Send CV", payload.action_items)
        self.assertIn("Sarah", payload.action_items)

    def test_format_action_items_skips_empty_entries(self):
        formatted = _format_action_items(
            [{"content": "Follow up"}, {"content": ""}, {"content": "Share notes"}]
        )
        self.assertEqual(formatted, "- Follow up\n- Share notes")


class FetchMeetingByParticipantsTests(unittest.IsolatedAsyncioTestCase):
    @patch("crm_integration.monday_fetcher._fetch_notetaker_meeting_page", new_callable=AsyncMock)
    async def test_returns_payload_when_match_found(self, mock_page):
        mock_page.return_value = (
            [
                {
                    "title": "Sarah Sync",
                    "start_time": "2026-06-17T10:00:00Z",
                    "summary": "Quick sync.",
                    "participants": [
                        {"email": "dev@beyondtcode.com"},
                        {"email": "saramauda06@gmail.com"},
                    ],
                    "action_items": [{"content": "Send recap"}],
                }
            ],
            None,
            False,
        )

        payload = await fetch_meeting_by_participants(
            "dev@beyondtcode.com",
            "saramauda06@gmail.com",
            date(2026, 6, 17),
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.meeting_title, "Sarah Sync")
        self.assertEqual(payload.meeting_summary, "Quick sync.")

    @patch("crm_integration.monday_fetcher._fetch_notetaker_meeting_page", new_callable=AsyncMock)
    async def test_returns_none_when_no_match(self, mock_page):
        mock_page.return_value = ([], None, False)

        payload = await fetch_meeting_by_participants(
            "dev@beyondtcode.com",
            "saramauda06@gmail.com",
            date(2026, 6, 17),
        )

        self.assertIsNone(payload)


class TestFetchSarahEndpointTests(unittest.TestCase):
    @patch("app.process_nodetaker_webhook", new_callable=AsyncMock)
    @patch("app.fetch_meeting_by_participants", new_callable=AsyncMock)
    def test_returns_not_found_when_meeting_missing(self, mock_fetch, mock_pipeline):
        mock_fetch.return_value = None

        response = client.get("/test-fetch-sarah")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "not_found")
        mock_pipeline.assert_not_called()

    @patch("app.process_nodetaker_webhook", new_callable=AsyncMock)
    @patch("app.fetch_meeting_by_participants", new_callable=AsyncMock)
    def test_runs_pipeline_when_meeting_found(self, mock_fetch, mock_pipeline):
        from crm_integration.schemas import NodeTakerWebhookPayload, NodeTakerWebhookResult

        mock_fetch.return_value = NodeTakerWebhookPayload.model_validate(VALID_PAYLOAD)
        mock_pipeline.return_value = NodeTakerWebhookResult(
            status="success",
            meeting_item_id="123",
            doc_created=True,
        )

        response = client.get("/test-fetch-sarah")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "processed")
        self.assertEqual(body["pipeline"]["meeting_item_id"], "123")
        mock_pipeline.assert_awaited_once()


class BuildMeetingDocBlocksTests(unittest.TestCase):
    def test_builds_title_date_participants_summary_and_action_item_blocks(self):
        payload = NodeTakerWebhookPayload.model_validate(VALID_PAYLOAD)
        blocks = build_meeting_doc_blocks(payload)
        block_types = [block.block_type for block in blocks]
        block_texts = [block.text for block in blocks]

        self.assertEqual(block_types[0], "large_title")
        self.assertEqual(block_texts[0], "Q1 Planning")
        self.assertIn("normal_text", block_types)
        self.assertIn("2026-06-17", "\n".join(block_texts))
        self.assertIn("Participants", block_texts)
        self.assertIn("client@example.com", block_texts)
        self.assertIn("Summary", block_texts)
        self.assertIn("Discussed roadmap priorities.", block_texts)
        self.assertIn("Action Items", block_texts)
        self.assertIn("Send proposal", block_texts)
        self.assertIn("bulleted_list", block_types)


class BuildMeetingDetailsMarkdownTests(unittest.TestCase):
    def test_includes_title_date_participants_summary_and_action_items(self):
        payload = NodeTakerWebhookPayload.model_validate(VALID_PAYLOAD)
        markdown = build_meeting_details_markdown(payload)

        self.assertIn("# Q1 Planning", markdown)
        self.assertIn("**Date:** 2026-06-17", markdown)
        self.assertIn("## Participants", markdown)
        self.assertIn("- client@example.com", markdown)
        self.assertIn("## Summary", markdown)
        self.assertIn("Discussed roadmap priorities.", markdown)
        self.assertIn("## Action Items", markdown)
        self.assertIn("- Send proposal", markdown)


class FindContactByEmailsTests(unittest.IsolatedAsyncioTestCase):
    @patch("crm_integration.lookup.execute_graphql", new_callable=AsyncMock)
    async def test_prioritizes_active_clients_over_leads(self, mock_graphql):
        mock_graphql.side_effect = [
            {
                "data": {
                    "items_page_by_column_values": {
                        "items": [{"id": "111", "name": "Acme Client"}],
                    }
                }
            },
        ]

        match = await find_contact_by_emails(
            ["client@example.com"],
            settings=TEST_CRM_SETTINGS,
        )

        self.assertEqual(
            match,
            ContactMatch(item_id="111", match_type="client", matched_email="client@example.com"),
        )
        self.assertEqual(mock_graphql.call_count, 1)

    @patch("crm_integration.lookup.execute_graphql", new_callable=AsyncMock)
    async def test_falls_back_to_leads_when_no_client_match(self, mock_graphql):
        mock_graphql.side_effect = [
            {"data": {"items_page_by_column_values": {"items": []}}},
            {
                "data": {
                    "items_page_by_column_values": {
                        "items": [{"id": "222", "name": "Lead Co"}],
                    }
                }
            },
        ]

        match = await find_contact_by_emails(
            ["lead@example.com"],
            settings=TEST_CRM_SETTINGS,
        )

        self.assertEqual(
            match,
            ContactMatch(item_id="222", match_type="lead", matched_email="lead@example.com"),
        )
        self.assertEqual(mock_graphql.call_count, 2)

    @patch("crm_integration.lookup.execute_graphql", new_callable=AsyncMock)
    async def test_returns_none_when_no_match(self, mock_graphql):
        mock_graphql.side_effect = [
            {"data": {"items_page_by_column_values": {"items": []}}},
            {"data": {"items_page_by_column_values": {"items": []}}},
        ]

        match = await find_contact_by_emails(
            ["unknown@example.com"],
            settings=TEST_CRM_SETTINGS,
        )

        self.assertIsNone(match)


class NodeTakerWebhookEndpointTests(unittest.TestCase):
    def test_invalid_json_returns_400(self):
        response = client.post(
            "/nodetaker-webhook",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Invalid JSON")

    def test_missing_fields_returns_422(self):
        response = client.post("/nodetaker-webhook", json={"meeting_title": "Only title"})
        self.assertEqual(response.status_code, 422)

    @patch("crm_integration.routes.process_nodetaker_webhook", new_callable=AsyncMock)
    def test_valid_payload_returns_pipeline_result(self, mock_pipeline):
        from crm_integration.schemas import NodeTakerWebhookResult

        mock_pipeline.return_value = NodeTakerWebhookResult(
            status="success",
            meeting_item_id="999",
            match_type="client",
            matched_email="client@example.com",
            doc_id="888",
            doc_created=True,
        )

        response = client.post("/nodetaker-webhook", json=VALID_PAYLOAD)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["meeting_item_id"], "999")
        self.assertEqual(body["doc_id"], "888")
        self.assertTrue(body["doc_created"])
        mock_pipeline.assert_awaited_once()


class ProcessNodetakerWebhookTests(unittest.IsolatedAsyncioTestCase):
    @patch("crm_integration.pipeline.create_meeting_workdoc", new_callable=AsyncMock)
    @patch("crm_integration.pipeline.create_meeting_item", new_callable=AsyncMock)
    @patch("crm_integration.pipeline.find_contact_by_emails", new_callable=AsyncMock)
    async def test_orchestrates_lookup_create_item_and_workdoc(
        self,
        mock_find,
        mock_create_item,
        mock_create_doc,
    ):
        from crm_integration.pipeline import process_nodetaker_webhook

        mock_find.return_value = ContactMatch(
            item_id="111",
            match_type="client",
            matched_email="client@example.com",
        )
        mock_create_item.return_value = "555"
        mock_create_doc.return_value = ("777", True, [])

        payload = NodeTakerWebhookPayload.model_validate(VALID_PAYLOAD)
        result = await process_nodetaker_webhook(payload, settings=TEST_CRM_SETTINGS)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.meeting_item_id, "555")
        self.assertEqual(result.match_type, "client")
        self.assertEqual(result.doc_id, "777")
        self.assertTrue(result.doc_created)
        mock_find.assert_awaited_once()
        mock_create_item.assert_awaited_once()
        mock_create_doc.assert_awaited_once()


class MorningBriefHelperTests(unittest.TestCase):
    def test_parse_comma_separated_emails_splits_and_trims(self):
        self.assertEqual(
            parse_comma_separated_emails(" client@example.com , lead@example.com "),
            ["client@example.com", "lead@example.com"],
        )

    def test_status_column_index_parses_json_value(self):
        column = {"value": '{"index": 0, "label": "פגישה חדשה"}'}
        self.assertEqual(status_column_index(column), 0)

    def test_column_text_reads_long_text_json(self):
        column = {"text": "", "value": '{"text": "סיכום פגישה"}'}
        self.assertEqual(column_text(column), "סיכום פגישה")

    def test_external_participant_emails_excludes_internal_from_parsed_list(self):
        emails = external_participant_emails(
            parse_comma_separated_emails("dev@beyondtcode.com, client@example.com")
        )
        self.assertEqual(emails, ["client@example.com"])


class ProcessMorningBriefsTests(unittest.IsolatedAsyncioTestCase):
    def _future_meeting_item(
        self,
        *,
        item_id: str,
        title: str,
        participants: str,
        status_index: int = 0,
    ) -> dict:
        status_label = "פגישה חדשה" if status_index == 0 else "נשלח סיכום"
        return {
            "id": item_id,
            "name": title,
            "column_values": [
                {
                    "id": "status",
                    "text": status_label,
                    "value": json.dumps({"index": status_index}),
                },
                {
                    "id": "text_mm4dmn71",
                    "text": participants,
                    "value": participants,
                },
            ],
        }

    @patch("crm_integration.batch.generate_meeting_brief", new_callable=AsyncMock)
    @patch("crm_integration.batch.gather_past_meeting_context", new_callable=AsyncMock)
    @patch("crm_integration.batch.execute_graphql", new_callable=AsyncMock)
    @patch("crm_integration.batch.datetime")
    async def test_processes_status_zero_meetings_for_today(
        self,
        mock_datetime,
        mock_graphql,
        mock_gather,
        mock_brief,
    ):
        mock_datetime.now.return_value = datetime(
            2026, 6, 18, 8, 0, tzinfo=ZoneInfo("Asia/Jerusalem")
        )
        mock_graphql.side_effect = [
            {
                "data": {
                    "items_page_by_column_values": {
                        "items": [
                            self._future_meeting_item(
                                item_id="100",
                                title="Client Call",
                                participants="client@example.com",
                            )
                        ]
                    }
                }
            },
            {"data": {"change_multiple_column_values": {"id": "100"}}},
        ]
        mock_gather.return_value = "היסטוריית פגישות"
        mock_brief.return_value = "תקציר הכנה"

        summary = await process_morning_briefs(settings=TEST_CRM_SETTINGS)

        self.assertEqual(summary["processed_count"], 1)
        self.assertEqual(summary["error_count"], 0)
        mock_gather.assert_awaited_once()
        mock_brief.assert_awaited_once_with(
            "היסטוריית פגישות",
            "Client Call",
            participant_emails=["client@example.com"],
        )

        update_call = mock_graphql.await_args_list[1]
        column_values = json.loads(update_call.args[1]["columnValues"])
        self.assertEqual(column_values["text_mm4eda8z"], "תקציר הכנה")
        self.assertEqual(column_values["status"], {"index": 1})

    @patch("crm_integration.batch.generate_meeting_brief", new_callable=AsyncMock)
    @patch("crm_integration.batch.gather_past_meeting_context", new_callable=AsyncMock)
    @patch("crm_integration.batch.execute_graphql", new_callable=AsyncMock)
    @patch("crm_integration.batch.datetime")
    async def test_skips_meetings_without_external_participants(
        self,
        mock_datetime,
        mock_graphql,
        mock_gather,
        mock_brief,
    ):
        mock_datetime.now.return_value = datetime(
            2026, 6, 18, 8, 0, tzinfo=ZoneInfo("Asia/Jerusalem")
        )
        mock_graphql.return_value = {
            "data": {
                "items_page_by_column_values": {
                    "items": [
                        self._future_meeting_item(
                            item_id="101",
                            title="Internal Sync",
                            participants="dev@beyondtcode.com",
                        )
                    ]
                }
            }
        }

        summary = await process_morning_briefs(settings=TEST_CRM_SETTINGS)

        self.assertEqual(summary["processed_count"], 0)
        self.assertEqual(summary["skipped_count"], 1)
        mock_gather.assert_not_awaited()
        mock_brief.assert_not_awaited()

    @patch("crm_integration.batch.generate_meeting_brief", new_callable=AsyncMock)
    @patch("crm_integration.batch.gather_past_meeting_context", new_callable=AsyncMock)
    @patch("crm_integration.batch.execute_graphql", new_callable=AsyncMock)
    @patch("crm_integration.batch.datetime")
    async def test_continues_when_one_meeting_fails(
        self,
        mock_datetime,
        mock_graphql,
        mock_gather,
        mock_brief,
    ):
        mock_datetime.now.return_value = datetime(
            2026, 6, 18, 8, 0, tzinfo=ZoneInfo("Asia/Jerusalem")
        )
        mock_graphql.side_effect = [
            {
                "data": {
                    "items_page_by_column_values": {
                        "items": [
                            self._future_meeting_item(
                                item_id="200",
                                title="First",
                                participants="first@example.com",
                            ),
                            self._future_meeting_item(
                                item_id="201",
                                title="Second",
                                participants="second@example.com",
                            ),
                        ]
                    }
                }
            },
            {"data": {"change_multiple_column_values": {"id": "201"}}},
        ]
        mock_gather.return_value = "הקשר"
        mock_brief.side_effect = [RuntimeError("AI failed"), "תקציר שני"]

        summary = await process_morning_briefs(settings=TEST_CRM_SETTINGS)

        self.assertEqual(summary["processed_count"], 1)
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["errors"][0]["item_id"], "200")

    @patch("crm_integration.batch.generate_meeting_brief", new_callable=AsyncMock)
    @patch("crm_integration.batch.gather_past_meeting_context", new_callable=AsyncMock)
    @patch("crm_integration.batch.execute_graphql", new_callable=AsyncMock)
    @patch("crm_integration.batch.datetime")
    async def test_no_history_still_generates_brief(
        self,
        mock_datetime,
        mock_graphql,
        mock_gather,
        mock_brief,
    ):
        mock_datetime.now.return_value = datetime(
            2026, 6, 18, 8, 0, tzinfo=ZoneInfo("Asia/Jerusalem")
        )
        mock_graphql.side_effect = [
            {
                "data": {
                    "items_page_by_column_values": {
                        "items": [
                            self._future_meeting_item(
                                item_id="300",
                                title="New Client",
                                participants="new@example.com",
                            )
                        ]
                    }
                }
            },
            {"data": {"change_multiple_column_values": {"id": "300"}}},
        ]
        mock_gather.return_value = ""
        mock_brief.return_value = "אין פגישות קודמות"

        summary = await process_morning_briefs(settings=TEST_CRM_SETTINGS)

        self.assertEqual(summary["processed_count"], 1)
        mock_brief.assert_awaited_once_with(
            "",
            "New Client",
            participant_emails=["new@example.com"],
        )

    @patch("crm_integration.batch.generate_meeting_brief", new_callable=AsyncMock)
    @patch("crm_integration.batch.gather_past_meeting_context", new_callable=AsyncMock)
    @patch("crm_integration.batch.execute_graphql", new_callable=AsyncMock)
    @patch("crm_integration.batch.datetime")
    async def test_ignores_meetings_not_in_new_status(
        self,
        mock_datetime,
        mock_graphql,
        mock_gather,
        mock_brief,
    ):
        mock_datetime.now.return_value = datetime(
            2026, 6, 18, 8, 0, tzinfo=ZoneInfo("Asia/Jerusalem")
        )
        mock_graphql.return_value = {
            "data": {
                "items_page_by_column_values": {
                    "items": [
                        self._future_meeting_item(
                            item_id="400",
                            title="Already Sent",
                            participants="client@example.com",
                            status_index=1,
                        )
                    ]
                }
            }
        }

        summary = await process_morning_briefs(settings=TEST_CRM_SETTINGS)

        self.assertEqual(summary["fetched"], 0)
        self.assertEqual(summary["processed_count"], 0)
        mock_gather.assert_not_awaited()
        mock_brief.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
