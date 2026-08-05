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
JOBS = ROOT / "docs" / "jobs.json"
OUT = ROOT / "docs" / "coach.json"
CONFIG = ROOT / "config.json"


def _resume_context(jobs: list) -> str:
    core = next((j.get("resume_core") for j in jobs if j.get("resume_core")), {}) or {}
    skills = []
    for g in core.get("skills", []):
        skills += (g.get("items", []) if isinstance(g, dict) else [])
    return (f"Summary: {core.get('summary','')}\n"
            f"Skills: {', '.join(skills[:24])}")


def _compact(jobs: list) -> list:
    out = []
    for j in jobs:
        out.append({
            "id": j.get("id"), "title": j.get("title", ""), "company": j.get("company", ""),
            "location": j.get("location", ""), "fit": j.get("fit_score"), "ats": j.get("ats_score"),
            "posted_at": j.get("posted_at", ""),
            "missing": (j.get("missing_keywords") or [])[:8],
            "jd": re.sub(r"\s+", " ", (j.get("excerpt") or ""))[:320],
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
  ]
}"""


def build_prompt(jobs: list) -> str:
    return (
        "You are a sharp career strategist and recruiter for this candidate. Re-rank today's "
        "on-target jobs with REAL judgment — go beyond the keyword 'fit' score.\n\n"
        "CANDIDATE (US-only; will need future H-1B sponsorship; based in Boston, MA):\n"
        "- Two target lanes: (1) product marketing / marketing — GTM, growth, marketing ops — "
        "NON-senior (under ~4 yrs); (2) analyst roles — marketing / business / sales analyst — 0-3 yrs.\n"
        + _resume_context(jobs) + "\n\n"
        "JUDGE each role on: genuine fit for the candidate's lanes AND level (not too senior); "
        "company quality / growth; SPONSORSHIP-friendliness (large/established or known H-1B sponsors "
        "beat tiny startups); Boston/remote-US location; and whether the keyword score mis-rated it. "
        "REWARD roles the keyword score under-rated (hidden gems); DEMOTE generic or over-scored ones. "
        "Be honest and specific in each 'why'.\n\n"
        + _SCHEMA_NOTE + "\n\n"
        "TODAY'S ON-TARGET JOBS (keyword-filtered already):\n"
        + json.dumps(_compact(jobs), separators=(",", ":"))
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
    sp.run(["git", "add", "docs/coach.json"], cwd=ROOT)
    if not sp.run(["git", "status", "--porcelain", "docs/coach.json"], cwd=ROOT,
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

    jobs = json.loads(JOBS.read_text()).get("jobs", [])
    if not jobs:
        print("coach_rank: no jobs in docs/jobs.json — run worker.py first."); return 1
    print(f"coach_rank: sending {len(jobs)} jobs to Claude ({args.model}) for judgment re-rank…")
    try:
        rep = claude_json(build_prompt(jobs), args.model)
    except Exception as e:
        print(f"coach_rank: Claude failed ({e})"); return 1

    ranked = [r for r in (rep.get("ranked") or []) if r.get("id")]
    valid_ids = {j["id"] for j in jobs}
    ranked = [r for r in ranked if r["id"] in valid_ids]          # drop any hallucinated ids
    briefing = rep.get("briefing") or {}
    briefing["top_ids"] = [i for i in (briefing.get("top_ids") or []) if i in valid_ids]
    from datetime import datetime, timezone
    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": args.model, "briefing": briefing, "ranked": ranked,
    }, indent=2))
    tiers = {}
    for r in ranked:
        tiers[r.get("tier", "?")] = tiers.get(r.get("tier", "?"), 0) + 1
    print(f"coach_rank: ranked {len(ranked)} jobs {tiers} · {len(briefing.get('top_ids',[]))} top picks")
    print(f"coach_rank: headline — {briefing.get('headline','')}")
    print(f"coach_rank: wrote {OUT.relative_to(ROOT)}")
    if args.publish:
        _publish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
