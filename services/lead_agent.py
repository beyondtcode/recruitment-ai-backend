"""Lead agent orchestration: the every-2-days run and the weekly campaign file.

run():
  1. Remove jobs of blocked companies; check open jobs, move closed ones to Archive.
  2. Collect new jobs (scrape → filter → dedup → classify) and add them to the jobs tab.
  3. Build / update the Companies tab: size, type, target audience.
  4. Find a contact + alternate for target companies that have none.

weekly():
  Rebuild the Weekly tab from Companies + jobs (one row per company).

Env vars:
  LEADS_MAX_COMPANY_ENRICH (default 0 = no limit) — company lookups per run
  LEADS_MAX_CONTACT_ENRICH (default 0 = no limit) — contact lookups per run
  (paid searches have no per-run cap; a Slack alert warns before the monthly
  Tavily quota runs out)
  LEADS_ENRICH_REFRESH_DAYS (default 90) — re-check size/type after N days
  LEADS_MAX_LINK_CHECKS (default 0 = all) — job links checked for "closed" per run
  LEADS_CONTACT_REFRESH_DAYS (default 60) — re-check a contact when a new job appears after N days
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from services.company_enrichment import (
    _TYPE_BY_LABEL,
    EXCLUDED_ALWAYS,
    _size_bounds,
    SYSTEMS_CATEGORIES,
    UNKNOWN,
    YES,
    NO,
    CompanyInfo,
    compute_target,
    enrich_company,
    fit_reason,
)
from services.contact_enrichment import NOT_FOUND, choose_profile, find_contacts
from services.job_status import check_jobs
from services.lead_dedup import _db_path, all_seen_links
from services.lead_filters import (
    BLOCKED_ADDITIONS,
    BLOCKED_COMPANIES,
    Blocklist,
    format_ddmmyyyy,
    normalize_company,
    today_israel,
)
from services.lead_pipeline import run_lead_pipeline, scrape_all_raw
from services.scraper import KNOWN_JOB_LINKS
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
    TAB_COMPANIES_ARCHIVE,
    TAB_DO_NOT_CONTACT,
    TAB_WATCH,
    TAB_OTHER_COMPANIES,
    TAB_OTHER_JOBS,
    TAB_WEEKLY,
    WEEKLY_HEADERS,
    SheetStore,
    Table,
    jobs_tab,
)
from services.search_client import BudgetExceeded, SearchClient, quota_alert, tavily_usage
from services.watch_list import (
    ANY_SIZE_DEFAULT,
    ANY_SIZE_YES,
    WATCH_HEADERS,
    WATCH_SEED,
    WatchCompany,
    WatchList,
    scrape_watch_list,
)

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


# A company gets one row per outreach track: development roles go to the
# CTO / VP R&D, Salesforce / monday.com roles to whoever owns those systems.
TRACK_DEV = "פיתוח"
TRACK_SYSTEMS = "מערכות"


def track_for(category: str) -> str:
    return TRACK_SYSTEMS if (category or "").strip() in SYSTEMS_CATEGORIES else TRACK_DEV


def _row_key(company_name: str, track: str) -> str:
    return f"{_key(company_name)}|{track or TRACK_DEV}"


def _company_row_key(row: dict[str, str]) -> str:
    return _row_key(row.get("Company Name", ""), row.get("Outreach Track") or TRACK_DEV)


def _job_row_key(row: dict[str, str]) -> str:
    return _row_key(row.get("Company Name", ""), track_for(row.get("Category", "")))


# Copied from Companies into every job row (read-only there; edit in Companies).
MIRRORED_COLUMNS: tuple[str, ...] = (
    "Fit",
    "Company Size",
    "Company Type",
    "Target",
    "Contact Name",
    "Contact Title",
    "Contact LinkedIn",
    "Alt Contact Name",
    "Alt Contact Title",
    "Alt Contact LinkedIn",
)


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
    """Rows are Companies rows; keyed by company + outreach track."""
    conn = _status_db()
    try:
        known = {
            k: (s, u)
            for k, s, u in conn.execute("SELECT company_key, status, status_updated FROM company_status")
        }
        for row in rows:
            key = _company_row_key(row)
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
                (_company_row_key(r), r.get("Outreach Status", ""), r.get("Status Updated", ""))
                for r in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _apply_job_status_tracking(rows: list[dict[str, str]], today_s: str) -> None:
    """Each job has its own Outreach Status (edited in the Jobs tab). Stamps
    "Status Updated" when a job's status was changed by hand."""
    conn = _status_db()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS job_status ("
            "job_link TEXT PRIMARY KEY, status TEXT NOT NULL, status_updated TEXT NOT NULL)"
        )
        known = {k: (s, u) for k, s, u in conn.execute("SELECT job_link, status, status_updated FROM job_status")}
        changes = []
        for row in rows:
            link = (row.get("Job Link") or "").strip()
            status = (row.get("Outreach Status") or "").strip() or NOT_SENT
            row["Outreach Status"] = status
            updated = (row.get("Status Updated") or "").strip()
            prev = known.get(link)
            if prev is None:
                if not updated:
                    row["Status Updated"] = today_s
            elif status != prev[0] and updated == prev[1]:
                row["Status Updated"] = today_s  # status edited by hand, date not
            if link:
                changes.append((link, row["Outreach Status"], row.get("Status Updated", "")))
        conn.executemany(
            "INSERT INTO job_status (job_link, status, status_updated) VALUES (?, ?, ?) "
            "ON CONFLICT(job_link) DO UPDATE SET status=excluded.status, status_updated=excluded.status_updated",
            changes,
        )
        conn.commit()
    finally:
        conn.close()


