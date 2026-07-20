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


_JUNK = re.compile(r"^\s*(\d+(st|nd|rd|th)?|·|Message|Connect|Follow|More|Contact info|"
                   r"Pending|Open to work|Premium)\b", re.I)
_DEGREE = re.compile(r"^·?\s*(1st|2nd|3rd)\b", re.I)
_PRONOUN = re.compile(r"^(he/him|she/her|they/them)\b", re.I)
# LinkedIn nav / UI chrome + section headers — never a person's name.
_CHROME = {"home", "my network", "jobs", "messaging", "notifications", "more", "me",
           "for business", "advertise", "message", "connect", "follow", "following",
           "cover photo", "contact info", "highlights", "activity", "about", "experience",
           "education", "skills", "interests", "featured", "recommendations", "posts",
           "comments", "i'm looking for…", "i'm looking for"}
# Everything from these markers on is OTHER people or the page footer — drop it.
_STOP = re.compile(r"^(more profiles for you|people you may know|you might like|"
                   r"explore premium profiles|others named|more from|linkedin corporation)\b", re.I)
_EXP_END = {"education", "skills", "licenses & certifications", "interests",
            "recommendations", "volunteering", "courses", "projects", "publications"}


def _heuristic(text: str) -> dict:
    raw = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = []
    for ln in raw:                       # cut off "other people" sections + footer
        if _STOP.match(ln):
            break
        lines.append(ln)

    # Name = the line right before the connection-degree marker ("· 2nd"), skipping a
    # pronoun line. This dodges the nav chrome at the top of a whole-page copy.
    name = ""
    for i, ln in enumerate(lines):
        if _DEGREE.match(ln):
            j = i - 1
            while j >= 0 and _PRONOUN.match(lines[j]):
                j -= 1
            cand = lines[j] if j >= 0 else ""
            if cand and cand.lower() not in _CHROME and not _DEGREE.match(cand) and 2 < len(cand) < 60:
                name = cand
                break
    if not name:
        name = next((ln for ln in lines if ln.lower() not in _CHROME
                     and not _JUNK.match(ln) and 2 < len(ln) < 60), "")

    # Headline = first title-ish line after the name.
    headline = ""
    if name in lines:
        k = lines.index(name)
        for ln in lines[k + 1:k + 6]:
            if _DEGREE.match(ln) or _PRONOUN.match(ln) or ln.lower() in _CHROME:
                continue
            headline = re.split(r"\s*[|⎮¦│‖]|\s+#|\s+We['’]re\b", ln)[0].strip()
            break

    # Experience companies: LinkedIn prefixes each role with "<Company> logo" and/or a
    # "<Company> · Full-time" line. First is the current employer; the rest are past.
    companies, seen_exp = [], False
    for ln in lines:
        low = ln.lower()
        if low == "experience":
            seen_exp = True
            continue
        if low in _EXP_END:
            seen_exp = False
        if not seen_exp:
            continue
        cand = ""
        if ln.endswith(" logo"):
            cand = ln[:-5].strip()
        elif re.search(r"·\s*(Full-time|Part-time|Contract|Internship|Freelance|Self-employed)", ln):
            cand = re.sub(r"\s*·.*$", "", ln).strip()
        if cand and 1 < len(cand) < 50 and cand.lower() != name.lower() and cand not in companies:
            companies.append(cand)

    company = ""
    if companies:
        company = companies[0]
    else:
        m = re.search(r"\bat\s+(.+)", headline)      # fall back to the headline's "at X"
        if m:
            company = re.split(r"\s*[|⎮¦│‖•]|\s+#|\s+We['’]re\b|\s{2,}", m.group(1))[0].strip(" .,-")
    past = [c for c in companies if c.lower() != company.lower()]

    # Location: a "City, …, Country/State" line near the top (before Contact info).
    location = ""
    for ln in lines[:30]:
        if ln.lower() in _CHROME or ln.endswith(" logo"):
            continue
        if "United States" in ln or "Area" in ln or re.search(r",\s*[A-Z]{2}(\s|,|$)", ln):
            location = ln.strip()
            break

    return {"name": name, "title": headline, "company": company, "past_companies": past,
            "seniority": "", "location": location, "linkedin": "", "email": "", "notes": "", "tags": []}


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
