"""Ever-expanding job pool — accumulate postings across runs instead of snapshotting.

Each run unions the freshly-fetched postings with everything seen before, deduping
by (company, title) and keeping a stable id so the dashboard's per-job status/inbox
survive. Postings age out once they're older than `pool_max_age_days`, and the pool
is capped at `pool_max_size` (newest kept) to bound repo growth.

Committed at data/job_pool.json so it persists across the stateless GitHub Actions
runs — that's what makes the list grow over time rather than reset each cycle.
"""

import datetime as _dt
import json
from pathlib import Path

_SEP = "\x01"


def _key(job: dict) -> str:
    # Dedup on company+title (not location) — same role posted per-location collapses.
    return ((job.get("company") or "").strip().lower() + _SEP +
            (job.get("title") or "").strip().lower())


def load_pool(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("jobs", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_pool(path: Path, jobs: list[dict], now: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated_at": now, "count": len(jobs), "jobs": jobs}, indent=1))


def _age_days(job: dict, today: _dt.date) -> int | None:
    """Best-effort age of a posting in days, from posted_at then first/last seen."""
    for field in ("posted_at", "_first_seen", "_last_seen"):
        raw = (job.get(field) or "").strip()
        if not raw:
            continue
        try:
            d = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00").split("T")[0]).date()
            return (today - d).days
        except (ValueError, TypeError):
            continue
    return None


def merge(prior: list[dict], fresh: list[dict], now: str,
          max_age_days: int = 45, max_size: int = 4000) -> tuple[list[dict], dict]:
    """Union prior pool with fresh postings; dedup, refresh, age out, cap.

    Returns (pool, stats). Existing entries keep their original id + _first_seen so
    downstream ids stay stable; their content and _last_seen refresh from `fresh`.
    """
    pool = {_key(j): j for j in prior}
    added = 0
    for j in fresh:
        k = _key(j)
        if not k.strip(_SEP).strip():
            continue
        cur = pool.get(k)
        if cur is None:
            j = dict(j)
            j["_first_seen"] = now
            j["_last_seen"] = now
            pool[k] = j
            added += 1
        else:
            # keep the stable id + first_seen; refresh the rest, prefer the richer excerpt
            keep_id = cur.get("id")
            first = cur.get("_first_seen", now)
            better_excerpt = (cur.get("excerpt") or "") if \
                len(cur.get("excerpt") or "") >= len(j.get("excerpt") or "") else (j.get("excerpt") or "")
            merged = dict(cur)
            merged.update(j)
            merged["id"] = keep_id or j.get("id")
            merged["_first_seen"] = first
            merged["_last_seen"] = now
            merged["excerpt"] = better_excerpt
            pool[k] = merged

    today = _dt.date.today()
    kept = [j for j in pool.values()
            if (a := _age_days(j, today)) is None or a <= max_age_days]
    aged_out = len(pool) - len(kept)

    # newest first (by last_seen then first_seen), then cap
    kept.sort(key=lambda j: (j.get("_last_seen") or "", j.get("_first_seen") or ""), reverse=True)
    capped = kept[:max_size]

    return capped, {"added": added, "aged_out": aged_out,
                    "total": len(capped), "dropped_over_cap": len(kept) - len(capped)}