def _weekly_baseline() -> dict[str, str]:
    conn = _status_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS weekly_status (job_link TEXT PRIMARY KEY, status TEXT NOT NULL)")
        return {k: s for k, s in conn.execute("SELECT job_link, status FROM weekly_status")}
    finally:
        conn.close()


def _save_weekly_baseline(pairs: list[tuple[str, str]]) -> None:
    conn = _status_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS weekly_status (job_link TEXT PRIMARY KEY, status TEXT NOT NULL)")
        conn.execute("DELETE FROM weekly_status")
        conn.executemany("INSERT OR REPLACE INTO weekly_status (job_link, status) VALUES (?, ?)", pairs)
        conn.commit()
    finally:
        conn.close()


def _pull_weekly_edits(weekly: Table | None, job_rows: list[dict[str, str]], today_s: str = "") -> int:
    """Outreach Status edited in the Weekly tab → the same job in Jobs.

    Each Weekly row stands for its "Latest Job Link". A Weekly status counts
    as an edit when it differs from what the agent last wrote there; if that
    job was also changed in Jobs since then, Jobs wins. Returns the number of
    jobs updated.
    """
    if weekly is None or "Outreach Status" not in weekly.headers:
        return 0
    base = _weekly_baseline()
    by_link = {(r.get("Job Link") or "").strip(): r for r in job_rows}
    updated = 0
    for w in weekly.rows:
        link = (w.get("Latest Job Link") or "").strip()
        status = (w.get("Outreach Status") or "").strip()
        job = by_link.get(link)
        if job is None or not status:
            continue
        current = (job.get("Outreach Status") or "").strip() or NOT_SENT
        written = base.get(link, current)
        if status == written or current != written:
            continue  # not edited in Weekly, or edited in Jobs too (Jobs wins)
        job["Outreach Status"] = status
        if today_s:
            job["Status Updated"] = today_s
        updated += 1
    return updated


def _refresh_weekly_statuses(store: SheetStore, job_rows: list[dict[str, str]]) -> None:
    """Show each Weekly row's current job status (after a run) and remember it."""
    weekly = store.read(TAB_WEEKLY)
    if weekly is None or not weekly.rows:
        return
    weekly.canonical_headers(list(WEEKLY_HEADERS))
    by_link = {(r.get("Job Link") or "").strip(): r for r in job_rows}
    pairs = []
    for w in weekly.rows:
        link = (w.get("Latest Job Link") or "").strip()
        job = by_link.get(link)
        if job is not None:
            w["Outreach Status"] = (job.get("Outreach Status") or "").strip() or NOT_SENT
            pairs.append((link, w["Outreach Status"]))
    store.write(TAB_WEEKLY, weekly)
    store.set_dropdown(TAB_WEEKLY, "Outreach Status", OUTREACH_STATUSES)
    _save_weekly_baseline(pairs)


def _company_status_from_jobs(company: dict[str, str], jobs: list[dict[str, str]]) -> None:
    """Companies tab: the furthest-along status among the company's open jobs,
    so it is clear at a glance whether anyone has been contacted. When none of
    the open jobs has been worked on, the company keeps its last status (the
    outreach history, also used by the weekly file for returning companies)."""
    order = {s: i for i, s in enumerate(OUTREACH_STATUSES)}
    worked = [j for j in jobs if (j.get("Outreach Status") or "").strip() not in ("", NOT_SENT)]
    if not worked:
        return
    best = max(worked, key=lambda j: (order.get(j["Outreach Status"].strip(), -1),
                                      parse_date(j.get("Status Updated", "")) or date.min))
    company["Outreach Status"] = best["Outreach Status"].strip()
    company["Status Updated"] = best.get("Status Updated", "")


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


