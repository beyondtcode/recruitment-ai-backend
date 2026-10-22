"""Tests for the lead agent: company rules, contacts, sheet sync, archive, weekly file."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("MONDAY_BOARD_ID", "1")

from services import company_enrichment as ce  # noqa: E402
from services import contact_enrichment as cc  # noqa: E402
from services import lead_agent  # noqa: E402
from services.lead_sheet import MemoryStore  # noqa: E402
from services.search_client import BudgetedSearch, BudgetExceeded, SearchResult  # noqa: E402

TODAY = date(2026, 10, 25)  # a Sunday


class CompanyRuleTests(unittest.TestCase):
    def test_size(self) -> None:
        self.assertEqual(ce.normalize_size("51-200"), "51-200")
        self.assertEqual(ce.normalize_size("120"), "51-200")
        self.assertEqual(ce.normalize_size("1,001-5,000"), "1001-5000")
        self.assertEqual(ce.normalize_size(None), ce.UNKNOWN)
        self.assertEqual(ce.size_from_snippets("Software · 51-200 employees · Tel Aviv"), "51-200")
        self.assertEqual(ce.size_from_snippets("10,001+ employees"), "10001+")

    def test_target(self) -> None:
        product = ce.CompanyInfo(size="51-200", company_type="מוצר")
        self.assertEqual(ce.compute_target(product, {"Backend"}), ("כן", "product"))
        self.assertEqual(ce.compute_target(product, {"Backend"}, do_not_contact=True)[0], "לא")
        bank = ce.CompanyInfo(size="5001-10000", company_type="בנק")
        self.assertEqual(ce.compute_target(bank, {"Backend"})[0], "לא")
        self.assertEqual(ce.compute_target(bank, {"יישום Salesforce"}), ("כן", "systems"))
        small = ce.CompanyInfo(size="11-50", company_type="מוצר")
        self.assertEqual(ce.compute_target(small, {"יישום monday.com"})[0], "לא")
        staffing = ce.CompanyInfo(size="201-500", company_type="השמה")
        self.assertEqual(ce.compute_target(staffing, {"יישום Salesforce"})[0], "לא")
        unknown = ce.CompanyInfo()
        self.assertEqual(ce.compute_target(unknown, {"Backend"})[0], "לא ידוע")

    def test_linkedin_page(self) -> None:
        html = """<html><head><meta name="description" content="Acme builds SaaS."></head><body>
        <div data-test-id="about-us__size"><dt>Company size</dt><dd>51-200 employees</dd></div>
        <div data-test-id="about-us__industry"><dt>Industry</dt><dd>Software Development</dd></div>
        </body></html>"""
        page = ce.parse_linkedin_company_page(html)
        self.assertEqual(page.size, "51-200")
        self.assertEqual(page.industry, "Software Development")
        self.assertIn("SaaS", page.description)
        self.assertIsNone(ce.parse_linkedin_company_page("<html><body>Sign in</body></html>"))


class FakeProvider:
    name = "fake"

    def __init__(self, results: dict[str, list[SearchResult]] | None = None, fail: bool = False):
        self.results = results or {}
        self.fail = fail
        self.queries: list[str] = []

    async def search(self, query, *, num=10, domain=None):
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("quota")
        for needle, res in self.results.items():
            if needle in query:
                return res
        return []


class SearchBudgetTests(unittest.TestCase):
    def test_budget_and_fallback(self) -> None:
        primary, backup = FakeProvider(fail=True), FakeProvider({"x": [SearchResult("t", "l", "s")]})
        backup.name = "backup"
        search = BudgetedSearch([primary, backup], budget=4)
        self.assertEqual(len(asyncio.run(search.search("x"))), 1)
        self.assertEqual(search.used, 2)
        asyncio.run(search.search("y"))
        with self.assertRaises(BudgetExceeded):
            asyncio.run(search.search("z"))


class ContactTests(unittest.TestCase):
    def test_contacts_validated(self) -> None:
        results = {
            "CTO": [
                SearchResult("Dana Levi - CTO - Acme | LinkedIn", "https://il.linkedin.com/in/dana?trk=x", "CTO at Acme"),
                SearchResult("Rina - Talent Acquisition - Acme", "https://il.linkedin.com/in/rina", "Recruiter"),
            ],
            "Director": [
                SearchResult("Avi Cohen - R&D Manager - Acme", "https://il.linkedin.com/in/avi", "Acme"),
            ],
        }
        search = BudgetedSearch([FakeProvider(results)], budget=10)

        async def fake_llm(system, user, max_tokens=0):
            self.assertIn("CTO or VP R&D", system)
            return {
                "primary": {"name": "Dana Levi", "title": "CTO", "linkedin_url": "https://il.linkedin.com/in/dana"},
                "alternate": {"name": "Rina", "title": "Talent Acquisition", "linkedin_url": "https://il.linkedin.com/in/rina"},
            }

        with mock.patch.object(cc, "complete_json", fake_llm):
            primary, alt = asyncio.run(cc.find_contacts("Acme", "dev", "Backend", search))
        self.assertEqual(primary.name, "Dana Levi")
        self.assertIsNone(alt)  # recruiter rejected
        self.assertEqual(search.used, 2)  # CEO fallback skipped: 2 people already found

    def test_invented_url_rejected(self) -> None:
        search = BudgetedSearch([FakeProvider({"": [SearchResult("A - CTO", "https://il.linkedin.com/in/a", "")]})], 10)

        async def fake_llm(system, user, max_tokens=0):
            return {"primary": {"name": "Ghost", "title": "CTO", "linkedin_url": "https://il.linkedin.com/in/ghost"}}

        with mock.patch.object(cc, "complete_json", fake_llm):
            self.assertEqual(asyncio.run(cc.find_contacts("Acme", "dev", "", search)), (None, None))

    def test_profiles(self) -> None:
        self.assertEqual(cc.choose_profile("systems", "Backend"), "systems")
        self.assertEqual(cc.choose_profile("product", "AI Builder"), "ai")
        self.assertEqual(cc.choose_profile("product", "QA"), "dev")


def _job(company, title, link, category="Backend", found="25/10/2026", **extra):
    row = {
        "Company Name": company, "Job Title": title, "Category": category,
        "Outreach Phrasing": "מפתחות", "Job Summary": "", "Job Link": link,
        "Publish Date": found, "Found Date": found, "Source": "לינקדאין",
    }
    row.update(extra)
    return row


class AgentRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LEADS_DEDUP_DB": os.path.join(self.tmp.name, "seen.db")})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _store(self) -> MemoryStore:
        return MemoryStore(
            {
                "Sheet1": [
                    ["Company Name", "Job Title", "Job Summary", "Job Link", "Contact Info", "Track", "Publish Date"],
                    ["OldCo", "Developer", "", "https://old/closed", "x", "A", "2026-08-04"],
                    ["StillOpen", "Backend Developer", "", "https://old/open", "x", "A", "2026-08-05"],
                    ["Intel", "Software Engineer", "", "https://old/intel", "x", "A", "2026-08-05"],
                ]
            }
        )

    def test_full_cycle(self) -> None:
        store = self._store()
        new_jobs = [
            {"company_name": "Acme", "job_title": "Backend Developer", "job_link": "https://li/acme1",
             "publish_date": "2026-10-24", "source": "LinkedIn",
             "company_link": "https://il.linkedin.com/company/acme"},
            {"company_name": "Acme", "job_title": "QA Engineer", "job_link": "https://li/acme2",
             "publish_date": "2026-10-23", "source": "LinkedIn",
             "company_link": "https://il.linkedin.com/company/acme"},
            {"company_name": "BigBank", "job_title": "Salesforce Admin", "job_link": "https://dr/bank",
             "publish_date": "אתמול", "source": "Drushim"},
            {"company_name": "Nvidia", "job_title": "Backend Developer", "job_link": "https://li/nv",
             "source": "LinkedIn"},
        ]

        async def scrape():
            return [dict(j) for j in new_jobs], ["LinkedIn / monday.com"]

        async def check(urls):
            return {u: ("closed" if "closed" in u else "open") for u in urls}

        categories = {"Backend Developer": "Backend", "QA Engineer": "QA", "Salesforce Admin": "יישום Salesforce"}

        async def classify(provider, company, title, snippet):
            return {"category": categories.get(title), "job_summary": "s"}

        async def fake_page(url):
            return ce.LinkedInPage(size="51-200", industry="Software Development", description="SaaS")

        async def fake_llm(system, user, max_tokens=0):
            if "profile Israeli employers" in system:
                if "BigBank" in user:
                    return {"size": "5001-10000", "company_type": "bank"}
                if "StillOpen" in user:
                    return {"size": None, "company_type": None}
                return {"size": "51-200", "company_type": "product"}
            if "BigBank" in user:
                return {"primary": {"name": "Ops Lead", "title": "VP Operations",
                                    "linkedin_url": "https://il.linkedin.com/in/ops"}}
            return {
                "primary": {"name": "Dana", "title": "CTO", "linkedin_url": "https://il.linkedin.com/in/dana"},
                "alternate": {"name": "Avi", "title": "VP R&D", "linkedin_url": "https://il.linkedin.com/in/avi"},
            }

        provider = FakeProvider({
            "Acme": [SearchResult("Dana - CTO - Acme", "https://il.linkedin.com/in/dana", ""),
                     SearchResult("Avi - VP R&D - Acme", "https://il.linkedin.com/in/avi", "")],
            "BigBank": [SearchResult("Ops Lead - VP Operations", "https://il.linkedin.com/in/ops", "")],
        })
        search = BudgetedSearch([provider], budget=60)

        with mock.patch("services.enrichment._classify_job", classify), \
                mock.patch.object(ce, "fetch_linkedin_company", fake_page), \
                mock.patch.object(ce, "complete_json", fake_llm), \
                mock.patch.object(cc, "complete_json", fake_llm):
            summary = asyncio.run(lead_agent.run(store, search, scrape=scrape, check=check, today=TODAY))

        self.assertEqual(summary["new_jobs"], 3)  # Nvidia blocked
        self.assertEqual(summary["archived_jobs"], 1)
        self.assertEqual(summary["removed_blocked_jobs"], 1)
        self.assertEqual(summary["empty_searches"], ["LinkedIn / monday.com"])

        jobs = store.read("Sheet1")
        links = {r["Job Link"] for r in jobs.rows}
        self.assertEqual(links, {"https://old/open", "https://li/acme1", "https://li/acme2", "https://dr/bank"})
        self.assertIn("Track", jobs.headers)  # old columns kept
        self.assertTrue(all(r["Last Checked"] == "25/10/2026" for r in jobs.rows))

        archive = store.read("Archive")
        self.assertEqual(archive.rows[0]["Job Link"], "https://old/closed")
        self.assertEqual(archive.rows[0]["Closed Date"], "25/10/2026")

        blocked = store.read("Blocked Companies")
        self.assertEqual(len(blocked.rows), 11)
        self.assertIsNotNone(store.read("Do Not Contact"))

        companies = {r["Company Name"]: r for r in store.read("Companies").rows}
        acme = companies["Acme"]
        self.assertEqual((acme["Target"], acme["Company Size"], acme["Company Type"]), ("כן", "51-200", "מוצר"))
        self.assertEqual(acme["Open Jobs"], "2")
        self.assertEqual(acme["Latest Job"], "Backend Developer")
        self.assertEqual((acme["Contact Name"], acme["Alt Contact Name"]), ("Dana", "Avi"))
        self.assertEqual(acme["Outreach Status"], "לא נשלח")
        self.assertEqual(acme["Company LinkedIn"], "https://il.linkedin.com/company/acme")
        bank = companies["BigBank"]
        self.assertEqual((bank["Target"], bank["Contact Title"]), ("כן", "VP Operations"))
        self.assertEqual(companies["StillOpen"]["Target"], "לא ידוע")
        self.assertEqual(companies["StillOpen"]["Contact Name"], "")
        self.assertEqual(list(companies)[:2], ["Acme", "BigBank"])  # targets first
        self.assertEqual(store.dropdowns["Companies"][0], "Outreach Status")

        # --- Weekly file ---
        summary_w = asyncio.run(lead_agent.weekly(store, today=TODAY))
        self.assertEqual(summary_w["new_companies"], 2)
        weekly = store.read("Weekly")
        self.assertEqual(weekly.rows[0]["Company Name"], "Acme")
        self.assertEqual(weekly.rows[0]["Other Jobs"], "QA Engineer")
        self.assertEqual(weekly.rows[0]["Contact"], "Dana — CTO")

        # --- Someone updates Acme's status; a new Acme job appears later ---
        comp = store.read("Companies")
        for r in comp.rows:
            if r["Company Name"] == "Acme":
                r["Outreach Status"] = "פולואפ נשלח"
                r["Contact Name"] = "Dana"
        store.write("Companies", comp)
        new_jobs[:] = [{"company_name": "Acme", "job_title": "Frontend Developer",
                        "job_link": "https://li/acme3", "publish_date": "2026-10-27", "source": "LinkedIn"}]
        categories["Frontend Developer"] = "Frontend"
        later = date(2026, 10, 27)
        with mock.patch("services.enrichment._classify_job", classify), \
                mock.patch.object(ce, "fetch_linkedin_company", fake_page), \
                mock.patch.object(ce, "complete_json", fake_llm), \
                mock.patch.object(cc, "complete_json", fake_llm):
            asyncio.run(lead_agent.run(store, search, scrape=scrape, check=check, today=later))
        companies = {r["Company Name"]: r for r in store.read("Companies").rows}
        self.assertEqual(companies["Acme"]["Outreach Status"], "פולואפ נשלח")  # not overwritten
        self.assertEqual(companies["Acme"]["Status Updated"], "27/10/2026")    # stamped on change
        self.assertEqual(companies["Acme"]["Latest Job"], "Frontend Developer")

        # Status updated 27/10; the Frontend job was found 27/10 → not newer. Simulate a later job.
        jobs = store.read("Sheet1")
        for r in jobs.rows:
            if r["Job Link"] == "https://li/acme3":
                r["Found Date"] = "28/10/2026"
        store.write("Sheet1", jobs)
        summary_w = asyncio.run(lead_agent.weekly(store, today=date(2026, 11, 1)))
        self.assertEqual(summary_w["returning_companies"], 1)
        weekly = store.read("Weekly")
        returning = [r for r in weekly.rows if r["Company Name"] == "Acme"][0]
        self.assertEqual(returning["Previous Outreach"], "פולואפ נשלח, 27/10/2026")
        self.assertEqual(len(store.highlights["Weekly"]), 1)

    def test_no_search_and_retry_limits(self) -> None:
        store = MemoryStore({"Sheet1": [["Company Name", "Job Title", "Job Link"]]})

        async def scrape():
            return [{"company_name": "Mystery", "job_title": "Backend Developer",
                     "job_link": "https://dr/m", "source": "Drushim"}], []

        async def check(urls):
            return {u: "unknown" for u in urls}

        async def classify(provider, company, title, snippet):
            return {"category": "Backend", "job_summary": "s"}

        calls = []

        async def fake_llm(system, user, max_tokens=0):
            calls.append(user)
            return {"size": None, "company_type": None}

        with mock.patch("services.enrichment._classify_job", classify), \
                mock.patch.object(ce, "complete_json", fake_llm):
            asyncio.run(lead_agent.run(store, None, scrape=scrape, check=check, today=TODAY))
            asyncio.run(lead_agent.run(store, None, scrape=scrape, check=check, today=date(2026, 10, 27)))
            asyncio.run(lead_agent.run(store, None, scrape=scrape, check=check, today=date(2026, 11, 2)))
        # unknown → retried only after 7 days
        self.assertEqual(len(calls), 2)
        company = store.read("Companies").rows[0]
        self.assertEqual(company["Target"], "לא ידוע")
        self.assertEqual(store.read("Sheet1").rows[0].get("Last Checked", ""), "25/10/2026")


if __name__ == "__main__":
    unittest.main()


class AppsScriptStoreTests(unittest.TestCase):
    """AppsScriptStore against a fake web app that mirrors scripts/leads_sheet_webapp.gs."""

    def _transport(self, tabs: dict, token: str = "t0k"):
        import json as _json

        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":  # Apps Script answers POST with a redirect
                body = _json.loads(request.content)
                if body.get("token") != token:
                    result = {"error": "unauthorized"}
                else:
                    a, tab = body["action"], body.get("tab")
                    if a == "ping":
                        result = {"ok": True, "name": "Leads"}
                    elif a == "read":
                        result = {"exists": tab in tabs, "values": tabs.get(tab, [])}
                    elif a == "write":
                        tabs[tab] = body["values"]
                        result = {"ok": True}
                    elif a == "append":
                        tabs.setdefault(tab, [body["headers"]])
                        tabs[tab][0] = body["headers"]
                        tabs[tab].extend(body["rows"])
                        result = {"ok": True}
                    else:
                        result = {"ok": True}
                handler.last = result
                return httpx.Response(302, headers={"Location": "https://script.googleusercontent.com/echo"})
            return httpx.Response(200, json=handler.last)

        return httpx.MockTransport(handler)

    def test_round_trip(self) -> None:
        from services.lead_sheet import AppsScriptStore, Table

        tabs = {"Sheet1": [["Company Name", "Job Link"], ["Acme", "https://x"]]}
        store = AppsScriptStore("https://script.google.com/macros/s/X/exec", "t0k", "SHEET",
                                transport=self._transport(tabs))
        self.assertEqual(store.ping()["name"], "Leads")
        self.assertIsNone(store.read("Companies"))
        table = store.read("Sheet1")
        self.assertEqual(table.rows, [{"Company Name": "Acme", "Job Link": "https://x"}])
        table.ensure_headers(["Found Date"])
        table.rows[0]["Found Date"] = "25/10/2026"
        store.write("Sheet1", table)
        self.assertEqual(tabs["Sheet1"][1], ["Acme", "https://x", "25/10/2026"])
        store.append("Archive", Table(headers=["A", "B"]), [{"A": "1", "B": "2"}])
        self.assertEqual(tabs["Archive"], [["A", "B"], ["1", "2"]])
        self.assertTrue(store.url.endswith("/SHEET/edit"))

    def test_bad_token(self) -> None:
        from services.lead_sheet import AppsScriptStore

        store = AppsScriptStore("https://script.google.com/x", "wrong", "S", transport=self._transport({}))
        with self.assertRaisesRegex(RuntimeError, "unauthorized"):
            store.read("Sheet1")
