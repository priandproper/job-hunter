"""Fold user-added companies (published from the dashboard to a Gist) into the scan.

The dashboard lets you maintain a company watchlist and publish it to a PUBLIC Gist
as `companies.json` = {"companies": [{"name", "url"}, ...]}. The runner is told the
gist id via the COMPANIES_GIST_ID Actions variable, fetches it (no auth needed for a
public gist), resolves each company's ATS from its careers URL, and merges any new
ones into data/companies.json so they become scannable next cycle.

Mirrors lib/persona.py. Fully guarded: no id / no network / bad json -> no-op and the
curated + auto-discovered list stands unchanged.
"""

import json
import os
import urllib.request

from . import ats as ats_mod
from . import discovery as disc_mod

UA = "job-hunter/1.0 (personal job search)"


def _fetch_gist(gist_id: str) -> list[dict]:
    url = f"https://api.github.com/gists/{gist_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    files = data.get("files", {}) or {}
    f = files.get("companies.json") or next(iter(files.values()), None)
    if not f:
        return []
    parsed = json.loads(f.get("content") or "{}")
    if isinstance(parsed, dict):
        return parsed.get("companies", []) or []
    return parsed or []


def load_gist_companies(env: str = "COMPANIES_GIST_ID") -> list[dict]:
    gid = (os.environ.get(env) or "").strip()
    if not gid:
        return []
    try:
        return _fetch_gist(gid)
    except Exception:
        return []


def _norm_name(name: str) -> str:
    return " ".join((name or "").lower().split())


def careers_url(c: dict) -> str:
    """Best-effort public careers URL for a company from its ATS + slug."""
    slug = c.get("slug")
    if not slug:
        return c.get("careers_url", "") or ""
    return {
        "greenhouse": f"https://boards.greenhouse.io/{slug}",
        "lever": f"https://jobs.lever.co/{slug}",
        "ashby": f"https://jobs.ashbyhq.com/{slug}",
    }.get(c.get("ats", ""), c.get("careers_url", "") or "")


def merge_into_list(companies: list[dict], gist_entries: list[dict]) -> tuple[list[dict], int]:
    """Merge gist company entries into the companies list. Returns (companies, added).

    Each gist entry is {name, url}. The ATS is resolved from the URL; entries without
    a resolvable ATS are still recorded (active=False) so they appear in the watchlist,
    just aren't scanned until a scannable careers URL is provided.
    """
    known = {_norm_name(c.get("name", "")) for c in companies}
    added = 0
    for e in gist_entries:
        name = (e.get("name") or "").strip()
        if not name or _norm_name(name) in known:
            continue
        url = (e.get("url") or e.get("careers_url") or "").strip()
        detected = ats_mod.detect_ats(url)
        companies.append({
            "name": name,
            "ats": detected[0] if detected else "",
            "slug": detected[1] if detected else "",
            "h1b": None,
            "h1b_note": "user-added via dashboard gist",
            "hq": "",
            "careers_url": url,
            "source": "gist:user",
            "active": bool(detected),
        })
        known.add(_norm_name(name))
        added += 1
    return companies, added


def merge(cfg: dict, repo_root, env: str = "COMPANIES_GIST_ID") -> dict:
    """Fetch the gist and merge new companies into data/companies.json. No-op w/o id."""
    entries = load_gist_companies(env)
    if not entries:
        return {"added": 0, "listed": 0}
    comp_path = (repo_root / cfg["companies_file"]).resolve()
    companies = disc_mod.load_companies(comp_path)
    companies, added = merge_into_list(companies, entries)
    if added:
        disc_mod.save_companies(comp_path, companies)
    return {"added": added, "listed": len(entries)}
