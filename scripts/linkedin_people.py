#!/usr/bin/env python3
"""Turn pasted LinkedIn text into a People CSV you import on the dashboard.

This is the ToS-safe way to bulk-add people: LinkedIn can't be scraped by a bot, but
YOU can open a page, press Cmd/Ctrl+A then Cmd/Ctrl+C, and paste what you can see.
Two shapes are accepted, auto-detected per block:

  A) A whole SEARCH-RESULTS page — e.g. the "Find referrers on LinkedIn" search from a
     job page. Cmd-A / Cmd-C the results and paste the WHOLE thing. Every person in the
     list is extracted in one shot (name, profile URL, title, company, location, degree,
     and the mutual connection who can intro you) by a fast structured parser — no Claude
     call needed. Each person's degree becomes their relationship (2nd-degree), recruiters
     are tagged as such, and the mutual connection lands in "how you know them".

  B) A single whole PROFILE page (current + PAST companies, title, location). Separate
     multiple profiles with a line containing only `---`. These go through Claude.

Output is `data/people.local.csv`, which you import on the dashboard's People page.
Nothing is uploaded; the CSV is git-ignored and the dashboard keeps it in localStorage.

Workflow:
  1. Paste into data/linkedin_paste.txt — a search-results page, or one/more profiles
     separated by a line of `---`. (Mix freely; each block is detected on its own.)
  2. python3 scripts/linkedin_people.py            # -> data/people.local.csv
  3. Dashboard -> People -> "Import CSV".

Other platforms (Reddit / X / community Slack/Discord / event attendee lists): paste the
whole page and pass --source, and Claude extracts every person into contacts:
  python3 scripts/linkedin_people.py data/reddit_paste.txt --source reddit
  python3 scripts/linkedin_people.py data/event.txt --source event
These are ToS-safe too (you paste what you can see); each person is tagged with the platform.

For PROFILE blocks the parser is chosen automatically: ANTHROPIC_API_KEY (env /
.secrets.json) if set, else the Claude Code CLI (`claude -p`, uses your logged-in
Max/Pro plan — no API key or billing), else a best-effort offline heuristic. Force one
with --api / --cli / --heuristic. Search-results blocks always use the structured parser
(they're regular enough that Claude adds only latency). Stdlib only.
"""

import argparse
import csv
import json
import re
import shutil
import subprocess
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


# ---- Claude Code CLI (uses your logged-in plan — no API key / billing) ----
_CLI_INSTRUCT = (_SYS + "\n\nReturn ONLY a single JSON object — no markdown, no code "
    "fences, no prose — with EXACTLY these keys: name (string), title (string), company "
    "(string), past_companies (array of strings), seniority (string), location (string), "
    "linkedin (string), email (string), notes (string), tags (array of strings).\n\n"
    "PROFILE TEXT:\n")


def _extract_json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):                       # strip a ```json … ``` fence
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    m = re.search(r"\{.*\}", t, re.S)             # grab the JSON object
    return json.loads(m.group(0) if m else t)


def _claude_cli(text: str, model: str = "sonnet") -> dict:
    """Parse a profile via `claude -p` (headless), using the signed-in Claude plan."""
    proc = subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "json"],
        input=_CLI_INSTRUCT + text, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip() or f"claude exited {proc.returncode}")
    out = (proc.stdout or "").strip()
    try:                                          # --output-format json wraps: {…,"result":"<text>"}
        env = json.loads(out)
        text_out = env.get("result", out) if isinstance(env, dict) else out
    except json.JSONDecodeError:
        text_out = out
    return _extract_json(text_out)


# ---- Multi-source people extraction (Reddit / X / community / event pastes) ----
# Any pasted blob -> a LIST of real people. Same paste-what-you-see, ToS-safe model as the
# LinkedIn parser; handled by Claude because these formats vary too much for one regex.
_SOURCE_HINT = {
    "reddit": "a Reddit thread — usernames look like u/name; each person is a redditor you could DM.",
    "x": "an X / Twitter search or thread — handles look like @name.",
    "twitter": "an X / Twitter search or thread — handles look like @name.",
    "community": "a Slack / Discord / newsletter community member list.",
    "event": "an event / webinar / meetup attendee or speaker list.",
    "other": "a web page that lists people.",
}


