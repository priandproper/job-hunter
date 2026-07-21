"""Fetch job postings from public ATS feeds (Greenhouse / Lever / Ashby).

These are the same key-free JSON endpoints the existing scanner/tracker use.
Every fetch is network-guarded: on any failure it returns [] so the pipeline
keeps running from other sources (e.g. the tracker DB) offline.
"""

import datetime as _dt
import hashlib
import html
import json
import re
import urllib.parse
import urllib.request

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
UA = "job-hunter/1.0 (personal job search)"


def clean_jd(text: str) -> str:
    """Decode HTML entities, strip tags, and tidy whitespace. Handles feeds that
    send entity-encoded markup (e.g. JSearch's '&lt;div&gt;'), which a plain tag
    strip would leave behind as literal text."""
    t = html.unescape(text or "")
    t = _TAG_RE.sub(" ", t)
    t = html.unescape(t)                 # second pass for double-encoded entities
    t = _WS_RE.sub(" ", t)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", t).strip()


def _get(url: str, timeout: float = 12.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _job_id(company: str, title: str, location: str) -> str:
    return hashlib.sha1(f"{company}|{title}|{location}".encode()).hexdigest()[:16]


def _norm(company, title, location, url, source, dept="", posted="", excerpt=""):
    return {
        "id": _job_id(company, title, location),
        "company": company, "title": title, "location": location, "url": url,
        "source": source, "department": dept, "posted_at": posted,
        "sponsorship": "Unknown", "sponsorship_note": "", "salary_raw": "",
        # Keep the (near-)full JD so the dashboard can show what to align to. Cap
        # generously to bound repo growth; longer wins on merge (see lib/pool.py).
        "tools": [], "excerpt": clean_jd(excerpt)[:8000],
        "min_exp": None, "status": "new",
    }


def _greenhouse(slug, name):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        out.append(_norm(
            name, j.get("title", ""), (j.get("location") or {}).get("name", ""),
            j.get("absolute_url", ""), "greenhouse",
            ", ".join(d.get("name", "") for d in j.get("departments", []) or []),
            j.get("updated_at", ""), j.get("content", "")))
    return out


def _lever(slug, name):
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories", {}) or {}
        out.append(_norm(
            name, j.get("text", ""), cats.get("location", ""),
            j.get("hostedUrl", ""), "lever", cats.get("team", ""),
            "", j.get("descriptionPlain", "")))
    return out


def _ashby(slug, name):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        out.append(_norm(
            name, j.get("title", ""), j.get("location", ""),
            j.get("jobUrl", ""), "ashby", j.get("department", ""),
            j.get("publishedAt", ""), j.get("descriptionPlain", "") or ""))
    return out


# Amazon runs its own board (amazon.jobs), not Greenhouse/Lever/Ashby. Its public
# search.json takes a free-text query, so we run a few marketing-adjacent queries and
# union the de-duped results; the global lane filter then trims to what fits you.
# US-only via country[]=USA (the location filter refines further).
_AMAZON_QUERIES = (
    "product marketing manager", "marketing manager", "marketing operations",
    "marketing analytics", "revenue operations", "sales operations analyst",
    "go to market strategy", "demand generation",
)


def _amazon_date(raw: str) -> str:
    """amazon.jobs posts dates like 'July 15, 2026' -> ISO, so aging/sort work."""
    try:
        return _dt.datetime.strptime((raw or "").strip(), "%B %d, %Y").date().isoformat()
    except (ValueError, TypeError):
        return ""


def _amazon(slug, name):
    seen, out = set(), []
    for q in _AMAZON_QUERIES:
        url = ("https://www.amazon.jobs/en/search.json?"
               f"base_query={urllib.parse.quote(q)}&country[]=USA&result_limit=100&sort=recent")
        try:
            data = _get(url)
        except Exception:
            continue
        for j in data.get("jobs", []) or []:
            # country[]=USA is loose on amazon.jobs (returns some IND/CHN roles) — enforce US.
            cc = (j.get("country_code") or "").upper()
            if cc and cc not in ("US", "USA"):
                continue
            path = j.get("job_path") or ""
            jid = j.get("id") or path
            if not jid or jid in seen:
                continue
            seen.add(jid)
            jd = "\n\n".join(p for p in (
                j.get("description") or j.get("description_short") or "",
                ("Basic qualifications:\n" + j["basic_qualifications"]) if j.get("basic_qualifications") else "",
                ("Preferred qualifications:\n" + j["preferred_qualifications"]) if j.get("preferred_qualifications") else "",
            ) if p)
            out.append(_norm(
                name, j.get("title", ""),
                j.get("normalized_location", "") or j.get("location", ""),
                ("https://www.amazon.jobs" + path) if path else "",
                "amazon", j.get("business_category", "") or j.get("team", ""),
                _amazon_date(j.get("posted_date", "")), jd))
    return out


_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "amazon": _amazon}


def fetch_company(company: dict) -> list[dict]:
    """Fetch all postings for one company dict {name, ats, slug}. Guarded."""
    fn = _FETCHERS.get((company.get("ats") or "").lower())
    if not fn or not company.get("slug"):
        return []
    try:
        return fn(company["slug"], company["name"])
    except Exception:
        return []


def detect_ats(apply_url: str):
    """Infer (ats, slug) from an application URL — used when discovery finds a
    new employer via a job board and we want to add it to the scan list."""
    if not apply_url:
        return None
    u = apply_url.lower()
    for host, ats in (("boards.greenhouse.io", "greenhouse"),
                      ("greenhouse.io", "greenhouse"),
                      ("jobs.lever.co", "lever"),
                      ("jobs.ashbyhq.com", "ashby")):
        if host in u:
            m = re.search(rf"{re.escape(host)}/([a-z0-9\-]+)", u)
            if m:
                return (ats, m.group(1))
    return None
