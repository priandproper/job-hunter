#!/usr/bin/env python3
"""Batch referral report: for every job, who can refer you + a ready-to-send message.

Pulls referral candidates from BOTH sources the app already has:
  1. data/private.local.json  -> "referrals": in-network connections the worker matched
     to each job (from your LinkedIn connections export, data/connections.csv).
  2. data/people.local.csv     -> your People directory (e.g. recruiters you imported),
     matched to a job by company name.

For each candidate it drafts a warm, ready-to-send message — recruiter-aware ("review my
candidacy" for recruiters, "refer me" for peers). Writes data/referrals.local.md (PII →
git-ignored). No network, no Claude; instant. For a single polished message use
scripts/reach_out.py. Stdlib only.
"""

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "docs" / "jobs.json"
PRIVATE = ROOT / "data" / "private.local.json"
PEOPLE_CSV = ROOT / "data" / "people.local.csv"
CONFIG = ROOT / "config.json"
OUT = ROOT / "data" / "referrals.local.md"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _me() -> str:
    try:
        return json.loads(CONFIG.read_text()).get("contact_public", {}).get("fullName", "").strip() or "Me"
    except Exception:
        return "Me"


def _is_recruiter(position: str, tags: str = "") -> bool:
    return bool(re.search(r"recruit|talent|sourcer|headhunter", f"{position} {tags}", re.I))


def _message(first: str, company: str, title: str, recruiter: bool, me: str) -> str:
    if recruiter:
        ask = (f"Since you recruit at {company}, would you be open to reviewing my candidacy "
               f"for this role — or pointing me to the right person on the hiring team?")
    else:
        ask = (f"Since you're at {company}, would you be open to referring me, or pointing me "
               f"to the right person? Happy to share my resume.")
    return (f"Hi {first} — I saw {company} is hiring a {title} and it lines up with my "
            f"background in B2B product marketing, GTM, and analytics. {ask} Thanks so much!\n\n— {me}")


def _load_people() -> list:
    if not PEOPLE_CSV.exists():
        return []
    with PEOPLE_CSV.open() as f:
        return list(csv.DictReader(f))


def main() -> int:
    jobs = json.loads(JOBS.read_text()).get("jobs", [])
    jobs.sort(key=lambda j: -(j.get("fit_score") or 0))
    try:
        refs = json.loads(PRIVATE.read_text()).get("referrals", {}) or {}
    except Exception:
        refs = {}
    people = _load_people()
    me = _me()

    def candidates_for(job):
        seen, out = set(), []
        # 1) worker-matched in-network connections for this job id
        for c in refs.get(job["id"], []) or []:
            key = _norm(c.get("name", ""))
            if key and key not in seen:
                seen.add(key)
                out.append({"name": c.get("name", ""), "position": c.get("position", ""),
                            "company": c.get("company", ""), "url": c.get("url", ""),
                            "source": "connection", "tags": ""})
        # 2) People directory rows whose company matches the job's company
        jc = _norm(job.get("company", ""))
        for p in people:
            pc = _norm(p.get("company", ""))
            if jc and pc and (jc == pc or jc in pc or pc in jc):
                key = _norm(p.get("name", ""))
                if key and key not in seen:
                    seen.add(key)
                    out.append({"name": p.get("name", ""), "position": p.get("title", ""),
                                "company": p.get("company", ""), "url": p.get("linkedin", ""),
                                "source": "people", "tags": p.get("tags", "")})
        return out

    lines = [f"# Referral report", "",
             f"_{len(jobs)} on-target jobs · your name: {me}_", ""]
    jobs_with, total_contacts = 0, 0
    for job in jobs:
        cands = candidates_for(job)
        if not cands:
            continue
        jobs_with += 1
        total_contacts += len(cands)
        lines.append(f"## {job.get('title','')} — {job.get('company','')}  ·  fit {job.get('fit_score','')}")
        if job.get("url"):
            lines.append(f"{job['url']}")
        lines.append("")
        for c in cands:
            first = (c["name"].split() or ["there"])[0]
            rec = _is_recruiter(c["position"], c["tags"])
            role = f" — {c['position']}" if c["position"] else ""
            tag = "recruiter" if rec else "referral"
            lines.append(f"### {c['name']}{role}  ({tag})")
            if c["url"]:
                lines.append(f"{c['url']}")
            lines.append("")
            lines.append("```")
            lines.append(_message(first, job.get("company", ""), job.get("title", ""), rec, me))
            lines.append("```")
            lines.append("")
        lines.append("---")
        lines.append("")

    header = [f"**{jobs_with} of {len(jobs)} jobs have a referral path** "
              f"({total_contacts} contacts). Messages below are ready to send — skim, tweak, paste.",
              ""]
    OUT.write_text("\n".join(lines[:4] + header + lines[4:]) + "\n")
    print(f"referrals_report: {len(jobs)} jobs · {jobs_with} with a referral path · "
          f"{total_contacts} contacts")
    print(f"referrals_report: wrote {OUT.relative_to(ROOT)} — ready-to-send messages per contact.")
    if not people and not refs:
        print("  (No contacts yet — import your LinkedIn connections as data/connections.csv, "
              "or add people on the dashboard, then re-run.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
