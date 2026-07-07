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


if __name__ == "__main__":
    unittest.main()
