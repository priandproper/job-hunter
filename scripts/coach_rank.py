#!/usr/bin/env python3
"""Claude-powered daily curation: give the job pool a real brain, not just keywords.

The keyword fit score is decent but dumb. This hands the whole on-target pool (plus the
candidate's profile) to the Claude CLI and asks it to RE-RANK with judgment: which roles
to actually apply to today, which are hidden gems the keyword score under-rated, which are
over-rated or a stretch or a sponsorship risk — plus a short daily briefing.

It writes docs/coach.json (a DATA layer the dashboard reads and displays — Coach's picks,
tier badges, one-line "why"). It never edits app code. Commit + push docs/coach.json and
the live dashboard shows the curation. Meant to run right after worker.py (see
auto_refresh.sh). Uses the logged-in Claude plan via `claude -p` — no API key.

  python3 scripts/coach_rank.py            # -> docs/coach.json
  python3 scripts/coach_rank.py --publish  # also commit + push
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib import dedup as dedup_mod  # noqa: E402
from lib import health as health_mod  # noqa: E402
from lib import jobspec as jobspec_mod  # noqa: E402
from lib import ranking as ranking_mod  # noqa: E402
from lib import state as state_mod  # noqa: E402

STATE = ROOT / "data" / "state.local.json"   # exported from the dashboard (git-ignored)
JOBS = ROOT / "docs" / "jobs.json"
OUT = ROOT / "docs" / "coach.json"
CONFIG = ROOT / "config.json"
HEALTH = ROOT / "docs" / "health.json"           # Phase 13 — record coach_status here
HISTORY = ROOT / "data" / "coach_history.json"   # id -> times previously recommended (memory)


def _load_history() -> dict:
    try:
        return json.loads(HISTORY.read_text())
    except Exception:
        return {}


def _resume_context(jobs: list) -> str:
    core = next((j.get("resume_core") for j in jobs if j.get("resume_core")), {}) or {}
    skills = []
    for g in core.get("skills", []):
        skills += (g.get("items", []) if isinstance(g, dict) else [])
    return (f"Summary: {core.get('summary','')}\n"
            f"Skills: {', '.join(skills[:24])}")


def _trim(items, n, chars=150):
    return [re.sub(r"\s+", " ", (s or ""))[:chars] for s in (items or [])[:n]]


def _compact(jobs: list, hist: dict) -> list:
    """Hand the ranker STRUCTURED requirements (Phase 3) — basic vs preferred quals,
    responsibilities, required years — plus a source snippet, not a 320-char blob, so
    it can judge genuine fit instead of keyword overlap. Spec is computed on the fly
    when worker.py hasn't yet written one, so this never depends on refresh order."""
    out = []
    for j in jobs:
        spec = j.get("spec") or jobspec_mod.structure(j, include_full=False, max_bullets=10)
        out.append({
            "id": j.get("id"), "title": j.get("title", ""), "company": j.get("company", ""),
            "location": j.get("location", ""), "fit": j.get("fit_score"), "ats": j.get("ats_score"),
            "posted_at": j.get("posted_at", ""),
            "imm_risk": (j.get("immigration") or {}).get("risk", "yellow"),  # green|yellow|red
            "band": (j.get("priority") or {}).get("band"),                   # A|B|C (Phase 6)
            "lifecycle": (j.get("_rank") or {}).get("lifecycle", "new_unreviewed"),  # Phase 10
            "freshness": (j.get("_rank") or {}).get("freshness", "unknown"),
            "det_rank": (j.get("_rank") or {}).get("score"),   # deterministic active-queue score
            "dup": (j.get("duplicate") or {}).get("classification"),  # Phase 11 repost/dup, if any
            "resembles_rejected": bool(j.get("_resembles_rejected")),  # look-alike of a rejected role
            "req_years": spec.get("required_years"),
            "basic_quals": _trim(spec.get("basic_qualifications"), 6),      # eligibility
            "preferred_quals": _trim(spec.get("preferred_qualifications"), 3),  # ranking only
            "responsibilities": _trim(spec.get("responsibilities"), 4),
            "missing": (j.get("missing_keywords") or [])[:8],
            "jd": re.sub(r"\s+", " ", (j.get("excerpt") or ""))[:300],      # source-text context
        })
    return out


_SCHEMA_NOTE = """Return ONLY one JSON object, no prose, no code fences:
{
  "briefing": {
    "headline": string,            // one punchy line on today's opportunity
    "focus": string,               // 2-3 sentences: where to spend today's ~20 min
    "top_ids": [string, ...]       // the 3-6 job ids to apply to FIRST today
  },
  "ranked": [                      // the ~30 best jobs, best first — your judgment, NOT the keyword score
    { "id": string,
      "tier": "top"|"strong"|"maybe",
      "priority": number,          // 0-100, higher = pursue first
      "why": string,               // ONE short line: the real reason
      "flag": "hidden-gem"|"over-rated"|"stretch"|"sponsorship-risk"|"" }
  ],
  "flagged": [                     // roles that slipped the keyword filter but are NOT relevant to the
                                   // search — MARK them (not deleted) so they can be reviewed & removed
    { "id": string, "reason": string }   // short reason: off-lane / wrong function / wrong level / etc.
  ]
}"""


