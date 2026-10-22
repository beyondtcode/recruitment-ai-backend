"""Lead agent orchestration: the every-2-days run and the weekly campaign file.

run():
  1. Remove jobs of blocked companies; check open jobs, move closed ones to Archive.
  2. Collect new jobs (scrape → filter → dedup → classify) and add them to the jobs tab.
  3. Build / update the Companies tab: size, type, target audience.
  4. Find a contact + alternate for target companies that have none.

weekly():
  Rebuild the Weekly tab from Companies + jobs (one row per company).

Env vars:
  LEADS_MAX_COMPANY_ENRICH (default 80)  — company lookups per run
  LEADS_MAX_CONTACT_ENRICH (default 40)  — contact lookups per run
  LEADS_ENRICH_REFRESH_DAYS (default 90) — re-check size/type after N days
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from services.company_enrichment import (
    UNKNOWN,
    YES,
    NO,
    CompanyInfo,
    compute_target,
    enrich_company,
)
from services.contact_enrichment import NOT_FOUND, choose_profile, find_contacts
from services.job_status import check_jobs
from services.lead_dedup import _db_path
from services.lead_filters import (
    BLOCKED_COMPANIES,
    Blocklist,
    format_ddmmyyyy,
    normalize_company,
    today_israel,
)
from services.lead_pipeline import run_lead_pipeline, scrape_all_raw
from services.lead_sheet import (
    ARCHIVE_HEADERS,
    BLOCKED_HEADERS,
    COMPANY_HEADERS,
    DO_NOT_CONTACT_HEADERS,
    JOB_HEADERS,
    OUTREACH_STATUSES,
    TAB_ARCHIVE,
    TAB_BLOCKED,
    TAB_COMPANIES,
    TAB_DO_NOT_CONTACT,
    TAB_WEEKLY,
    WEEKLY_HEADERS,
    SheetStore,
    Table,
    jobs_tab,
)
from services.search_client import BudgetExceeded, SearchClient

logger = logging.getLogger(__name__)

NOT_SENT = OUTREACH_STATUSES[0]  # "לא נשלח"
RETURNING_COLOR = (1.0, 0.95, 0.8)  # light amber
ENRICH_CONCURRENCY = 4

_agent_lock = asyncio.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def parse_date(text: str) -> date | None:
    """Parse DD/MM/YYYY or YYYY-MM-DD (older rows)."""
    t = (text or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(t[:10], fmt).date()
        except ValueError:
            continue
    return None


def _key(name: str) -> str:
    return normalize_company(name) or (name or "").strip().casefold()


# ---------------------------------------------------------------------------
# Status-change tracking (fills "Status Updated" when someone edits the status)
# ---------------------------------------------------------------------------


def _status_db() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS company_status ("
        "company_key TEXT PRIMARY KEY, status TEXT NOT NULL, status_updated TEXT NOT NULL)"
    )
    return conn


def _apply_status_tracking(rows: list[dict[str, str]], today_s: str) -> None:
    conn = _status_db()
    try:
        known = {
            k: (s, u)
            for k, s, u in conn.execute("SELECT company_key, status, status_updated FROM company_status")
        }
        for row in rows:
            key = _key(row.get("Company Name", ""))
            status = (row.get("Outreach Status") or "").strip()
            if not status:
                status = NOT_SENT
                row["Outreach Status"] = status
            updated = (row.get("Status Updated") or "").strip()
            prev = known.get(key)
            if prev is None:
                if not updated:
                    row["Status Updated"] = today_s
            elif status != prev[0] and updated == prev[1]:
                # Status edited by hand but the date was not → stamp today.
                row["Status Updated"] = today_s
        conn.executemany(
            "INSERT INTO company_status (company_key, status, status_updated) VALUES (?, ?, ?) "
            "ON CONFLICT(company_key) DO UPDATE SET status=excluded.status, "
            "status_updated=excluded.status_updated",
            [
                (_key(r.get("Company Name", "")), r.get("Outreach Status", ""), r.get("Status Updated", ""))
                for r in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Retry limits for lookups that found nothing ("לא ידוע" / "לא נמצא")
# ---------------------------------------------------------------------------

MAX_ATTEMPTS = 3
RETRY_AFTER_DAYS = 7


def _attempts_db() -> sqlite3.Connection:
    conn = _status_db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS enrich_attempts ("
        "company_key TEXT NOT NULL, kind TEXT NOT NULL, attempts INTEGER NOT NULL, "
        "last_attempt TEXT NOT NULL, PRIMARY KEY (company_key, kind))"
    )
    return conn


def _load_attempts() -> dict[tuple[str, str], tuple[int, date]]:
    conn = _attempts_db()
    try:
        return {
            (k, kind): (n, date.fromisoformat(last))
            for k, kind, n, last in conn.execute(
                "SELECT company_key, kind, attempts, last_attempt FROM enrich_attempts"
            )
        }
    finally:
        conn.close()


def _save_attempts(updates: dict[tuple[str, str], tuple[int, date]]) -> None:
    if not updates:
        return
    conn = _attempts_db()
    try:
        conn.executemany(
            "INSERT INTO enrich_attempts (company_key, kind, attempts, last_attempt) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(company_key, kind) DO UPDATE SET attempts=excluded.attempts, "
            "last_attempt=excluded.last_attempt",
            [(k, kind, n, d.isoformat()) for (k, kind), (n, d) in updates.items()],
        )
        conn.commit()
    finally:
        conn.close()


def _may_retry(attempts: dict, key: str, kind: str, today: date) -> bool:
    record = attempts.get((key, kind))
    if record is None:
        return True
    n, last = record
    return n < MAX_ATTEMPTS and (today - last).days >= RETRY_AFTER_DAYS


# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------


def load_blocklist(store: SheetStore) -> Blocklist:
    table = store.read(TAB_BLOCKED)
    if table is None or not table.rows:
        table = Table(headers=list(BLOCKED_HEADERS))
        table.rows = [
            {"Company": name, "Name Variants": ", ".join(variants)}
            for name, variants in BLOCKED_COMPANIES.items()
        ]
        store.write(TAB_BLOCKED, table)
        logger.info("lead_agent: seeded %s tab with %d companies", TAB_BLOCKED, len(table.rows))
    entries: dict[str, list[str]] = {}
    for row in table.rows:
        name = (row.get("Company") or "").strip()
        if not name:
            continue
        variants = [v.strip() for v in (row.get("Name Variants") or "").split(",") if v.strip()]
        entries[name] = variants
    return Blocklist(entries)


def load_do_not_contact(store: SheetStore) -> set[str]:
    table = store.read(TAB_DO_NOT_CONTACT)
    if table is None:
        store.write(TAB_DO_NOT_CONTACT, Table(headers=list(DO_NOT_CONTACT_HEADERS)))
        return set()
    return {_key(r.get("Company Name", "")) for r in table.rows if r.get("Company Name", "").strip()}


def _job_date(row: dict[str, str]) -> date:
    return (
        parse_date(row.get("Publish Date", ""))
        or parse_date(row.get("Found Date", ""))
        or date.min
    )


def _latest_job(jobs: list[dict[str, str]]) -> dict[str, str]:
    return max(jobs, key=_job_date)


# ---------------------------------------------------------------------------
# The every-2-days run
# ---------------------------------------------------------------------------


async def _gather_limited(funcs: list[Callable[[], Awaitable[Any]]], limit: int) -> list[Any]:
    sem = asyncio.Semaphore(limit)

    async def wrap(fn: Callable[[], Awaitable[Any]]) -> Any:
        async with sem:
            return await fn()

    return await asyncio.gather(*(wrap(f) for f in funcs))


async def run(
    store: SheetStore,
    search: SearchClient | None,
    *,
    scrape: Callable[[], Awaitable[tuple[list[dict], list[str]]]] = scrape_all_raw,
    check: Callable[[list[str]], Awaitable[dict[str, str]]] = check_jobs,
    today: date | None = None,
) -> dict[str, Any]:
    async with _agent_lock:
        return await _run(store, search, scrape=scrape, check=check, today=today or today_israel())


async def _run(store, search, *, scrape, check, today: date) -> dict[str, Any]:
    today_s = format_ddmmyyyy(today)
    summary: dict[str, Any] = {"sheet_url": store.url, "search_enabled": search is not None}

    blocklist = load_blocklist(store)
    do_not_contact = load_do_not_contact(store)

    jobs = store.read(jobs_tab()) or Table(headers=[])
    jobs.ensure_headers(JOB_HEADERS)
    companies = store.read(TAB_COMPANIES) or Table(headers=[])
    companies.ensure_headers(COMPANY_HEADERS)
    company_rows = {_key(r.get("Company Name", "")): r for r in companies.rows}

    # 1. Blocked companies out, closed jobs to Archive.
    kept: list[dict[str, str]] = []
    removed_blocked = 0
    for row in jobs.rows:
        if blocklist.match(row.get("Company Name", "")):
            removed_blocked += 1
        else:
            kept.append(row)

    links = [r["Job Link"].strip() for r in kept if r.get("Job Link", "").strip().startswith("http")]
    statuses = await check(links) if links else {}
    open_rows: list[dict[str, str]] = []
    archived: list[dict[str, str]] = []
    for row in kept:
        status = statuses.get(row.get("Job Link", "").strip(), "unknown")
        if status == "closed":
            company = company_rows.get(_key(row.get("Company Name", "")), {})
            archived.append(
                {**row, "Closed Date": today_s, "Outreach Status": company.get("Outreach Status", "")}
            )
            continue
        if status == "open":
            row["Last Checked"] = today_s
        open_rows.append(row)

    # 2. New jobs.
    raw, empty_searches = await scrape()
    known_links = {r.get("Job Link", "").strip() for r in jobs.rows}
    leads = await run_lead_pipeline(raw, blocklist=blocklist, known_links=known_links)
    new_rows = []
    for lead in leads:
        row = {k: str(v) for k, v in lead.model_dump(by_alias=True).items()}
        row["Last Checked"] = today_s
        new_rows.append(row)
    jobs.rows = open_rows + new_rows

    # 3. Companies.
    by_company: dict[str, list[dict[str, str]]] = {}
    for row in jobs.rows:
        name = (row.get("Company Name") or "").strip()
        if name and name.casefold() != "unknown":
            by_company.setdefault(_key(name), []).append(row)

    for key, company_jobs in by_company.items():
        row = company_rows.get(key)
        if row is None:
            row = {h: "" for h in companies.headers}
            row["Company Name"] = company_jobs[0]["Company Name"].strip()
            row["Outreach Status"] = NOT_SENT
            company_rows[key] = row
        latest = _latest_job(company_jobs)
        row["Open Jobs"] = str(len(company_jobs))
        row["Latest Job"] = latest.get("Job Title", "")
        row["Latest Job Link"] = latest.get("Job Link", "")
        row["Category"] = latest.get("Category", "")
        row["Outreach Phrasing"] = latest.get("Outreach Phrasing", "")
        found = [d for d in (parse_date(j.get("Found Date", "")) for j in company_jobs) if d]
        if found:
            row["Last Job Found"] = format_ddmmyyyy(max(found))
    for key, row in company_rows.items():
        if key not in by_company:
            row["Open Jobs"] = "0"

    # 3b. Size / type enrichment.
    attempts = _load_attempts()
    attempt_updates: dict[tuple[str, str], tuple[int, date]] = {}
    refresh = timedelta(days=_env_int("LEADS_ENRICH_REFRESH_DAYS", 90))
    to_enrich = []
    for key, row in company_rows.items():
        if key not in by_company or key in do_not_contact:
            continue
        enriched_on = parse_date(row.get("Last Enriched", ""))
        unknown = row.get("Company Size", "") in ("", UNKNOWN) or row.get("Company Type", "") in ("", UNKNOWN)
        if enriched_on is None:
            to_enrich.append(key)
        elif unknown:
            if _may_retry(attempts, key, "company", today):
                to_enrich.append(key)
        elif today - enriched_on >= refresh:
            to_enrich.append(key)
    # Newest companies first, capped per run.
    to_enrich.sort(key=lambda k: parse_date(company_rows[k].get("Last Job Found", "")) or date.min, reverse=True)
    to_enrich = to_enrich[: _env_int("LEADS_MAX_COMPANY_ENRICH", 80)]
    # Company lookups may use at most half of the run's search budget, so
    # contacts for target companies always get searches too.
    company_search_cap = (search.budget // 2) if search is not None and hasattr(search, "budget") else 0
    skipped_for_budget = 0

    def company_link_for(key: str) -> str:
        row = company_rows[key]
        if row.get("Company LinkedIn"):
            return row["Company LinkedIn"]
        for j in by_company.get(key, []):
            if j.get("Company LinkedIn"):
                return j["Company LinkedIn"]
        return ""

    async def enrich_one(key: str) -> None:
        nonlocal skipped_for_budget
        row = company_rows[key]
        titles = [j.get("Job Title", "") for j in by_company[key]]
        link = company_link_for(key)
        use_search = search
        if search is not None and getattr(search, "used", 0) >= company_search_cap:
            if not link:
                skipped_for_budget += 1
                return  # no free source and no budget left for companies: try next run
            use_search = None
        info = await enrich_company(row["Company Name"], titles, use_search, link)
        row["Company Size"] = info.size
        row["Company Type"] = info.company_type
        if info.linkedin_url:
            row["Company LinkedIn"] = info.linkedin_url
        row["Last Enriched"] = today_s
        if info.size == UNKNOWN or info.company_type == UNKNOWN:
            n = attempts.get((key, "company"), (0, today))[0]
            attempt_updates[(key, "company")] = (n + 1, today)

    await _gather_limited([lambda k=k: enrich_one(k) for k in to_enrich], ENRICH_CONCURRENCY)

    # 3c. Target audience.
    reasons: dict[str, str] = {}
    for key, row in company_rows.items():
        info = CompanyInfo(size=row.get("Company Size") or UNKNOWN, company_type=row.get("Company Type") or UNKNOWN)
        categories = {j.get("Category", "") for j in by_company.get(key, [])}
        target, reason = compute_target(info, categories, do_not_contact=key in do_not_contact)
        row["Target"] = target
        reasons[key] = reason

    # 4. Contacts for target companies without one.
    need_contact = []
    for key, row in company_rows.items():
        if row.get("Target") != YES or key not in by_company:
            continue
        contact = row.get("Contact Name", "").strip()
        if contact == "":
            need_contact.append(key)
        elif contact == NOT_FOUND and _may_retry(attempts, key, "contact", today):
            need_contact.append(key)
    need_contact.sort(key=lambda k: parse_date(company_rows[k].get("Last Job Found", "")) or date.min, reverse=True)
    need_contact = need_contact[: _env_int("LEADS_MAX_CONTACT_ENRICH", 40)]
    contacts_found = 0
    contacts_searched = 0

    async def contact_one(key: str) -> None:
        nonlocal contacts_found, contacts_searched
        if getattr(search, "remaining", 1) <= 0:
            return  # budget used up: stays empty / "לא נמצא", retried next run
        contacts_searched += 1
        row = company_rows[key]
        profile = choose_profile(reasons.get(key, ""), row.get("Category", ""))
        try:
            primary, alternate = await find_contacts(
                row["Company Name"], profile, row.get("Category", ""), search
            )
        except BudgetExceeded:
            contacts_searched -= 1
            return
        if primary is None:
            row["Contact Name"] = NOT_FOUND
            n = attempts.get((key, "contact"), (0, today))[0]
            attempt_updates[(key, "contact")] = (n + 1, today)
            return
        contacts_found += 1
        row["Contact Name"], row["Contact Title"], row["Contact LinkedIn"] = (
            primary.name, primary.title, primary.linkedin_url,
        )
        if alternate is not None:
            row["Alt Contact Name"], row["Alt Contact Title"], row["Alt Contact LinkedIn"] = (
                alternate.name, alternate.title, alternate.linkedin_url,
            )
        else:
            row["Alt Contact Name"] = NOT_FOUND

    if search is not None:
        # The search client hard-caps the budget; extra lookups just get no results.
        await _gather_limited([lambda k=k: contact_one(k) for k in need_contact], ENRICH_CONCURRENCY)
    _save_attempts(attempt_updates)

    # 5. Status tracking, ordering, write back.
    rows = list(company_rows.values())
    _apply_status_tracking(rows, today_s)
    order = {YES: 0, UNKNOWN: 1, NO: 2}
    rows.sort(
        key=lambda r: (
            order.get(r.get("Target", ""), 3),
            -(parse_date(r.get("Last Job Found", "")) or date.min).toordinal(),
        )
    )
    companies.rows = rows

    store.write(jobs_tab(), jobs)
    store.write(TAB_COMPANIES, companies)
    store.set_dropdown(TAB_COMPANIES, "Outreach Status", OUTREACH_STATUSES)
    if archived:
        archive = store.read(TAB_ARCHIVE) or Table(headers=[])
        archive.ensure_headers(ARCHIVE_HEADERS)
        store.append(TAB_ARCHIVE, archive, archived)

    summary.update(
        {
            "new_jobs": len(new_rows),
            "archived_jobs": len(archived),
            "removed_blocked_jobs": removed_blocked,
            "open_jobs": len(jobs.rows),
            "companies": len(rows),
            "target_companies": sum(1 for r in rows if r.get("Target") == YES),
            "companies_enriched": len(to_enrich),
            "companies_waiting_for_budget": skipped_for_budget,
            "contacts_searched": contacts_searched,
            "contacts_found": contacts_found,
            "searches_used": getattr(search, "used", 0) if search is not None else 0,
            "searches_by_provider": dict(getattr(search, "by_provider", {}) or {}),
            "empty_searches": empty_searches,
            "link_checks": {
                s: sum(1 for v in statuses.values() if v == s) for s in ("open", "closed", "unknown")
            },
        }
    )
    logger.info("lead_agent: run summary %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Weekly campaign file
# ---------------------------------------------------------------------------


def _contact_text(name: str, title: str) -> str:
    name = (name or "").strip()
    if not name or name == NOT_FOUND:
        return ""
    return f"{name} — {title}".strip(" —") if title else name


async def weekly(store: SheetStore, *, today: date | None = None) -> dict[str, Any]:
    async with _agent_lock:
        return _weekly(store, today or today_israel())


def _weekly(store: SheetStore, today: date) -> dict[str, Any]:
    week_ago = today - timedelta(days=7)
    companies = store.read(TAB_COMPANIES) or Table(headers=list(COMPANY_HEADERS))
    jobs = store.read(jobs_tab()) or Table(headers=list(JOB_HEADERS))
    do_not_contact = load_do_not_contact(store)

    by_company: dict[str, list[dict[str, str]]] = {}
    for row in jobs.rows:
        by_company.setdefault(_key(row.get("Company Name", "")), []).append(row)

    new_rows: list[dict[str, str]] = []
    returning_rows: list[dict[str, str]] = []
    for company in companies.rows:
        key = _key(company.get("Company Name", ""))
        company_jobs = by_company.get(key, [])
        contact = (company.get("Contact Name") or "").strip()
        if company.get("Target") != YES or key in do_not_contact or not company_jobs:
            continue
        if not contact or contact == NOT_FOUND:
            continue
        recently_open = any(
            (d := parse_date(j.get("Last Checked", ""))) and d >= week_ago
            or (f := parse_date(j.get("Found Date", ""))) and f >= week_ago
            for j in company_jobs
        )
        if not recently_open:
            continue

        status = (company.get("Outreach Status") or "").strip() or NOT_SENT
        status_date = parse_date(company.get("Status Updated", ""))
        if status == NOT_SENT:
            section = new_rows
        else:
            newer = [
                j for j in company_jobs
                if (f := parse_date(j.get("Found Date", ""))) and status_date and f > status_date
            ]
            if not newer:
                continue
            section = returning_rows

        latest = _latest_job(company_jobs)
        others = [j.get("Job Title", "") for j in company_jobs if j is not latest]
        section.append(
            {
                "Company Name": company.get("Company Name", ""),
                "Company Size": company.get("Company Size", ""),
                "Latest Job": latest.get("Job Title", ""),
                "Latest Job Link": latest.get("Job Link", ""),
                "Other Jobs": "; ".join(t for t in others if t),
                "Outreach Phrasing": latest.get("Outreach Phrasing", ""),
                "Contact": _contact_text(contact, company.get("Contact Title", "")),
                "Contact LinkedIn": company.get("Contact LinkedIn", ""),
                "Alt Contact": _contact_text(company.get("Alt Contact Name", ""), company.get("Alt Contact Title", "")),
                "Alt Contact LinkedIn": company.get("Alt Contact LinkedIn", ""),
                "Previous Outreach": "" if status == NOT_SENT else f"{status}, {company.get('Status Updated', '')}".strip(", "),
            }
        )

    table = Table(headers=list(WEEKLY_HEADERS))
    table.rows = list(new_rows)
    highlight: list[int] = []
    if returning_rows:
        table.rows.append({h: "" for h in WEEKLY_HEADERS})
        table.rows.append({"Company Name": "חברות שכבר פנו אליהן ופרסמו משרה חדשה"})
        start = len(table.rows) + 2  # +1 header row, +1 for 1-based numbering
        table.rows.extend(returning_rows)
        highlight = list(range(start, start + len(returning_rows)))
    # Table.from_values drops blank rows, so write the separator as a single space.
    for row in table.rows:
        if not any(row.values()):
            row["Company Name"] = " "
    store.write(TAB_WEEKLY, table, reset_colors=True)
    store.highlight_rows(TAB_WEEKLY, highlight, RETURNING_COLOR)

    summary = {
        "sheet_url": store.url,
        "new_companies": len(new_rows),
        "returning_companies": len(returning_rows),
        "total_companies": len(new_rows) + len(returning_rows),
    }
    logger.info("lead_agent: weekly summary %s", summary)
    return summary
