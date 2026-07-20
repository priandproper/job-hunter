"""Fold your dashboard-published persona into the match config.

The dashboard publishes `persona.json` to a PUBLIC Gist; the runner is told its id
via the `PERSONA_GIST_ID` Actions variable. We fetch it (no auth needed for a public
gist) and merge it into cfg['match'] so the cloud scrape keeps only jobs that fit you:

  roles + skills  -> extra_lane_terms   (raise relevance / fit for on-persona jobs)
  exclude         -> exclude_title_terms (hard-drop titles you don't want)
  min_fit         -> min_fit_score        (tighten the keep threshold)

Fully guarded: no id / no network / bad json -> {} and the pipeline runs unchanged.
"""

import json
import os
import urllib.request

from . import match as match_mod

UA = "job-hunter/1.0 (personal job search)"


def _fetch_gist(gist_id: str) -> dict:
    url = f"https://api.github.com/gists/{gist_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    files = data.get("files", {}) or {}
    f = files.get("persona.json") or next(iter(files.values()), None)
    if not f:
        return {}
    return json.loads(f.get("content") or "{}")


def load_persona(env: str = "PERSONA_GIST_ID") -> dict:
    gid = (os.environ.get(env) or "").strip()
    if not gid:
        return {}
    try:
        return _fetch_gist(gid)
    except Exception:
        return {}


def _lower_list(v):
    return [s.strip().lower() for s in (v or []) if isinstance(s, str) and s.strip()]


def apply(persona: dict, cfg_match: dict) -> dict:
    """Return a copy of cfg_match with the persona folded in (no-op if persona empty)."""
    if not persona:
        return cfg_match
    m = dict(cfg_match)
    extra = list(m.get("extra_lane_terms", []) or [])
    for t in _lower_list(persona.get("roles")) + _lower_list(persona.get("skills")):
        if t not in extra:
            extra.append(t)
    m["extra_lane_terms"] = extra

    base = m.get("exclude_title_terms") or list(match_mod.DEFAULT_EXCLUDE_TITLE_TERMS)
    for t in _lower_list(persona.get("exclude")):
        if t not in base:
            base.append(t)
    m["exclude_title_terms"] = base

    try:
        mf = int(persona.get("min_fit") or 0)
        if mf > 0:
            m["min_fit_score"] = mf
    except (TypeError, ValueError):
        pass
    return m
