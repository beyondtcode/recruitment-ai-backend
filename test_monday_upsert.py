"""Unit tests for Monday.com upsert logic (mocked GraphQL)."""

from __future__ import annotations

import json
import os
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONDAY_BOARD_ID", "5096673346")

from models.candidate import CandidateSchema, ProgrammingLanguageExperience
from services import monday_service
from services.monday_service import (
    CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
    CREATE_ITEM_MUTATION,
    ECOSYSTEM_DOTNET_COLUMN_ID,
    ECOSYSTEM_JAVA_COLUMN_ID,
    ECOSYSTEM_NODEJS_COLUMN_ID,
    ECOSYSTEM_NUMERIC_COLUMN_IDS,
    ECOSYSTEM_REACT_COLUMN_ID,
    ENTER_DATE_COLUMN_ID,
    FoundItem,
    INTERVIEW_SUMMARIES_COLUMN_ID,
    JOB_CATEGORY_DROPDOWN_COLUMN_ID,
    LANGUAGE_EXPERIENCE_COLUMN_ID,
    get_main_hub_board_id,
    PROGRAMMING_LANGUAGES_COLUMN_ID,
    RECRUITER_NOTES_COLUMN_ID,
    TEAM_LEAD_DROPDOWN_LABEL,
    build_column_values,
    build_job_category_dropdown_labels,
    build_update_column_values,
    format_language_experience_compact,
    map_language_experience_to_ecosystem_columns,
    normalize_email,
    normalize_phone,
    normalize_programming_languages,
    parse_language_experience_entries,
    prepare_column_values_for_board,
    resolve_programming_language_labels,
    upsert_candidate_item,
)


def _candidate(**overrides) -> CandidateSchema:
    defaults = {
        "name": "Jane Doe",
        "email": "Jane@Example.com",
        "phone": "055-672-2091",
        "recruiter_notes": "New note",
    }
    defaults.update(overrides)
    return CandidateSchema(**defaults)


class NormalizeContactTests(unittest.TestCase):
    def test_normalize_email_lowercases_and_strips(self):
        self.assertEqual(normalize_email("  Jane@Example.COM  "), "jane@example.com")

    def test_normalize_phone_digits_only(self):
        self.assertEqual(normalize_phone("055-672 (2091)"), "0556722091")


class NormalizeProgrammingLanguagesTests(unittest.TestCase):
    def test_case_insensitive_match_uses_monday_label(self):
        result = normalize_programming_languages(["jquery", "PYTHON", "react"])
        self.assertEqual(result, ["JQuery", "Python", "React"])

    def test_unknown_language_preserved(self):
        with self.assertLogs("services.monday_service", level="INFO") as logs:
            result = resolve_programming_language_labels(["COBOL", "Java"])
        self.assertEqual(result, ["COBOL", "Java"])
        self.assertTrue(any("COBOL" in msg for msg in logs.output))

    def test_build_column_values_includes_unknown_languages(self):
        candidate = _candidate(
            programming_languages=[
                ProgrammingLanguageExperience(language="COBOL", years=5),
                ProgrammingLanguageExperience(language="Fortran", years=3),
            ]
        )
        column_values = build_column_values(candidate)
        self.assertEqual(
            column_values[PROGRAMMING_LANGUAGES_COLUMN_ID],
            {"labels": ["COBOL", "Fortran"]},
        )
        self.assertEqual(
            column_values[LANGUAGE_EXPERIENCE_COLUMN_ID],
            "COBOL (5y), Fortran (3y)",
        )


class FormatLanguageExperienceCompactTests(unittest.TestCase):
    def test_formats_top_languages_with_compact_years(self):
        experiences = [
            ProgrammingLanguageExperience(language="java", years=7),
            ProgrammingLanguageExperience(language="react", years=3.5),
            ProgrammingLanguageExperience(language="node.js", years=2),
        ]
        result = format_language_experience_compact(experiences)
        self.assertEqual(result, "Java (7y), React (3.5y), Node.js (2y)")

    def test_whole_years_without_decimal(self):
        experiences = [ProgrammingLanguageExperience(language="Python", years=7.0)]
        self.assertEqual(format_language_experience_compact(experiences), "Python (7y)")

    def test_rounds_fractional_years_to_one_decimal(self):
        experiences = [ProgrammingLanguageExperience(language="React", years=3.54)]
        self.assertEqual(format_language_experience_compact(experiences), "React (3.5y)")

    def test_unknown_labels_included_in_text(self):
        experiences = [
            ProgrammingLanguageExperience(language="COBOL", years=10),
            ProgrammingLanguageExperience(language="Java", years=5),
        ]
        self.assertEqual(
            format_language_experience_compact(experiences),
            "COBOL (10y), Java (5y)",
        )


