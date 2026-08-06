"""SQLite-backed deduplication of matcher job URLs with a sliding lookback window."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "jobs.db"
_DEFAULT_WINDOW_DAYS = 30
_lock = threading.Lock()


def _db_path() -> Path:
    override = (os.environ.get("JOBS_DEDUP_DB") or "").strip()
    return Path(override) if override else _DEFAULT_DB


def get_window_days() -> int:
    raw = (os.environ.get("JOBS_DEDUP_DAYS") or "").strip()
    if not raw:
        return _DEFAULT_WINDOW_DAYS
    try:
        days = int(raw)
    except ValueError:
        logger.warning("JOBS_DEDUP_DAYS=%r is not an int; using %d", raw, _DEFAULT_WINDOW_DAYS)
        return _DEFAULT_WINDOW_DAYS
    return days if days > 0 else _DEFAULT_WINDOW_DAYS


def job_id_for_url(url: str) -> str:
    """Stable MD5 hex digest of a job application URL."""
    return hashlib.md5(url.strip().encode("utf-8")).hexdigest()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_link TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def filter_new_jobs(
    jobs: list[dict[str, str]],
    *,
    window_days: int | None = None,
) -> list[dict[str, str]]:
    """Return jobs whose ``job_link`` was not first-seen within the lookback window."""
    if not jobs:
        return []

    days = get_window_days() if window_days is None else window_days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with_links = [(i, j) for i, j in enumerate(jobs) if (j.get("job_link") or "").strip()]
    without_links = [j for j in jobs if not (j.get("job_link") or "").strip()]
    if not with_links:
        return list(jobs)

    links = [j["job_link"].strip() for _, j in with_links]
    with _lock:
        conn = _connect()
        try:
            placeholders = ",".join("?" * len(links))
            rows = conn.execute(
                f"""
                SELECT job_link FROM seen_jobs
                WHERE job_link IN ({placeholders})
                  AND first_seen_at > ?
                """,
                [*links, cutoff],
            ).fetchall()
            recently_seen = {row[0] for row in rows}
        finally:
            conn.close()

    new_jobs = [j for _, j in with_links if j["job_link"].strip() not in recently_seen]
    skipped = len(with_links) - len(new_jobs)
    if skipped:
        logger.info(
            "job_dedup: skipped %d job_link(s) seen within last %d day(s)",
            skipped,
            days,
        )

    # Jobs without a link cannot be deduped; pass them through once.
    return new_jobs + without_links


def mark_jobs_seen(jobs: list[dict[str, str]]) -> int:
    """Persist job_link values as seen. Resets first_seen_at on re-mark.

    Returns number of newly inserted rows (SQLite total_changes delta).
    """
    rows: list[tuple[str, str, str, str, str]] = []
    now = datetime.now(timezone.utc).isoformat()
    for job in jobs:
        link = (job.get("job_link") or "").strip()
        if not link:
            continue
        job_id = (job.get("job_id") or "").strip() or job_id_for_url(link)
        rows.append(
            (
                link,
                job_id,
                (job.get("source") or "").strip(),
                now,
                now,
            )
        )

    if not rows:
        return 0

    with _lock:
        conn = _connect()
        try:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO seen_jobs (job_link, job_id, source, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_link) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    first_seen_at = excluded.first_seen_at,
                    job_id = excluded.job_id,
                    source = COALESCE(NULLIF(excluded.source, ''), seen_jobs.source)
                """,
                rows,
            )
            conn.commit()
            inserted = conn.total_changes - before
        finally:
            conn.close()

    logger.info("job_dedup: recorded %d job_link(s) (%d db changes)", len(rows), inserted)
    return inserted


def clear_seen_jobs() -> int:
    """Delete all rows from ``seen_jobs``. Returns number of rows removed."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM seen_jobs")
            deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
            conn.commit()
        finally:
            conn.close()

    logger.info("job_dedup: cleared %d seen job_link(s)", deleted)
    return deleted
