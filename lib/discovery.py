"""Grow the company list — the "ever-expanding, H-1B-sponsoring" requirement.

LinkedIn scraping is not used (it's ToS-blocked and gets accounts flagged). We
grow the list through legitimate, key-free/keyed sources instead:

  1. Job boards (JSearch API — the scanner already uses it) surface employers
     actively hiring for the target roles. From each posting's apply URL we can
     often infer the company's ATS + slug (`ats.detect_ats`), which makes the
     company directly scannable next cycle.
  2. H-1B sponsorship is verified against PUBLIC DOL LCA disclosure data, which
     h1bdata.info republishes per employer. A company only becomes 'active'
     (scanned + shown) if it has a recent sponsorship history — or stays
     'pending' until verified.

Everything network-touching is guarded: with no key / no network, discovery is a
no-op and the existing curated list stands. Companies persist in companies.json.
"""

import json
import urllib.parse
import urllib.request
from pathlib import Path

from . import ats
from . import secrets as secrets_mod

UA = "job-hunter/1.0 (personal job search)"


# ---- company list persistence ------------------------------------------------

def load_companies(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("companies", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_companies(path: Path, companies: list[dict]):
    path.write_text(json.dumps({"companies": companies}, indent=2))


def _norm_name(name: str) -> str:
    return " ".join((name or "").lower().split())


# ---- H-1B verification (public DOL data via h1bdata.info) --------------------

def verify_h1b(company_name: str) -> tuple[bool | None, str]:
    """Return (sponsors, note). None = couldn't verify (network/no data)."""
    try:
        q = urllib.parse.quote(company_name)
        url = f"https://h1bdata.info/index.php?em={q}&year=2024"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", "replace")
        # h1bdata renders a data row per certified LCA; a populated table = sponsor.
        rows = html.count("<tr>")
        if rows > 1:
            return True, f"public LCA rows found (h1bdata, 2024)"
        return False, "no 2024 LCA rows on h1bdata"
    except Exception:
        return None, "h1b lookup unavailable"


# ---- job-board discovery (JSearch) ------------------------------------------

def _jsearch(query: str, api_key: str, page: int = 1) -> list[dict]:
    params = urllib.parse.urlencode({"query": query, "page": page, "num_pages": 1})
    url = f"https://jsearch.p.rapidapi.com/search?{params}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return data.get("data", []) or []


def _norm_jsearch(posting: dict) -> dict:
    """Turn a JSearch posting into the pipeline's normalized job dict."""
    city = posting.get("job_city") or ""
    state = posting.get("job_state") or ""
    country = posting.get("job_country") or ""
    loc = ", ".join(x for x in (city, state, country) if x)
    url = posting.get("job_apply_link") or posting.get("job_google_link") or ""
    return ats._norm(
        posting.get("employer_name", ""), posting.get("job_title", ""), loc,
        url, "jsearch", "", posting.get("job_posted_at_datetime_utc", "") or "",
        posting.get("job_description", "") or "")


def fetch_postings(config: dict, repo_root: Path, log=print) -> list[dict]:
    """Ingest JSearch job postings directly as jobs (paginated). No key/net -> []."""
    disc = config.get("discovery", {})
    if not disc.get("enabled"):
        return []
    secrets_file = (repo_root / disc.get("secrets_file", "")).resolve() \
        if disc.get("secrets_file") else None
    api_key = secrets_mod.get_key(disc.get("jsearch_api_key_env", ""), secrets_file)
    if not api_key:
        return []
    pages = max(1, int(disc.get("results_pages", 1)))
    queries = disc.get("queries", [])
    out, seen = [], set()
    for query in queries:
        for page in range(1, pages + 1):
            try:
                postings = _jsearch(query, api_key, page)
            except Exception as e:
                log(f"        postings  — jsearch '{query[:28]}...' p{page} failed: {e}")
                break  # stop paging this query on error
            if not postings:
                break
            for posting in postings:
                j = _norm_jsearch(posting)
                if j["company"] and j["title"] and j["id"] not in seen:
                    seen.add(j["id"])
                    out.append(j)
    if out:
        log(f"        postings  — +{len(out)} job(s) from JSearch ({len(queries)} queries x {pages}p)")
    return out


def discover_companies(config: dict, repo_root: Path, log=print) -> dict:
    """Find new employers via job boards, verify H-1B, merge into companies.json.

    Returns a summary dict. Safe to call on a cadence; no-ops without a key/net.
    """
    disc = config.get("discovery", {})
    comp_path = (repo_root / config["companies_file"]).resolve()
    companies = load_companies(comp_path)
    known = {_norm_name(c["name"]) for c in companies}

    summary = {"checked": 0, "added": 0, "verified": 0}
    if not disc.get("enabled"):
        return summary

    secrets_file = (repo_root / disc.get("secrets_file", "")).resolve() \
        if disc.get("secrets_file") else None
    api_key = secrets_mod.get_key(disc.get("jsearch_api_key_env", ""), secrets_file)
    if not api_key:
        log("        discovery — no JSearch key; keeping curated company list")
        return summary

    seen_employers: dict[str, str] = {}  # name -> apply url
    for query in disc.get("queries", []):
        try:
            for posting in _jsearch(query, api_key, 1):
                name = posting.get("employer_name")
                if name and _norm_name(name) not in known:
                    seen_employers.setdefault(
                        name, posting.get("job_apply_link") or posting.get("job_google_link", ""))
        except Exception as e:
            log(f"        discovery — jsearch '{query[:30]}...' failed: {e}")

    for name, apply_url in seen_employers.items():
        summary["checked"] += 1
        sponsors, note = verify_h1b(name)
        if sponsors is False:
            continue  # confirmed non-sponsor — skip
        detected = ats.detect_ats(apply_url)
        company = {
            "name": name,
            "ats": detected[0] if detected else "",
            "slug": detected[1] if detected else "",
            "h1b": sponsors,  # True or None (pending)
            "h1b_note": note,
            "hq": "",
            "source": "discovery:jsearch",
            "active": bool(detected),  # scannable only if we resolved an ATS
        }
        companies.append(company)
        known.add(_norm_name(name))
        summary["added"] += 1
        if sponsors:
            summary["verified"] += 1

    save_companies(comp_path, companies)
    return summary


def scannable_companies(config: dict, repo_root: Path) -> list[dict]:
    """Companies with a resolvable ATS feed and not a confirmed non-sponsor."""
    comp_path = (repo_root / config["companies_file"]).resolve()
    out = []
    for c in load_companies(comp_path):
        if c.get("active") and c.get("ats") and c.get("slug") and c.get("h1b") is not False:
            out.append(c)
    return out