class EcosystemColumnMappingTests(unittest.TestCase):
    def test_parse_language_experience_entries(self):
        entries = parse_language_experience_entries(
            "Java (7y), React (3.5y), Python (2.5)"
        )
        self.assertEqual(
            entries,
            [("Java", 7.0), ("React", 3.5), ("Python", 2.5)],
        )

    def test_map_languages_to_ecosystem_columns_max_wins(self):
        mapped = map_language_experience_to_ecosystem_columns(
            "C# (3y), .NET Core (5y), Java (7y)"
        )
        self.assertEqual(mapped[ECOSYSTEM_DOTNET_COLUMN_ID], 5.0)
        self.assertEqual(mapped[ECOSYSTEM_JAVA_COLUMN_ID], 7.0)

    def test_map_combo_alias_to_multiple_columns(self):
        mapped = map_language_experience_to_ecosystem_columns("React / Node.js (4y)")
        self.assertEqual(mapped[ECOSYSTEM_REACT_COLUMN_ID], 4.0)
        self.assertEqual(mapped[ECOSYSTEM_NODEJS_COLUMN_ID], 4.0)

    def test_build_column_values_sets_all_ecosystem_numeric_columns(self):
        candidate = _candidate(
            programming_languages=[
                ProgrammingLanguageExperience(language="Java", years=7),
                ProgrammingLanguageExperience(language="React", years=3),
            ]
        )
        column_values = build_column_values(candidate)
        for column_id in ECOSYSTEM_NUMERIC_COLUMN_IDS:
            self.assertIn(column_id, column_values)
        self.assertEqual(column_values[ECOSYSTEM_JAVA_COLUMN_ID], "7")
        self.assertEqual(column_values[ECOSYSTEM_REACT_COLUMN_ID], "3")
        self.assertEqual(column_values[ECOSYSTEM_DOTNET_COLUMN_ID], "")

    def test_team_lead_label_appended_from_raw_cv_text(self):
        candidate = _candidate(
            job_category=["Backend"],
            ai_summary="מועמד מנוסה עם ניסיון בפיתוח.",
        )
        raw_cv_text = "ניסיון תעסוקתי: שימש כראש צוות בפרויקט גדול."
        column_values = build_column_values(candidate, raw_cv_text=raw_cv_text)
        self.assertEqual(
            column_values[JOB_CATEGORY_DROPDOWN_COLUMN_ID],
            {"labels": ["Backend", TEAM_LEAD_DROPDOWN_LABEL]},
        )

    def test_team_lead_not_detected_from_ai_summary_without_raw_cv(self):
        candidate = _candidate(
            job_category=["Backend"],
            ai_summary="מועמד מנוסה. שימש כראש צוות בפרויקט גדול.",
        )
        column_values = build_column_values(candidate)
        self.assertEqual(
            column_values[JOB_CATEGORY_DROPDOWN_COLUMN_ID],
            {"labels": ["Backend"]},
        )

    def test_team_lead_only_when_phrase_in_raw_cv_text(self):
        payload = build_job_category_dropdown_labels([], "מנהל צוות בכיר")
        self.assertIsNone(payload)

    def test_team_lead_without_job_category(self):
        payload = build_job_category_dropdown_labels(
            None,
            "ניסיון כראש צוות בחברת הייטק",
        )
        self.assertEqual(payload, {"labels": [TEAM_LEAD_DROPDOWN_LABEL]})


class BuildColumnValuesTests(unittest.TestCase):
    def test_sets_language_experience_column(self):
        candidate = _candidate(
            programming_languages=[
                ProgrammingLanguageExperience(language="Java", years=7),
                ProgrammingLanguageExperience(language="React", years=3),
            ]
        )
        column_values = build_column_values(candidate)
        self.assertEqual(
            column_values[LANGUAGE_EXPERIENCE_COLUMN_ID],
            "Java (7y), React (3y)",
        )
        self.assertEqual(
            column_values[PROGRAMMING_LANGUAGES_COLUMN_ID],
            {"labels": ["Java", "React"]},
        )

    def test_never_sets_interview_summaries_column(self):
        candidate = _candidate(interview_summaries="CV-derived interview notes")
        column_values = build_column_values(candidate)
        self.assertNotIn(INTERVIEW_SUMMARIES_COLUMN_ID, column_values)


