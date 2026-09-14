"""Phase 10 — ranking memory & freshness (replaces `times_recommended` as primary).

The old memory was a single counter (how many daily runs had recommended a job) with a
blunt "shown a lot -> demote" rule. That both under-served genuinely strong roles and
ignored where a job actually sits in the pipeline. This module ranks on the real
lifecycle (from the Phase 9 event state), posting freshness bands, and company/lane
diversity — using time decay, but NEVER demoting a strong role merely for having been
shown several times.

Deterministic, stdlib-only, testable. Consumed by scripts/coach_rank.py to order and
focus the daily active queue; the LLM refines the deterministic order rather than
inventing it.
"""

import datetime as _dt

# Active-queue freshness by posting age (per the brief). "aging" (15–30d) counts only
# when the role is STRONG (high priority or a referral path); "archive" (31–45d) is
# historical unless manually verified still active; past 45d is expired.
FRESHNESS = [
    (3, "highest", 1.00),
    (7, "high", 0.90),
    (14, "moderate", 0.70),
    (30, "aging", 0.45),
    (45, "archive", 0.20),
]
STRONG_TOTAL = 75          # a Priority-A-ish score is "strong"


def age_days(job, today=None):
    today = today or _dt.date.today()
    raw = (job.get("posted_at") or "").strip()
    if not raw:
        return None
    try:
        d = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00").split("T")[0]).date()
    except (ValueError, TypeError):
        return None
    return (today - d).days


def freshness(age, strong=False, manually_active=False):
    """(band, weight, note) for a posting age. Unknown age is treated as 'moderate'
    (we don't guess it's stale). Applies the strong/manual exceptions for old roles."""
    if age is None:
        return {"band": "unknown", "weight": 0.70, "note": "posting date unknown"}
    for cutoff, band, weight in FRESHNESS:
        if age <= cutoff:
            if band == "aging" and not strong:
                weight = 0.30      # 15–30d only really counts when fit/access is strong
            if band == "archive" and not manually_active:
                weight = 0.12      # 31–45d historical unless manually verified active
            return {"band": band, "weight": weight, "note": f"posted {age}d ago"}
    return {"band": "expired", "weight": 0.0, "note": f"posted {age}d ago (past 45d)"}


# Lifecycle stage of a JOB, from the folded Phase 9 event state (+ repost flag).
# Ordering nudge (points added to the freshness-weighted priority) in NUDGE.
_ACTIVE_NUDGE = {
    "application_started": 15,   # you began it — finish it
    "follow_up_due": 12,         # outreach sent, time to nudge
    "saved_not_applied": 8,      # you flagged it; act
    "new_unreviewed": 5,         # fresh, needs a look
    "reposted": 4,
    "outreach_sent": 3,
    "reviewed": 0,
}
INACTIVE_STAGES = frozenset({"applied", "recruiter_screen", "interview", "offer",
                             "rejected", "closed", "dismissed", "snoozed"})
_FOLLOW_UP_DAYS = 3


def lifecycle_stage(job, folded, today=None):
    """Where this job sits: new_unreviewed | reviewed | saved_not_applied |
    application_started | outreach_sent | follow_up_due | snoozed | dismissed |
    applied | recruiter_screen | interview | offer | rejected | closed (+ reposted)."""
    today = today or _dt.date.today()
    s = (folded or {}).get(job.get("id"))
    if not s:
        return "reposted" if job.get("_reposted") or job.get("reposted") else "new_unreviewed"
    stages, latest = s.get("stages", set()), s.get("stage")
    committed = stages & {"applied", "recruiter_screen", "interview", "offer", "rejected", "closed"}
    if committed:
        return latest if latest in committed else next(iter(committed))
    if latest == "dismissed":
        return "dismissed"
    if latest == "snoozed":
        return "snoozed"
    if "resume_generated" in stages:
        return "application_started"          # tailored but not yet applied — incomplete
    if latest == "outreach_sent":
        try:
            sent = _dt.datetime.fromisoformat((s.get("ts") or "").replace("Z", "+00:00").split("T")[0]).date()
            if (today - sent).days >= _FOLLOW_UP_DAYS:
                return "follow_up_due"
        except (ValueError, TypeError):
            pass
        return "outreach_sent"
    if latest == "saved":
        return "saved_not_applied"
    if latest == "viewed":
        return "reviewed"
    return "new_unreviewed"