def _list_instruct(source: str) -> str:
    hint = _SOURCE_HINT.get(source, _SOURCE_HINT["other"])
    plat = "x" if source == "twitter" else source
    return (
        "From the pasted text, extract EVERY distinct real person who could be a networking "
        "contact. The text is " + hint + " Return ONLY a JSON ARRAY (no prose, no code fences); "
        "each element has EXACTLY these keys: name (real name if shown, else the handle/username), "
        "title, company, location, linkedin (a linkedin.com/in URL if present else \"\"), handle "
        "(@handle / u/username / profile URL on the source platform else \"\"), notes (1 sentence on "
        "who they are or what they said, useful for outreach), tags (array of short lowercase labels; "
        "ALWAYS include \"" + plat + "\"). NEVER invent a person, name, employer, or fact — use \"\" for "
        "anything not present. Skip bots, organizations, and the reader themselves.\n\nPASTED TEXT:\n")


def _extract_json_array(text: str) -> list:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    m = re.search(r"\[.*\]", t, re.S)
    data = json.loads(m.group(0) if m else t)
    return data if isinstance(data, list) else []


def _claude_list_cli(text: str, source: str, model: str = "sonnet") -> list:
    proc = subprocess.run(["claude", "-p", "--model", model, "--output-format", "json"],
                          input=_list_instruct(source) + text, capture_output=True, text=True, timeout=240)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip() or f"claude exited {proc.returncode}")
    out = (proc.stdout or "").strip()
    try:
        env = json.loads(out)
        txt = env.get("result", out) if isinstance(env, dict) else out
    except json.JSONDecodeError:
        txt = out
    return _extract_json_array(txt)


def _claude_list_api(api_key: str, model: str, text: str, source: str) -> list:
    body = json.dumps({"model": model, "max_tokens": 4000,
                       "messages": [{"role": "user", "content": _list_instruct(source) + text}]}).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return _extract_json_array(txt)


def _list_row(p: dict, source: str) -> dict:
    """Map an extracted person to the People CSV columns."""
    j = lambda v: "; ".join(v) if isinstance(v, list) else (v or "")
    plat = "x" if source == "twitter" else source
    tags = list(p.get("tags") or [])
    if plat not in [str(t).lower() for t in tags]:
        tags.append(plat)
    handle = p.get("handle", "") or ""
    linkedin = p.get("linkedin", "") or (handle if handle.startswith("http") else "")
    how = f"{plat.capitalize()} contact" + (f" ({handle})" if handle and not handle.startswith("http") else "")
    return {"name": p.get("name", ""), "title": p.get("title", ""), "company": p.get("company", ""),
            "past_companies": "", "seniority": "", "location": p.get("location", ""),
            "linkedin": linkedin, "email": "", "relationship": "unknown", "tags": j(tags),
            "how_known": how, "notes": p.get("notes", "")}


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


# ---- Search-results paste (many people at once) ----
# The "Find referrers on LinkedIn" search (and any LinkedIn people search) copies as a
# regular list: one linked name per person, then headline, then location, then a Connect
# link and a "<X> is a mutual connection" line. That structure is reliable enough to
# parse deterministically — faster, free, and offline vs a Claude call per person.
_IN_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]*linkedin\.com/in/[^)\s]+)\)", re.I)
_ANY_MDLINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_DEG_RE = re.compile(r"(1st|2nd|3rd)\b", re.I)
_DEG_ONLY = re.compile(r"^[•·\-\s]*(1st|2nd|3rd)\s*$", re.I)
# In a plain-text paste the degree sits on the SAME line as the name: "Adam Webster  • 2nd".
_DEG_INLINE = re.compile(r"^(.+?)\s*[•·]\s*(1st|2nd|3rd)\b", re.I)
_MUTUAL_RE = re.compile(r"mutual connection", re.I)
_RECRUITER_RE = re.compile(r"(recruit\w*|talent acquisition|talent partner|sourcer|headhunter|\btalent\b)", re.I)
_REL_FROM_DEG = {"1st": "1st", "2nd": "2nd", "3rd": "unknown"}
# LinkedIn UI chrome that shows up between people in a plain-text paste.
_CHROME_RE = re.compile(r"^(connect|message|follow|following|pending|more|save|view my services|"
                        r"open to work|premium|status is (online|offline)|"
                        r"\d[\d,.]*[kKmM]?\+?\s+followers?)$", re.I)


def _is_chrome(s: str) -> bool:
    return bool(_CHROME_RE.match(s.strip()))


