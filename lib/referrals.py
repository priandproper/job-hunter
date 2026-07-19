"""Stage 3 — referral matching from a LinkedIn Connections export.

LinkedIn blocks automated scraping and it violates their ToS, so we DON'T crawl
it. Instead we use LinkedIn's own official data export:

    LinkedIn -> Settings -> Data Privacy -> Get a copy of your data ->
    "Connections" -> Request archive.  You get a Connections.csv with columns:
    First Name, Last Name, URL, Email Address, Company, Position, Connected On.

Drop that file at `data/connections.csv`. This module matches each connection's
Company against a target company and ranks who is best placed to refer you.
Everything runs locally and offline.
"""

import csv
import re
import urllib.parse
from pathlib import Path

# Corporate suffixes / noise stripped before comparing company names.
_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|"
    r"technologies|technology|labs|group|holdings|plc|gmbh|sa|nv)\b",
    re.IGNORECASE,
)
_NONWORD_RE = re.compile(r"[^a-z0-9 ]+")


def normalize_company(name: str | None) -> str:
    if not name:
        return ""
    s = name.lower()
    s = _NONWORD_RE.sub(" ", s)
    s = _SUFFIX_RE.sub(" ", s)
    return " ".join(s.split())


def _seniority_rank(position: str | None) -> int:
    """Higher = more useful for a referral. Recruiters and people senior enough
    to have pull, but a peer in the same function is great too. Rough heuristic."""
    p = (position or "").lower()
    if any(t in p for t in ("recruit", "talent acquisition", "sourcer", "people ops")):
        return 5  # can route a referral directly into the ATS
    if any(t in p for t in ("vp", "vice president", "chief", "head of", "director")):
        return 4
    if any(t in p for t in ("lead", "principal", "senior manager", "manager")):
        return 3
    if any(t in p for t in ("marketing", "growth", "product", "gtm", "brand", "analyst")):
        return 2  # same lane as the candidate — natural referrer
    return 1


class Connection:
    __slots__ = ("first", "last", "url", "email", "company", "position", "connected_on")

    def __init__(self, row: dict):
        self.first = (row.get("First Name") or "").strip()
        self.last = (row.get("Last Name") or "").strip()
        self.url = (row.get("URL") or "").strip()
        self.email = (row.get("Email Address") or "").strip()
        self.company = (row.get("Company") or "").strip()
        self.position = (row.get("Position") or "").strip()
        self.connected_on = (row.get("Connected On") or "").strip()

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.first, self.last) if p) or "(unknown)"


def load_connections(csv_path: Path) -> list[Connection]:
    if not csv_path.exists():
        return []
    text = csv_path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    # LinkedIn's export prepends a "Notes:" preamble before the real header row.
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("First Name,") or "First Name" in line and "Company" in line:
            start = i
            break
    reader = csv.DictReader(lines[start:])
    return [Connection(row) for row in reader if any(row.values())]


def _alias_set(company: str, aliases_cfg: dict) -> set[str]:
    """Normalized name plus any configured aliases for a target company."""
    norm = normalize_company(company)
    out = {norm}
    for key, aliases in (aliases_cfg or {}).items():
        pool = {normalize_company(key)} | {normalize_company(a) for a in aliases}
        if norm in pool:
            out |= pool
    return {a for a in out if a}


def match_referrers(company: str, connections: list[Connection], cfg: dict) -> list[dict]:
    """Return ranked connections who work at `company`."""
    targets = _alias_set(company, cfg.get("company_aliases", {}))
    matches = []
    for c in connections:
        cn = normalize_company(c.company)
        if not cn:
            continue
        # match if either name contains the other (handles "HubSpot" vs
        # "HubSpot, Inc." and "Infosys" vs "Infosys Finacle").
        hit = any(t and (t == cn or t in cn or cn in t) for t in targets)
        if hit:
            matches.append((c, _seniority_rank(c.position)))

    matches.sort(key=lambda x: x[1], reverse=True)
    max_n = cfg.get("max_contacts_per_job", 8)
    return [
        {
            "name": c.name,
            "position": c.position,
            "company": c.company,
            "url": c.url,
            "email": c.email,
            "connected_on": c.connected_on,
            "kind": "in_network",
            "rank": rank,
        }
        for c, rank in matches[:max_n]
    ]


def linkedin_search_url(company: str, titles: list[str]) -> str:
    """A public LinkedIn people-search URL (she clicks it — no scraping) that
    lists people at `company` with any of the target titles."""
    title_expr = " OR ".join(f'"{t}"' for t in titles) if titles else ""
    keywords = f'"{company}"' + (f" ({title_expr})" if title_expr else "")
    return ("https://www.linkedin.com/search/results/people/?"
            + urllib.parse.urlencode({"keywords": keywords, "origin": "GLOBAL_SEARCH_HEADER"}))


def build_referrals(company: str, connections: list[Connection], cfg: dict,
                    apollo_people: list[dict] | None = None) -> list[dict]:
    """Assemble the full ranked referral list for a company:
    in-network connections first, then Apollo-sourced people (if any). The
    caller adds the LinkedIn search link separately (it's a link, not a person).
    """
    referrers = match_referrers(company, connections, cfg)
    have = {(r["name"] or "").lower() for r in referrers}
    for p in (apollo_people or []):
        if (p.get("name") or "").lower() in have:
            continue
        referrers.append({
            "name": p.get("name", ""),
            "position": p.get("position", ""),
            "company": company,
            "url": p.get("url", ""),
            "email": p.get("email", ""),
            "connected_on": "",
            "kind": "apollo",
            "rank": _seniority_rank(p.get("position")),
        })
    referrers.sort(key=lambda r: (r["kind"] != "in_network", -r["rank"]))
    return referrers[: cfg.get("max_contacts_per_job", 8)]


def draft_message(contact: dict, job: dict, candidate_name: str) -> str:
    """A short, ready-to-send referral ask. Staged, never sent automatically."""
    first = (contact.get("name") or "there").split()[0]
    company = job.get("company", "your company")
    title = job.get("title", "the role")
    url = job.get("url", "")
    return (
        f"Hi {first} — hope you're doing well! I saw that {company} is hiring "
        f"for a {title} role ({url}), and it lines up closely with my background "
        f"in B2B marketing, GTM, and analytics. Since you're at {company}, would "
        f"you be open to referring me or pointing me to the right person on the "
        f"team? Happy to send my resume and a quick blurb to make it easy. "
        f"Thanks so much!\n\n— {candidate_name}"
    )
