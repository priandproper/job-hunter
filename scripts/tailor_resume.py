#!/usr/bin/env python3
"""Tailor your resume to ONE job via the Claude CLI → a ready-to-import JSON payload.

Replaces the dashboard's "Copy prompt" flow. Given a job (by --id / --company / --job)
it reads the live jobs.json (same data the dashboard shows), takes the resume the app
loaded for that job (job.resume_core, or --resume <file>), and asks Claude — through the
`claude -p` CLI, your logged-in plan, no API key — to rewrite it for this role using the
exact same rules the app uses (rephrase language, never invent facts).

The FROZEN facts (contact, employers, titles, dates, education) are re-applied from your
real resume after generation, so nothing factual can drift. Output:
  - data/tailored.<company>-<title>.json  — the resume JSON, ready to paste into the
    builder's "Paste tailored JSON" box, and
  - a resume-builder import URL printed to the terminal — open it and the resume is ready.

  python3 scripts/tailor_resume.py --id <job-id>
  python3 scripts/tailor_resume.py --company Datadog --job "Product Marketing Manager - APM"
  python3 scripts/tailor_resume.py --company Datadog --list         # find the id first
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from lib import ats as ats_mod              # noqa: E402  (clean_jd)
import reach_out as ro                       # noqa: E402  (find_job, _load_jobs, _job_line)

# The same instruction block the dashboard uses, so the CLI output matches the app.
TAILOR_INSTRUCTIONS = """You tailor a candidate's resume to a specific job. You rewrite only the language that
sells the candidate — never the facts — and return ONE JSON object with EXACTLY the
structure defined below.

OUTPUT FORMAT
- Output the JSON object and NOTHING else: no prose, no explanation, no markdown, no code fences.
- Valid JSON only: double-quoted keys and strings, no trailing commas, no comments. Keep non-ASCII characters as-is.
- Return EXACTLY the keys and nesting in the DATA SHAPE. Do not add, remove, or rename keys.

