"""Load the candidate's resume, published from the dashboard to a public Gist.

The dashboard's "Publish resume to Gist" writes `resume.json` (the resume core with
contact email/phone stripped) to a public Gist and the runner is told its id via the
RESUME_GIST_ID Actions variable. This lets the cloud worker do resume-aware analysis
(profile ROI) without the resume living in the repo.

Mirrors lib/persona.py. Fully guarded: no id / no net / bad json -> {}.
"""

import json
import os
import urllib.request

UA = "job-hunter/1.0 (personal job search)"


def _fetch_gist(gist_id: str) -> dict:
    url = f"https://api.github.com/gists/{gist_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    files = data.get("files", {}) or {}
    f = files.get("resume.json") or next(iter(files.values()), None)
    if not f:
        return {}
    return json.loads(f.get("content") or "{}")


def load_resume(env: str = "RESUME_GIST_ID") -> dict:
    gid = (os.environ.get(env) or "").strip()
    if not gid:
        return {}
    try:
        return _fetch_gist(gid)
    except Exception:
        return {}
