"""Unit tests for recruiter_notes sanitization when city is empty."""

from __future__ import annotations

import unittest

from models.candidate import CandidateSchema, ProgrammingLanguageExperience
from services.ai_service import (
    _sanitize_candidate,
    _sanitize_interview_summaries,
    _sanitize_programming_languages,
    _sanitize_recruiter_notes,
    _sanitize_test_score,
    _strip_city_education_notes,
)


def _minimal_candidate(**overrides) -> CandidateSchema:
    defaults = {"name": "Test User"}
    defaults.update(overrides)
    return CandidateSchema(**defaults)


class RecruiterNotesSanitizeTests(unittest.TestCase):
    def test_keeps_notes_when_city_populated(self):
        candidate = _minimal_candidate(
            city="ירושלים",
            recruiter_notes="העיר נגזרה אוטומטית ממוסד הלימודים (בינת)",
        )
        result = _sanitize_recruiter_notes(candidate)
        self.assertEqual(result.recruiter_notes, candidate.recruiter_notes)

    def test_strips_derivation_sentence_when_city_empty(self):
        notes = "העיר נגזרה אוטומטית ממוסד הלימודים (בינת)"
        candidate = _minimal_candidate(city=None, recruiter_notes=notes)
        result = _sanitize_recruiter_notes(candidate)
        self.assertIsNone(result.recruiter_notes)

    def test_keeps_red_flag_when_city_empty(self):
        notes = "פער של 3 שנים ללא תעסוקה\nהעיר נגזרה אוטומטית ממוסד הלימודים (בינת)"
        candidate = _minimal_candidate(city=None, recruiter_notes=notes)
        result = _sanitize_recruiter_notes(candidate)
        self.assertEqual(result.recruiter_notes, "פער של 3 שנים ללא תעסוקה")

    def test_strip_helper_removes_derivation_only(self):
        self.assertIsNone(
            _strip_city_education_notes("העיר נגזרה אוטומטית ממוסד הלימודים (קאמטאק)")
        )


class TestScoreSanitizeTests(unittest.TestCase):
    def test_clears_test_score_from_cv_parse(self):
        candidate = _minimal_candidate(test_score=95)
        result = _sanitize_test_score(candidate)
        self.assertIsNone(result.test_score)

    def test_keeps_null_test_score(self):
        candidate = _minimal_candidate(test_score=None)
        result = _sanitize_test_score(candidate)
        self.assertIsNone(result.test_score)

    def test_sanitize_candidate_clears_test_score(self):
        candidate = _minimal_candidate(test_score=88, city="תל אביב")
        result = _sanitize_candidate(candidate)
        self.assertIsNone(result.test_score)
        self.assertEqual(result.city, "תל אביב")


class InterviewSummariesSanitizeTests(unittest.TestCase):
    def test_clears_interview_summaries_from_cv_parse(self):
        candidate = _minimal_candidate(interview_summaries="Phone screen went well")
        result = _sanitize_interview_summaries(candidate)
        self.assertIsNone(result.interview_summaries)

    def test_keeps_null_interview_summaries(self):
        candidate = _minimal_candidate(interview_summaries=None)
        result = _sanitize_interview_summaries(candidate)
        self.assertIsNone(result.interview_summaries)


class ProgrammingLanguagesSanitizeTests(unittest.TestCase):
    def test_sorts_by_years_descending_and_caps_at_five(self):
        candidate = _minimal_candidate(
            programming_languages=[
                ProgrammingLanguageExperience(language="Go", years=1),
                ProgrammingLanguageExperience(language="Java", years=7),
                ProgrammingLanguageExperience(language="Python", years=5),
                ProgrammingLanguageExperience(language="Rust", years=2),
                ProgrammingLanguageExperience(language="C#", years=4),
                ProgrammingLanguageExperience(language="Ruby", years=3),
            ]
        )
        result = _sanitize_programming_languages(candidate)
        languages = [e.language for e in result.programming_languages]
        self.assertEqual(languages, ["Java", "Python", "C#", "Ruby", "Rust"])
        self.assertEqual(result.programming_languages[0].years, 7)

    def test_drops_zero_year_entries(self):
        candidate = _minimal_candidate(
            programming_languages=[
                ProgrammingLanguageExperience(language="Java", years=0),
                ProgrammingLanguageExperience(language="Python", years=3),
            ]
        )
        result = _sanitize_programming_languages(candidate)
        self.assertEqual(len(result.programming_languages), 1)
        self.assertEqual(result.programming_languages[0].language, "Python")


if __name__ == "__main__":
    unittest.main()