def clear_sheet(store: SheetStore) -> list[str]:
    """Empty the data tabs (headers and formatting stay). Settings tabs are kept."""
    cleared = []
    for tab, headers in (
        (jobs_tab(), JOB_HEADERS),
        (TAB_COMPANIES, COMPANY_HEADERS),
        (TAB_COMPANIES_ARCHIVE, COMPANY_HEADERS),
        (TAB_ARCHIVE, ARCHIVE_HEADERS),
        (TAB_WEEKLY, WEEKLY_HEADERS),
        (TAB_OTHER_JOBS, JOB_HEADERS),
        (TAB_OTHER_COMPANIES, COMPANY_HEADERS),
    ):
        existing = store.read(tab)
        if existing is None and tab in (TAB_OTHER_JOBS, TAB_OTHER_COMPANIES):
            continue
        table = Table(headers=list(existing.headers) if existing and existing.headers else list(headers))
        store.write(tab, table, reset_colors=(tab == TAB_WEEKLY))
        cleared.append(tab)
    return cleared


def reset_state() -> dict[str, int]:
    """Forget status history and retry counters (used when starting a new sheet)."""
    conn = _attempts_db()
    try:
        a = conn.execute("DELETE FROM enrich_attempts").rowcount
        b = conn.execute("DELETE FROM company_status").rowcount
        conn.commit()
    finally:
        conn.close()
    return {"retry_counters": max(a, 0), "status_history": max(b, 0)}


def _may_retry(attempts: dict, key: str, kind: str, today: date) -> bool:
    record = attempts.get((key, kind))
    if record is None:
        return True
    n, last = record
    return n < MAX_ATTEMPTS and (today - last).days >= RETRY_AFTER_DAYS


# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------


_BLOCKED_ADDITIONS_MARK = "blocklist-2026-10"


def _add_blocked_once(table: Table) -> bool:
    """Add BLOCKED_ADDITIONS to the tab once (new rows / missing spellings).

    Done once per server (a flag in the status database), so a company the
    team later removes from the tab is not added back.
    """
    conn = _status_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY)")
        if conn.execute("SELECT 1 FROM flags WHERE name = ?", (_BLOCKED_ADDITIONS_MARK,)).fetchone():
            return False
        conn.execute("INSERT INTO flags (name) VALUES (?)", (_BLOCKED_ADDITIONS_MARK,))
        conn.commit()
    finally:
        conn.close()
    rows = {(r.get("Company") or "").strip(): r for r in table.rows}
    for name, variants in BLOCKED_ADDITIONS.items():
        row = rows.get(name)
        if row is None:
            table.rows.append({"Company": name, "Name Variants": ", ".join(variants)})
            continue
        have = [v.strip() for v in (row.get("Name Variants") or "").split(",") if v.strip()]
        row["Name Variants"] = ", ".join(have + [v for v in variants if v not in have])
    logger.info("lead_agent: added %d companies / spellings to %s", len(BLOCKED_ADDITIONS), TAB_BLOCKED)
    return True


_SYSTEMS_TITLE_RE = re.compile(r"salesforce|monday|מאנדיי|\bcrm\b", re.IGNORECASE)


def _known_not_fit(
    company_rows: dict[str, dict[str, str]], do_not_contact: set[str], watch: WatchList | None
) -> Callable[[dict], bool]:
    """True for a scraped job whose company is already known not to fit,
    whatever the job's category, so the LLM call can be skipped.

    Category matters only for the Salesforce / monday exception (any company of
    51+), so a job whose title looks like that is still classified, as are
    jobs of companies not seen before, with unknown size, or marked Any Size.
    """
    by_company: dict[str, list[dict[str, str]]] = {}
    for r in company_rows.values():
        by_company.setdefault(_key(r.get("Company Name", "")), []).append(r)

    def check(job: dict) -> bool:
        name = job.get("company_name") or ""
        key = _key(name)
        if key in do_not_contact:
            return True
        rows = by_company.get(key)
        if not rows or any(r.get("Target") != NO for r in rows):
            return False
        if watch is not None and watch.any_size(name, [job.get("company_link") or ""]):
            return False
        type_key = _TYPE_BY_LABEL.get(rows[0].get("Company Type") or "")
        if type_key in EXCLUDED_ALWAYS:
            return True
        bounds = _size_bounds(rows[0].get("Company Size") or UNKNOWN)
        if bounds is None:
            return False
        if bounds[1] <= 50:
            return True  # too small even for the Salesforce / monday exception
        return not _SYSTEMS_TITLE_RE.search(job.get("job_title") or "")

    return check


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
    if _add_blocked_once(table):
        store.write(TAB_BLOCKED, table)
    entries: dict[str, list[str]] = {}
    for row in table.rows:
        name = (row.get("Company") or "").strip()
        if not name:
            continue
        variants = [v.strip() for v in (row.get("Name Variants") or "").split(",") if v.strip()]
        entries[name] = variants
    return Blocklist(entries)