def is_active(stage):
    return stage not in INACTIVE_STAGES


def rank_score(job, folded, times_shown=0, today=None, manually_active=False):
    """Compute a job's active-queue score with a transparent breakdown.
    score = priority × freshness  + lifecycle nudge  − small repeat-exposure decay
    (decay is capped and NEVER applied to a strong role — the brief's rule)."""
    prio = (job.get("priority") or {}).get("total") or 0
    strong = prio >= STRONG_TOTAL or bool(job.get("_has_referral"))
    fr = freshness(age_days(job, today), strong=strong, manually_active=manually_active)
    stage = lifecycle_stage(job, folded, today)

    if not is_active(stage):
        return {"score": None, "active": False, "stage": stage,
                "freshness": fr, "why": f"inactive ({stage}) — not in the active queue"}

    base = prio * fr["weight"]
    nudge = _ACTIVE_NUDGE.get(stage, 0)
    # Repeat-exposure decay: gentle, capped at 5, and waived entirely for strong roles.
    decay = 0 if strong else min(int(times_shown or 0), 5) * 1.0
    score = round(max(0.0, base + nudge - decay), 1)
    why = (f"{stage.replace('_',' ')}; priority {prio}×{fr['weight']:.2f} freshness "
           f"({fr['band']}) +{nudge} stage" + (f" −{decay:.0f} repeats" if decay else "")
           + ("; strong→no repeat penalty" if strong and times_shown else ""))
    return {"score": score, "active": True, "stage": stage, "strong": strong,
            "freshness": fr, "nudge": nudge, "decay": decay, "why": why}


def lane_of(title):
    t = (title or "").lower()
    if "analyst" in t:
        return "analyst"
    if "product marketing" in t or "pmm" in t:
        return "pmm"
    if "growth" in t or "demand" in t:
        return "growth"
    if "operations" in t or " ops" in t or "revops" in t:
        return "ops"
    if "content" in t:
        return "content"
    if "brand" in t:
        return "brand"
    if "field" in t:
        return "field"
    return "marketing"


def order_active_queue(jobs, folded, history=None, today=None,
                       company_penalty=8.0, lane_penalty=4.0):
    """Rank active jobs, then re-order for company & lane DIVERSITY so one employer or
    one lane can't dominate the top. Returns [{job, rank}] best-first. Inactive jobs
    (applied/dismissed/snoozed/…) are dropped from the queue."""
    history = history or {}
    scored = []
    for j in jobs:
        r = rank_score(j, folded, times_shown=history.get(j.get("id"), 0), today=today)
        if r["active"]:
            scored.append({"job": j, "rank": r, "base": r["score"]})
    scored.sort(key=lambda x: x["base"], reverse=True)

    # Greedy diversity: pick the current best after penalizing repeats of an already-
    # picked company/lane; deterministic and stable.
    picked, seen_co, seen_lane = [], {}, {}
    pool = scored[:]
    while pool:
        best_i, best_adj = 0, None
        for i, x in enumerate(pool):
            co = (x["job"].get("company") or "").lower()
            lane = lane_of(x["job"].get("title"))
            adj = x["base"] - company_penalty * seen_co.get(co, 0) - lane_penalty * seen_lane.get(lane, 0)
            if best_adj is None or adj > best_adj:
                best_adj, best_i = adj, i
        x = pool.pop(best_i)
        co = (x["job"].get("company") or "").lower()
        lane = lane_of(x["job"].get("title"))
        seen_co[co] = seen_co.get(co, 0) + 1
        seen_lane[lane] = seen_lane.get(lane, 0) + 1
        x["rank"]["diversity_adj"] = round(best_adj - x["base"], 1)
        picked.append(x)
    return picked