def _degree_anchor(line: str):
    """(name, degree) if this line begins a person ('Name • 2nd'), else None."""
    m = _DEG_INLINE.match(line.strip())
    if not m:
        return None
    name = m.group(1).strip(" •·-")
    if not name or _is_chrome(name) or _MUTUAL_RE.search(name):
        return None
    return name, m.group(2).lower()


def _is_search_results(block: str) -> bool:
    """Does this block list several people (a search page) rather than one profile?"""
    prof = sum(1 for ln in block.splitlines()
               if _IN_LINK.search(ln) and not _MUTUAL_RE.search(ln))
    if prof >= 2:
        return True
    # Plain-text paste (no markdown links): count person anchors ("Name • 2nd" lines,
    # or a lone "· 2nd" line).
    deg = sum(1 for ln in block.splitlines()
              if _degree_anchor(ln) or _DEG_ONLY.match(ln.strip()))
    return deg >= 2


def _clean_profile_url(u: str) -> str:
    return re.sub(r"[?#].*$", "", u).rstrip("/")


def _split_headline(h: str) -> tuple:
    """A LinkedIn headline -> (title, current_company, [past_companies])."""
    h = (h or "").strip()
    past = []
    for m in re.finditer(r"\bEx[-\s]+([A-Z][\w.&,'’ /-]*?)(?=\s*[|•·]|$)", h):  # "Ex-PayPal"
        c = m.group(1).strip(" .,-")
        if c:
            past.append(c)
    am = re.search(r"([A-Za-z0-9.&,'’/ +-]+?)\s+alumni\b", h, re.I)                # "HubSpot & BCG alumni"
    if am:
        for c in re.split(r"\s*&\s*|,\s*", am.group(1)):
            c = c.strip(" .,-")
            if c:
                past.append(c)
    title, company = h, ""
    m = re.search(r"^(.*?\S)\s*(?:\bat\b|@)\s*(.+)$", h)                          # "... at X" / "@X"
    if m:
        title = m.group(1).strip(" .,-")
        company = re.split(r"\s*[|•·]\s*", m.group(2))[0]
        company = re.sub(r"\s+alumni\b.*$", "", company, flags=re.I).strip(" .,-")
    else:
        title = re.split(r"\s*[|•]\s*", h)[0].strip(" .,-")                        # first segment
    past = [c for c in dict.fromkeys(past) if c.lower() != company.lower()]
    return title, company, past


def _mutual_names(s: str) -> list:
    """Names from a "<X> (and Y / and N other) mutual connection(s)" line."""
    names = [t.strip() for t, _ in _ANY_MDLINK.findall(s)]     # markdown links, if any
    if names:
        return names
    core = re.sub(r"\s+(is|are)\s+.*$", "", s)                  # drop " is/are a mutual …"
    core = re.sub(r"\s+and\s+\d+\s+other.*$", "", core, flags=re.I)  # "… and 9 other"
    core = re.sub(r"\s+mutual connections?.*$", "", core, flags=re.I)
    parts = re.split(r",\s*|\s+and\s+", core)
    return [p.strip() for p in parts if p.strip() and not re.match(r"\d+\s+other", p, re.I)]


def _collect_md(text: str) -> list:
    """Markdown-link paste ([Name](/in/…)): anchor each person on their profile link."""
    people, cur = [], None

    def flush():
        nonlocal cur
        if cur and cur["name"]:
            people.append(cur)
        cur = None

    for raw in text.splitlines():
        s = raw.strip()
        if not s or s in ("*", "•", "·", "-", "—"):
            continue
        if _MUTUAL_RE.search(s):                       # "<X> is a mutual connection"
            if cur is not None:
                names = [t.strip() for t, _ in _ANY_MDLINK.findall(s)]
                cur["mutuals"].extend(n for n in names if n)
            continue
        m = _IN_LINK.search(s)
        if m:                                          # profile link -> a new person
            flush()
            deg = _DEG_RE.search(s.split(")", 1)[-1])  # degree sits after the link
            cur = {"name": m.group(1).strip(), "url": _clean_profile_url(m.group(2)),
                   "degree": deg.group(1).lower() if deg else "", "details": [], "mutuals": []}
            continue
        other = _ANY_MDLINK.search(s)
        if other and "/in/" not in other.group(2):     # Connect / Follow / Message chrome
            continue
        if cur is None:                                # header/nav noise before the first person
            continue
        if _DEG_ONLY.match(s):                          # a lone "· 2nd" line
            if not cur["degree"]:
                cur["degree"] = _DEG_RE.search(s).group(1).lower()
            continue
        cur["details"].append(s)                        # headline, then location
    flush()
    return people