def build_prompt(jobs: list, hist: dict) -> str:
    return (
        "You are a sharp career strategist and recruiter for this candidate. Re-rank today's "
        "on-target jobs with REAL judgment — go beyond the keyword 'fit' score.\n\n"
        "CANDIDATE (US-only; will need future H-1B sponsorship; based in Boston, MA):\n"
        "- ~4+ years of relevant experience. Target roles asking for roughly 3-6 years (primary), "
        "will stretch to 7; roles requiring 8+ years are out of band. Do NOT treat a 'Senior' / "
        "'Lead' / 'Principal' / 'Director' TITLE as disqualifying by itself — judge level by the "
        "required years and scope in the JD. Flag a role as 'stretch' if it clearly wants more than "
        "~6 years, and note over-qualification if a role wants 0-1 years.\n"
        "- Two target lanes: (1) product marketing / marketing — GTM, growth, marketing ops; "
        "(2) analyst roles — marketing / business / sales analyst.\n"
        + _resume_context(jobs) + "\n\n"
        "REQUIREMENT-TO-EVIDENCE (judge fit HONESTLY, not by keyword overlap): each role now "
        "carries structured 'basic_quals' (eligibility — the bar to clear), 'preferred_quals' "
        "(ranking only, never eligibility) and 'responsibilities'. For each role weigh the "
        "candidate's ACTUAL experience against the basic_quals and classify the match as: DIRECT "
        "evidence (they've demonstrably done it), TRANSFERABLE (adjacent/analogous, reframe honestly), "
        "GAP (no real basis), or HARD DISQUALIFIER (a basic_qual they cannot meet — e.g. a required "
        "degree/certification/tool they lack, or years far beyond ~7). Do NOT let a repeated keyword "
        "inflate fit when the underlying experience doesn't support it; a title match with unmet "
        "basic_quals is a weak fit, not a strong one. State the real evidence basis in 'why'.\n"
        "JUDGE each role on: genuine fit for the candidate's lanes AND level (not too senior); "
        "company quality / growth; SPONSORSHIP-friendliness (large/established or known H-1B sponsors "
        "beat tiny startups); Boston/remote-US location; and whether the keyword score mis-rated it. "
        "REWARD roles the keyword score under-rated (hidden gems); DEMOTE generic or over-scored ones. "
        "Be honest and specific in each 'why'.\n"
        "IMMIGRATION: each job has 'imm_risk' — green (posting indicates sponsorship), yellow (unknown: "
        "verify with recruiter before the hiring-manager stage), red (explicit hard stop; these are already "
        "filtered out). Prefer green; for yellow, keep it but note it needs sponsorship verification. Historical "
        "company H-1B use is evidence, NOT proof the current team sponsors — never present it as a guarantee.\n"
        "ACTIVE-QUEUE RANKING (Phase 10): the jobs are ALREADY ordered by a deterministic model that "
        "combines priority, posting freshness, pipeline lifecycle and company/lane diversity — each carries "
        "'lifecycle', 'freshness', 'band' and 'det_rank'. Treat that order as your PRIOR and refine it with "
        "judgment; don't reshuffle wildly. Rules: PUSH 'application_started' (a résumé was tailored but not "
        "submitted — finish it) and 'follow_up_due' to the top; favor fresher postings (highest>high>moderate; "
        "an 'aging'/'archive' role only if fit or access is strong); keep company AND lane diversity in your "
        "top picks (don't stack one employer). Repeat exposure is NOT a reason to demote a genuinely strong "
        "role — rank on fit, not on how often it has appeared.\n"
        "DUPLICATES (Phase 11): 'dup' marks a posting our detector judged a repost/duplicate/separate-"
        "headcount of another role at the same company — don't stack several of these in the top picks; "
        "pick the best one. 'resembles_rejected'=true means the role closely matches one the candidate was "
        "already REJECTED from — deprioritize it and note why in 'why'.\n"
        "RELEVANCE: the list is keyword-filtered but imperfect. Any role that is genuinely NOT relevant "
        "to the candidate's two lanes (off-function despite the title, wrong seniority, a role they'd never "
        "want) — put it in 'flagged' with a short reason. Don't delete anything; flagging just lets the "
        "candidate review and prune. Do NOT flag a role merely for being a lower-priority but valid fit.\n\n"
        + _SCHEMA_NOTE + "\n\n"
        "TODAY'S ON-TARGET JOBS (keyword-filtered already):\n"
        + json.dumps(_compact(jobs, hist), separators=(",", ":"))
    )