DATA SHAPE  ("FROZEN" = copy from the input verbatim; "EDIT" = you may rewrite)
{
  "schemaVersion": number,              // FROZEN
  "id": string,                         // FROZEN
  "label": string,                      // SET to "<Company> — <Job Title>" for this application
  "contact": { "fullName","email","phone","location","website","linkedin","github" },  // FROZEN, copy exactly
  "summary": string,                    // EDIT — rewrite to target the job (2-4 sentences)
  "experience": [                       // Keep the SAME entries in the SAME order. Do not add, drop, merge, or reorder.
    { "company","title","location","startDate","endDate": FROZEN,
      "highlights": [string, ...]       // EDIT — rephrase/re-emphasize; keep roughly the same number }
  ],
  "education": [ {...} ],               // FROZEN — copy every entry and field exactly
  "projects": [ { "name" FROZEN, "link"?, "description"? EDIT, "highlights": [..] EDIT } ],
  "skills": [ { "name": string, "items": [string, ...] } ],  // EDIT — surface job-relevant first
  "createdAt": string, "updatedAt": string   // FROZEN if present
}

RULES
1. FROZEN = factual. Copy character-for-character. Never alter or invent employers, titles, companies,
   locations, dates, degrees, institutions, awards, or contact details.
2. Keep the SAME experience and education entries, in the SAME order — no adds, drops, merges, or reordering.
   Within each experience, keep about the same number of highlights.
3. You MAY rewrite only: "summary", the TEXT of each experience "highlights" bullet, "projects"
   descriptions/highlights, and the ORDER/grouping of "skills". Set "label" to "<Company> — <Job Title>".
4. Truthfulness is absolute: only rephrase and re-emphasize accomplishments, metrics, and tools that are
   genuinely present. Never fabricate a result, and never add a skill/tool the candidate does not have.
5. Weave the target job's keywords in where they authentically apply — prioritize the "missing keywords".
   If a keyword can't be used truthfully, omit it. Do not keyword-stuff.
6. Skills: include only skills from the candidate's real skill set; surface the most job-relevant first.
7. Keep each highlight tight (ideally one line) so the resume stays a single page."""


def base_resume(job: dict) -> dict:
    """The resume the app would tailor: job.resume_core, normalized to the builder shape."""
    core = job.get("resume_core") or {}
    return {
        "schemaVersion": core.get("schemaVersion", 1),
        "id": core.get("id") or ("res_" + job.get("id", "")),
        "label": core.get("label") or job.get("best_variant") or "",
        "contact": core.get("contact", {}) or {},
        "summary": core.get("summary", ""),
        "experience": core.get("experience", []) or [],
        "education": core.get("education", []) or [],
        "projects": core.get("projects", []) or [],
        "skills": core.get("skills", []) or [],
        "createdAt": core.get("createdAt", ""),
        "updatedAt": core.get("updatedAt", ""),
    }


def build_prompt(job: dict, base: dict) -> str:
    s = (TAILOR_INSTRUCTIONS
         + "\n\nCURRENT RESUME  (return the same shape)\n" + json.dumps(base)
         + "\n\nTARGET JOB\nTitle:    " + (job.get("title") or "")
         + "\nCompany:  " + (job.get("company") or "")
         + "\nKeywords requested:            " + (", ".join(job.get("requested_keywords") or []) or "(none captured)")
         + "\nKeywords missing from resume:  " + (", ".join(job.get("missing_keywords") or []) or "(none)"))
    jd = ats_mod.clean_jd(job.get("excerpt") or "")
    if jd:
        s += "\n\nFULL JOB DESCRIPTION  (align the resume to this)\n" + jd[:8000]
    s += "\n\nReturn the tailored resume as JSON only — no prose, no code fences."
    return s


def claude_json(prompt: str, model: str) -> dict:
    proc = subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "json"],
        input=prompt, capture_output=True, text=True, timeout=300)
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
    return json.loads(m.group(0) if m else t)


_FROZEN_EXP = ("company", "title", "location", "startDate", "endDate")


def enforce_frozen(base: dict, t: dict, job: dict) -> tuple[dict, list]:
    """Re-apply the factual fields from the real resume so nothing can drift/fabricate."""
    warns = []
    t = dict(t)
    t["schemaVersion"] = base.get("schemaVersion", 1)
    t["id"] = base.get("id")
    t["contact"] = base.get("contact", {})            # identity is frozen
    t["education"] = base.get("education", [])         # education is frozen
    if base.get("createdAt"):
        t["createdAt"] = base["createdAt"]
    t.setdefault("label", "") or None
    t["label"] = f"{job.get('company','')} — {job.get('title','')}".strip(" —")
    be, te = base.get("experience", []), t.get("experience", []) or []
    if len(te) == len(be):
        for b, x in zip(be, te):
            for k in _FROZEN_EXP:
                x[k] = b.get(k, "")
    else:
        warns.append(f"experience count changed ({len(be)}→{len(te)}); kept the model's — review it")
    # project names are frozen (descriptions/highlights may change) when counts match
    bp, tp = base.get("projects", []), t.get("projects", []) or []
    if len(tp) == len(bp):
        for b, x in zip(bp, tp):
            if b.get("name"):
                x["name"] = b["name"]
    for k in ("summary", "experience", "education", "projects", "skills"):
        t.setdefault(k, base.get(k, "" if k == "summary" else []))
    return t, warns


def import_url(core: dict) -> str:
    try:
        app = json.loads((ROOT / "config.json").read_text())["resume_builder"]["app_url"]
    except Exception:
        app = "https://priandproper.github.io/resume-builder"
    return app.rstrip("/") + "/?import=" + urllib.parse.quote(json.dumps(core))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "job"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default="")
    ap.add_argument("--job", default="")
    ap.add_argument("--id", dest="job_id", default="", help="exact job id (from the dashboard URL #/job/<id>)")
    ap.add_argument("--resume", default="", help="base resume JSON file (else the job's resume_core)")
    ap.add_argument("--model", default="opus", help="Claude model for the CLI (default opus)")
    ap.add_argument("--out", default="", help="output JSON file (default data/tailored.<slug>.json)")
    ap.add_argument("--list", action="store_true", help="list matching roles (with ids) and exit")
    ap.add_argument("--local", action="store_true", help="use local docs/jobs.json instead of the live one")
    args = ap.parse_args()

    if not __import__("shutil").which("claude"):
        print("tailor_resume: the `claude` CLI is required (and you must be logged in).")
        return 2
    if not (args.company or args.job or args.job_id):
        print("tailor_resume: pass --id, or --company and/or --job.")
        return 2

    doc, src = ro._load_jobs(args.local)
    job, cands = ro.find_job(doc.get("jobs", []), args.company, args.job, args.job_id)
    if not job:
        who = f"id={args.job_id!r}" if args.job_id else f"company={args.company!r} job={args.job!r}"
        print(f"tailor_resume: no job matched {who} in {src}.")
        if args.job_id:
            print("  That id isn't in the current scan — use --list to find the current one.")
        return 1
    if args.list:
        print(f"{len(cands)} matching role(s), best-fit first:\n")
        for c in cands[:25]:
            print("  - " + ro._job_line(c, with_id=True))
        return 0

    print("Tailoring resume for: " + ro._job_line(job, with_id=True))

    if args.resume:
        base = json.loads(Path(args.resume).read_text())
    else:
        base = base_resume(job)
    if not base.get("experience"):
        print("tailor_resume: no resume found for this job (empty resume_core). Pass --resume <file>.")
        return 1

    print(f"  drafting with Claude CLI ({args.model})…")
    try:
        raw = claude_json(build_prompt(job, base), args.model)
    except Exception as e:
        print(f"tailor_resume: Claude failed ({e})")
        return 1
    if not (raw.get("contact") is not None and raw.get("experience")):
        print("tailor_resume: model output didn't look like a resume — try again.")
        return 1
    core, warns = enforce_frozen(base, raw, job)

    out = Path(args.out) if args.out else (ROOT / "data" / f"tailored.{_slug(job.get('company',''))}-{_slug(job.get('title',''))}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(core, indent=2))
    rel = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out

    print(f"\n✓ Tailored resume written -> {rel}")
    for w in warns:
        print(f"  ⚠ {w}")
    print("\nUse it either way:")
    print(f"  • paste {rel} into the dashboard's \"Paste tailored JSON → build resume link\" box, or")
    print(f"  • open this URL — the resume loads into the builder, ready for this job:\n\n{import_url(core)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
