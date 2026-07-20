#!/usr/bin/env python3
"""Email the profile-ROI report ("what to work on") when the worker refreshes it.

The worker computes meta.profile_roi in docs/jobs.json at most ~once/day. This script
runs after it, compares the report's timestamp to docs/roi_seen.json (the last one we
emailed), and — only when it's newer — sends a readable digest via Gmail and records
the new timestamp. So you get roughly one profile-coaching email per day, not per run.

Credentials come from the same GitHub Actions Secrets as scripts/notify.py
(GMAIL_USER / GMAIL_USERNAME, GMAIL_APP_PASSWORD / GMAIL_PASS, ALERT_TO/ALERT_CC).
With no creds it prints what it would send and exits 0.
"""

import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "docs" / "jobs.json"
SEEN = ROOT / "docs" / "roi_seen.json"


def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _addrs(val):
    if not val:
        return []
    if isinstance(val, list):
        return [a.strip() for a in val if a and a.strip()]
    return [a.strip() for a in re.split(r"[,;\s]+", val) if a.strip()]


def render(roi: dict) -> str:
    rep = roi.get("report", {})
    L = [f"Profile readiness: {rep.get('completeness_score','?')}/100",
         rep.get("summary", ""), "",
         f"(based on your top {roi.get('jobs','?')} matching jobs)", ""]
    moves = rep.get("top_moves", [])
    if moves:
        L.append("HIGHEST-ROI MOVES")
        for i, m in enumerate(moves, 1):
            L.append(f"  {i}. [{m.get('type','')}/{m.get('effort','')} effort] {m.get('title','')}")
            L.append(f"      {m.get('why','')}" + (f"  ->  {m['impact']}" if m.get('impact') else ""))
        L.append("")
    if rep.get("skills_to_learn"):
        L.append("SKILLS TO LEARN")
        for s in rep["skills_to_learn"]:
            L.append(f"  - {s.get('skill','')} ({s.get('demand','')}): {s.get('note','')}")
        L.append("")
    if rep.get("projects_to_build"):
        L.append("PROJECTS TO BUILD")
        for p in rep["projects_to_build"]:
            L.append(f"  - {p.get('name','')}: {p.get('description','')}")
            if p.get("unlocks"):
                L.append(f"      unlocks: {p['unlocks']}")
        L.append("")
    if rep.get("resume_reframes"):
        L.append("RESUME REFRAMES  (⚠ = verify it's true before using)")
        for f in rep["resume_reframes"]:
            flag = "" if f.get("defensible") else "⚠ "
            L.append(f"  {flag}now: {f.get('current','')}")
            L.append(f"      -> {f.get('suggested','')}")
        L.append("")
    if rep.get("study_plan"):
        L.append("WHAT TO STUDY")
        for s in rep["study_plan"]:
            L.append(f"  - {s.get('topic','')}: {s.get('why','')}")
        L.append("")
    gh = (load_json(JOBS, {}).get("meta", {}) or {}).get("github", {}) or {}
    if gh.get("owner") and gh.get("repo"):
        L.append(f"Full breakdown on your Cockpit: https://{gh['owner']}.github.io/{gh['repo']}/")
    return "\n".join(L)


def main() -> int:
    doc = load_json(JOBS, {})
    roi = (doc.get("meta", {}) or {}).get("profile_roi")
    if not roi or not roi.get("report") or not roi.get("ts"):
        print("roi_email: no profile-ROI report to send")
        return 0
    if load_json(SEEN, {}).get("ts") == roi["ts"]:
        print("roi_email: report unchanged since last email — skipping")
        return 0

    cfg = load_json(ROOT / "config.json", {})
    alerts = cfg.get("alerts", {}) if isinstance(cfg, dict) else {}
    body = render(roi)
    score = roi.get("report", {}).get("completeness_score", "?")

    user = os.environ.get("GMAIL_USER") or os.environ.get("GMAIL_USERNAME")
    pw = os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get("GMAIL_PASS")
    to_list = _addrs(os.environ.get("ALERT_TO") or alerts.get("to")) or ([user] if user else [])
    cc_list = _addrs(os.environ.get("ALERT_CC") or alerts.get("cc"))

    if not (user and pw and to_list):
        print("roi_email: no Gmail creds — would have sent:\n")
        print(body)
    else:
        msg = EmailMessage()
        msg["Subject"] = f"📈 Job Hunter: what to work on (profile {score}/100)"
        msg["From"] = user
        msg["To"] = ", ".join(to_list)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(user, pw)
            s.send_message(msg)
        print(f"roi_email: emailed profile-ROI ({score}/100) to {', '.join(to_list)}")

    SEEN.write_text(json.dumps({"ts": roi["ts"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
