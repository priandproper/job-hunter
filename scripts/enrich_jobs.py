#!/usr/bin/env python3
"""Enrich docs/jobs.json with Claude — via the Claude Code CLI (your logged-in plan).

For each job it reads the full JD (plus your published persona from the Gist) and adds:
  - sponsorship: Yes / No / Unknown, with the evidence from the JD
  - skills / tools / preferences the role actually asks for
  - seniority + a years range
  - boston_score (0-100): how good the location is for a Boston-based candidate
  - persona_fit (0-100) + why, given your persona

Results are written into docs/jobs.json (job["enrichment"], job["boston_score"]) AND
persisted in data/enrichment.json, keyed by a company+title signature, so:
  - re-runs are incremental (only new/changed jobs hit Claude), and
  - the worker re-merges them after a cloud rebuild (enrichment survives).

Uses `claude -p` headless — no API key or billing. Persona comes from PERSONA_GIST_ID
(env, or --persona-gist). Stdlib + lib/persona only.

Usage:
  python3 scripts/enrich_jobs.py                    # enrich everything not yet done
  python3 scripts/enrich_jobs.py --limit 30         # top 30 by fit (quick pass)
  python3 scripts/enrich_jobs.py --force            # re-do all
  python3 scripts/enrich_jobs.py --persona-gist <id>
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib import persona as persona_mod  # noqa: E402

JOBS = ROOT / "docs" / "jobs.json"
STORE = ROOT / "data" / "enrichment.json"
JD_CHARS = 2600

_INSTRUCT = (
    "You enrich job postings for a candidate's job search. For EACH job below, read its "
    "description and return one JSON object. Rules:\n"
    "- sponsorship: base it ONLY on what the JD says — \"Yes\" if it mentions offering visa/"
    "H-1B sponsorship, \"No\" if it says it will NOT sponsor / requires existing work "
    "authorization, else \"Unknown\". Put the exact phrase (or '') in sponsorship_evidence.\n"
    "- skills: the concrete hard skills the JD requires. tools: named platforms/software "
    "(e.g. SQL, Salesforce, GA4). preferences: nice-to-haves / preferred qualifications.\n"
    "- seniority: one short label (IC / Manager / Senior Manager / Director / VP / etc.).\n"
    "- min_years / max_years: required experience range from the JD (integers, or null).\n"
    "- boston_score: 0-100 for how good this job's LOCATION is for a candidate based in "
    "Boston, MA. Boston/Cambridge = 100; Greater Boston / eastern MA = 85-95; fully remote "
    "in the US = 80; Northeast/commutable (Providence, southern NH) = 70; elsewhere-US "
    "remote-friendly = 55-65; other US onsite & far (SF, LA, Austin, Seattle) = 30-45; "
    "outside the US = 0. boston_note: one short phrase explaining it.\n"
    "- persona_fit: 0-100 for how well the role matches the CANDIDATE PERSONA below "
    "(roles, skills, seniority, exclusions). persona_note: one short phrase.\n"
    "Return ONLY a JSON array — no markdown, no code fences, no prose — one object per job, "
    "each with keys: ref (the job's number), sponsorship, sponsorship_evidence, skills "
    "(array), tools (array), preferences (array), seniority, min_years, max_years, "
    "boston_score, boston_note, persona_fit, persona_note."
)


def _sig(job: dict) -> str:
    c = re.sub(r"[^a-z0-9]+", " ", (job.get("company") or "").lower()).strip()
    t = re.sub(r"[^a-z0-9]+", " ", (job.get("title") or "").lower()).strip()
    return f"{c}|{t}"


def _persona_text(p: dict) -> str:
    if not p:
        return "(no persona published — judge persona_fit on general marketing/GTM/analytics fit)"
    L = []
    if p.get("roles"):
        L.append("Target roles: " + ", ".join(p["roles"]))
    if p.get("skills"):
        L.append("Skills/tools: " + ", ".join(p["skills"]))
    if p.get("years"):
        L.append(f"Years of experience: {p['years']}")
    if p.get("seniority"):
        L.append("Seniority target: " + p["seniority"])
    if p.get("exclude"):
        L.append("Avoid titles containing: " + ", ".join(p["exclude"]))
    if p.get("locations"):
        L.append("Preferred locations: " + ", ".join(p["locations"]))
    if p.get("projects"):
        L.append("Highlights: " + p["projects"])
    return "\n".join("- " + x for x in L)


def _clean(text: str) -> str:
    import html
    t = html.unescape(text or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t]+", " ", t).strip()


def _extract_array(text: str):
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    m = re.search(r"\[.*\]", t, re.S)
    return json.loads(m.group(0) if m else t)


def _claude_cli(prompt: str, model: str = "sonnet") -> list:
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
    return _extract_array(text)


def _batch_prompt(batch: list, persona_txt: str) -> str:
    lines = [_INSTRUCT, "", "CANDIDATE PERSONA", persona_txt, "", f"JOBS ({len(batch)})"]
    for i, job in enumerate(batch, 1):
        jd = _clean(job.get("excerpt") or "")[:JD_CHARS]
        kw = ", ".join((job.get("requested_keywords") or [])[:12])
        lines.append(
            f"\n[{i}] {job.get('title','')} @ {job.get('company','')}"
            f"\n    location: {job.get('location') or '(unknown)'}"
            f"\n    current sponsorship guess: {job.get('sponsorship','Unknown')}"
            f"    min_years guess: {job.get('min_years')}"
            f"\n    keywords the pipeline saw: {kw or '(none)'}"
            f"\n    DESCRIPTION:\n{jd or '(no description captured)'}")
    return "\n".join(lines)


ENRICH_KEYS = ("sponsorship", "sponsorship_evidence", "skills", "tools", "preferences",
               "seniority", "min_years", "max_years", "boston_score", "boston_note",
               "persona_fit", "persona_note")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", default=str(JOBS))
    ap.add_argument("--limit", type=int, default=0, help="only enrich the top-N by fit")
    ap.add_argument("--batch", type=int, default=6, help="jobs per Claude call")
    ap.add_argument("--force", action="store_true", help="re-enrich even if already done")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--persona-gist", default="")
    args = ap.parse_args()

    if not __import__("shutil").which("claude"):
        print("enrich_jobs: the `claude` CLI is required (npm i -g @anthropic-ai/claude-code, then log in).")
        return 2
    jobs_path = Path(args.jobs)
    doc = json.loads(jobs_path.read_text())
    jobs = doc.get("jobs", [])
    if not jobs:
        print("enrich_jobs: no jobs in", jobs_path)
        return 1

    if args.persona_gist:
        os.environ["PERSONA_GIST_ID"] = args.persona_gist.strip()
    persona = persona_mod.load_persona()
    persona_txt = _persona_text(persona)
    print(f"enrich_jobs: persona {'loaded' if persona else 'NOT found (set PERSONA_GIST_ID or --persona-gist)'}")

    store = {}
    if STORE.exists():
        try:
            store = json.loads(STORE.read_text()).get("by_sig", {})
        except (json.JSONDecodeError, OSError):
            store = {}

    # Which jobs still need enriching?
    todo = jobs if args.force else [j for j in jobs if _sig(j) not in store]
    todo.sort(key=lambda j: j.get("fit_score", 0), reverse=True)
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("enrich_jobs: everything already enriched (use --force to redo). Merging into jobs.json…")
    else:
        print(f"enrich_jobs: enriching {len(todo)} job(s) in batches of {args.batch} via Claude CLI "
              f"(model {args.model}) — a few seconds per batch…")

    done = 0
    for start in range(0, len(todo), args.batch):
        batch = todo[start:start + args.batch]
        try:
            results = _claude_cli(_batch_prompt(batch, persona_txt), args.model)
        except Exception as e:
            print(f"  batch {start//args.batch + 1}: failed ({e}); skipping")
            continue
        by_ref = {int(r.get("ref")): r for r in results if isinstance(r, dict) and str(r.get("ref", "")).isdigit()}
        for i, job in enumerate(batch, 1):
            r = by_ref.get(i)
            if not r:
                continue
            store[_sig(job)] = {k: r.get(k) for k in ENRICH_KEYS}
            done += 1
        print(f"  batch {start//args.batch + 1}/{(len(todo)+args.batch-1)//args.batch}: "
              f"+{sum(1 for i in range(1,len(batch)+1) if i in by_ref)} enriched  ({done}/{len(todo)})")
        # save progress after every batch so a long run is never lost
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps({"count": len(store), "by_sig": store}, indent=1))

    # Merge enrichment into jobs.json.
    merged = 0
    for job in jobs:
        e = store.get(_sig(job))
        if not e:
            continue
        job["enrichment"] = e
        if isinstance(e.get("boston_score"), int):
            job["boston_score"] = e["boston_score"]
        if e.get("sponsorship") in ("Yes", "No"):
            job["sponsorship"] = e["sponsorship"]      # JD-derived beats the pipeline's guess
        merged += 1
    doc["jobs"] = jobs
    jobs_path.write_text(json.dumps(doc, indent=2))

    def _rel(p):
        try:
            return p.resolve().relative_to(ROOT)
        except ValueError:
            return p
    print(f"\nenrich_jobs: {done} newly enriched · {merged}/{len(jobs)} jobs now carry enrichment")
    print(f"  store  -> {_rel(STORE)}   jobs -> {_rel(jobs_path)}")
    print("Commit both, or just refresh the dashboard to see it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
