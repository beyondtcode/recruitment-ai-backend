"""Unit tests for email CV batch validation and deduplication."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from services.cv_pipeline import UpsertResult
from services.email_batch import (
    _dedup_key,
    _prune_processed_state,
    _record_processed,
    _validate_cv_text,
    process_email_cv_batch,
)
from services.email_service import CvEmailAttachment, _attachment_reject_reason
from services.position_routing import ActivePosition, PositionMatch, RoutingContext
from utils.file_parser import is_plausible_cv_text, validate_cv_attachment_bytes

PDF_HEADER = b"%PDF-1.4\n" + b"x" * 2100


class TestAttachmentGate(unittest.TestCase):
    def test_rejects_non_cv_extension(self):
        self.assertEqual(
            _attachment_reject_reason("photo.png", b"\x89PNG" + b"x" * 3000),
            "bad_extension",
        )

    def test_rejects_tiny_pdf(self):
        self.assertEqual(_attachment_reject_reason("cv.pdf", b"%PDF tiny"), "too_small")

    def test_rejects_spoofed_pdf_extension(self):
        self.assertEqual(
            _attachment_reject_reason("cv.pdf", b"not a pdf" + b"x" * 3000),
            "bad_magic",
        )

    def test_rejects_denylisted_filename(self):
        self.assertEqual(_attachment_reject_reason("logo.pdf", PDF_HEADER), "denylisted_name")

    def test_accepts_valid_pdf(self):
        self.assertIsNone(_attachment_reject_reason("john_doe_cv.pdf", PDF_HEADER))

    def test_validate_cv_attachment_bytes_public_helper(self):
        self.assertEqual(validate_cv_attachment_bytes("cv.pdf", PDF_HEADER), ".pdf")
        self.assertIsNone(validate_cv_attachment_bytes("cv.pdf", b"bad"))


class TestPlausibleCvText(unittest.TestCase):
    def test_rejects_short_footer_only_text(self):
        text = "Unsubscribe from this mailing list.\nPrivacy policy."
        self.assertFalse(is_plausible_cv_text(text))

    def test_accepts_cv_keywords(self):
        text = (
            "John Doe\n"
            "Experience: 5 years as a software engineer.\n"
            "Skills: Python, JavaScript, SQL.\n"
            "Education: BSc Computer Science.\n"
        )
        self.assertTrue(is_plausible_cv_text(text))

    def test_validate_cv_text_rejects_short_content(self):
        reason = _validate_cv_text(PDF_HEADER, "cv.pdf")
        self.assertIn(reason, {"unreadable_file", "text_too_short", "not_cv_content"})


class TestProcessedState(unittest.TestCase):
    def test_prune_removes_old_entries(self):
        now = datetime(2026, 6, 28, 8, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        old = now.replace(day=1).isoformat()
        recent = now.replace(day=27).isoformat()
        state = {"old:key": old, "new:key": recent}
        pruned = _prune_processed_state(state, now=now)
        self.assertNotIn("old:key", pruned)
        self.assertIn("new:key", pruned)

    def test_dedup_key_format(self):
        attachment = CvEmailAttachment(
            path="/tmp/x.pdf",
            message_id="<abc@mail.com>",
            filename="cv.pdf",
            sha256="deadbeef",
            size_bytes=5000,
        )
        self.assertEqual(_dedup_key(attachment), "<abc@mail.com>:deadbeef")


class TestProcessEmailCvBatch(unittest.IsolatedAsyncioTestCase):
    async def test_skips_when_email_not_configured(self):
        with patch("services.email_batch._email_credentials_configured", return_value=False):
            summary = await process_email_cv_batch()
        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(summary["reason"], "email_not_configured")

    async def test_hash_dedup_skips_second_run(self):
        with patch("services.email_batch._email_credentials_configured", return_value=True):
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                state_file = tmp_path / ".processed_attachments.json"
                with patch("services.email_batch.PROCESSED_STATE_FILE", state_file), patch(
                    "services.email_batch.TEMP_CV_DIR", tmp_path
                ):
                    attachment = CvEmailAttachment(
                        path=str(tmp_path / "cv.pdf"),
                        message_id="<msg-1@example.com>",
                        filename="cv.pdf",
                        sha256="abc123",
                        size_bytes=5000,
                    )
                    Path(attachment.path).write_bytes(PDF_HEADER)

                    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
                    state: dict[str, str] = {}
                    _record_processed(state, _dedup_key(attachment), now=now)
                    state_file.write_text(json.dumps(state), encoding="utf-8")

                    with patch(
                        "services.email_batch.fetch_cv_attachments",
                        return_value=[attachment],
                    ), patch(
                        "services.email_batch.load_routing_context",
                        new_callable=AsyncMock,
                        return_value=RoutingContext(active_positions=(), board_name_to_id={}),
                    ), patch(
                        "services.email_batch.process_cv_bytes",
                        new_callable=AsyncMock,
                    ) as mock_process:
                        summary = await process_email_cv_batch()

                    self.assertEqual(summary["skipped_count"], 1)
                    self.assertEqual(summary["skipped"][0]["reason"], "already_processed")
                    mock_process.assert_not_called()

    async def test_validation_failure_counts_as_skipped_not_error(self):
        with patch("services.email_batch._email_credentials_configured", return_value=True):
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                state_file = tmp_path / ".processed_attachments.json"
                with patch("services.email_batch.PROCESSED_STATE_FILE", state_file), patch(
                    "services.email_batch.TEMP_CV_DIR", tmp_path
                ):
                    attachment = CvEmailAttachment(
                        path=str(tmp_path / "cv.pdf"),
                        message_id="<msg-2@example.com>",
                        filename="cv.pdf",
                        sha256="def456",
                        size_bytes=5000,
                    )
                    Path(attachment.path).write_bytes(PDF_HEADER)

                    with patch(
                        "services.email_batch.fetch_cv_attachments",
                        return_value=[attachment],
                    ), patch(
                        "services.email_batch.load_routing_context",
                        new_callable=AsyncMock,
                        return_value=RoutingContext(active_positions=(), board_name_to_id={}),
                    ), patch(
                        "services.email_batch._validate_cv_text",
                        return_value="not_cv_content",
                    ), patch(
                        "services.email_batch.process_cv_bytes",
                        new_callable=AsyncMock,
                    ) as mock_process:
                        summary = await process_email_cv_batch()

                    self.assertEqual(summary["skipped_count"], 1)
                    self.assertEqual(summary["error_count"], 0)
                    self.assertEqual(summary["skipped"][0]["reason"], "not_cv_content")
                    mock_process.assert_not_called()

    async def test_successful_processing_records_created_count(self):
        with patch("services.email_batch._email_credentials_configured", return_value=True):
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                state_file = tmp_path / ".processed_attachments.json"
                with patch("services.email_batch.PROCESSED_STATE_FILE", state_file), patch(
                    "services.email_batch.TEMP_CV_DIR", tmp_path
                ):
                    attachment = CvEmailAttachment(
                        path=str(tmp_path / "cv.pdf"),
                        message_id="<msg-3@example.com>",
                        filename="cv.pdf",
                        sha256="ghi789",
                        size_bytes=5000,
                        subject="קורות חיים",
                    )
                    Path(attachment.path).write_bytes(PDF_HEADER)

                    with patch(
                        "services.email_batch.fetch_cv_attachments",
                        return_value=[attachment],
                    ), patch(
                        "services.email_batch.load_routing_context",
                        new_callable=AsyncMock,
                        return_value=RoutingContext(active_positions=(), board_name_to_id={}),
                    ), patch(
                        "services.email_batch._validate_cv_text",
                        return_value=None,
                    ), patch(
                        "services.email_batch.match_email_to_position",
                        return_value=None,
                    ), patch(
                        "services.email_batch.get_main_hub_board_id",
                        return_value="hub-board",
                    ), patch(
                        "services.email_batch.process_cv_bytes",
                        new_callable=AsyncMock,
                        return_value=UpsertResult(item_id="123", created=True),
                    ) as mock_process:
                        summary = await process_email_cv_batch()

                    self.assertEqual(summary["created_count"], 1)
                    self.assertEqual(summary["main_hub_only_count"], 1)
                    self.assertEqual(summary["error_count"], 0)
                    mock_process.assert_called_once()
                    kwargs = mock_process.await_args.kwargs
                    self.assertFalse(kwargs["sync_to_hub"])
                    saved = json.loads(state_file.read_text(encoding="utf-8"))
                    self.assertIn("<msg-3@example.com>:ghi789", saved)

    async def test_routes_to_job_board_when_position_matches(self):
        with patch("services.email_batch._email_credentials_configured", return_value=True):
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                state_file = tmp_path / ".processed_attachments.json"
                with patch("services.email_batch.PROCESSED_STATE_FILE", state_file), patch(
                    "services.email_batch.TEMP_CV_DIR", tmp_path
                ):
                    attachment = CvEmailAttachment(
                        path=str(tmp_path / "cv.pdf"),
                        message_id="<msg-4@example.com>",
                        filename="cv.pdf",
                        sha256="jkl012",
                        size_bytes=5000,
                        subject="מועמדות לתפקיד CTO",
                    )
                    Path(attachment.path).write_bytes(PDF_HEADER)

                    position = ActivePosition(
                        item_id="pos-1",
                        item_name="CTO",
                        requirements_text="CTO requirements",
                        recruiter_people_text="",
                        updated_at="2026-07-01T00:00:00Z",
                        board_id="5099999999",
                        board_name="CTO",
                    )
                    routing_context = RoutingContext(
                        active_positions=(position,),
                        board_name_to_id={"cto": "5099999999"},
                    )
                    match = PositionMatch(
                        position=position,
                        board_id="5099999999",
                        score=100,
                        matched_hint="CTO",
                    )

                    with patch(
                        "services.email_batch.fetch_cv_attachments",
                        return_value=[attachment],
                    ), patch(
                        "services.email_batch.load_routing_context",
                        new_callable=AsyncMock,
                        return_value=routing_context,
                    ), patch(
                        "services.email_batch._validate_cv_text",
                        return_value=None,
                    ), patch(
                        "services.email_batch.match_email_to_position",
                        return_value=match,
                    ), patch(
                        "services.email_batch.resolve_job_requirements_for_match",
                        new_callable=AsyncMock,
                        return_value="CTO requirements",
                    ), patch(
                        "services.email_batch.process_cv_bytes",
                        new_callable=AsyncMock,
                        return_value=UpsertResult(item_id="456", created=True),
                    ) as mock_process:
                        summary = await process_email_cv_batch()

                    self.assertEqual(summary["created_count"], 1)
                    self.assertEqual(summary["routed_to_job_board_count"], 1)
                    mock_process.assert_called_once()
                    kwargs = mock_process.await_args.kwargs
                    self.assertEqual(kwargs["board_id"], "5099999999")
                    self.assertTrue(kwargs["sync_to_hub"])
                    self.assertEqual(kwargs["job_requirements"], "CTO requirements")


if __name__ == "__main__":
    unittest.main()
