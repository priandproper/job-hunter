"""Phase 9 (Option A) — ingest the dashboard's exported application state.

The dashboard keeps per-job state in the browser (localStorage) where the Python
layer can't see it, which is why the worker/coach used to re-recommend jobs the
candidate had already applied to or dismissed. Option A closes that gap without a
server: the dashboard exports an append-only EVENT LOG to a git-ignored local file
(data/state.local.json), and this module ingests it so worker.py / coach_rank.py can
skip what's already been acted on.

Event model (per the brief): {job_id, event_type, timestamp, metadata}. event_type ∈
discovered|viewed|saved|dismissed|snoozed|resume_generated|applied|outreach_sent|
recruiter_screen|interview|rejected|offer|closed.

Deterministic, stdlib-only, and fully reversible — delete the file (and this module's
callers) and the pipeline behaves exactly as before.
"""

import datetime as _dt
import json
from pathlib import Path

EVENT_TYPES = ("discovered", "viewed", "saved", "dismissed", "snoozed",
               "resume_generated", "applied", "outreach_sent", "recruiter_screen",
               "interview", "rejected", "offer", "closed")

# COMMITTED stages are STICKY: once a job has ever reached one, it is never
# re-recommended — you applied / interviewed / were rejected / it closed, and a later
# "saved" or "viewed" doesn't undo that. DISMISSED is reversible (a later "viewed" from
# un-hiding re-surfaces it), so it only skips when it's the LATEST event.
COMMITTED_STAGES = frozenset({"applied", "recruiter_screen", "interview", "offer",
                              "rejected", "closed"})
# Kept for callers/back-compat: everything that keeps a job out of the recommender.
SKIP_STAGES = COMMITTED_STAGES | frozenset({"dismissed"})


def load(path) -> dict:
    """Load the exported state file. Tolerant: returns {'events':[], 'snapshot':{}}
    on any problem, so a missing/garbled file never breaks the pipeline."""
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return {"events": [], "snapshot": {}}
    if not isinstance(d, dict):
        return {"events": [], "snapshot": {}}
    d.setdefault("events", [])
    d.setdefault("snapshot", {})
    return d


def _ts(e) -> str:
    return str(e.get("timestamp") or "")


def fold(events) -> dict:
    """Fold the event log to the LATEST meaningful state per job.
    Returns {job_id: {'stage', 'ts', 'snooze_until', 'stages': set}}."""
    out = {}
    for e in sorted(events or [], key=_ts):
        jid = e.get("job_id")
        et = e.get("event_type")
        if not jid or et not in EVENT_TYPES:
            continue
        cur = out.setdefault(jid, {"stage": None, "ts": "", "snooze_until": None, "stages": set()})
        cur["stages"].add(et)
        cur["stage"] = et                     # sorted asc → last wins
        cur["ts"] = _ts(e)
        if et == "snoozed":
            cur["snooze_until"] = (e.get("metadata") or {}).get("until")
    return out


def _today(now) -> str:
    if now is None:
        return _dt.date.today().isoformat()
    if isinstance(now, _dt.date):
        return now.isoformat()
    return str(now)


def _from_snapshot(snap) -> set:
    """Fallback skip set when there's no event log — derive from a state snapshot
    ({statuses:{id:{status}}, dismissed:[ids], snoozed:{id:until}})."""
    skip = set()
    st = snap.get("statuses") or {}
    _map = {"applied": "applied", "screen": "recruiter_screen", "interview": "interview",
            "offer": "offer", "rejected": "rejected", "archived": "closed"}
    for jid, o in st.items():
        if _map.get((o or {}).get("status")) in SKIP_STAGES:
            skip.add(jid)
    skip |= set(snap.get("dismissed") or [])
    return skip


def skip_ids(data, now=None) -> set:
    """Job ids the recommender should NOT re-surface: applied/beyond, rejected,
    closed, dismissed, or snoozed until a future date. Events preferred; falls back
    to the snapshot when no events are present."""
    data = data or {}
    events = data.get("events") or []
    today = _today(now)
    if not events:
        return _from_snapshot(data.get("snapshot") or {})
    skip = set()
    for jid, s in fold(events).items():
        if s["stages"] & COMMITTED_STAGES:              # sticky — applied/interviewed/rejected/closed
            skip.add(jid)
        elif s["stage"] == "dismissed":                 # reversible — only when it's the latest event
            skip.add(jid)
        elif s["stage"] == "snoozed" and s.get("snooze_until") and str(s["snooze_until"]) > today:
            skip.add(jid)
    return skip


def status_by_job(data) -> dict:
    """{job_id: latest stage} — for provenance (e.g. was a résumé generated / applied)."""
    return {jid: s["stage"] for jid, s in fold((data or {}).get("events") or []).items()}


def summary(data) -> dict:
    """Counts per stage across the folded state — for logging."""
    counts = {}
    for s in fold((data or {}).get("events") or []).values():
        counts[s["stage"]] = counts.get(s["stage"], 0) + 1
    return counts
