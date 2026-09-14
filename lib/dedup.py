"""Phase 11 — duplicate & repost detection.

Boards re-post the same role under new IDs, split one team's headcount into several
near-identical postings, and re-open a role you were already rejected from. This module
classifies the relationship between two postings using the brief's six signals and five
classes, keeping the evidence and a confidence behind every call. Deterministic,
stdlib-only, testable.

Signals: exact source id/url · normalized company+title+location · JD fingerprint ·
JD similarity · same team/product different id · previously-rejected resemblance.
Classes: exact_duplicate · likely_repost · separate_headcount (same template) ·
same_role_family (different product) · unrelated.

Comparisons are scoped to the SAME company (reposts are essentially always same-employer),
so pool-wide annotation stays cheap.
"""

import hashlib
import re

from lib import ranking as _ranking   # lane_of (no heavy deps, no cycle)

CLASSES = ("exact_duplicate", "likely_repost", "separate_headcount",
           "same_role_family", "unrelated")

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")
# Noise stripped from titles before comparison: req numbers, seniority/level words,
# and common parenthetical qualifiers — so "Sr. PMM (Remote) #123" ~ "PMM".
_TITLE_NOISE = re.compile(r"\b(senior|sr|jr|junior|staff|lead|principal|ii|iii|iv|"
                          r"i|1|2|3|remote|hybrid|onsite|contract|req|r?\d{3,})\b")


def _norm(s):
    return _WS.sub(" ", _PUNCT.sub(" ", (s or "").lower())).strip()


def _norm_title(t):
    return _WS.sub(" ", _TITLE_NOISE.sub(" ", _norm(t))).strip()


def _norm_loc(loc):
    l = _norm(loc)
    return "remote" if "remote" in l else l


def _tok(text):
    return {w for w in _norm(text).split() if len(w) > 2}


def _jaccard(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0


def fingerprint(job):
    """Stable hash of the normalized JD body — identical text across two postings is a
    strong repost signal even when ids/urls differ. Empty JD -> ''."""
    jd = _norm(job.get("excerpt"))
    return hashlib.sha1(jd[:4000].encode()).hexdigest()[:16] if jd else ""


def jd_similarity(a, b):
    ta, tb = _tok(a.get("excerpt")), _tok(b.get("excerpt"))
    if not ta or not tb:
        return 0.0
    return round(_jaccard(ta, tb), 3)


def classify(a, b):
    """Classify posting A relative to B. Returns {classification, confidence, evidence}."""
    ev = []
    if a.get("id") and a.get("id") == b.get("id"):
        return {"classification": "exact_duplicate", "confidence": 1.0, "evidence": ["same id"]}
    if a.get("url") and a.get("url") == b.get("url"):
        return {"classification": "exact_duplicate", "confidence": 0.98, "evidence": ["same source URL"]}

    if _norm(a.get("company")) != _norm(b.get("company")):
        return {"classification": "unrelated", "confidence": 0.0, "evidence": ["different company"]}
    ev.append("same company")

    same_title = _norm_title(a.get("title")) == _norm_title(b.get("title"))
    same_loc = _norm_loc(a.get("location")) == _norm_loc(b.get("location"))
    fp = fingerprint(a) and fingerprint(a) == fingerprint(b)
    sim = jd_similarity(a, b)
    same_team = _norm(a.get("department")) and _norm(a.get("department")) == _norm(b.get("department"))
    same_lane = _ranking.lane_of(a.get("title")) == _ranking.lane_of(b.get("title"))

    if same_title and same_loc:
        return {"classification": "exact_duplicate", "confidence": 0.95,
                "evidence": ev + ["same normalized title + location"]}
    if fp:
        return {"classification": "likely_repost", "confidence": 0.9,
                "evidence": ev + ["identical JD fingerprint, different posting"]}
    if same_title:
        return {"classification": "likely_repost", "confidence": 0.85,
                "evidence": ev + ["same normalized title, different location/id"]}
    if sim >= 0.85:
        return {"classification": "likely_repost", "confidence": 0.75,
                "evidence": ev + [f"near-identical JD (similarity {sim})"]}
    if sim >= 0.6 or (same_team and sim >= 0.45):
        return {"classification": "separate_headcount", "confidence": 0.6,
                "evidence": ev + ([f"same team ({a.get('department')})"] if same_team else [])
                + [f"shared JD template (similarity {sim})"]}
    if same_lane and sim >= 0.3:
        return {"classification": "same_role_family", "confidence": 0.5,
                "evidence": ev + [f"same lane ({_ranking.lane_of(a.get('title'))}), different product (similarity {sim})"]}
    if same_lane:
        return {"classification": "same_role_family", "confidence": 0.4,
                "evidence": ev + [f"same lane ({_ranking.lane_of(a.get('title'))})"]}
    return {"classification": "unrelated", "confidence": 0.2, "evidence": ev + ["same company, unrelated role"]}


_DUP_CLASSES = {"exact_duplicate", "likely_repost", "separate_headcount"}


def annotate_reposts(jobs, min_confidence=0.6):
    """Cluster the pool by company and annotate each job that duplicates/reposts an
    EARLIER (canonical) posting with a `duplicate` object. The earliest posting in a
    cluster stays canonical (unannotated). Returns the number annotated.

    A job's canonical is the first same-company job (by _first_seen, else input order)
    it matches at >= min_confidence in a duplicate/repost/headcount class."""
    by_company = {}
    for j in jobs:
        by_company.setdefault(_norm(j.get("company")), []).append(j)
    n = 0
    for co, group in by_company.items():
        if not co or len(group) < 2:
            continue
        group.sort(key=lambda j: (j.get("_first_seen") or "", j.get("id") or ""))
        for i in range(1, len(group)):
            best = None
            for k in range(i):
                r = classify(group[i], group[k])
                if r["classification"] in _DUP_CLASSES and r["confidence"] >= min_confidence:
                    if best is None or r["confidence"] > best[0]["confidence"]:
                        best = (r, group[k])
            if best:
                r, canon = best
                group[i]["duplicate"] = {"of": canon.get("id"), "classification": r["classification"],
                                         "confidence": r["confidence"], "evidence": r["evidence"]}
                n += 1
    return n


def resembles_rejected(job, rejected_jobs, min_confidence=0.5):
    """Signal 6: does a new opening resemble a role the candidate was already rejected
    from? Returns the best match {of, classification, confidence, evidence} or None."""
    best = None
    for rj in rejected_jobs or []:
        if rj.get("id") == job.get("id"):
            continue
        r = classify(job, rj)
        if r["classification"] != "unrelated" and r["confidence"] >= min_confidence:
            if best is None or r["confidence"] > best["confidence"]:
                best = {"of": rj.get("id"), "classification": r["classification"],
                        "confidence": r["confidence"], "evidence": r["evidence"]}
    return best