class MutationConstantsTests(unittest.TestCase):
    def test_create_mutation_includes_create_labels_if_missing(self):
        self.assertIn("create_labels_if_missing: true", CREATE_ITEM_MUTATION)

    def test_update_mutation_includes_create_labels_if_missing(self):
        self.assertIn("create_labels_if_missing: true", CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION)


class BuildUpdateColumnValuesTests(unittest.TestCase):
    def test_appends_recruiter_notes_and_sets_enter_date(self):
        candidate = _candidate(recruiter_notes="Follow up")
        existing = {RECRUITER_NOTES_COLUMN_ID: "Prior recruiter note"}
        stamp = date.today().strftime("%Y-%m-%d")

        column_values = build_update_column_values(candidate, existing)

        self.assertEqual(column_values[ENTER_DATE_COLUMN_ID], {"date": stamp})
        recruiter_text = column_values[RECRUITER_NOTES_COLUMN_ID]["text"]
        self.assertIn(f"--- {stamp} ---", recruiter_text)
        self.assertIn("Prior recruiter note", recruiter_text)
        self.assertIn("Follow up", recruiter_text)

    def test_never_appends_interview_summaries_even_when_set(self):
        candidate = _candidate(interview_summaries="Passed phone screen")
        existing = {RECRUITER_NOTES_COLUMN_ID: None}
        column_values = build_update_column_values(candidate, existing)
        self.assertNotIn(INTERVIEW_SUMMARIES_COLUMN_ID, column_values)


class UpsertCandidateItemTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_contact_creates_with_warning(self):
        candidate = _candidate(email=None, phone=None)

        with (
            patch.object(monday_service, "create_candidate_item", new_callable=AsyncMock) as mock_create,
            patch.object(monday_service, "find_existing_item_by_email", new_callable=AsyncMock) as mock_find_email,
        ):
            mock_create.return_value = "999"
            item_id, created = await upsert_candidate_item(candidate)

        mock_find_email.assert_not_called()
        mock_create.assert_awaited_once_with(
            candidate,
            cv_file_path=None,
            raw_cv_text="",
            board_id=get_main_hub_board_id(),
        )
        self.assertEqual(item_id, "999")
        self.assertTrue(created)

    async def test_email_match_updates_not_creates(self):
        candidate = _candidate()
        existing = FoundItem(item_id="111", name="Jane Doe")

        with (
            patch.object(monday_service, "find_existing_item_by_email", new_callable=AsyncMock) as mock_find,
            patch.object(monday_service, "update_candidate_item", new_callable=AsyncMock) as mock_update,
            patch.object(monday_service, "create_candidate_item", new_callable=AsyncMock) as mock_create,
        ):
            mock_find.return_value = existing
            mock_update.return_value = "111"
            item_id, created = await upsert_candidate_item(candidate)

        mock_create.assert_not_called()
        mock_update.assert_awaited_once_with(
            "111",
            candidate,
            existing_name="Jane Doe",
            raw_cv_text="",
            board_id=get_main_hub_board_id(),
        )
        self.assertEqual(item_id, "111")
        self.assertFalse(created)

    async def test_no_match_creates(self):
        candidate = _candidate()

        with (
            patch.object(monday_service, "find_existing_item_by_email", new_callable=AsyncMock) as mock_find,
            patch.object(monday_service, "create_candidate_item", new_callable=AsyncMock) as mock_create,
            patch.object(monday_service, "update_candidate_item", new_callable=AsyncMock) as mock_update,
            patch.object(monday_service, "resolve_column_id_by_type", new_callable=AsyncMock) as mock_resolve,
            patch.object(monday_service, "_query_items_by_column", new_callable=AsyncMock) as mock_phone_query,
            patch.object(monday_service, "_disambiguate_phone_matches", new_callable=AsyncMock) as mock_phone,
        ):
            mock_find.return_value = None
            mock_resolve.return_value = monday_service.PHONE_COLUMN_ID
            mock_phone_query.return_value = []
            mock_phone.return_value = None
            mock_create.return_value = "222"
            item_id, created = await upsert_candidate_item(candidate)

        mock_update.assert_not_called()
        mock_create.assert_awaited_once_with(
            candidate,
            cv_file_path=None,
            raw_cv_text="",
            board_id=get_main_hub_board_id(),
        )
        self.assertEqual(item_id, "222")
        self.assertTrue(created)

    async def test_update_flow_skips_file_upload_with_qa_log(self):
        candidate = _candidate()
        existing = FoundItem(item_id="111", name="Jane Doe")

        with (
            patch.object(monday_service, "find_existing_item_by_email", new_callable=AsyncMock) as mock_find,
            patch.object(monday_service, "update_candidate_item", new_callable=AsyncMock) as mock_update,
            patch.object(monday_service, "upload_file_to_item") as mock_upload,
        ):
            mock_find.return_value = existing
            mock_update.return_value = "111"

            with self.assertLogs("services.monday_service", level="INFO") as logs:
                item_id, created = await upsert_candidate_item(
                    candidate,
                    cv_file_path="/tmp/candidate.pdf",
                )

        mock_update.assert_awaited_once_with(
            "111",
            candidate,
            existing_name="Jane Doe",
            raw_cv_text="",
            board_id=get_main_hub_board_id(),
        )
        mock_upload.assert_not_called()
        self.assertEqual(item_id, "111")
        self.assertFalse(created)
        self.assertTrue(
            any(
                "Candidate item updated with new extracted data. Skipping file upload to prevent duplicates during QA."
                in line
                for line in logs.output
            )
        )


class UpdateCandidateItemTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_without_dropdown_preserves_text_column(self):
        candidate = _candidate(
            programming_languages=[
                ProgrammingLanguageExperience(language="COBOL", years=1),
            ]
        )

        async def fake_post(query, variables, *, column_ids=None):
            payload = json.loads(variables["columnValues"])
            if PROGRAMMING_LANGUAGES_COLUMN_ID in payload:
                raise Exception("Monday.com API error: invalid dropdown label")
            if LANGUAGE_EXPERIENCE_COLUMN_ID not in payload:
                raise AssertionError("text column missing from retry payload")
            return {"data": {"change_multiple_column_values": {"id": "111"}}}

        with (
            patch.object(monday_service, "_fetch_existing_notes", new_callable=AsyncMock) as mock_notes,
            patch.object(monday_service, "ensure_dropdown_labels_exist", new_callable=AsyncMock),
            patch.object(monday_service, "_post_graphql", side_effect=fake_post) as mock_post,
            patch.object(monday_service, "change_item_name", new_callable=AsyncMock),
        ):
            mock_notes.return_value = {RECRUITER_NOTES_COLUMN_ID: None}
            await monday_service.update_candidate_item(
                "111",
                candidate,
                existing_name="Jane Doe",
            )

        self.assertEqual(mock_post.await_count, 2)
        retry_payload = json.loads(mock_post.await_args_list[1].args[1]["columnValues"])
        self.assertNotIn(PROGRAMMING_LANGUAGES_COLUMN_ID, retry_payload)
        self.assertEqual(retry_payload[LANGUAGE_EXPERIENCE_COLUMN_ID], "COBOL (1y)")

    async def test_renames_when_name_differs(self):
        candidate = _candidate(name="Jane D. Updated")

        with (
            patch.object(monday_service, "_fetch_existing_notes", new_callable=AsyncMock) as mock_notes,
            patch.object(monday_service, "_post_graphql", new_callable=AsyncMock) as mock_post,
            patch.object(monday_service, "change_item_name", new_callable=AsyncMock) as mock_rename,
        ):
            mock_notes.return_value = {RECRUITER_NOTES_COLUMN_ID: None}
            mock_post.return_value = {"data": {"change_multiple_column_values": {"id": "111"}}}
            await monday_service.update_candidate_item(
                "111",
                candidate,
                existing_name="Jane Doe",
            )

        mock_rename.assert_awaited_once_with("111", "Jane D. Updated", board_id=get_main_hub_board_id())
        self.assertEqual(mock_post.await_count, 1)

    async def test_skips_rename_when_name_unchanged(self):
        candidate = _candidate(name="Jane Doe")

        with (
            patch.object(monday_service, "_fetch_existing_notes", new_callable=AsyncMock) as mock_notes,
            patch.object(monday_service, "_post_graphql", new_callable=AsyncMock) as mock_post,
            patch.object(monday_service, "change_item_name", new_callable=AsyncMock) as mock_rename,
        ):
            mock_notes.return_value = {RECRUITER_NOTES_COLUMN_ID: None}
            mock_post.return_value = {"data": {"change_multiple_column_values": {"id": "111"}}}
            await monday_service.update_candidate_item(
                "111",
                candidate,
                existing_name="Jane Doe",
            )

        mock_rename.assert_not_called()


class FindExistingItemTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_email_over_phone(self):
        candidate = _candidate()
        email_item = FoundItem(item_id="email-item", name="From Email")
        phone_item = FoundItem(item_id="phone-item", name="From Phone")

        with (
            patch.object(monday_service, "find_existing_item_by_email", new_callable=AsyncMock) as mock_email,
            patch.object(monday_service, "_query_items_by_column", new_callable=AsyncMock) as mock_query,
            patch.object(monday_service, "_disambiguate_phone_matches", new_callable=AsyncMock) as mock_phone,
        ):
            mock_email.return_value = email_item
            mock_query.return_value = [phone_item]
            mock_phone.return_value = phone_item
            result = await monday_service.find_existing_item_by_contact(candidate)

        self.assertEqual(result, email_item)
        mock_phone.assert_not_called()


class PrepareColumnValuesForBoardTests(unittest.IsolatedAsyncioTestCase):
    async def test_removes_unknown_columns_and_remaps_email_phone(self):
        candidate = _candidate()
        column_values = build_column_values(candidate)

        form_board_columns = [
            {"id": "email_mm438sbe", "type": "email"},
            {"id": "phone_mm43s4mh", "type": "phone"},
        ]

        with patch.object(
            monday_service,
            "_fetch_board_columns",
            new_callable=AsyncMock,
            return_value=form_board_columns,
        ):
            prepared = await prepare_column_values_for_board("5098534551", column_values)

        self.assertEqual(set(prepared.keys()), {"email_mm438sbe", "phone_mm43s4mh"})
        self.assertEqual(prepared["email_mm438sbe"]["email"], "jane@example.com")
        self.assertEqual(prepared["phone_mm43s4mh"]["phone"], "0556722091")

    async def test_main_hub_board_keeps_all_columns(self):
        candidate = _candidate()
        column_values = build_column_values(candidate)
        prepared = await prepare_column_values_for_board(get_main_hub_board_id(), column_values)
        self.assertEqual(prepared, column_values)


class CreateCandidateItemTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_without_dropdown_preserves_text_column(self):
        candidate = _candidate(
            programming_languages=[
                ProgrammingLanguageExperience(language="COBOL", years=2),
            ]
        )
        os.environ["MONDAY_API_KEY"] = "test-key"

        async def fake_post(query, variables, *, column_ids=None):
            payload = json.loads(variables["columnValues"])
            if PROGRAMMING_LANGUAGES_COLUMN_ID in payload:
                raise Exception("Monday.com API error: invalid dropdown label")
            if LANGUAGE_EXPERIENCE_COLUMN_ID not in payload:
                raise AssertionError("text column missing from retry payload")
            return {"data": {"create_item": {"id": "555"}}}

        with (
            patch.object(monday_service, "ensure_dropdown_labels_exist", new_callable=AsyncMock),
            patch.object(monday_service, "_post_graphql", side_effect=fake_post) as mock_post,
        ):
            item_id = await monday_service.create_candidate_item(candidate)

        self.assertEqual(item_id, "555")
        self.assertEqual(mock_post.await_count, 2)
        retry_payload = json.loads(mock_post.await_args_list[1].args[1]["columnValues"])
        self.assertNotIn(PROGRAMMING_LANGUAGES_COLUMN_ID, retry_payload)
        self.assertEqual(retry_payload[LANGUAGE_EXPERIENCE_COLUMN_ID], "COBOL (2y)")

    async def test_uses_post_graphql_with_normalized_contact(self):
        candidate = _candidate()
        os.environ["MONDAY_API_KEY"] = "test-key"

        with patch.object(monday_service, "_post_graphql", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"data": {"create_item": {"id": "555"}}}
            item_id = await monday_service.create_candidate_item(candidate)

        self.assertEqual(item_id, "555")
        mock_post.assert_awaited_once()
        variables = mock_post.await_args.args[1]
        column_values = json.loads(variables["columnValues"])
        self.assertEqual(column_values[monday_service.EMAIL_COLUMN_ID]["email"], "jane@example.com")
        self.assertEqual(column_values[monday_service.PHONE_COLUMN_ID]["phone"], "0556722091")


if __name__ == "__main__":
    unittest.main()
