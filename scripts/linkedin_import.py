#!/usr/bin/env python3
"""Reconcile your LinkedIn outreach from LinkedIn's OFFICIAL data export.

This does NOT touch LinkedIn's systems. You export your own data
(LinkedIn -> Settings -> Data Privacy -> Get a copy of your data -> Messages),
unzip it, and point this at the `messages.csv`. It reads the messages *you sent*,
matches each to a tracked job by company (via your connections' companies, or a
company name in the message), and writes a git-ignored `scripts/outreach.local.js`.

Load that through the dashboard's "Run script" modal — the sends show up in each
job's Outreach log (merged, de-duplicated). Everything is your own data, on your
machine; nothing is scraped and nothing is uploaded.

Usage:
    python3 scripts/linkedin_import.py --messages ~/Downloads/Messages.csv
    python3 scripts/linkedin_import.py --messages path.csv --me "Priyanka Tambe"
    python3 scripts/linkedin_import.py --demo        # synthetic messages, real jobs
"""

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "docs" / "jobs.json"
CONNECTIONS = ROOT / "data" / "connections.csv"
CONFIG = ROOT / "config.json"
OUT = ROOT / "scripts" / "outreach.local.js"
OUTREACH_KEY = "job-hunter:outreach:v1"

_SUFFIX = re.compile(r"[,\.]?\s+(inc|llc|ltd|corp|co|company|technologies|technology|labs|"
                     r"software|group|holdings|global)\.?$", re.I)


def norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def norm_company(name: str) -> str:
    s = _SUFFIX.sub("", (name or "").lower().strip())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def load_jobs():
    try:
        return json.loads(JOBS.read_text()).get("jobs", [])
    except (OSError, json.JSONDecodeError):
        return []


def my_name() -> str:
    try:
        return json.loads(CONFIG.read_text()).get("contact_public", {}).get("fullName", "")
    except Exception:
        return ""


def load_connection_companies() -> dict:
    """name (lower) -> company, from the LinkedIn connections export (if present)."""
    out = {}
    if not CONNECTIONS.exists():
        return out
    try:
        text = CONNECTIONS.read_text(errors="replace").splitlines()
    except OSError:
        return out
    # LinkedIn's Connections.csv has a preamble; the real header starts at "First Name".
    start = next((i for i, ln in enumerate(text) if ln.lower().startswith("first name")), 0)
    for row in csv.DictReader(text[start:]):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = norm(f"{row.get('first name','')} {row.get('last name','')}")
        if name and row.get("company"):
            out[name] = row["company"]
    return out


def _col(row, *names):
    for n in names:
        if n in row and row[n]:
            return row[n]
    return ""


def parse_date(raw: str) -> str:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M:%S", "%m/%d/%y, %I:%M %p", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(raw, fmt).isoformat()
        except ValueError:
            continue
    return raw  # keep as-is; the UI just shows it


def read_messages(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = []
        for r in csv.DictReader(f):
            rows.append({(k or "").strip().lower(): v for k, v in r.items()})
        return rows


def match_job(recipient: str, content: str, jobs_by_company: dict, company_keys, conn_co: dict):
    # 1) a tracked company named in the message body (strongest — it states the target)
    c = norm(content)
    cand = next((k for k in company_keys if k and k in c), "")
    # 2) else fall back to the recipient's company (from your connections export)
    if not cand:
        comp = conn_co.get(norm(recipient))
        cand = norm_company(comp) if comp else ""
    jobs = jobs_by_company.get(cand)
    return jobs[0] if jobs else None   # best-fit job at that company


def build(messages, me, jobs, conn_co):
    jobs_by_company = {}
    for j in sorted(jobs, key=lambda x: x.get("fit_score", 0), reverse=True):
        jobs_by_company.setdefault(norm_company(j.get("company", "")), []).append(j)
    company_keys = [k for k in jobs_by_company if k]
    me_n = norm(me)

    out = {}
    matched = sent = 0
    for row in messages:
        sender = _col(row, "from", "sender")
        if me_n and me_n not in norm(sender):
            continue                                   # only messages you sent
        sent += 1
        recipient = _col(row, "to", "recipient")
        content = _col(row, "content", "message", "body")
        if not content.strip():
            continue
        job = match_job(recipient, content, jobs_by_company, company_keys, conn_co)
        if not job:
            continue
        ts = parse_date(_col(row, "date", "sent at", "timestamp"))
        sig = hashlib.sha1(f"{recipient}|{ts}|{content[:200]}".encode()).hexdigest()[:12]
        out.setdefault(job["id"], []).append({
            "to": recipient.strip(), "message": content.strip()[:1500], "ts": ts,
            "channel": "linkedin", "source": "linkedin-export", "sig": sig})
        matched += 1
    return out, {"sent": sent, "matched": matched, "jobs": len(out)}


def write_inject(by_job: dict):
    payload = json.dumps(by_job)
    OUT.write_text(
        "// One-off: open your dashboard, hit \"Run script\", load this file. It merges\n"
        "// your LinkedIn-export outreach into each job's Outreach log (de-duped by sig).\n"
        "// From your own official data export. Git-ignored — never committed.\n"
        f"var O = {payload};\n"
        "(function(){try{var cur=JSON.parse(localStorage.getItem('" + OUTREACH_KEY + "'))||{};"
        "Object.keys(O).forEach(function(id){cur[id]=cur[id]||[];"
        "var have={};cur[id].forEach(function(r){if(r.sig)have[r.sig]=1;});"
        "O[id].forEach(function(r){if(!r.sig||!have[r.sig])cur[id].push(r);});});"
        "localStorage.setItem('" + OUTREACH_KEY + "',JSON.stringify(cur));}catch(e){}})();\n"
        "location.reload();\n")


DEMO = [
    {"from": "Priyanka Tambe", "to": "Rahul Mehta", "date": "2026-07-15 09:00:00 UTC",
     "content": "Hi Rahul — I noticed Stripe is hiring a GTM Product Marketing Manager and it fits my background. Would you be open to referring me?"},
    {"from": "Priyanka Tambe", "to": "Anita Sharma", "date": "2026-07-16 14:30:00 UTC",
     "content": "Hi Anita, hope you're well! I'm applying to Notion for a Product Marketing role — any chance you could point me to the hiring manager?"},
    {"from": "Someone Else", "to": "Priyanka Tambe", "date": "2026-07-16 15:00:00 UTC",
     "content": "Thanks for reaching out, will take a look."},  # inbound — ignored
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", help="path to LinkedIn Messages.csv from your data export")
    ap.add_argument("--me", default=None, help="your name as it appears in the FROM column")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    me = args.me or my_name()
    jobs = load_jobs()
    conn_co = load_connection_companies()

    if args.demo:
        messages = DEMO
    elif args.messages:
        p = Path(args.messages).expanduser()
        if not p.exists():
            print(f"linkedin_import: no such file: {p}", file=sys.stderr)
            return 2
        messages = read_messages(p)
    else:
        print("linkedin_import: pass --messages <Messages.csv> (or --demo).\n"
              "  Export it: LinkedIn -> Settings -> Data Privacy -> Get a copy of your data -> Messages.",
              file=sys.stderr)
        return 2

    if not me:
        print("linkedin_import: couldn't determine your name; pass --me \"Your Name\".", file=sys.stderr)
        return 2

    by_job, stats = build(messages, me, jobs, conn_co)
    write_inject(by_job)
    print(f"linkedin_import: {stats['sent']} sent message(s), matched {stats['matched']} "
          f"to {stats['jobs']} job(s) -> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
