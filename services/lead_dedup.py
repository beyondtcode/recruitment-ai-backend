"""SQLite-backed deduplication of scraped job_link URLs for daily lead runs."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "seen_leads.db"
_lock = threading.Lock()


def _db_path() -> Path:
    override = (os.environ.get("LEADS_DEDUP_DB") or "").strip()
    return Path(override) if override else _DEFAULT_DB


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_link TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_fingerprints (
            fingerprint TEXT PRIMARY KEY,
            job_link TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def filter_unseen_jobs(jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return jobs whose ``job_link`` has not been recorded yet (keeps order)."""
    if not jobs:
        return []

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
                f"SELECT job_link FROM seen_jobs WHERE job_link IN ({placeholders})",
                links,
            ).fetchall()
            seen = {row[0] for row in rows}
        finally:
            conn.close()

    new_jobs = [j for _, j in with_links if j["job_link"].strip() not in seen]
    skipped = len(with_links) - len(new_jobs)
    if skipped:
        logger.info("lead_dedup: skipped %d previously seen job_link(s)", skipped)

    # Jobs without a link cannot be deduped; pass them through once.
    return new_jobs + without_links


def mark_jobs_seen(jobs: list[dict[str, str]]) -> int:
    """Persist job_link values as seen. Returns number of newly inserted rows."""
    rows: list[tuple[str, str, str, str]] = []
    now = datetime.now(timezone.utc).isoformat()
    for job in jobs:
        link = (job.get("job_link") or "").strip()
        if not link:
            continue
        rows.append((link, (job.get("source") or "").strip(), now, now))

    if not rows:
        return 0

    with _lock:
        conn = _connect()
        try:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO seen_jobs (job_link, source, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_link) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    source = COALESCE(NULLIF(excluded.source, ''), seen_jobs.source)
                """,
                rows,
            )
            conn.commit()
            inserted = conn.total_changes - before
        finally:
            conn.close()

    logger.info("lead_dedup: recorded %d job_link(s) (%d db changes)", len(rows), inserted)
    return inserted


def filter_unseen_fingerprints(jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop jobs whose ``fingerprint`` (company + title) was already seen.

    Catches the same opening posted on several boards. Also dedupes within the
    given batch (first occurrence wins). Jobs without a fingerprint pass through.
    """
    if not jobs:
        return []
    prints = sorted({(j.get("fingerprint") or "") for j in jobs} - {""})
    seen: set[str] = set()
    if prints:
        with _lock:
            conn = _connect()
            try:
                placeholders = ",".join("?" * len(prints))
                rows = conn.execute(
                    f"SELECT fingerprint FROM seen_fingerprints WHERE fingerprint IN ({placeholders})",
                    prints,
                ).fetchall()
                seen = {row[0] for row in rows}
            finally:
                conn.close()

    kept: list[dict[str, str]] = []
    batch: set[str] = set()
    for job in jobs:
        fp = job.get("fingerprint") or ""
        if fp and (fp in seen or fp in batch):
            continue
        if fp:
            batch.add(fp)
        kept.append(job)
    skipped = len(jobs) - len(kept)
    if skipped:
        logger.info("lead_dedup: skipped %d cross-board duplicate(s)", skipped)
    return kept


def mark_fingerprints_seen(jobs: list[dict[str, str]]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (j["fingerprint"], (j.get("job_link") or "").strip(), now)
        for j in jobs
        if j.get("fingerprint")
    ]
    if not rows:
        return 0
    with _lock:
        conn = _connect()
        try:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO seen_fingerprints (fingerprint, job_link, first_seen_at) "
                "VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()
            return conn.total_changes - before
        finally:
            conn.close()


def clear_seen_jobs() -> int:
    """Delete all rows from ``seen_jobs``. Returns number of rows removed."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM seen_jobs")
            deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
            conn.execute("DELETE FROM seen_fingerprints")
            conn.commit()
        finally:
            conn.close()

    logger.info("lead_dedup: cleared %d seen job_link(s)", deleted)
    return deleted