def _collect_plain(text: str) -> list:
    """Plain-text paste (no links): each person is a "Name • 2nd" line followed by a
    headline, a location, some chrome (Connect / Follow / N followers) and a
    "<X> is a mutual connection" line — with blank lines in between.
    """
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and ln.strip() not in ("*", "•", "·", "-", "—")]
    n = len(lines)

    # Locate every person anchor and its (name, degree).
    anchors = []
    for i, ln in enumerate(lines):
        a = _degree_anchor(ln)                      # "Name • 2nd" on one line
        if a:
            anchors.append((i, a[0], a[1]))
        elif _DEG_ONLY.match(ln):                   # lone "· 2nd": name is the line above
            j = i - 1
            while j >= 0 and (_DEG_ONLY.match(lines[j]) or _MUTUAL_RE.search(lines[j])
                              or _is_chrome(lines[j])):
                j -= 1
            anchors.append((i, lines[j] if j >= 0 else "", _DEG_RE.search(ln).group(1).lower()))

    people = []
    for idx, (i, name, deg) in enumerate(anchors):
        end = anchors[idx + 1][0] if idx + 1 < len(anchors) else n
        details, mutuals = [], []
        for k in range(i + 1, end):
            c = lines[k]
            if _MUTUAL_RE.search(c):
                mutuals.extend(_mutual_names(c))
            elif not _is_chrome(c) and len(details) < 2:   # headline, then location
                details.append(c)
        people.append({"name": name, "url": "", "degree": deg,
                       "details": details, "mutuals": mutuals})
    return people


def _parse_search_results(text: str) -> list:
    """Extract every person from a pasted LinkedIn search-results list."""
    people = _collect_md(text) if _IN_LINK.search(text) else _collect_plain(text)

    out = []
    for c in people:
        headline = c["details"][0] if c["details"] else ""
        location = c["details"][1] if len(c["details"]) > 1 else ""
        title, company, past = _split_headline(headline)
        seniority = "Recruiter" if _RECRUITER_RE.search(headline) else ""
        mutuals = list(dict.fromkeys(c["mutuals"]))
        out.append({
            "name": c["name"], "title": title, "company": company, "past_companies": past,
            "seniority": seniority, "location": location, "linkedin": c["url"], "email": "",
            "relationship": _REL_FROM_DEG.get(c["degree"], ""),
            "tags": (["recruiter"] if seniority == "Recruiter" else []) + ["referral"],
            "how_known": ("Mutual connection: " + ", ".join(mutuals)) if mutuals else "",
            "notes": "",
        })
    return out


def _row(p: dict) -> dict:
    j = lambda v: "; ".join(v) if isinstance(v, list) else (v or "")
    return {
        "name": p.get("name", ""), "title": p.get("title", ""), "company": p.get("company", ""),
        "past_companies": j(p.get("past_companies", [])), "seniority": p.get("seniority", ""),
        "location": p.get("location", ""), "linkedin": p.get("linkedin", ""),
        "email": p.get("email", ""), "relationship": p.get("relationship", ""),
        "tags": j(p.get("tags", [])), "how_known": p.get("how_known", ""),
        "notes": p.get("notes", ""),
    }


def _write_people(rows: list, out_path: str, source_label: str) -> int:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"\nlinkedin_people: wrote {len(rows)} person(s) -> {shown}  ({source_label})")
    print("Next: open the dashboard -> People -> Import CSV, and pick that file.")
    return 0


