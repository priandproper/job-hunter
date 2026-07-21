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


def _post(url: str, payload: dict, timeout: float = 15.0):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# Search terms for the two target lanes, reused by the free-text sources
# (amazon/netflix/workday). Lane 1: product marketing + adjacent. Lane 2: analyst
# (marketing / business / sales / sales-ops / data).
_MKT_QUERIES = (
    "product marketing", "marketing manager", "marketing operations",
    "marketing analytics", "revenue operations", "demand generation",
    "go to market", "sales operations",
    "marketing analyst", "business analyst", "sales analyst",
    "sales operations analyst", "data analyst",
)

_US_ABBR = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
            "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
            "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
            "VA","WA","WV","WI","WY","DC"}
_US_FULL = {"alabama","alaska","arizona","arkansas","california","colorado","connecticut",
            "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
            "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
            "minnesota","mississippi","missouri","montana","nebraska","nevada","new hampshire",
            "new jersey","new mexico","new york","north carolina","north dakota","ohio",
            "oklahoma","oregon","pennsylvania","rhode island","south carolina","south dakota",
            "tennessee","texas","utah","vermont","virginia","washington","west virginia",
            "wisconsin","wyoming","district of columbia"}


def _looks_us(loc: str) -> bool:
    """Best-effort: does a location string denote a US role? (careers APIs' own
    country filters are unreliable, so we also screen here.)"""
    t = (loc or "").strip()
    if not t:
        return False
    tl = t.lower()
    if any(h in tl for h in ("united states", "usa", "u.s.", "us-remote",
                             "us remote", "remote - us", "remote-us", "remote, us")):
        return True
    for p in re.split(r"[,/|]", t):
        p = p.strip()
        if p.upper() in _US_ABBR or p.lower() in _US_FULL:
            return True
    return False


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
def _amazon_date(raw: str) -> str:
    """amazon.jobs posts dates like 'July 15, 2026' -> ISO, so aging/sort work."""
    try:
        return _dt.datetime.strptime((raw or "").strip(), "%B %d, %Y").date().isoformat()
    except (ValueError, TypeError):
        return ""


def _amazon(slug, name):
    seen, out = set(), []
    for q in _MKT_QUERIES:
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


def _netflix(company):
    """Netflix runs on Eightfold (explore.jobs.netflix.net); the list response carries
    the full JD, so no per-job fetch is needed. US-screened."""
    name = company.get("name", "Netflix")
    seen, out = set(), []
    for q in _MKT_QUERIES:
        url = ("https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com"
               f"&query={urllib.parse.quote(q)}&start=0&num=100&sort_by=relevance")
        try:
            data = _get(url)
        except Exception:
            continue
        for j in data.get("positions", []) or []:
            jid = j.get("id") or j.get("display_job_id")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            locs = j.get("locations") if isinstance(j.get("locations"), list) else []
            loc = j.get("location") or (locs[0] if locs else "")
            if not _looks_us(loc) and not any(_looks_us(x) for x in locs):
                continue
            out.append(_norm(
                name, j.get("name", ""), loc, j.get("canonicalPositionUrl", ""),
                "netflix", j.get("department", "") or j.get("business_unit", ""),
                "", j.get("job_description", "")))
    return out


# Workday's global reference id for "United States of America" (stable across tenants).
_WD_USA = "bc33aa3152ec42d4995f4791a106ed09"


def _workday(company):
    """Generic Workday (myworkdayjobs) source. Needs wd_host, wd_tenant, wd_site on the
    company. Paginates a few marketing queries (list only — fast, title-driven match).
    For US-only it first tries Workday's locationCountry facet; tenants that reject that
    facet fall back to screening locationsText."""
    host, tenant, site = company.get("wd_host"), company.get("wd_tenant"), company.get("wd_site")
    name = company.get("name", "")
    if not (host and tenant and site):
        return []
    base = f"https://{host}/wday/cxs/{tenant}/{site}"

    # Probe once: does this tenant accept the US country facet? If so, trust it (its
    # locationsText is often just "3 Locations"); otherwise screen locations ourselves.
    facets, trust_us = {}, False
    try:
        _post(f"{base}/jobs", {"appliedFacets": {"locationCountry": [_WD_USA]},
                               "limit": 1, "offset": 0, "searchText": "marketing"})
        facets, trust_us = {"locationCountry": [_WD_USA]}, True
    except Exception:
        pass

    seen, out = set(), []
    for q in _MKT_QUERIES:
        for page in range(4):                      # up to 4 pages (80 hits) per query
            try:
                data = _post(f"{base}/jobs", {"appliedFacets": facets, "limit": 20,
                                              "offset": page * 20, "searchText": q})
            except Exception:
                break
            posts = data.get("jobPostings", []) or []
            if not posts:
                break
            for jp in posts:
                path = jp.get("externalPath") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                loc = jp.get("locationsText", "")
                if not trust_us and not _looks_us(loc):
                    continue
                out.append(_norm(
                    name, jp.get("title", ""), loc or "United States",
                    f"https://{host}/en-US/{site}{path}", "workday",
                    "", jp.get("postedOn", ""), ""))    # list has no JD body
            if page * 20 + len(posts) >= (data.get("total") or 0):
                break
    return out


_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "amazon": _amazon}
# Sources that need more than {slug, name} — they receive the whole company dict.
_DICT_FETCHERS = {"netflix": _netflix, "workday": _workday}


def fetch_company(company: dict) -> list[dict]:
    """Fetch all postings for one company dict. Guarded — any failure returns []."""
    ats = (company.get("ats") or "").lower()
    try:
        if ats in _DICT_FETCHERS:
            return _DICT_FETCHERS[ats](company)
        fn = _FETCHERS.get(ats)
        if not fn or not company.get("slug"):
            return []
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
