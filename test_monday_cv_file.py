"""Unit tests for dynamic Monday file column resolution."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services import monday_service
from services.monday_service import (
    MAIN_HUB_BOARD_ID,
    _BOARD_FILE_COLUMN_CACHE,
    _extract_cv_file_from_column_values,
    _parse_item_cv_file_response,
    resolve_file_column_id,
    upload_file_to_item,
)


def _file_column(
    column_id: str,
    *,
    name: str = "resume.pdf",
    url: str = "https://cdn.monday.com/files/resume.pdf",
) -> dict:
    return {
        "id": column_id,
        "type": "file",
        "files": [
            {
                "id": "file-1",
                "name": name,
                "url": url,
            }
        ],
    }


class ExtractCvFileTests(unittest.TestCase):
    def test_main_hub_file_column(self):
        column_values = [_file_column("file_mm3gnkmj")]
        url, name = _extract_cv_file_from_column_values("123", column_values)
        self.assertEqual(url, "https://cdn.monday.com/files/resume.pdf")
        self.assertEqual(name, "resume.pdf")

    def test_job_board_file_column(self):
        column_values = [_file_column("file_mm43j6y2", name="cv.docx", url="https://cdn.monday.com/cv.docx")]
        url, name = _extract_cv_file_from_column_values("456", column_values)
        self.assertEqual(name, "cv.docx")

    def test_multiple_file_columns_uses_last_column(self):
        column_values = [
            {"id": "text_col", "type": "text", "text": "hello"},
            _file_column("file_first", name="first.pdf", url="https://cdn.monday.com/first.pdf"),
            _file_column("file_second", name="second.pdf", url="https://cdn.monday.com/second.pdf"),
        ]
        url, name = _extract_cv_file_from_column_values("789", column_values)
        self.assertEqual(url, "https://cdn.monday.com/second.pdf")
        self.assertEqual(name, "second.pdf")

    def test_no_file_columns_raises(self):
        column_values = [{"id": "text_col", "type": "text", "text": "hello"}]
        with self.assertRaisesRegex(ValueError, "No CV file"):
            _extract_cv_file_from_column_values("999", column_values)

    def test_parse_item_cv_file_response_scans_all_columns(self):
        body = {
            "data": {
                "items": [
                    {
                        "column_values": [
                            {"id": "email_mm3ga25b", "type": "email"},
                            _file_column("file_mm43j6y2"),
                        ]
                    }
                ]
            }
        }
        url, name = _parse_item_cv_file_response("111", body)
        self.assertEqual(name, "resume.pdf")


class ResolveFileColumnIdTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _BOARD_FILE_COLUMN_CACHE.clear()

    async def test_returns_first_file_type_column(self):
        with patch.object(
            monday_service,
            "_post_graphql",
            new_callable=AsyncMock,
            return_value={
                "data": {
                    "boards": [
                        {
                            "columns": [
                                {"id": "text_col", "type": "text"},
                                {"id": "file_mm43j6y2", "type": "file"},
                            ]
                        }
                    ]
                }
            },
        ):
            column_id = await resolve_file_column_id("job-board-1")

        self.assertEqual(column_id, "file_mm43j6y2")
        self.assertEqual(_BOARD_FILE_COLUMN_CACHE["job-board-1"], "file_mm43j6y2")

    async def test_uses_cached_value_on_second_call(self):
        _BOARD_FILE_COLUMN_CACHE["job-board-1"] = "file_cached"
        with patch.object(monday_service, "_post_graphql", new_callable=AsyncMock) as mock_post:
            column_id = await resolve_file_column_id("job-board-1")

        self.assertEqual(column_id, "file_cached")
        mock_post.assert_not_called()

    async def test_env_override_when_column_exists_on_board(self):
        os.environ["MONDAY_FILE_COLUMN_ID"] = "file_mm3gnkmj"
        try:
            with patch.object(
                monday_service,
                "_post_graphql",
                new_callable=AsyncMock,
                return_value={
                    "data": {
                        "boards": [
                            {
                                "columns": [
                                    {"id": "file_mm3gnkmj", "type": "file"},
                                    {"id": "file_mm43j6y2", "type": "file"},
                                ]
                            }
                        ]
                    }
                },
            ):
                column_id = await resolve_file_column_id(MAIN_HUB_BOARD_ID)

            self.assertEqual(column_id, "file_mm3gnkmj")
        finally:
            os.environ.pop("MONDAY_FILE_COLUMN_ID", None)
            _BOARD_FILE_COLUMN_CACHE.clear()


class UploadFileToItemTests(unittest.TestCase):
    def test_uses_passed_column_id(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(b"%PDF-1.4 test")
            tmp_path = tmp.name

        os.environ["MONDAY_API_KEY"] = "test-key"
        try:
            mock_response = MagicMock()
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = {"data": {"add_file_to_column": {"id": "1"}}}

            with (
                patch("services.monday_service.requests.post", return_value=mock_response) as mock_post,
                patch("builtins.print"),
            ):
                upload_file_to_item("12345", tmp_path, column_id="file_mm43j6y2")

            query = mock_post.call_args.kwargs["data"]["query"]
            self.assertIn('column_id: "file_mm43j6y2"', query)
            self.assertNotIn("file_mm3gnkmj", query)
        finally:
            os.unlink(tmp_path)
            os.environ.pop("MONDAY_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
