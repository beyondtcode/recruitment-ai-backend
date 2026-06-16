"""Tests for the Monday.com webhook FastAPI endpoint."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import app

client = TestClient(app)

MAIN_HUB_FILE_COLUMN_ID = "file_mm3gnkmj"
JOB_BOARD_FILE_COLUMN_ID = "file_mm43j6y2"


class MondayWebhookTests(unittest.TestCase):
    def test_health(self):
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_challenge_echo(self):
        response = client.post("/monday-webhook", json={"challenge": "abc123token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"challenge": "abc123token"})

    def test_ignores_unsupported_events(self):
        payload = {
            "event": {
                "type": "delete_pulse",
                "boardId": 123,
                "pulseId": 456,
            }
        }
        response = client.post("/monday-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})

    def test_ignores_wrong_column_id(self):
        payload = {
            "event": {
                "type": "change_column_value",
                "boardId": 123,
                "pulseId": 456,
                "columnId": "other_column",
            }
        }
        response = client.post("/monday-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})

    @patch("app.run_webhook_pipeline_sync")
    def test_create_item_event_schedules_background_task(self, mock_pipeline):
        payload = {
            "event": {
                "type": "create_item",
                "boardId": 5096673346,
                "pulseId": 1234567890,
                "triggerUuid": "form-submit-uuid",
            }
        }
        response = client.post("/monday-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success"})
        mock_pipeline.assert_called_once_with("1234567890", "5096673346")

    @patch("app.run_webhook_pipeline_sync")
    def test_file_column_event_schedules_background_task(self, mock_pipeline):
        payload = {
            "event": {
                "type": "change_column_value",
                "boardId": 5096673346,
                "pulseId": 9876543210,
                "columnId": MAIN_HUB_FILE_COLUMN_ID,
                "triggerUuid": "test-uuid",
            }
        }
        response = client.post("/monday-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success"})
        mock_pipeline.assert_called_once_with("9876543210", "5096673346")

    @patch("app.run_webhook_pipeline_sync")
    def test_job_board_file_column_event_schedules_background_task(self, mock_pipeline):
        payload = {
            "event": {
                "type": "change_column_value",
                "boardId": 9876543210,
                "pulseId": 1112223333,
                "columnId": JOB_BOARD_FILE_COLUMN_ID,
                "triggerUuid": "job-board-uuid",
            }
        }
        response = client.post("/monday-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success"})
        mock_pipeline.assert_called_once_with("1112223333", "9876543210")

    def test_missing_pulse_or_board_returns_400(self):
        payload = {
            "event": {
                "type": "change_column_value",
                "columnId": MAIN_HUB_FILE_COLUMN_ID,
            }
        }
        response = client.post("/monday-webhook", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "error")

    @patch("app.run_webhook_pipeline_sync")
    def test_custom_app_input_fields_schedules_background_task(self, mock_pipeline):
        payload = {
            "payload": {
                "blockKind": "action",
                "inputFields": {
                    "itemId": 1234567890,
                    "boardId": 5096673346,
                },
                "recipeId": 30440660,
                "integrationId": 398759485,
            },
            "runtimeMetadata": {
                "triggerUuid": "custom-app-uuid",
            },
        }
        response = client.post("/monday-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success"})
        mock_pipeline.assert_called_once_with("1234567890", "5096673346")

    @patch("app.run_webhook_pipeline_sync")
    def test_custom_app_inbound_field_values_schedules_background_task(self, mock_pipeline):
        payload = {
            "payload": {
                "blockKind": "action",
                "inboundFieldValues": {
                    "itemId": 1112223333,
                    "boardId": 9876543210,
                },
                "recipeId": 30440660,
                "integrationId": 398759485,
            },
        }
        response = client.post("/monday-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success"})
        mock_pipeline.assert_called_once_with("1112223333", "9876543210")

    def test_custom_app_payload_missing_ids_is_ignored(self):
        payload = {
            "payload": {
                "blockKind": "action",
                "webhookUrl": "https://api-gw.monday.com/automations/apps-events/481709001",
                "subscriptionId": 481709001,
                "inputFields": {},
                "recipeId": 629280,
                "integrationId": 398528596,
            }
        }
        response = client.post("/monday-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})


if __name__ == "__main__":
    unittest.main()
