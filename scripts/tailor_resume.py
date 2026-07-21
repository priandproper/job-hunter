#!/usr/bin/env python3
"""Tailor your resume to ONE job via the Claude CLI → a ready-to-import JSON payload.

Replaces the dashboard's "Copy prompt" flow. Given a job (by --id / --company / --job)
it reads your local jobs.json (run worker.py locally to refresh it), takes the resume the app
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

Steer the tailoring with your own instructions via -p/--prompt (or just trailing words):
  python3 scripts/tailor_resume.py --id <job-id> -p "make it more analytics-driven; add a
      bullet on SQL funnel dashboards to the Glidely role and lead the summary with data skills"
  python3 scripts/tailor_resume.py --id <job-id> lean harder into product marketing storytelling
Your steering guides emphasis, tone, and which experiences/skills to foreground — it never
overrides the truthfulness rules or the hard requirements (≥3 bullets/role, ≥1 project,
≤2-sentence summary), and Claude won't invent facts to satisfy it.
"""

import argparse
import base64
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
  "summary": string,                    // EDIT — MAX 2 sentences (see HARD REQUIREMENTS)
  "experience": [                       // Keep the SAME entries in the SAME order. Do not add, drop, merge, or reorder.
    { "company","title","location","startDate","endDate": FROZEN,
      "highlights": [string, ...]       // EDIT — AT LEAST 3 per entry (see HARD REQUIREMENTS) }
  ],
  "education": [ {...} ],               // FROZEN — copy every entry and field exactly
  "projects": [ { "name", "link"?, "description"? EDIT, "highlights": [..] EDIT } ],  // AT LEAST 1 (see HARD REQUIREMENTS)
  "skills": [ { "name": string, "items": [string, ...] } ],  // EDIT — surface job-relevant first
  "createdAt": string, "updatedAt": string   // FROZEN if present
}

RULES
1. FROZEN = factual. Copy character-for-character. Never alter or invent employers, titles, companies,
   locations, dates, degrees, institutions, awards, or contact details.
2. Keep the SAME experience and education entries, in the SAME order — no adds, drops, merges, or reordering.
3. You MAY rewrite only: "summary", the TEXT of each experience "highlights" bullet, "projects"
   descriptions/highlights, and the ORDER/grouping of "skills". Set "label" to "<Company> — <Job Title>".
4. Truthfulness is absolute: only rephrase and re-emphasize accomplishments, metrics, and tools that are
   genuinely present. Never fabricate a result, and never add a skill/tool the candidate does not have.
5. Weave the target job's keywords in where they authentically apply — prioritize the "missing keywords".
   If a keyword can't be used truthfully, omit it. Do not keyword-stuff.
6. Skills: include only skills from the candidate's real skill set; surface the most job-relevant first.
7. Keep each highlight tight (ideally one line) so the resume stays a single page.

HARD REQUIREMENTS (all must hold):
A. SUMMARY — AT MOST 2 sentences, tight enough to fit ~3 lines (about 320 characters or fewer).
   Never write more than 2 sentences.
B. EXPERIENCE — EVERY experience entry has AT LEAST 3 highlights. If the source has fewer, expand
   TRUTHFULLY: split a compound accomplishment into its parts, or surface distinct real facets of the
   same work (scope, method, result, tools, cross-functional partners). NEVER invent a new result,
   metric, tool, client, or responsibility just to reach 3.
