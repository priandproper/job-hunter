"""Phase 13 — daily-run health record.

A single, honest status for each pipeline run so a silent failure (the classic
"SSL broke, fetched 0 jobs, published an empty board" bug) can't pass unnoticed and
the dashboard can warn on stale/degraded data. Deterministic, stdlib-only, testable.

Writes docs/health.json (no PII — run ids, counts, source names, timings), which the
dashboard reads to show a stale-data / partial-run banner. Also carries the last full
success forward so "how long since a clean run" is always answerable.
"""

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path

STATUSES = ("success", "partial", "failed")
DEFAULT_MIN_JOBS = 10
STALE_HOURS = 36


def classify(jobs_matched, sources_succeeded, sources_failed, min_jobs=DEFAULT_MIN_JOBS):
    """success / partial / failed from the run's counts.
    failed: nothing matched or every source failed. partial: below the job-count floor
    or most sources failed. success otherwise."""
    n_failed = len(sources_failed or [])
    total = (sources_succeeded or 0) + n_failed
    if jobs_matched <= 0 or (sources_succeeded or 0) == 0:
        return "failed"
    if jobs_matched < min_jobs or (total and n_failed / total > 0.5):
        return "partial"
    return "success"


def _iso(dt):
    return dt.replace(microsecond=0).isoformat() if isinstance(dt, _dt.datetime) else str(dt or "")


def _duration(started, completed):
    try:
        a = _dt.datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        b = _dt.datetime.fromisoformat(str(completed).replace("Z", "+00:00"))
        return max(0, int((b - a).total_seconds()))
    except (ValueError, TypeError):
        return 0


def build_record(run_id, started_at, completed_at, *, jobs_fetched=0, jobs_matched=0,
                 jobs_ranked=0, sources_succeeded=0, sources_failed=None,
                 coach_status="", prior=None, min_jobs=DEFAULT_MIN_JOBS):
    sources_failed = sources_failed or []
    status = classify(jobs_matched, sources_succeeded, sources_failed, min_jobs)
    prior = prior or {}
    last_full = _iso(completed_at) if status == "success" else prior.get("last_full_success", "")
    return {
        "run_id": run_id,
        "started_at": _iso(started_at),
        "completed_at": _iso(completed_at),
        "status": status,
        "jobs_fetched": jobs_fetched,
        "jobs_matched": jobs_matched,
        "jobs_ranked": jobs_ranked,
        "sources_succeeded": sources_succeeded,
        "sources_failed": sources_failed,
        "duration_seconds": _duration(started_at, completed_at),
        "coach_status": coach_status,
        "last_full_success": last_full,
    }


def is_stale(health, now=None, max_age_hours=STALE_HOURS):
    """True when the data should be treated as stale: no health, a non-success status,
    or the last completed run is older than max_age_hours."""
    if not health:
        return True
    if health.get("status") != "success":
        return True
    now = now or _dt.datetime.now(_dt.timezone.utc)
    try:
        done = _dt.datetime.fromisoformat(str(health.get("completed_at")).replace("Z", "+00:00"))
        if done.tzinfo is None:
            done = done.replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return True
    return (now - done).total_seconds() > max_age_hours * 3600


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save(path, record):
    """Atomic write (temp file + os.replace) so a crash mid-write can't leave a
    truncated health file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, indent=2)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def set_coach_status(path, coach_status):
    """Update just the coach_status on an existing health record (coach runs as a
    separate process after the worker). No file -> no-op."""
    rec = load(path)
    if rec:
        rec["coach_status"] = coach_status
        save(path, rec)
    return rec
