"""Optional out-of-network referral sourcing via the Apollo.io API.

Apollo is a sales-intelligence tool the candidate already uses; its API is a
legitimate way to find people at a company by title (unlike scraping LinkedIn).
Fully optional: with no APOLLO_API_KEY it no-ops and referral sourcing falls
back to the LinkedIn search link. Network/errors are guarded.
"""

import json
import urllib.request
from pathlib import Path

from . import secrets as secrets_mod

_ENDPOINT = "https://api.apollo.io/v1/mixed_people/search"


def _api_key(config: dict, repo_root: Path) -> str | None:
    ap = config.get("apollo", {})
    if not ap.get("enabled"):
        return None
    secrets_file = (repo_root / ap.get("secrets_file", "")).resolve() \
        if ap.get("secrets_file") else None
    return secrets_mod.get_key(ap.get("api_key_env", ""), secrets_file)


def find_people(company: str, titles: list[str], config: dict, repo_root: Path) -> list[dict]:
    """Return [{name, position, url, email}] for people at `company`. Guarded."""
    key = _api_key(config, repo_root)
    if not key:
        return []
    limit = config.get("apollo", {}).get("per_job_limit", 5)
    body = json.dumps({
        "api_key": key,
        "q_organization_domains": "",
        "organization_names": [company],
        "person_titles": titles,
        "page": 1,
        "per_page": limit,
    }).encode()
    req = urllib.request.Request(
        _ENDPOINT, data=body,
        headers={"Content-Type": "application/json",
                 "Cache-Control": "no-cache", "User-Agent": "job-hunter/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return []
    out = []
    for p in data.get("people", [])[:limit]:
        name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
        out.append({
            "name": name or p.get("name", ""),
            "position": p.get("title", ""),
            "url": p.get("linkedin_url", "") or "",
            "email": p.get("email", "") or "",
        })
    return out