def load_watch_list(store: SheetStore) -> WatchList:
    table = store.read(TAB_WATCH)
    if table is None:
        table = Table(headers=list(WATCH_HEADERS))
        table.rows = [{"Company Name": n, "Company LinkedIn": u, "Note": note} for n, u, note in WATCH_SEED]
        for r in table.rows:
            r["Any Size"] = ANY_SIZE_YES if normalize_company(r["Company Name"]) in ANY_SIZE_DEFAULT else ""
        store.write(TAB_WATCH, table)
        logger.info("lead_agent: seeded %s with %d companies", TAB_WATCH, len(table.rows))
    elif "Any Size" not in table.headers:
        # One-time: add the column (after Company LinkedIn) and mark the default companies.
        table.canonical_headers(list(WATCH_HEADERS))
        for r in table.rows:
            name = normalize_company(r.get("Company Name", ""))
            short = normalize_company(re.split(r"\s*[(|–-]\s*", r.get("Company Name", ""), maxsplit=1)[0])
            r["Any Size"] = ANY_SIZE_YES if name in ANY_SIZE_DEFAULT or short in ANY_SIZE_DEFAULT else ""
        store.write(TAB_WATCH, table)
        logger.info("lead_agent: added the Any Size column to %s", TAB_WATCH)
    companies = [
        WatchCompany(
            r.get("Company Name", "").strip(), r.get("Company LinkedIn", "").strip(), r.get("Note", ""),
            any_size=(r.get("Any Size", "") or "").strip() in (ANY_SIZE_YES, "yes", "Yes", "YES", "v", "V", "✓"),
        )
        for r in table.rows
        if r.get("Company Name", "").strip()
    ]
    return WatchList(companies)


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
    scrape_watch: Callable[[WatchList], Awaitable[tuple[list[dict], list[str]]]] | None = None,
    today: date | None = None,
    lookback_days: int | None = None,
) -> dict[str, Any]:
    if lookback_days and scrape is scrape_all_raw:
        scrape = lambda: scrape_all_raw(lookback_days)  # noqa: E731 — one-time backfill
    async with _agent_lock:
        return await _run(
            store, search, scrape=scrape, check=check,
            scrape_watch=scrape_watch or (lambda w: scrape_watch_list(w)),
            today=today or today_israel(),
        )