def claude_json(prompt: str, model: str = "opus") -> dict:
    proc = subprocess.run(["claude", "-p", "--model", model, "--output-format", "json"],
                          input=prompt, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip() or f"claude exited {proc.returncode}")
    out = (proc.stdout or "").strip()
    try:
        env = json.loads(out)
        text = env.get("result", out) if isinstance(env, dict) else out
    except json.JSONDecodeError:
        text = out
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise RuntimeError("Claude returned no JSON:\n" + t[:400])
    return json.loads(m.group(0))


def _publish():
    import subprocess as sp
    files = ["docs/coach.json", "docs/health.json"]   # health carries coach_status (Phase 13)
    sp.run(["git", "add", *files], cwd=ROOT)
    if not sp.run(["git", "status", "--porcelain", *files], cwd=ROOT,
                  capture_output=True, text=True).stdout.strip():
        print("coach_rank: coach.json unchanged; nothing to publish."); return
    sp.run(["git", "commit", "-m", "coach: refresh Claude job curation"], cwd=ROOT,
           capture_output=True, text=True)
    p = sp.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
    print("coach_rank: published." if p.returncode == 0 else "coach_rank: push failed:\n" + (p.stderr or ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="opus")
    ap.add_argument("--publish", action="store_true", help="commit + push docs/coach.json")
    args = ap.parse_args()

    all_jobs = json.loads(JOBS.read_text()).get("jobs", [])
    jobs = all_jobs
    if not jobs:
        print("coach_rank: no jobs in docs/jobs.json — run worker.py first."); return 1
    # Phase 9/10: read the dashboard's exported state; skip already-acted-on jobs, then
    # order the rest by the deterministic active-queue model (lifecycle + freshness +
    # priority + company/lane diversity) and send Claude a focused top slice to refine.
    state_data = state_mod.load(STATE)
    folded = state_mod.fold(state_data.get("events", []))
    skip = state_mod.skip_ids(state_data)
    # Phase 11: roles the candidate was already REJECTED from, to warn on look-alikes.
    stages = state_mod.status_by_job(state_data)
    rejected_jobs = [j for j in all_jobs if stages.get(j.get("id")) == "rejected"]
    if skip:
        before = len(jobs)
        jobs = [j for j in jobs if j.get("id") not in skip]
        print(f"coach_rank: skipped {before - len(jobs)} already-acted-on job(s) from state.local.json")
    if not jobs:
        print("coach_rank: every job is already acted on — nothing to rank."); return 0
    hist = _load_history()

    ordered = ranking_mod.order_active_queue(jobs, folded, history=hist)
    for x in ordered:                       # annotate each job with its deterministic rank
        r = x["rank"]
        x["job"]["_rank"] = {"lifecycle": r["stage"], "freshness": r["freshness"]["band"],
                             "score": r["score"], "why": r["why"]}
    TOPN = 45
    jobs = [x["job"] for x in ordered[:TOPN]]   # deterministic ranking is PRIMARY; Claude refines
    if rejected_jobs:                            # Phase 11: flag look-alikes of rejected roles
        for j in jobs:
            m = dedup_mod.resembles_rejected(j, rejected_jobs)
            if m:
                j["_resembles_rejected"] = m
    print(f"coach_rank: {len(ordered)} active job(s) ranked (lifecycle+freshness+diversity); "
          f"sending top {len(jobs)} to Claude ({args.model}) to refine"
          f"{' (with '+str(len(hist))+' prior-pick counts as a minor signal)' if hist else ''}…")
    try:
        rep = claude_json(build_prompt(jobs, hist), args.model)
    except Exception as e:
        print(f"coach_rank: Claude failed ({e})")
        health_mod.set_coach_status(HEALTH, "failed")   # Phase 13: record the degraded state
        return 1

    ranked = [r for r in (rep.get("ranked") or []) if r.get("id")]
    valid_ids = {j["id"] for j in jobs}
    ranked = [r for r in ranked if r["id"] in valid_ids]          # drop any hallucinated ids
    briefing = rep.get("briefing") or {}
    briefing["top_ids"] = [i for i in (briefing.get("top_ids") or []) if i in valid_ids]
    flagged = [f for f in (rep.get("flagged") or []) if f.get("id") in valid_ids]
    from datetime import datetime, timezone
    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": args.model, "briefing": briefing, "ranked": ranked, "flagged": flagged,
    }, indent=2))
    # Remember what we recommended so future runs can freshen instead of repeating.
    for r in ranked:
        hist[r["id"]] = hist.get(r["id"], 0) + 1
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(json.dumps(hist, indent=0))
    except Exception:
        pass
    tiers = {}
    for r in ranked:
        tiers[r.get("tier", "?")] = tiers.get(r.get("tier", "?"), 0) + 1
    print(f"coach_rank: ranked {len(ranked)} jobs {tiers} · {len(briefing.get('top_ids',[]))} top picks "
          f"· {len(flagged)} flagged not-relevant")
    print(f"coach_rank: headline — {briefing.get('headline','')}")
    print(f"coach_rank: wrote {OUT.relative_to(ROOT)}")
    health_mod.set_coach_status(HEALTH, "success")   # Phase 13: coach ran cleanly
    if args.publish:
        _publish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