def _run_source_extract(raw_text: str, args) -> int:
    """Extract people from a whole Reddit / X / community / event paste (via Claude)."""
    api_key = None if args.heuristic else secrets_mod.get_key("ANTHROPIC_API_KEY", SECRETS)
    has_cli = shutil.which("claude") is not None
    if args.api and not api_key:
        print("linkedin_people: --api needs ANTHROPIC_API_KEY (env or .secrets.json).")
        return 2
    use_api = bool(api_key) and not args.cli
    if not use_api and not has_cli:
        print(f"linkedin_people: reading a {args.source} paste needs the `claude` CLI or an API key "
              "(there's no offline parser for these sources).")
        return 2
    engine = "Anthropic API" if use_api else "Claude CLI (your plan)"
    print(f"linkedin_people: extracting people from a {args.source} paste with {engine}…")
    try:
        people = (_claude_list_api(api_key, "claude-opus-4-8", raw_text, args.source) if use_api
                  else _claude_list_cli(raw_text, args.source))
    except Exception as e:
        print(f"linkedin_people: extraction failed ({e})")
        return 1
    rows = [_list_row(p, args.source) for p in people if p.get("name")]
    for r in rows:
        tail = r["company"] or r["title"] or r["how_known"]
        print(f"  ✓ {r['name']} — {tail}")
    if not rows:
        print("  (no people found — is the paste from a page that lists people?)")
    return _write_people(rows, args.out, args.source)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("infile", nargs="?", default=str(IN_DEFAULT),
                    help="text file of profiles separated by a line of '---'")
    ap.add_argument("-o", "--out", default=str(OUT_DEFAULT))
    ap.add_argument("--heuristic", action="store_true", help="skip Claude; use the offline heuristic parser")
    ap.add_argument("--cli", action="store_true", help="force the Claude Code CLI (your logged-in plan)")
    ap.add_argument("--api", action="store_true", help="force the Anthropic API (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--source", default="auto",
                    choices=["auto", "linkedin", "reddit", "x", "twitter", "community", "event", "other"],
                    help="what you pasted. auto/linkedin = LinkedIn profiles or search results (the "
                         "structured parser). reddit/x/community/event/other = a whole page of people that "
                         "Claude extracts into contacts (paste a thread, a member list, an attendee list).")
    args = ap.parse_args()

    src = Path(args.infile)
    if not src.exists():
        print(f"linkedin_people: create {src} — paste people into it (a LinkedIn search, or a "
              f"reddit/community/event page with --source).")
        return 2
    raw_text = src.read_text()

    # Non-LinkedIn source: extract every person from the whole paste with Claude.
    if args.source not in ("auto", "linkedin"):
        return _run_source_extract(raw_text, args)

    blocks = [b.strip() for b in re.split(r"(?m)^\s*---+\s*$", raw_text) if b.strip()]
    if not blocks:
        print("linkedin_people: no profiles found in the input file.")
        return 1

    # Search-results pastes are parsed structurally; single profiles go through Claude.
    profile_blocks = [b for b in blocks if not _is_search_results(b)]

    # Choose the profile parser (only needed if there are profile blocks): explicit
    # flag wins, else API key > Claude CLI > heuristic.
    mode = label = None
    if profile_blocks:
        api_key = None if args.heuristic else secrets_mod.get_key("ANTHROPIC_API_KEY", SECRETS)
        has_cli = shutil.which("claude") is not None
        if args.heuristic:
            mode = "heuristic"
        elif args.api:
            mode = "api"
        elif args.cli:
            mode = "cli"
        elif api_key:
            mode = "api"
        elif has_cli:
            mode = "cli"
        else:
            mode = "heuristic"
        if mode == "api" and not api_key:
            print("linkedin_people: --api needs ANTHROPIC_API_KEY (env or .secrets.json).")
            return 2
        if mode == "cli" and not has_cli:
            print("linkedin_people: --cli needs the `claude` CLI on PATH.")
            return 2
        label = {"api": "Anthropic API", "cli": "Claude CLI (your plan)", "heuristic": "heuristic"}[mode]
        print(f"linkedin_people: {len(profile_blocks)} profile(s) via {label}"
              + (" — a few seconds each" if mode == "cli" else "") + "…")

    rows = []
    for i, block in enumerate(blocks, 1):
        if _is_search_results(block):
            found = [p for p in _parse_search_results(block) if p.get("name")]
            print(f"linkedin_people: search-results paste -> {len(found)} people (structured parser)")
            for p in found:
                rows.append(_row(p))
                bits = p.get("company") or "?"
                if p.get("seniority"):
                    bits += f" · {p['seniority']}"
                if p.get("how_known"):
                    bits += f" · {p['how_known']}"
                print(f"  ✓ {p['name']} — {bits}")
            continue

        p = None
        if mode == "api":
            try:
                p = _claude(api_key, "claude-opus-4-8", block)
            except Exception as e:
                print(f"  profile {i}: API parse failed ({e}); using heuristic")
        elif mode == "cli":
            try:
                p = _claude_cli(block)
            except Exception as e:
                print(f"  profile {i}: CLI parse failed ({e}); using heuristic")
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
    print(f"\nlinkedin_people: wrote {len(rows)} person(s) -> {shown}  ({label or 'structured'})")
    print("Next: open the dashboard -> People -> Import CSV, and pick that file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