async def _run(store, search, *, scrape, check, scrape_watch, today: date) -> dict[str, Any]:
    today_s = format_ddmmyyyy(today)
    summary: dict[str, Any] = {"sheet_url": store.url, "search_enabled": search is not None}

    blocklist = load_blocklist(store)
    do_not_contact = load_do_not_contact(store)

    jobs = store.read(jobs_tab()) or Table(headers=[])
    jobs.ensure_headers(JOB_HEADERS)
    # Jobs of non-target companies live in "Other Jobs"; process both together.
    other_jobs = store.read(TAB_OTHER_JOBS) or Table(headers=[])
    for h in other_jobs.headers:
        if h and h not in jobs.headers:
            jobs.headers.append(h)
    jobs.rows = jobs.rows + other_jobs.rows
    companies = store.read(TAB_COMPANIES) or Table(headers=[])
    companies.canonical_headers(COMPANY_HEADERS)
    companies_archive = store.read(TAB_COMPANIES_ARCHIVE) or Table(headers=[])
    companies_archive.canonical_headers(COMPANY_HEADERS)
    other_companies = store.read(TAB_OTHER_COMPANIES) or Table(headers=[])
    other_companies.canonical_headers(COMPANY_HEADERS)
    for h in companies_archive.headers + other_companies.headers:
        if h not in companies.headers:
            companies.headers.append(h)
    company_rows: dict[str, dict[str, str]] = {}
    # Archived and non-target companies are loaded too, so a company keeps its
    # history (status, contacts, size) when it posts again or becomes a target.
    for r in companies_archive.rows + other_companies.rows + companies.rows:
        r["Outreach Track"] = r.get("Outreach Track") or TRACK_DEV
        company_rows[_company_row_key(r)] = r

    # 0. Older rows used YYYY-MM-DD; normalize every date column to DD/MM/YYYY.
    for row in jobs.rows:
        for col in ("Publish Date", "Found Date", "Last Checked"):
            value = (row.get(col) or "").strip()
            if value and "/" not in value:
                d = parse_date(value)
                if d:
                    row[col] = format_ddmmyyyy(d)

    # 1. Blocked companies out, closed jobs to Archive.
    kept: list[dict[str, str]] = []
    removed_blocked = 0
    for row in jobs.rows:
        if blocklist.match(row.get("Company Name", "")):
            removed_blocked += 1
        else:
            kept.append(row)

    # Check every open job each run (longest-unchecked first, in case a cap is set).
    checkable = [r for r in kept if r.get("Job Link", "").strip().startswith("http")]
    checkable.sort(key=lambda r: parse_date(r.get("Last Checked", "")) or date.min)
    links = [r["Job Link"].strip() for r in checkable[: _env_int("LEADS_MAX_LINK_CHECKS", 0) or None]]
    statuses = await check(links) if links else {}
    open_rows: list[dict[str, str]] = []
    archived: list[dict[str, str]] = []
    for row in kept:
        status = statuses.get(row.get("Job Link", "").strip(), "unknown")
        if status == "closed":
            archived.append({**row, "Closed Date": today_s})
            continue
        if status == "open":
            row["Last Checked"] = today_s
        open_rows.append(row)

    # 2. New jobs. LinkedIn paging stops at the first page of jobs we already
    #    know (results are newest first), which keeps runs short.
    known_token = KNOWN_JOB_LINKS.set(
        all_seen_links() | {(r.get("Job Link") or "").strip() for r in jobs.rows} - {""}
    )
    try:
        raw, empty_searches = await scrape()
        watch = load_watch_list(store)
        watch_raw: list[dict] = []
        watch_without_jobs: list[str] = []
        if len(watch):
            watch_raw, watch_without_jobs = await scrape_watch(watch)
            raw = raw + watch_raw
    finally:
        KNOWN_JOB_LINKS.reset(known_token)
    known_links = {r.get("Job Link", "").strip() for r in jobs.rows}
    leads = await run_lead_pipeline(
        raw, blocklist=blocklist, known_links=known_links,
        skip_classify=_known_not_fit(company_rows, do_not_contact, watch),
    )
    new_rows = []
    for lead in leads:
        row = {k: str(v) for k, v in lead.model_dump(by_alias=True).items()}
        row["Last Checked"] = today_s
        row["Outreach Status"] = NOT_SENT
        new_rows.append(row)
    jobs.rows = open_rows + new_rows

    # 3. Companies: one row per company + outreach track.
    by_row: dict[str, list[dict[str, str]]] = {}
    for row in jobs.rows:
        name = (row.get("Company Name") or "").strip()
        if name and name.casefold() != "unknown":
            by_row.setdefault(_job_row_key(row), []).append(row)

    for rk, row_jobs in by_row.items():
        row = company_rows.get(rk)
        if row is None:
            row = {h: "" for h in companies.headers}
            row["Company Name"] = row_jobs[0]["Company Name"].strip()
            row["Outreach Track"] = track_for(row_jobs[0].get("Category", ""))
            row["Outreach Status"] = NOT_SENT
            # Share company facts already known from the company's other track.
            sibling = next(
                (r for k, r in company_rows.items() if k.split("|")[0] == rk.split("|")[0]), None
            )
            if sibling is not None:
                for col in ("Company Size", "Company Type", "Company LinkedIn", "Last Enriched"):
                    row[col] = sibling.get(col, "")
            company_rows[rk] = row
        latest = _latest_job(row_jobs)
        row["Open Jobs"] = str(len(row_jobs))
        row["Latest Job"] = latest.get("Job Title", "")
        row["Latest Job Link"] = latest.get("Job Link", "")
        row["Category"] = latest.get("Category", "")
        row["Outreach Phrasing"] = latest.get("Outreach Phrasing", "")
        found = [d for d in (parse_date(j.get("Found Date", "")) for j in row_jobs) if d]
        if found:
            row["Last Job Found"] = format_ddmmyyyy(max(found))
    for rk, row in company_rows.items():
        if rk not in by_row:
            row["Open Jobs"] = "0"

    # Company-level groups (size / type are shared by a company's tracks).
    rows_by_company: dict[str, list[str]] = {}
    for rk in company_rows:
        rows_by_company.setdefault(rk.split("|")[0], []).append(rk)
    active_companies = {rk.split("|")[0] for rk in by_row}

    def company_last_found(ck: str) -> date:
        dates = [parse_date(company_rows[rk].get("Last Job Found", "")) for rk in rows_by_company[ck]]
        return max((d for d in dates if d), default=date.min)

    # 3b. Size / type enrichment (once per company).
    attempts = _load_attempts()
    attempt_updates: dict[tuple[str, str], tuple[int, date]] = {}
    refresh = timedelta(days=_env_int("LEADS_ENRICH_REFRESH_DAYS", 90))
    to_enrich = []
    for ck in active_companies:
        if ck in do_not_contact:
            continue
        row = company_rows[rows_by_company[ck][0]]
        enriched_on = parse_date(row.get("Last Enriched", ""))
        unknown = row.get("Company Size", "") in ("", UNKNOWN) or row.get("Company Type", "") in ("", UNKNOWN)
        if enriched_on is None:
            to_enrich.append(ck)
        elif unknown:
            if _may_retry(attempts, ck, "company", today):
                to_enrich.append(ck)
        elif today - enriched_on >= refresh:
            to_enrich.append(ck)
    # Newest companies first, capped per run.
    to_enrich.sort(key=company_last_found, reverse=True)
    to_enrich = to_enrich[: _env_int("LEADS_MAX_COMPANY_ENRICH", 0) or None]
    # Company lookups may use at most half of the run's search budget, so
    # contacts for target companies always get searches too.
    budget = getattr(search, "budget", 0) if search is not None else 0
    company_search_cap = budget // 2 if budget else None  # None = no cap
    skipped_for_budget = 0

    def company_jobs(ck: str) -> list[dict[str, str]]:
        return [j for rk in rows_by_company[ck] for j in by_row.get(rk, [])]

    def company_link_for(ck: str) -> str:
        for rk in rows_by_company[ck]:
            if company_rows[rk].get("Company LinkedIn"):
                return company_rows[rk]["Company LinkedIn"]
        for j in company_jobs(ck):
            if j.get("Company LinkedIn"):
                return j["Company LinkedIn"]
        return ""

    async def enrich_one(ck: str) -> None:
        nonlocal skipped_for_budget
        name = company_rows[rows_by_company[ck][0]]["Company Name"]
        titles = [j.get("Job Title", "") for j in company_jobs(ck)]
        link = company_link_for(ck)
        use_search = search
        if search is not None and hasattr(search, "limited") and company_search_cap is not None:
            # Paid calls stop at the companies' share (enforced per call, so
            # concurrent lookups cannot eat the contacts' share).
            use_search = search.limited(company_search_cap)
        if (search is not None and company_search_cap is not None
                and getattr(search, "used", 0) >= company_search_cap and not getattr(search, "has_free", False)):
            if not link:
                skipped_for_budget += 1
                return  # no free source and no budget left for companies: try next run
            use_search = None
        info = await enrich_company(name, titles, use_search, link)
        for rk in rows_by_company[ck]:
            row = company_rows[rk]
            row["Company Size"] = info.size
            row["Company Type"] = info.company_type
            if info.linkedin_url:
                row["Company LinkedIn"] = info.linkedin_url
            row["Last Enriched"] = today_s
        if info.size == UNKNOWN or info.company_type == UNKNOWN:
            n = attempts.get((ck, "company"), (0, today))[0]
            attempt_updates[(ck, "company")] = (n + 1, today)

    await _gather_limited([lambda k=k: enrich_one(k) for k in to_enrich], ENRICH_CONCURRENCY)

    # 3c. Target audience (per track).
    for rk, row in company_rows.items():
        info = CompanyInfo(size=row.get("Company Size") or UNKNOWN, company_type=row.get("Company Type") or UNKNOWN)
        categories = {j.get("Category", "") for j in by_row.get(rk, [])}
        if not categories and row.get("Outreach Track") == TRACK_SYSTEMS:
            categories = set(SYSTEMS_CATEGORIES)
        dnc = rk.split("|")[0] in do_not_contact
        target, _reason = compute_target(info, categories, do_not_contact=dnc)
        fit = fit_reason(info, categories, do_not_contact=dnc)
        links = [row.get("Company LinkedIn", "")] + [j.get("Company LinkedIn", "") for j in by_row.get(rk, [])]
        if not dnc and categories and watch.any_size(row.get("Company Name", ""), links):
            # Watch-list company marked "Any Size": a fit whatever its size / type.
            target, fit = YES, "מתאימה (רשימת מעקב)"
        row["Target"] = target
        row["Fit"] = fit

    # 4. Contacts for target rows (and rows whose size/type is still unknown)
    #    without one; the profile follows the track.
    # Contacts the agent found more than CONTACT_REFRESH_DAYS ago are re-checked
    # when the company posts a new job (people change roles / leave). Contacts
    # typed in by hand have no "Contact Found" date and are never replaced.
    refresh_after = timedelta(days=_env_int("LEADS_CONTACT_REFRESH_DAYS", 60))
    need_contact = []
    refreshing: set[str] = set()
    for rk, row in company_rows.items():
        if row.get("Target") not in (YES, UNKNOWN) or rk not in by_row:
            continue
        contact = row.get("Contact Name", "").strip()
        if contact == "":
            need_contact.append(rk)
        elif contact == NOT_FOUND:
            if _may_retry(attempts, rk, "contact", today):
                need_contact.append(rk)
        else:
            found_on = parse_date(row.get("Contact Found", ""))
            last_job = parse_date(row.get("Last Job Found", ""))
            if found_on and last_job and last_job > found_on and today - found_on >= refresh_after:
                need_contact.append(rk)
                refreshing.add(rk)
    # Confirmed targets first, then unknown size/type; newest jobs first within each.
    need_contact.sort(key=lambda k: (
        company_rows[k].get("Target") == YES,
        parse_date(company_rows[k].get("Last Job Found", "")) or date.min,
    ), reverse=True)
    need_contact = need_contact[: _env_int("LEADS_MAX_CONTACT_ENRICH", 0) or None]
    contacts_found = 0
    contacts_searched = 0

    async def contact_one(rk: str) -> None:
        nonlocal contacts_found, contacts_searched
        if getattr(search, "remaining", 1) <= 0 and not getattr(search, "has_free", False):
            return  # budget used up: stays empty / "לא נמצא", retried next run
        contacts_searched += 1
        row = company_rows[rk]
        reason = "systems" if row.get("Outreach Track") == TRACK_SYSTEMS else ""
        profile = choose_profile(reason, row.get("Category", ""))
        try:
            primary, alternate = await find_contacts(
                row["Company Name"], profile, row.get("Category", ""), search,
                company_hint=row.get("Company LinkedIn", ""),
            )
        except BudgetExceeded:
            contacts_searched -= 1
            return
        if primary is None:
            if rk in refreshing:
                return  # keep the existing contact; try again on the next new job
            row["Contact Name"] = NOT_FOUND
            n = attempts.get((rk, "contact"), (0, today))[0]
            attempt_updates[(rk, "contact")] = (n + 1, today)
            return
        contacts_found += 1
        row["Contact Found"] = today_s
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

    # 5. Status tracking, ordering, write back. Companies without open jobs
    #    move to the Companies Archive tab (and come back when they post again).
    statuses_from_weekly = _pull_weekly_edits(store.read(TAB_WEEKLY), jobs.rows)
    _apply_job_status_tracking(jobs.rows, today_s)
    for rk, company in company_rows.items():
        _company_status_from_jobs(company, by_row.get(rk, []))
    rows = list(company_rows.values())
    _apply_status_tracking(rows, today_s)
    order = {YES: 0, UNKNOWN: 1, NO: 2}
    rows.sort(
        key=lambda r: (
            order.get(r.get("Target", ""), 3),
            -(parse_date(r.get("Last Job Found", "")) or date.min).toordinal(),
        )
    )
    active = [r for r in rows if _company_row_key(r) in by_row]
    companies.rows = [r for r in active if r.get("Target") != NO]
    other_companies.headers = list(companies.headers)
    other_companies.rows = [r for r in active if r.get("Target") == NO]
    companies_archive.headers = list(companies.headers)
    companies_archive.rows = [r for r in rows if _company_row_key(r) not in by_row]

    # Mirror company facts / contacts into each job row (read-only copy).
    for job in jobs.rows:
        company = company_rows.get(_job_row_key(job))
        if company is not None:
            for col in MIRRORED_COLUMNS:
                job[col] = company.get(col, "")

    # Jobs of non-target companies go to the "Other Jobs" tab.
    all_jobs = jobs.rows
    jobs.rows = [j for j in all_jobs if j.get("Target") != NO]
    other_jobs.headers = list(jobs.headers)
    other_jobs.rows = [j for j in all_jobs if j.get("Target") == NO]

    store.write(jobs_tab(), jobs)
    _refresh_weekly_statuses(store, jobs.rows + other_jobs.rows)
    if other_jobs.rows or store.read(TAB_OTHER_JOBS) is not None:
        store.write(TAB_OTHER_JOBS, other_jobs)
    store.write(TAB_COMPANIES, companies)
    if other_companies.rows or store.read(TAB_OTHER_COMPANIES) is not None:
        store.write(TAB_OTHER_COMPANIES, other_companies)
    store.set_dropdown(jobs_tab(), "Outreach Status", OUTREACH_STATUSES)
    if companies_archive.rows or store.read(TAB_COMPANIES_ARCHIVE) is not None:
        store.write(TAB_COMPANIES_ARCHIVE, companies_archive)
    if archived:
        archive = store.read(TAB_ARCHIVE) or Table(headers=[])
        archive.ensure_headers(ARCHIVE_HEADERS)
        store.append(TAB_ARCHIVE, archive, archived)

    summary.update(
        {
            "new_jobs": len(new_rows),
            "archived_jobs": len(archived),
            "removed_blocked_jobs": removed_blocked,
            "open_jobs": len(all_jobs),
            "target_or_unknown_jobs": len(jobs.rows),
            "other_jobs": len(other_jobs.rows),
            "companies": len({_key(r.get("Company Name", "")) for r in active}),
            "other_companies": len(other_companies.rows),
            "companies_archived": len(companies_archive.rows),
            "target_companies": len(
                {rk.split("|")[0] for rk, r in company_rows.items() if r.get("Target") == YES}
            ),
            "companies_enriched": len(to_enrich),
            "companies_waiting_for_budget": skipped_for_budget,
            "contacts_searched": contacts_searched,
            "contacts_found": contacts_found,
            "statuses_from_weekly_tab": statuses_from_weekly,
            "searches_used": getattr(search, "used", 0) if search is not None else 0,
            "searches_by_provider": dict(getattr(search, "by_provider", {}) or {}),
            "empty_searches": empty_searches,
            "watch_list_jobs_found": len(watch_raw),
            "watch_list_without_jobs": watch_without_jobs,
            "links_not_checked_yet": max(len(checkable) - len(links), 0),
            "link_checks": {
                s: sum(1 for v in statuses.values() if v == s) for s in ("open", "closed", "unknown")
            },
        }
    )
    summary.update(await _quota_status(search))
    logger.info("lead_agent: run summary %s", summary)
    return summary


