"""Compare contact search on real companies (read-only; uses Tavily searches).

Run on the server:
  docker exec -w /app -e PYTHONPATH=/app recruitment-backend python scripts/contact_check.py
"""
import asyncio, os
from routers.leads import _store
from services import contact_enrichment as ce
from services.lead_agent import TRACK_SYSTEMS, YES
from services.search_client import get_search_client, tavily_usage

FOUND, MISSING = 12, 8


async def main():
    key = (os.environ.get("TAVILY_API_KEY") or "").strip()
    print("Tavily before:", await tavily_usage(key))
    rows = [r for r in _store().read("Companies").rows if r.get("Target") == YES]
    rows.sort(key=lambda r: r.get("Last Job Found", "")[6:] + r.get("Last Job Found", "")[3:5] + r.get("Last Job Found", "")[:2], reverse=True)
    found = [r for r in rows if r.get("Contact Found") and r.get("Contact Name") not in ("", ce.NOT_FOUND)][:FOUND]
    missing = [r for r in rows if r.get("Contact Name") == ce.NOT_FOUND][:MISSING]
    search = get_search_client()
    for label, group in (("HAD A CONTACT", found), ("WAS 'NOT FOUND'", missing)):
        print(f"\n######## {label} ({len(group)})")
        for r in group:
            name = r["Company Name"]
            reason = "systems" if r.get("Outreach Track") == TRACK_SYSTEMS else ""
            profile = ce.choose_profile(reason, r.get("Category", ""))
            before = search.used
            p, a = await ce.find_contacts(name, profile, r.get("Category", ""), search, company_hint=r.get("Company LinkedIn", ""))
            fmt = lambda c: f"{c.name} ({c.title})" if c else "-"
            print(f"\n{name} [{profile}, {search.used - before} searches]")
            print(f"   old: {r.get('Contact Name', '')} ({r.get('Contact Title', '')}) | alt {r.get('Alt Contact Name', '')} ({r.get('Alt Contact Title', '')})")
            print(f"   new: {fmt(p)} | alt {fmt(a)}")
            if p:
                print(f"        {p.linkedin_url}")
    print("\nTavily after:", await tavily_usage(key))


asyncio.run(main())
