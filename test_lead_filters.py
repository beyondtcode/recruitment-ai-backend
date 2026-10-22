"""Unit tests for the lead agent rules (categories, blocklist, hardware, dates, dedup)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("MONDAY_BOARD_ID", "1")

from services import lead_filters as lf  # noqa: E402


class CategoryTests(unittest.TestCase):
    def test_twelve_categories(self) -> None:
        self.assertEqual(len(lf.CATEGORIES), 12)

    def test_normalize(self) -> None:
        self.assertEqual(lf.normalize_category("full stack"), "Full Stack")
        self.assertEqual(lf.normalize_category("AI Builder"), "AI Builder")
        self.assertEqual(lf.normalize_category("Salesforce"), "יישום Salesforce")
        self.assertIsNone(lf.normalize_category(None))
        self.assertIsNone(lf.normalize_category("DevOps"))

    def test_phrasing(self) -> None:
        self.assertEqual(lf.outreach_phrasing("Backend"), "מפתחות")
        self.assertEqual(lf.outreach_phrasing("פיתוח אוטומציה"), "בודקות תוכנה ומפתחות אוטומציה")
        self.assertEqual(lf.outreach_phrasing("יישום monday.com"), "מיישמות monday.com")
        self.assertEqual(lf.outreach_phrasing("AI Agent Developer"), "AI Builders")


class BlocklistTests(unittest.TestCase):
    def test_blocked(self) -> None:
        for name in (
            "Matrix IT",
            "matrix",
            "NVIDIA",
            "Nvidia Israel",
            "אנבידיה",
            "SQLink Group",
            "IAI - Israel Aerospace Industries",
            "התעשייה האווירית לישראל",
            "Elad Software Systems",
            "Intel Corporation",
            "ComBlack",
            "בינת סמך",
        ):
            self.assertIsNotNone(lf.blocked_company(name), name)

    def test_not_blocked(self) -> None:
        for name in ("Intellinx", "Wix", "Monday.com", "One Zero Bank", "Matrixx Software", ""):
            self.assertIsNone(lf.blocked_company(name), name)


class HardwareTests(unittest.TestCase):
    def test_hardware(self) -> None:
        self.assertTrue(lf.is_hardware_role("Embedded Software Engineer"))
        self.assertTrue(lf.is_hardware_role("Software Engineer", "Linux kernel and BSP experience"))
        self.assertTrue(lf.is_hardware_role("מפתח/ת קושחה"))
        self.assertTrue(lf.is_hardware_role("Device Drivers Developer"))
        self.assertFalse(lf.is_hardware_role("Backend Developer", "Python, AWS"))


class PrefilterTests(unittest.TestCase):
    def test_keep(self) -> None:
        for title in (
            "Backend Developer",
            "QA Engineer",
            "QA Automation Engineer",
            "בודק/ת תוכנה",
            "Salesforce Admin",
            "monday.com Implementation Expert",
            "AI Builder",
            "AI Workflow Builder",
            "מפתח/ת פול סטאק",
            "SDET",
        ):
            self.assertTrue(lf.passes_title_prefilter(title), title)

    def test_drop(self) -> None:
        for title in (
            "DevOps Engineer",
            "Data Scientist",
            "Product Manager",
            "Cyber Security Analyst",
            "Technical Support",
            "Account Executive",
            "",
        ):
            self.assertFalse(lf.passes_title_prefilter(title), title)


class FingerprintTests(unittest.TestCase):
    def test_same_job_across_boards(self) -> None:
        a = lf.job_fingerprint("Wix.com Ltd", "Backend Developer")
        b = lf.job_fingerprint("WIX.COM", "Backend  developer")
        self.assertEqual(a, b)
        self.assertEqual(
            lf.job_fingerprint("חברה בע\"מ", "מפתח/ת Backend"),
            lf.job_fingerprint("חברה", "מפתח Backend"),
        )
        self.assertEqual(lf.job_fingerprint("Unknown", "Developer"), "")


class DateTests(unittest.TestCase):
    TODAY = date(2026, 10, 22)

    def check(self, raw: str, expected: str) -> None:
        self.assertEqual(lf.to_ddmmyyyy(raw, today=self.TODAY), expected, raw)

    def test_formats(self) -> None:
        self.check("2026-10-20", "20/10/2026")
        self.check("2026-10-20T08:00:00Z", "20/10/2026")
        self.check("21.10.26", "21/10/2026")
        self.check("5/9/2026", "05/09/2026")
        self.check("היום", "22/10/2026")
        self.check("אתמול", "21/10/2026")
        self.check("לפני 3 ימים", "19/10/2026")
        self.check("לפני 5 שעות", "22/10/2026")
        self.check("3 days ago", "19/10/2026")
        self.check("2d", "20/10/2026")
        self.check("24h", "22/10/2026")
        self.check("1 week ago", "15/10/2026")
        self.check("", "")
        self.check("משרה חמה", "")


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ, {"LEADS_DEDUP_DB": os.path.join(self.tmp.name, "seen.db")}
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_pipeline(self) -> None:
        from routers import leads

        raw = [
            {"company_name": "Wix", "job_title": "Backend Developer",
             "job_link": "https://li/1", "publish_date": "2026-10-20", "source": "LinkedIn"},
            # same job on another board → duplicate
            {"company_name": "Wix Ltd", "job_title": "Backend Developer",
             "job_link": "https://drushim/9", "publish_date": "אתמול", "source": "Drushim"},
            {"company_name": "Intel", "job_title": "Software Engineer",
             "job_link": "https://li/2", "source": "LinkedIn"},
            {"company_name": "Acme", "job_title": "Firmware Engineer",
             "job_link": "https://li/3", "source": "LinkedIn"},
            {"company_name": "Beta", "job_title": "Data Engineer",
             "job_link": "https://li/4", "source": "LinkedIn"},
            {"company_name": "Gamma", "job_title": "AI Builder",
             "job_link": "https://li/5", "source": "LinkedIn"},
            {"company_name": "Delta", "job_title": "QA Engineer",
             "job_link": "https://li/6", "source": "LinkedIn"},
        ]
        answers = {
            "Backend Developer": {"category": "Backend", "job_summary": "s"},
            "Data Engineer": {"category": None, "job_summary": "s"},
            "AI Builder": {"category": "AI Builder", "job_summary": "s"},
        }

        async def fake_classify(provider, company, title, snippet):
            if title == "QA Engineer":
                raise RuntimeError("LLM down")
            return {"category": lf.normalize_category(answers[title]["category"]),
                    "job_summary": "s"}

        with mock.patch("services.enrichment._classify_job", fake_classify):
            leads1 = asyncio.run(leads._run_lead_pipeline([dict(j) for j in raw]))
            got = {(l.company_name, l.category, l.outreach_phrasing) for l in leads1}
            self.assertEqual(
                got, {("Wix", "Backend", "מפתחות"), ("Gamma", "AI Builder", "AI Builders")}
            )
            row = leads1[0].model_dump(by_alias=True)
            self.assertEqual(
                list(row),
                ["Company Name", "Job Title", "קטגוריה", "ניסוח לפנייה", "Job Summary",
                 "Job Link", "Publish Date", "תאריך מציאה", "מקור"],
            )
            wix = next(l for l in leads1 if l.company_name == "Wix")
            self.assertEqual(wix.publish_date, "20/10/2026")
            self.assertEqual(wix.source, "לינקדאין")

            # Second run: everything seen, except the LLM failure which is retried.
            answers["QA Engineer"] = {"category": "QA"}

            async def fake_ok(provider, company, title, snippet):
                return {"category": lf.normalize_category(answers[title]["category"]),
                        "job_summary": "s"}

        with mock.patch("services.enrichment._classify_job", fake_ok):
            leads2 = asyncio.run(leads._run_lead_pipeline([dict(j) for j in raw]))
        self.assertEqual([(l.company_name, l.category) for l in leads2], [("Delta", "QA")])


if __name__ == "__main__":
    unittest.main()