async def _quota_status(search: SearchClient | None) -> dict[str, Any]:
    """Monthly Tavily usage and a Slack warning when it is about to run out."""
    run_used = getattr(search, "by_provider", {}).get("tavily", 0) if search is not None else 0
    exhausted = "tavily" in (getattr(search, "exhausted_providers", []) if search is not None else [])
    usage = await tavily_usage((os.environ.get("TAVILY_API_KEY") or "").strip())
    used, limit = usage if usage else (None, None)
    return {
        "tavily_month_used": used,
        "tavily_month_limit": limit,
        "tavily_alert": quota_alert(used, limit, run_used, exhausted),
    }


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
    # Statuses edited in last week's file go to the jobs before it is replaced.
    if _pull_weekly_edits(store.read(TAB_WEEKLY), jobs.rows, today.strftime("%d/%m/%Y")):
        store.write(jobs_tab(), jobs)

    by_row: dict[str, list[dict[str, str]]] = {}
    for row in jobs.rows:
        by_row.setdefault(_job_row_key(row), []).append(row)

    new_rows: list[dict[str, str]] = []
    returning_rows: list[dict[str, str]] = []
    for company in companies.rows:
        key = _key(company.get("Company Name", ""))
        company_jobs = by_row.get(_company_row_key(company), [])
        contact = (company.get("Contact Name") or "").strip()
        if company.get("Target") != YES or key in do_not_contact or not company_jobs:
            continue
        # Companies without a contact yet are listed too (the contact cells say
        # so), so every new fitting job reaches the weekly file.
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
                "Outreach Track": company.get("Outreach Track", "") or TRACK_DEV,
                "Latest Job": latest.get("Job Title", ""),
                "Latest Job Link": latest.get("Job Link", ""),
                "Outreach Status": (latest.get("Outreach Status") or "").strip() or NOT_SENT,
                "Other Jobs": "; ".join(t for t in others if t),
                "Outreach Phrasing": latest.get("Outreach Phrasing", ""),
                "Contact": _contact_text(contact, company.get("Contact Title", "")) if contact and contact != NOT_FOUND
                else "עוד לא נמצא איש קשר",
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
    store.set_dropdown(TAB_WEEKLY, "Outreach Status", OUTREACH_STATUSES)
    _save_weekly_baseline([
        (r["Latest Job Link"], r["Outreach Status"]) for r in new_rows + returning_rows if r.get("Latest Job Link")
    ])

    summary = {
        "sheet_url": store.url,
        "new_companies": len(new_rows),
        "returning_companies": len(returning_rows),
        "total_companies": len(new_rows) + len(returning_rows),
    }
    logger.info("lead_agent: weekly summary %s", summary)
    return summary
