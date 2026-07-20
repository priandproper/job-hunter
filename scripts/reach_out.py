#!/usr/bin/env python3
"""Who to reach out to for a job — best people + ready-to-send messages.

Given a job (by --company / --job) it:
  1. finds the matching job in docs/jobs.json,
  2. loads your People directory exported from the dashboard (data/people.local.json —
     People page -> Export), because people live in the browser's localStorage only,
  3. ranks who to reach out to for THIS job — people linked to it, current/former
     employees of the company, weighted by relationship strength + recruiter/HM signal,
  4. drafts a warm, ready-to-SEND message for each of the top people via the Claude Code
     CLI, using your resume (the job's resume_core, or --resume) as the reference.

Uses `claude -p` (your logged-in Claude plan — no API key). Stdlib only.

This drafts outreach for ONE job you name — it does NOT decide which roles fit you
(that's fit_score / the dashboard's 'Best for me' sort / the Cockpit 'What to work on').
With only --company it targets your highest-FIT role there; pass --job to pick another,
or --list to browse a company's roles by fit first.

  # In the dashboard: People -> Export (saves people.local.json into data/), then:
  python3 scripts/reach_out.py --company Datadog --list           # see the roles, by fit
  python3 scripts/reach_out.py --company Datadog --job "Product Marketing Manager"
  python3 scripts/reach_out.py --company Twilio --top 5 --out data/reachout.md
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "docs" / "jobs.json"
PEOPLE = ROOT / "data" / "people.local.json"

REL_RANK = {"1st": 6, "colleague": 5, "alum": 4, "friend": 3, "2nd": 2,
            "recruiter": 1, "unknown": 0}
REL_LABEL = {"1st": "1st-degree connection", "2nd": "2nd-degree", "colleague":
             "former colleague", "alum": "school/alum connection", "friend": "friend",
             "recruiter": "recruiter", "unknown": "not yet connected"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def find_job(jobs, company, title):
    """Best-matching job + all candidates. With a title, rank by word overlap then
    fit; without one, rank by fit_score (so 'just --company' picks your best match)."""
    cands = jobs
    if company:
        nc = _norm(company)
        cands = [j for j in jobs if nc and nc in _norm(j.get("company", ""))]
    if title:
        toks = set(_norm(title).split())
        cands = sorted(cands, key=lambda j: (len(toks & set(_norm(j.get("title", "")).split())),
                                             j.get("fit_score", 0)), reverse=True)
    else:
        cands = sorted(cands, key=lambda j: j.get("fit_score", 0), reverse=True)
    return (cands[0] if cands else None), cands


def _job_line(j):
    loc = f" ({j.get('location')})" if j.get("location") else ""
    return f"{j.get('title')} @ {j.get('company')}{loc}  ·  fit {j.get('fit_score', '?')}"


_REC_RE = re.compile(r"recruit|talent acquisition|talent partner|sourcer|talent acquisition", re.I)


def _is_recruiter(p: dict, assoc: dict | None) -> bool:
    tags = [t.lower() for t in (p.get("tags") or [])]
    return ("recruiter" in tags or p.get("relationship") == "recruiter"
            or (assoc and assoc.get("role") == "recruiter")
            or bool(_REC_RE.search(p.get("title") or "")))


def rank_people(people, job):
    jc = _norm(job.get("company", ""))
    scored = []
    for p in people:
        score, why = 0, []
        assoc = next((a for a in (p.get("jobs") or []) if a.get("id") == job.get("id")), None)
        if assoc:
            score += 100
            why.append(f"linked to this job as {assoc.get('role', 'contact')}")
        cur = bool(jc) and _norm(p.get("company", "")) == jc
        past = bool(jc) and jc in [_norm(c) for c in (p.get("past_companies") or [])]
        if cur:
            score += 60
            why.append(f"works at {job.get('company')} now")
        elif past:
            score += 40
            why.append(f"previously at {job.get('company')}")
        # Only people actually connected to THIS job or company qualify — relationship
        # strength / recruiter signal rank them, but don't make an unrelated contact relevant.
        if not (assoc or cur or past):
            continue
        rel = p.get("relationship", "unknown")
        score += REL_RANK.get(rel, 0) * 4
        is_rec = _is_recruiter(p, assoc)
        if is_rec:
            score += 15
        if p.get("email"):
            score += 2
        # Ask type drives the tone: recruiters get a "consider my candidacy" ask (NOT
        # "refer me"); peers who work there get a referral ask; former employees an intro.
        if is_rec:
            ask = "recruiter"
        elif cur or (assoc and assoc.get("role") in ("hiring manager", "referrer", "decision maker")):
            ask = "referral"
        elif past:
            ask = "intro"
        else:
            ask = "connect"
        scored.append({"score": score, "person": p, "why": why, "ask": ask})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def resume_summary(core: dict) -> str:
    if not core:
        return "(resume unavailable)"
    c = core.get("contact", {}) or {}
    L = [f"Name: {c.get('fullName', '')}".strip()]
    if core.get("summary"):
        L.append("Summary: " + core["summary"])
    for e in (core.get("experience") or [])[:3]:
        h = (e.get("highlights") or [""])[0]
        L.append(f"- {e.get('title', '')} @ {e.get('company', '')}: {h}".strip())
    skills = []
    for g in core.get("skills", []) or []:
        skills += (g.get("items", []) if isinstance(g, dict) else [g])
    if skills:
        L.append("Skills: " + ", ".join(skills[:15]))
    return "\n".join(x for x in L if x.strip())


def _cli(prompt: str, model: str) -> str:
    proc = subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "json"],
        input=prompt, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip() or f"claude exited {proc.returncode}")
    out = (proc.stdout or "").strip()
    try:
        env = json.loads(out)
        return (env.get("result", out) if isinstance(env, dict) else out).strip()
    except json.JSONDecodeError:
        return out


ASK_GUIDE = {
    "recruiter": "IMPORTANT: they are a RECRUITER at the company — do NOT ask them to 'refer' "
                 "me; that is not a recruiter's role and reads as odd. Instead, professionally ask "
                 "whether they'd be open to reviewing / considering my candidacy for this role "
                 "(e.g. 'would you be open to taking a look at my background for this role') and/or "
                 "pointing me to the right person on the hiring team. Offer to send my resume.",
    "referral": "They work at the company and are NOT a recruiter. Ask whether they'd be open to "
                "referring me for this role, or connecting me to the right person on the team — "
                "make it easy to say yes; offer my resume and a short blurb.",
    "intro": "They previously worked at the company. Ask whether they still know anyone there "
             "who could refer me, or would make a quick intro. Acknowledge it's been a while; no "
             "pressure.",
    "connect": "Open a warm conversation, signal specific interest in this role and why I'd fit, "
               "and invite a short reply.",
}


def draft_message(me: str, entry: dict, job: dict, model: str) -> str:
    p, ask = entry["person"], entry["ask"]
    channel = ("a short email (a 'Subject:' line then the body)" if p.get("email")
               else "a LinkedIn message (max ~120 words)")
    lines = [
        f"Draft a warm, ready-to-SEND {channel} from me to {p.get('name')} about a specific job. "
        "Output ONLY the message text — no preamble, no options, no explanation, no placeholders "
        "like [Name] (use the real names given).",
        "",
        "GUIDELINES: genuine, specific, concise; reference my relevant background briefly; exactly "
        "one clear ask; easy to reply to; no clichés or fake flattery; American English.",
        "- " + ASK_GUIDE.get(ask, ASK_GUIDE["connect"]),
        "",
        "WHO I AM (the sender):", me, "",
        f"WHO I'M MESSAGING: {p.get('name')}"
        + (f", {p.get('title')}" if p.get("title") else "")
        + (f" @ {p.get('company')}" if p.get("company") else ""),
        f"- Relationship to me: {REL_LABEL.get(p.get('relationship', 'unknown'), 'unknown')}",
    ]
    if p.get("howKnown"):
        lines.append(f"- How I know them: {p['howKnown']}")
    if p.get("past_companies"):
        lines.append("- Past companies: " + ", ".join(p["past_companies"]))
    if p.get("notes"):
        lines.append(f"- Notes: {p['notes']}")
    kw = ", ".join((job.get("requested_keywords") or [])[:8])
    lines += ["",
              f"THE JOB: {job.get('title')} at {job.get('company')}"
              + (f" ({job.get('location')})" if job.get("location") else ""),
              (f"- Role emphasizes: {kw}" if kw else ""),
              (f"- Posting: {job['url']}" if job.get("url") else "")]
    return _cli("\n".join(x for x in lines if x is not None), model)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default="", help="company name (recommended)")
    ap.add_argument("--job", default="", help="job title (or part of it)")
    ap.add_argument("--people", default=str(PEOPLE))
    ap.add_argument("--resume", default="", help="resume JSON file (else the job's resume_core)")
    ap.add_argument("--top", type=int, default=3, help="how many people to draft for")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--out", default="", help="also write the results to this markdown file")
    ap.add_argument("--list", action="store_true",
                    help="just list the matching roles (fit-ranked) and exit — don't draft")
    args = ap.parse_args()

    if not __import__("shutil").which("claude"):
        print("reach_out: the `claude` CLI is required (and you must be logged in).")
        return 2
    if not (args.company or args.job):
        print("reach_out: pass --company and/or --job.")
        return 2

    doc = json.loads(JOBS.read_text())
    job, cands = find_job(doc.get("jobs", []), args.company, args.job)
    if not job:
        print(f"reach_out: no job matched company={args.company!r} job={args.job!r}.")
        return 1

    # --list: browse the company's roles by fit (to choose which one to reach out about).
    if args.list:
        print(f"{len(cands)} matching role(s), best-fit first:\n")
        for c in cands[:25]:
            print("  - " + _job_line(c))
        print("\nRe-run with --job \"<title>\" to draft outreach for a specific one.")
        return 0

    # Be explicit about WHICH job we picked and why — this script drafts outreach for
    # ONE job you choose; it does not decide which roles fit you (that's fit_score /
    # the 'Best for me' sort / the Cockpit 'What to work on').
    if args.job:
        print("Matched job: " + _job_line(job))
    else:
        print(f"No --job given → drafting for your highest-FIT {args.company or job.get('company')} "
              f"role:\n  {_job_line(job)}")
    if len(cands) > 1:
        label = "Other close matches" if args.job else f"Other {job.get('company')} roles (re-run with --job to target one)"
        print(f"  {label}:")
        for c in cands[1:6]:
            print("    - " + _job_line(c))

    ppath = Path(args.people)
    if not ppath.exists():
        print(f"reach_out: {ppath} not found. In the dashboard: People -> Export, save it there.")
        return 2
    pdata = json.loads(ppath.read_text())
    people = pdata.get("people", pdata) if isinstance(pdata, dict) else pdata
    if not people:
        print("reach_out: your exported People directory is empty.")
        return 1

    ranked = rank_people(people, job)
    if not ranked:
        print(f"reach_out: no one in your People directory is connected to {job.get('company')} "
              "or this job yet. Add people (or import from LinkedIn) and link/tag them.")
        return 0

    me = resume_summary(json.loads(Path(args.resume).read_text()) if args.resume
                        else (job.get("resume_core") or {}))
    top = ranked[:args.top]
    print(f"\nreach_out: {len(ranked)} relevant contact(s) for {job.get('title')} @ "
          f"{job.get('company')} — drafting the top {len(top)} via Claude CLI…\n")

    md = [f"# Outreach — {job.get('title')} @ {job.get('company')}",
          f"_{job.get('location', '')}_\n"]
    for i, entry in enumerate(top, 1):
        p = entry["person"]
        head = (f"{i}. {p.get('name')}"
                + (f" — {p.get('title')}" if p.get("title") else "")
                + (f" @ {p.get('company')}" if p.get("company") else ""))
        why = "; ".join(entry["why"]) or "in your network"
        try:
            msg = draft_message(me, entry, job, args.model)
        except Exception as e:
            msg = f"(couldn't draft — {e})"
        print("=" * 72)
        print(head)
        print(f"   why: {why}   |   ask: {entry['ask']}"
              + (f"   |   {p['linkedin']}" if p.get("linkedin") else ""))
        print("-" * 72)
        print(msg + "\n")
        md += [f"## {head}", f"*Why:* {why} · *ask:* {entry['ask']}"
               + (f" · [LinkedIn]({p['linkedin']})" if p.get("linkedin") else ""),
               "", "```", msg, "```", ""]

    if len(ranked) > len(top):
        extra = ", ".join(f"{e['person'].get('name')} ({'; '.join(e['why'])})" for e in ranked[len(top):len(top)+5])
        print(f"More relevant contacts (not drafted): {extra}")

    if args.out:
        outp = Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text("\n".join(md))
        print(f"\nreach_out: wrote {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
