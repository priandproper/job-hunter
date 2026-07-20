#!/usr/bin/env python3
"""Turn pasted LinkedIn profile text into a People CSV you import on the dashboard.

This is the ToS-safe way to bulk-add people: LinkedIn can't be scraped by a bot, but
YOU can open a profile, press Cmd/Ctrl+A then Cmd/Ctrl+C, and paste what you can see.
This script structures that text (current + PAST companies, title, location, etc.) into
`data/people.local.csv`, which you then import on the dashboard's People page. Nothing
is uploaded; the CSV is git-ignored and the dashboard keeps it in localStorage only.

Workflow:
  1. Paste one or more profiles into data/linkedin_paste.txt, separated by a line
     containing only `---`.
  2. python3 scripts/linkedin_people.py            # -> data/people.local.csv
  3. Dashboard -> People -> "Import CSV".

Parsing uses your Claude key if available (ANTHROPIC_API_KEY via env or .secrets.json)
for robust extraction incl. past companies; otherwise a best-effort heuristic runs and
you fill gaps in the dashboard. Stdlib only.
"""

import argparse
import csv
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib import secrets as secrets_mod  # noqa: E402

IN_DEFAULT = ROOT / "data" / "linkedin_paste.txt"
OUT_DEFAULT = ROOT / "data" / "people.local.csv"
SECRETS = ROOT / ".secrets.json"
COLUMNS = ["name", "title", "company", "past_companies", "seniority", "location",
           "linkedin", "email", "relationship", "tags", "how_known", "notes"]

_SYS = (
    "Extract structured facts from a pasted LinkedIn profile (the visible text a person "
    "copied). Return ONLY JSON matching the schema. Use empty strings/arrays for anything "
    "not present — never guess or invent. 'company' = their CURRENT employer. "
    "'past_companies' = the OTHER companies in their Experience section (previous "
    "employers), most recent first, company names only. 'seniority' is one of: "
    "Individual contributor, Manager, Senior Manager, Director, Senior Director, VP, "
    "Head of, C-level, Founder, Recruiter, Other — or empty. 'notes' = a 1-2 sentence "
    "summary of their focus useful for outreach. 'tags' = a few short lowercase labels."
)
_STR = {"type": "string"}
_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "name": _STR, "title": _STR, "company": _STR,
        "past_companies": {"type": "array", "items": _STR},
        "seniority": _STR, "location": _STR, "linkedin": _STR, "email": _STR,
        "notes": _STR, "tags": {"type": "array", "items": _STR},
    },
    "required": ["name", "title", "company", "past_companies", "seniority",
                 "location", "linkedin", "email", "notes", "tags"],
}


def _claude(api_key: str, model: str, text: str) -> dict:
    body = json.dumps({
        "model": model, "max_tokens": 1200, "system": _SYS,
        "output_config": {"format": {"type": "json_schema", "schema": _SCHEMA}},
        "messages": [{"role": "user", "content": "PROFILE TEXT:\n" + text}],
    }).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "content-type": "application/json", "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return json.loads(txt)


_JUNK = re.compile(r"^\s*(\d+(st|nd|rd|th)|·|Message|Connect|Follow|More|Contact info|"
                   r"Pending|Open to work|Premium)\b", re.I)


def _heuristic(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    name = next((ln for ln in lines if not _JUNK.match(ln)), "")
    headline = ""
    for ln in lines[1:6]:
        if " at " in ln.lower() or any(w in ln.lower() for w in ("manager", "director", "lead", "engineer", "marketing")):
            headline = ln
            break
    company = ""
    m = re.search(r"\bat\s+([A-Z][\w&.,'\- ]{1,40})", headline)
    if m:
        company = m.group(1).strip(" .,")
    # Past companies: lines just before an employment-type/date marker in Experience.
    past, seen_exp = [], False
    for i, ln in enumerate(lines):
        if ln.lower() == "experience":
            seen_exp = True
            continue
        if seen_exp and re.search(r"·\s*(Full-time|Part-time|Contract|Internship|Freelance)", ln):
            # The company is on THIS line, before the "·" (e.g. "Twilio · Full-time").
            cand = re.sub(r"\s*·.*$", "", ln).strip()
            if cand and cand.lower() not in (company.lower(), name.lower()) and cand not in past:
                past.append(cand)
    return {"name": name, "title": headline, "company": company, "past_companies": past,
            "seniority": "", "location": "", "linkedin": "", "email": "", "notes": "", "tags": []}


def _row(p: dict) -> dict:
    j = lambda v: "; ".join(v) if isinstance(v, list) else (v or "")
    return {
        "name": p.get("name", ""), "title": p.get("title", ""), "company": p.get("company", ""),
        "past_companies": j(p.get("past_companies", [])), "seniority": p.get("seniority", ""),
        "location": p.get("location", ""), "linkedin": p.get("linkedin", ""),
        "email": p.get("email", ""), "relationship": "", "tags": j(p.get("tags", [])),
        "how_known": "", "notes": p.get("notes", ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("infile", nargs="?", default=str(IN_DEFAULT),
                    help="text file of profiles separated by a line of '---'")
    ap.add_argument("-o", "--out", default=str(OUT_DEFAULT))
    ap.add_argument("--heuristic", action="store_true", help="skip Claude; use the heuristic parser")
    args = ap.parse_args()

    src = Path(args.infile)
    if not src.exists():
        print(f"linkedin_people: create {src} — paste profiles separated by a line of '---'.")
        return 2
    blocks = [b.strip() for b in re.split(r"(?m)^\s*---+\s*$", src.read_text()) if b.strip()]
    if not blocks:
        print("linkedin_people: no profiles found in the input file.")
        return 1

    api_key = None if args.heuristic else secrets_mod.get_key("ANTHROPIC_API_KEY", SECRETS)
    model = "claude-opus-4-8"
    rows, used_claude = [], False
    for i, block in enumerate(blocks, 1):
        p = None
        if api_key:
            try:
                p = _claude(api_key, model, block)
                used_claude = True
            except Exception as e:
                print(f"  profile {i}: Claude parse failed ({e}); using heuristic")
        if p is None:
            p = _heuristic(block)
        if p.get("name"):
            rows.append(_row(p))
            print(f"  ✓ {p.get('name')} — {p.get('company') or '?'}"
                  + (f"  (past: {', '.join(p.get('past_companies') or [])})" if p.get("past_companies") else ""))
        else:
            print(f"  ✗ profile {i}: couldn't find a name — skipped")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    try:
        shown = out.relative_to(ROOT)
    except ValueError:
        shown = out
    print(f"\nlinkedin_people: wrote {len(rows)} person(s) -> {shown}"
          f"  ({'Claude' if used_claude else 'heuristic'} parse)")
    print("Next: open the dashboard -> People -> Import CSV, and pick that file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
