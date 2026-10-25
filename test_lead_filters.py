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
                ["Company Name", "Job Title", "Category", "Outreach Phrasing", "Job Summary",
                 "Job Link", "Publish Date", "Found Date", "Source", "Company LinkedIn"],
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


class LinkedInPaginationTests(unittest.TestCase):
    def test_fetches_more_pages_until_empty(self) -> None:
        from services import scraper

        card = '<li><div class="base-card base-search-card"><h3 class="base-search-card__title">X</h3></div></li>'

        class Resp:
            def __init__(self, status, text):
                self.status, self._text = status, text

            def text(self):
                return self._text

        pages = [Resp(200, card * 10), Resp(429, ""), Resp(200, card * 10), Resp(200, "")]
        calls = []

        class Ctx:
            class request:
                @staticmethod
                def get(url, timeout=0):
                    calls.append(url)
                    return pages.pop(0)

        url = "https://www.linkedin.com/jobs/search?location=Israel&f_TPR=r259200&keywords=Backend"
        with mock.patch.object(scraper.time, "sleep", lambda s: None):
            extra = scraper._linkedin_more_pages(Ctx, url, card * 60)
            small = scraper._linkedin_more_pages(Ctx, url, card * 5)
            token = scraper.LINKEDIN_MAX_JOBS_OVERRIDE.set(0)
            first_only = scraper._linkedin_more_pages(Ctx, url, card * 60)
            scraper.LINKEDIN_MAX_JOBS_OVERRIDE.reset(token)
        self.assertEqual(first_only, "")  # watch list: first page only
        self.assertEqual(extra.count("base-search-card__title"), 20)
        self.assertEqual([c.split("start=")[1] for c in calls], ["60", "70", "70", "80"])
        self.assertTrue(calls[0].startswith(scraper.LINKEDIN_MORE_API + "?location=Israel&f_TPR=r259200"))
        self.assertEqual(small, "")  # fewer than 20 jobs: no extra requests
        real = ('<li><div class="base-card base-search-card"><a class="base-card__full-link" '
                'href="https://il.linkedin.com/jobs/view/{i}"></a><h3 class="base-search-card__title">Backend Developer</h3>'
                '<h4 class="base-search-card__subtitle">Acme</h4></div></li>')
        html = "<ul>" + "".join(real.format(i=i) for i in range(3)) + "</ul>" + "".join(real.format(i=i) for i in range(3, 5))
        self.assertEqual(len(scraper.parse_job_cards(html, scraper.BOARD_CONFIGS["LinkedIn"])), 5)


class LookbackTests(unittest.TestCase):
    def test_with_lookback(self) -> None:
        from services.job_sources import LEAD_SEARCHES, with_lookback

        urls = {label if board == "LinkedIn" else board: u for board, label, u in LEAD_SEARCHES}
        li = with_lookback(urls["Backend"], 30)
        self.assertIn("f_TPR=r2592000", li)
        self.assertIn("keywords=Backend%20Developer", li)
        self.assertNotIn("duration=", with_lookback(urls["AllJobs"], 30))
        self.assertIn("position=235", with_lookback(urls["AllJobs"], 30))
        self.assertNotIn("ssaen", with_lookback(urls["Drushim"], 30))
        self.assertNotIn("fromAge", with_lookback(urls["Glassdoor"], 30))
        self.assertEqual(with_lookback(urls["Jobnet"], 30), urls["Jobnet"])

    def test_scrape_all_raw_uses_lookback(self) -> None:
        from services import lead_pipeline, scraper

        seen = []

        async def fake_scrape_one(url, source):
            seen.append((url, scraper.LINKEDIN_MAX_JOBS_OVERRIDE.get()))
            return []

        async def go(days):
            return await lead_pipeline.scrape_all_raw(days)

        with mock.patch.object(lead_pipeline, "scrape_one", fake_scrape_one), \
                mock.patch.object(lead_pipeline, "SEARCH_PAUSE_SECONDS", 0), \
                mock.patch.dict(os.environ, {"LEADS_SEARCH_LIMIT": "2"}):
            asyncio.run(go(30))
            self.assertTrue(all("r2592000" in u and m == 400 for u, m in seen))
            seen.clear()
            asyncio.run(go(None))
            self.assertTrue(all("r259200" in u and "r2592000" not in u and m is None for u, m in seen))


if __name__ == "__main__":
    unittest.main()
