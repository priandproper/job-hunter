"""Fetch job postings from public ATS feeds (Greenhouse / Lever / Ashby).

These are the same key-free JSON endpoints the existing scanner/tracker use.
Every fetch is network-guarded: on any failure it returns [] so the pipeline
keeps running from other sources (e.g. the tracker DB) offline.
"""

import hashlib
import json
import re
import urllib.request

_TAG_RE = re.compile(r"<[^>]+>")
UA = "job-hunter/1.0 (personal job search)"


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
        "tools": [], "excerpt": _TAG_RE.sub(" ", excerpt or "")[:1200],
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


_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


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
