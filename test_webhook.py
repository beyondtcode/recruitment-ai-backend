"""Tests for the Monday.com webhook FastAPI endpoint."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import app
from services.monday_service import FILE_COLUMN_ID

client = TestClient(app)


class MondayWebhookTests(unittest.TestCase):
    def test_health(self):
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_challenge_echo(self):
        response = client.post("/monday-webhook", json={"challenge": "abc123token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"challenge": "abc123token"})

    def test_ignores_non_file_column_events(self):
        payload = {
            "event": {
                "type": "create_pulse",
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
    def test_file_column_event_schedules_background_task(self, mock_pipeline):
        payload = {
            "event": {
                "type": "change_column_value",
                "boardId": 5096673346,
                "pulseId": 9876543210,
                "columnId": FILE_COLUMN_ID,
                "triggerUuid": "test-uuid",
            }
        }
        response = client.post("/monday-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success"})
        mock_pipeline.assert_called_once_with("9876543210", "5096673346")

    def test_missing_pulse_or_board_returns_400(self):
        payload = {
            "event": {
                "type": "change_column_value",
                "columnId": FILE_COLUMN_ID,
            }
        }
        response = client.post("/monday-webhook", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "error")


if __name__ == "__main__":
    unittest.main()
