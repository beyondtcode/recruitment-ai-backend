"""Tests for CV pipeline temp-file lifecycle."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONDAY_BOARD_ID", "5096673346")

from models.candidate import CandidateSchema
from services import cv_pipeline
from services.cv_pipeline import process_cv_bytes


def _candidate() -> CandidateSchema:
    return CandidateSchema(
        name="Jane Doe",
        email="jane@example.com",
        phone="0556722091",
        extraction_confidence="medium",
        confidence_reasoning="Test reasoning",
    )


class ProcessCvBytesConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_runs_do_not_delete_each_others_upload_files(self):
        file_bytes = b"%PDF-1.4 concurrent test"
        upload_paths: list[str] = []

        async def fake_upsert(candidate, *, cv_file_path, raw_cv_text, board_id, source_item_id=None):
            upload_paths.append(cv_file_path)
            await asyncio.sleep(0.05)
            return "111", False

        with (
            patch.object(cv_pipeline, "extract_text_from_file", return_value="cv text"),
            patch.object(cv_pipeline, "analyze_cv_with_claude", new_callable=AsyncMock) as mock_ai,
            patch.object(cv_pipeline, "upsert_candidate_item", side_effect=fake_upsert),
            patch.object(cv_pipeline, "get_main_hub_board_id", return_value="5096673346"),
        ):
            mock_ai.return_value = _candidate()
            await asyncio.gather(
                process_cv_bytes(
                    file_bytes,
                    "resume.pdf",
                    board_id="5096673346",
                    sync_to_hub=False,
                    source_item_id="3038533627",
                ),
                process_cv_bytes(
                    file_bytes,
                    "resume.pdf",
                    board_id="5096673346",
                    sync_to_hub=False,
                    source_item_id="3038533627",
                ),
            )

        self.assertEqual(len(upload_paths), 2)
        self.assertNotEqual(upload_paths[0], upload_paths[1])
        for path in upload_paths:
            self.assertFalse(os.path.exists(path))


class ProcessMondayJobFitWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_main_hub_board(self):
        with (
            patch.object(cv_pipeline, "is_job_board", return_value=False),
            patch.object(
                cv_pipeline,
                "fetch_board_job_requirements",
                new_callable=AsyncMock,
            ) as mock_reqs,
            patch.object(
                cv_pipeline,
                "get_item_cv_file_url",
                new_callable=AsyncMock,
            ) as mock_file,
            patch.object(cv_pipeline, "process_cv_bytes", new_callable=AsyncMock) as mock_process,
        ):
            await cv_pipeline.process_monday_job_fit_webhook("111", "5096673346")

        mock_reqs.assert_not_awaited()
        mock_file.assert_not_awaited()
        mock_process.assert_not_awaited()

    async def test_skips_when_job_requirements_missing(self):
        with (
            patch.object(cv_pipeline, "is_job_board", return_value=True),
            patch.object(
                cv_pipeline,
                "fetch_board_job_requirements",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                cv_pipeline,
                "get_item_cv_file_url",
                new_callable=AsyncMock,
            ) as mock_file,
            patch.object(cv_pipeline, "process_cv_bytes", new_callable=AsyncMock) as mock_process,
        ):
            await cv_pipeline.process_monday_job_fit_webhook("111", "5098534551")

        mock_file.assert_not_awaited()
        mock_process.assert_not_awaited()

    async def test_scores_job_board_without_hub_sync(self):
        with (
            patch.object(cv_pipeline, "is_job_board", return_value=True),
            patch.object(
                cv_pipeline,
                "fetch_board_job_requirements",
                new_callable=AsyncMock,
                return_value="Must know Python",
            ),
            patch.object(
                cv_pipeline,
                "get_item_cv_file_url",
                new_callable=AsyncMock,
                return_value=("https://example.com/cv.pdf", "cv.pdf"),
            ),
            patch.object(
                cv_pipeline,
                "download_cv_from_url",
                new_callable=AsyncMock,
                return_value=b"%PDF-1.4",
            ),
            patch.object(cv_pipeline, "process_cv_bytes", new_callable=AsyncMock) as mock_process,
        ):
            mock_process.return_value = cv_pipeline.UpsertResult(item_id="111", created=False)
            await cv_pipeline.process_monday_job_fit_webhook("111", "5098534551")

        mock_process.assert_awaited_once()
        kwargs = mock_process.await_args.kwargs
        self.assertEqual(kwargs["board_id"], "5098534551")
        self.assertEqual(kwargs["source_item_id"], "111")
        self.assertEqual(kwargs["job_requirements"], "Must know Python")
        self.assertFalse(kwargs["sync_to_hub"])


if __name__ == "__main__":
    unittest.main()