C. PROJECTS — include AT LEAST 1 project. If the input "projects" is empty, create one from a concrete
   REAL initiative already in the candidate's experience (a launch, dashboard, campaign, or analysis they
   actually delivered) — repackage true facts, invent nothing."""


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


def build_prompt(job: dict, base: dict, extra: str = "") -> str:
    s = (TAILOR_INSTRUCTIONS
         + "\n\nCURRENT RESUME  (return the same shape)\n" + json.dumps(base)
         + "\n\nTARGET JOB\nTitle:    " + (job.get("title") or "")
         + "\nCompany:  " + (job.get("company") or "")
         + "\nKeywords requested:            " + (", ".join(job.get("requested_keywords") or []) or "(none captured)")
         + "\nKeywords missing from resume:  " + (", ".join(job.get("missing_keywords") or []) or "(none)"))
    jd = ats_mod.clean_jd(job.get("excerpt") or "")
    if jd:
        s += "\n\nFULL JOB DESCRIPTION  (align the resume to this)\n" + jd[:8000]
    extra = (extra or "").strip()
    if extra:
        s += ("\n\nUSER INSTRUCTIONS  (extra steering from the candidate — HIGH PRIORITY for what to "
              "emphasize: tone, angle, which experiences/skills to foreground, and where to add or expand "
              "bullets. These re-weight and rephrase the resume; they DO NOT override the HARD REQUIREMENTS "
              "above or the rule against inventing facts. Honor them only by drawing on TRUE content the "
              "candidate actually has — if an instruction would require a fact that isn't in the resume, "
              "get as close as you truthfully can and do not fabricate.\n"
              "IMPORTANT — if any instruction asks you to FABRICATE, add fake / made-up bullets, or claim "
              "experience the candidate does not have: do NOT refuse and do NOT return an empty or prose "
              "response. Instead satisfy the honest INTENT behind it and still return the full resume JSON. "
              "E.g. 'add fake analyst bullets so I look technical' -> surface and lead with the candidate's "
              "REAL analytical/technical work (SQL, dashboards, funnel metrics, experiments, data projects) "
              "that already exists in the resume, truthfully reworded to read more technical — never invent "
              "a role, employer, metric, or project that isn't already there.)\n" + extra)
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
    if not m:
        snippet = t.strip()
        if not snippet:
            raise RuntimeError("Claude returned an empty response — it most likely declined the "
                               "request. If your -p steering asked to fabricate or add fake/made-up "
                               "content, rephrase it to emphasize your REAL experience instead.")
        raise RuntimeError("Claude replied without a resume JSON:\n    "
                           + snippet[:400].replace("\n", "\n    ")
                           + "\n  (If your -p steering asked for fabricated content, the tool won't "
                             "invent facts — rephrase it to foreground real experience.)")
    return json.loads(m.group(0))


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


def _sentences(s: str) -> list:
    return [x for x in re.split(r"(?<=[.!?])\s+", (s or "").strip()) if x.strip()]


def _trim_summary(s: str) -> str:
    """Hard guarantee: keep at most the first 2 sentences (non-fabricating — just cuts)."""
    return " ".join(_sentences(s)[:2]).strip()


def _requirement_failures(core: dict) -> list:
    """The hard requirements the model must satisfy (checked after enforcement)."""
    fails = []
    if len(_sentences(core.get("summary", ""))) > 2:
        fails.append("summary is more than 2 sentences")
    thin = [e.get("title") or e.get("company") or "?"
            for e in core.get("experience", []) if len(e.get("highlights") or []) < 3]
    if thin:
        fails.append("these roles need ≥3 highlights: " + ", ".join(thin))
    if len(core.get("projects") or []) < 1:
        fails.append("needs at least 1 project (none present)")
    return fails


def _clip(text: str) -> str:
    """Copy text to the system clipboard. Returns the tool used, or '' if none."""
    import shutil
    for tool, cmd in (("pbcopy", ["pbcopy"]), ("wl-copy", ["wl-copy"]),
                      ("xclip", ["xclip", "-selection", "clipboard"])):
        if shutil.which(tool):
            try:
                subprocess.run(cmd, input=text, text=True, timeout=5)
                return tool
            except Exception:
                return ""
    return ""


def import_url(core: dict) -> str:
    try:
        app = json.loads((ROOT / "config.json").read_text())["resume_builder"]["app_url"]
    except Exception:
        app = "https://priandproper.github.io/resume-builder"
    # Payload goes in the URL *hash* (#import=), not the query string. A full
    # resume JSON is ~6–10 KB; in the query string that overflows the server's
    # request-line limit and GitHub Pages returns "414 URI Too Long" before the
    # app loads. The hash is never sent to the server, so it always loads. base64
    # keeps it compact (and the builder decodes base64 UTF-8).
    b64 = base64.b64encode(json.dumps(core, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return app.rstrip("/") + "/#import=" + urllib.parse.quote(b64)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "job"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default="")
    ap.add_argument("--job", default="")
    ap.add_argument("--id", dest="job_id", default="", help="exact job id (from the dashboard URL #/job/<id>)")
    ap.add_argument("--resume", default="", help="base resume JSON file (else the job's resume_core)")
    ap.add_argument("-p", "--prompt", dest="extra", default="",
                    help="extra freeform steering, e.g. -p \"make it more analytics-driven; add a "
                         "bullet on SQL dashboards to the Glidely role\". Guides emphasis/framing only "
                         "— it never overrides the truthfulness rules or the hard requirements.")
    ap.add_argument("extra_words", nargs="*",
                    help="trailing words are also treated as steering (so you can skip the quotes "
                         "after -p); combined with --prompt if both are given")
    ap.add_argument("--model", default="opus", help="Claude model for the CLI (default opus)")
    ap.add_argument("--out", default="", help="output JSON file (default data/tailored.<slug>.json)")
    ap.add_argument("--list", action="store_true", help="list matching roles (with ids) and exit")
    ap.add_argument("--live", action="store_true", help="use the deployed jobs.json (GitHub Pages) instead of your local one")
    args = ap.parse_args()

    if not __import__("shutil").which("claude"):
        print("tailor_resume: the `claude` CLI is required (and you must be logged in).")
        return 2
    if not (args.company or args.job or args.job_id):
        print("tailor_resume: pass --id, or --company and/or --job.")
        return 2

    doc, src = ro._load_jobs(args.live)
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

    extra = " ".join(x for x in ([args.extra] + list(args.extra_words)) if x).strip()
    if extra:
        print(f"  steering: {extra}")
    print(f"  drafting with Claude CLI ({args.model})…")
    prompt = build_prompt(job, base, extra)
    core, warns, fails = None, [], []
    for attempt in range(2):
        p = prompt if not fails else (prompt + "\n\nYour previous draft broke these HARD REQUIREMENTS. "
                                      "Fix ALL of them (truthfully, no fabrication) and return the full JSON again:\n- "
                                      + "\n- ".join(fails))
        try:
            raw = claude_json(p, args.model)
        except Exception as e:
            print(f"tailor_resume: Claude failed ({e})")
            return 1
        if not (raw.get("contact") is not None and raw.get("experience")):
            print("tailor_resume: model output didn't look like a resume — try again.")
            return 1
        core, warns = enforce_frozen(base, raw, job)
        core["summary"] = _trim_summary(core.get("summary", ""))   # guarantee ≤2 sentences
        fails = _requirement_failures(core)
        if not fails:
            break
        if attempt == 0:
            print(f"  re-drafting to meet requirements ({'; '.join(fails)})…")
    warns += fails   # anything still short after the retry becomes a visible warning (never fabricated to force it)

    out = Path(args.out) if args.out else (ROOT / "data" / f"tailored.{_slug(job.get('company',''))}-{_slug(job.get('title',''))}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(core, indent=2))
    rel = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out

    url = import_url(core)
    clipped = _clip(url)
    print(f"\n✓ Tailored resume written -> {rel}")
    for w in warns:
        print(f"  ⚠ {w}")
    if clipped:
        print("\n✓ Resume-builder link copied to your clipboard — just paste it in your browser:\n")
    else:
        print("\nOpen this URL to load the resume into the builder:\n")
    print(url + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
