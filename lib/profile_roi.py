"""Profile-ROI analysis — "what to work on" from the resume + the real JDs.

Deep-reads the candidate's top-fit jobs' full descriptions against their best resume
and asks Claude for a prioritized, highest-ROI plan (skills to learn, projects to
build, resume reframings, study topics). Mirrors the in-browser version in
docs/index.html so the Cockpit renders either identically.

Guarded end-to-end: without an ANTHROPIC_API_KEY or a resume it returns None and the
pipeline is unaffected. Stdlib only.
"""

import json
import urllib.request

from . import ats as ats_mod
from . import secrets as secrets_mod

API_URL = "https://api.anthropic.com/v1/messages"

SYS = (
    "You are a sharp, honest career and resume coach for a job seeker. Using their "
    "current resume and the REAL job descriptions they're targeting, produce a "
    "prioritized, highest-ROI plan to make their profile more competitive for these "
    "roles. Return ONLY JSON matching the schema.\n\n"
    "Rules:\n"
    "- Ground every recommendation in the actual JDs. In \"why\"/\"demand\", cite how "
    "many of the target roles want it (e.g. \"12 of 20 roles ask for SQL\").\n"
    "- Rank by ROI = (how many target jobs it helps) x (impact) / (effort). Biggest "
    "wins first.\n"
    "- skills_to_learn and projects_to_build must be things the candidate can genuinely "
    "acquire. Each project should unlock concrete, truthful resume bullets and cover "
    "specific JD keywords (list them).\n"
    "- resume_reframes: \"current\" = how the resume reads now (or the gap); "
    "\"suggested\" = a stronger phrasing. Set defensible=true when it's a truthful "
    "re-emphasis of what the candidate clearly did. Set defensible=false for bolder "
    "phrasings that ASSUME plausible-but-unverified experience — write those as a "
    "template the candidate must verify is true before using. NEVER fabricate "
    "employers, job titles, dates, or degrees.\n"
    "- completeness_score (0-100) = how ready this profile is for the target roles.\n"
    "- \"type\" is one of: skill, project, reframe, study. \"effort\" is one of: low, "
    "medium, high."
)

_STR = {"type": "string"}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "completeness_score": {"type": "integer"},
        "summary": _STR,
        "top_moves": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"title": _STR, "type": _STR, "why": _STR, "effort": _STR, "impact": _STR},
            "required": ["title", "type", "why", "effort", "impact"]}},
        "skills_to_learn": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"skill": _STR, "demand": _STR, "note": _STR},
            "required": ["skill", "demand", "note"]}},
        "projects_to_build": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"name": _STR, "description": _STR, "unlocks": _STR,
                           "keywords": {"type": "array", "items": _STR}},
            "required": ["name", "description", "unlocks", "keywords"]}},
        "resume_reframes": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"current": _STR, "suggested": _STR, "defensible": {"type": "boolean"}},
            "required": ["current", "suggested", "defensible"]}},
        "study_plan": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"topic": _STR, "why": _STR}, "required": ["topic", "why"]}},
    },
    "required": ["completeness_score", "summary", "top_moves", "skills_to_learn",
                 "projects_to_build", "resume_reframes", "study_plan"],
}


def _top_jobs(jobs: list[dict], n: int = 20) -> list[dict]:
    ok = [j for j in jobs if len((j.get("excerpt") or "").strip()) > 200]
    ok.sort(key=lambda j: j.get("fit_score", 0), reverse=True)
    return ok[:n]


def _build_user(resume: dict, jobs: list[dict], leaderboard: list[dict], persona: dict) -> str:
    lead = ", ".join(f"{k.get('keyword')}: {k.get('count')}" for k in (leaderboard or [])[:20])
    s = "CURRENT RESUME (JSON):\n" + json.dumps(resume) + "\n\nCANDIDATE FOCUS\n"
    if persona.get("roles"):
        s += "- Target roles: " + ", ".join(persona["roles"]) + "\n"
    if persona.get("skills"):
        s += "- Known skills/tools: " + ", ".join(persona["skills"]) + "\n"
    if persona.get("years"):
        s += f"- Years of experience: {persona['years']}\n"
    s += ("\nMOST-REQUESTED SKILLS THE RESUME IS MISSING (across ALL target jobs — "
          "keyword: #jobs)\n" + (lead or "(none computed)") + "\n")
    s += f"\nTOP {len(jobs)} TARGET JOBS BY FIT (full descriptions)\n"
    for i, j in enumerate(jobs, 1):
        jd = ats_mod.clean_jd(j.get("excerpt") or "")[:2200]
        s += f"\n[{i}] {j.get('title','')} @ {j.get('company','')} — fit {j.get('fit_score',0)}, ATS {j.get('ats_score',0)}%\n{jd}\n"
    s += "\nProduce the highest-ROI plan as JSON only."
    return s


def _call(api_key: str, model: str, user: str) -> dict | None:
    body = json.dumps({
        "model": model, "max_tokens": 6000, "system": SYS,
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers={
        "content-type": "application/json", "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    if data.get("stop_reason") == "refusal":
        return None
    txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return json.loads(txt)


def analyze(cfg: dict, repo_root, public_jobs: list[dict], leaderboard: list[dict],
            resume: dict, persona: dict, now: str, log=print) -> dict | None:
    """Return {report, ts, jobs, source:'ci'} or None (no key / no resume / error)."""
    roi_cfg = cfg.get("profile_roi", {}) or {}
    secrets_file = (repo_root / roi_cfg.get("secrets_file", ".secrets.json")).resolve()
    api_key = secrets_mod.get_key(roi_cfg.get("api_key_env", "ANTHROPIC_API_KEY"), secrets_file)
    if not api_key or not resume or not public_jobs:
        return None
    jobs = _top_jobs(public_jobs, int(roi_cfg.get("top_jobs", 20)))
    if not jobs:
        return None
    try:
        report = _call(api_key, roi_cfg.get("model", "claude-opus-4-8"),
                       _build_user(resume, jobs, leaderboard, persona))
    except Exception as e:
        log(f"        profile-roi — analysis failed: {e}")
        return None
    if not report:
        return None
    return {"report": report, "ts": now, "jobs": len(jobs), "source": "ci"}
