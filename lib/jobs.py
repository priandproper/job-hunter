"""Stage 1 — ingest.

Read jobs that the existing scanner/tracker already discovered from ATS feeds
(Greenhouse / Lever / Ashby). We do NOT re-scrape here; that scraping is already
solved and curated in `job-search/`. This module just normalizes those sources
into a single list of Job dicts the rest of the pipeline consumes.

Primary source: the tracker's SQLite `jobs.db` (richest — has sponsorship,
salary, tools, excerpt, min_exp). Fallback: the scanner's `seen_jobs.json`.
"""

import json
import sqlite3
from pathlib import Path

# The canonical shape every downstream stage relies on.
JOB_FIELDS = (
    "id", "company", "title", "location", "url", "source", "department",
    "posted_at", "sponsorship", "sponsorship_note", "salary_raw",
    "tools", "excerpt", "min_exp", "status",
)


def _from_tracker_db(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT * FROM jobs")
        cols = {d[0] for d in cur.description}
        jobs = []
        for row in cur.fetchall():
            job = {f: (row[f] if f in cols else None) for f in JOB_FIELDS}
            # tools is stored as a JSON string; decode to a list for convenience.
            try:
                job["tools"] = json.loads(job.get("tools") or "[]")
            except (json.JSONDecodeError, TypeError):
                job["tools"] = []
            jobs.append(job)
        return jobs
    finally:
        conn.close()


def _from_scanner_seen(seen_path: Path) -> list[dict]:
    """Fallback: the scanner's seen_jobs.json (id -> metadata)."""
    if not seen_path.exists():
        return []
    try:
        data = json.loads(seen_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    jobs = []
    # seen_jobs.json may be a dict keyed by id, or a list of entries.
    entries = data.values() if isinstance(data, dict) else data
    for e in entries:
        if not isinstance(e, dict):
            continue
        jobs.append({
            "id": e.get("id") or e.get("job_id"),
            "company": e.get("company"),
            "title": e.get("title"),
            "location": e.get("location"),
            "url": e.get("url"),
            "source": e.get("source") or e.get("ats"),
            "department": e.get("department"),
            "posted_at": e.get("posted_at") or e.get("date"),
            "sponsorship": e.get("sponsorship") or "Unknown",
            "sponsorship_note": e.get("sponsorship_note") or "",
            "salary_raw": e.get("salary_raw") or "",
            "tools": e.get("tools") or [],
            "excerpt": e.get("excerpt") or "",
            "min_exp": e.get("min_exp"),
            "status": e.get("status") or "new",
        })
    return jobs


def load_jobs(config: dict, repo_root: Path) -> list[dict]:
    """Load and de-duplicate jobs from all configured sources."""
    sources = config.get("sources", {})
    db_path = (repo_root / sources.get("tracker_db", "")).resolve()
    seen_path = (repo_root / sources.get("scanner_seen", "")).resolve()

    jobs = _from_tracker_db(db_path)
    seen_ids = {j["id"] for j in jobs if j.get("id")}

    # Merge in scanner-only jobs the tracker DB didn't have.
    for j in _from_scanner_seen(seen_path):
        key = j.get("id") or (j.get("company"), j.get("title"))
        if key not in seen_ids:
            jobs.append(j)
            seen_ids.add(key)

    # Drop rows missing the essentials.
    return [j for j in jobs if j.get("company") and j.get("title")]
