"""Unit tests for email CV position routing."""

from __future__ import annotations

import unittest

from services.position_routing import (
    ActivePosition,
    build_board_id_to_name,
    build_board_name_index,
    extract_position_hint,
    match_email_to_position,
    normalize_position_name,
    resolve_positions_with_boards,
)


def _position(
    item_name: str,
    *,
    requirements_text: str = "",
    board_id: str | None = None,
    board_name: str | None = None,
    updated_at: str = "2026-07-01T00:00:00Z",
) -> ActivePosition:
    return ActivePosition(
        item_id=f"item-{item_name}",
        item_name=item_name,
        requirements_text=requirements_text,
        recruiter_people_text="",
        updated_at=updated_at,
        board_id=board_id,
        board_name=board_name,
    )


class TestNormalizePositionName(unittest.TestCase):
    def test_collapses_whitespace_and_case(self):
        self.assertEqual(
            normalize_position_name("  Technology   Businesses Lead  "),
            "technology businesses lead",
        )

    def test_normalizes_hebrew_prefix(self):
        self.assertEqual(
            normalize_position_name("משרה עבור עופר"),
            normalize_position_name("משרה לעופר"),
        )


class TestExtractPositionHint(unittest.TestCase):
    def test_cto_hebrew_subject(self):
        self.assertEqual(
            extract_position_hint("CTO פתח תקווה | משרה מלאה"),
            "CTO",
        )

    def test_cto_application_subject(self):
        self.assertEqual(
            extract_position_hint("מועמדות לתפקיד CTO"),
            "CTO",
        )

    def test_cto_english_subject(self):
        self.assertEqual(
            extract_position_hint("Nominee for CTO at Digital Media & E-commerce"),
            "CTO",
        )

    def test_technology_business_lead_subject(self):
        self.assertEqual(
            extract_position_hint("מועמדות עבור משרת - Technology Business Lead"),
            "Technology Business Lead",
        )

    def test_technology_business_lead_in_body(self):
        self.assertEqual(
            extract_position_hint(
                "קורות חיים חן נעמן - משרה: Technology Business lead",
            ),
            "Technology Business lead",
        )

    def test_drushim_job_title(self):
        self.assertEqual(
            extract_position_hint(
                'קו"ח: Full-Stack Microsoft Developer | Khaled mussa mussa | עילוט',
            ),
            "Full-Stack Microsoft Developer",
        )

    def test_generic_resume_returns_none(self):
        self.assertIsNone(extract_position_hint("קורות חיים"))
        self.assertIsNone(extract_position_hint("חיפוש עבודה - 4 שנות נסיון"))


class TestMatchEmailToPosition(unittest.TestCase):
    def setUp(self):
        self.positions = (
            _position(
                "CTO",
                board_id="board-cto",
                board_name="CTO",
            ),
            _position(
                "Technology Businesses Lead",
                board_id="board-tbl",
                board_name="Technology Businesses Lead",
            ),
            _position(
                "משרה עבור עופר",
                board_id="board-ofer",
                board_name="משרה לעופר",
                requirements_text=(
                    "Full-Stack Microsoft Developer-100% Remote. "
                    "מחפשים מפתח/ת שחי/ה ונושם/ת את עולמות ה Microsoft."
                ),
            ),
            _position(
                "משרה מוקפאת לדוגמה",
                board_id="board-frozen",
                board_name="משרה מוקפאת לדוגמה",
            ),
        )

    def test_matches_cto_from_hebrew_subject(self):
        match = match_email_to_position(
            "CTO פתח תקווה | משרה מלאה",
            "",
            self.positions,
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.position.item_name, "CTO")
        self.assertEqual(match.board_id, "board-cto")

    def test_matches_technology_business_lead_fuzzy(self):
        match = match_email_to_position(
            "מועמדות עבור משרת - Technology Business Lead",
            "",
            self.positions,
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.position.item_name, "Technology Businesses Lead")

    def test_matches_drushim_title_via_requirements_text(self):
        match = match_email_to_position(
            'קו"ח: Full-Stack Microsoft Developer | Khaled mussa mussa | עילוט',
            "",
            self.positions,
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.position.item_name, "משרה עבור עופר")
        self.assertEqual(match.board_id, "board-ofer")

    def test_generic_resume_does_not_match(self):
        match = match_email_to_position("קורות חיים", "", self.positions)
        self.assertIsNone(match)

    def test_skips_positions_without_board_id(self):
        positions = (
            _position("CTO", board_id=None),
        )
        match = match_email_to_position("מועמדות לתפקיד CTO", "", positions)
        self.assertIsNone(match)


class TestBoardResolution(unittest.TestCase):
    def test_build_board_name_index(self):
        index = build_board_name_index(
            [
                {"id": "1", "name": "CTO"},
                {"id": "2", "name": "משרה לעופר"},
            ]
        )
        self.assertEqual(index[normalize_position_name("CTO")], "1")
        self.assertEqual(index[normalize_position_name("משרה לעופר")], "2")

    def test_resolve_positions_with_alias(self):
        boards = [{"id": "5098604137", "name": "משרה לעופר"}]
        positions = [_position("משרה עבור עופר")]
        board_index = build_board_name_index(boards)
        board_id_to_name = build_board_id_to_name(boards)
        resolved = resolve_positions_with_boards(
            positions,
            board_index,
            board_id_to_name,
        )
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].board_id, "5098604137")
        self.assertEqual(resolved[0].board_name, "משרה לעופר")


if __name__ == "__main__":
    unittest.main()
