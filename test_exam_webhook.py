"""Tests for the Exam board → Hub Monday webhook."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("MONDAY_API_KEY", "test-monday-key")
os.environ.setdefault("MONDAY_BOARD_ID", "5096673346")
os.environ.setdefault("MONDAY_EXAM_BOARD_ID", "5097679903")

from app import app  # noqa: E402
from services import exam_pipeline  # noqa: E402
from services.exam_pipeline import (  # noqa: E402
    ExamItemFields,
    candidate_from_exam_fields,
    fold_exam_context_into_notes,
    process_exam_to_hub,
)

client = TestClient(app)

EXAM_BOARD_ID = "5097679903"
HUB_BOARD_ID = "5096673346"


class MondayExamWebhookTests(unittest.TestCase):
    def test_challenge_echo(self):
        response = client.post(
            "/monday-exam-webhook",
            json={"challenge": "exam-challenge"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"challenge": "exam-challenge"})

    def test_ignores_unsupported_events(self):
        payload = {
            "event": {
                "type": "delete_pulse",
                "boardId": int(EXAM_BOARD_ID),
                "pulseId": 456,
            }
        }
        response = client.post("/monday-exam-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})

    @patch("app.process_exam_to_hub_sync")
    def test_create_item_schedules_exam_pipeline(self, mock_pipeline):
        payload = {
            "event": {
                "type": "create_item",
                "boardId": int(EXAM_BOARD_ID),
                "pulseId": 1112223333,
                "triggerUuid": "exam-uuid",
            }
        }
        response = client.post("/monday-exam-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success"})
        mock_pipeline.assert_called_once_with("1112223333", EXAM_BOARD_ID)

    @patch("app.process_exam_to_hub_sync")
    def test_custom_app_schedules_exam_pipeline(self, mock_pipeline):
        payload = {
            "payload": {
                "blockKind": "action",
                "inputFields": {
                    "itemId": 5556667777,
                    "boardId": int(EXAM_BOARD_ID),
                },
            },
            "runtimeMetadata": {"triggerUuid": "exam-custom"},
        }
        response = client.post("/monday-exam-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success"})
        mock_pipeline.assert_called_once_with("5556667777", EXAM_BOARD_ID)


class ExamPipelineUnitTests(unittest.IsolatedAsyncioTestCase):
    def test_fold_exam_context_into_notes(self):
        notes = fold_exam_context_into_notes(
            source="LinkedIn",
            position="Backend",
            track="Junior",
            notes="Called Monday",
        )
        self.assertEqual(
            notes,
            "מקור מועמד: LinkedIn\nמשרה: Backend\nמסלול נבחן: Junior\nהערות: Called Monday",
        )

    def test_candidate_from_exam_fields_maps_identity(self):
        fields = ExamItemFields(
            item_id="1",
            name="ישראל ישראלי",
            email="israel@example.com",
            phone="0521234567",
            source="Form",
            position="QA",
            track="Regular",
            notes=None,
            linked_hub_item_id=None,
        )
        candidate = candidate_from_exam_fields(fields)
        self.assertEqual(candidate.name, "ישראל ישראלי")
        self.assertEqual(candidate.email, "israel@example.com")
        self.assertEqual(candidate.phone, "0521234567")
        self.assertEqual(candidate.extraction_confidence, "low")
        self.assertIn("מקור מועמד: Form", candidate.recruiter_notes or "")
        self.assertIn("משרה: QA", candidate.recruiter_notes or "")
        self.assertIn("מסלול נבחן: Regular", candidate.recruiter_notes or "")

    async def test_process_exam_to_hub_ignores_wrong_board(self):
        with patch.object(
            exam_pipeline,
            "fetch_exam_item",
            new_callable=AsyncMock,
        ) as mock_fetch:
            await process_exam_to_hub("111", HUB_BOARD_ID)
            mock_fetch.assert_not_called()

    async def test_process_exam_to_hub_no_cv_upserts_and_links(self):
        fields = ExamItemFields(
            item_id="exam-1",
            name="Dana",
            email="dana@example.com",
            phone="0501112233",
            source="Referral",
            position="Frontend",
            track="Junior",
            notes="Hi",
            linked_hub_item_id=None,
        )
        with (
            patch.object(
                exam_pipeline,
                "fetch_exam_item",
                new_callable=AsyncMock,
                return_value=fields,
            ),
            patch.object(
                exam_pipeline,
                "resolve_existing_hub_item",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                exam_pipeline,
                "_try_get_exam_cv_file",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                exam_pipeline,
                "get_main_hub_board_id",
                return_value=HUB_BOARD_ID,
            ),
            patch.object(
                exam_pipeline,
                "_upsert_hub_candidate",
                new_callable=AsyncMock,
                return_value=("hub-9", True),
            ) as mock_upsert,
            patch.object(
                exam_pipeline,
                "link_exam_and_hub",
                new_callable=AsyncMock,
            ) as mock_link,
        ):
            await process_exam_to_hub("exam-1", EXAM_BOARD_ID)

            mock_upsert.assert_awaited_once()
            upsert_kwargs = mock_upsert.await_args.kwargs
            self.assertEqual(upsert_kwargs["hub_board_id"], HUB_BOARD_ID)
            self.assertIsNone(upsert_kwargs["existing_hub"])
            candidate = mock_upsert.await_args.args[0]
            self.assertEqual(candidate.email, "dana@example.com")
            self.assertIn("מקור מועמד: Referral", candidate.recruiter_notes or "")

            mock_link.assert_awaited_once_with(
                exam_item_id="exam-1",
                hub_item_id="hub-9",
                exam_board_id=EXAM_BOARD_ID,
                hub_board_id=HUB_BOARD_ID,
            )

    async def test_process_exam_to_hub_updates_existing_by_email(self):
        from services.monday_service import FoundItem

        fields = ExamItemFields(
            item_id="exam-2",
            name="Dana",
            email="dana@example.com",
            phone="0501112233",
            source="Referral",
            position="Frontend",
            track="Junior",
            notes="Hi",
            linked_hub_item_id=None,
        )
        existing = FoundItem(item_id="hub-existing", name="Dana Cohen")
        with (
            patch.object(
                exam_pipeline,
                "fetch_exam_item",
                new_callable=AsyncMock,
                return_value=fields,
            ),
            patch.object(
                exam_pipeline,
                "resolve_existing_hub_item",
                new_callable=AsyncMock,
                return_value=existing,
            ),
            patch.object(
                exam_pipeline,
                "_try_get_exam_cv_file",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                exam_pipeline,
                "get_main_hub_board_id",
                return_value=HUB_BOARD_ID,
            ),
            patch.object(
                exam_pipeline,
                "_upsert_hub_candidate",
                new_callable=AsyncMock,
                return_value=("hub-existing", False),
            ) as mock_upsert,
            patch.object(
                exam_pipeline,
                "link_exam_and_hub",
                new_callable=AsyncMock,
            ) as mock_link,
        ):
            await process_exam_to_hub("exam-2", EXAM_BOARD_ID)

            mock_upsert.assert_awaited_once()
            upsert_kwargs = mock_upsert.await_args.kwargs
            self.assertEqual(upsert_kwargs["existing_hub"], existing)
            mock_link.assert_awaited_once_with(
                exam_item_id="exam-2",
                hub_item_id="hub-existing",
                exam_board_id=EXAM_BOARD_ID,
                hub_board_id=HUB_BOARD_ID,
            )


if __name__ == "__main__":
    unittest.main()
