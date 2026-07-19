"""ATS keyword-gap analysis — the data-driven "what am I missing" engine.

The honest, high-signal answer to "what do stronger candidates have that I
don't" comes from the job descriptions themselves: they list the skills/tools a
role wants. For each job we:

  1. Extract the in-vocabulary keywords the JD actually asks for.
  2. For each of the candidate's resume variants, see which of those the resume
     already contains vs is missing.
  3. Score each variant (present / requested) and pick the best-fitting one.

The dashboard then shows, per job: the ATS score of the best variant, which
variant won, and the concrete MISSING keywords. Aggregated across all jobs, the
most-frequently-missing keywords are the highest-leverage skills to add.

Needs the JD body — which the direct ATS fetch (lib/ats.py) provides in full
(Greenhouse content / Lever & Ashby descriptionPlain). Jobs with no description
degrade to a title-only analysis.
"""

import re

# Skills / tools that matter for the candidate's lanes (marketing, GTM, product
# marketing, marketing analytics, revops). Matching is case-insensitive
# substring on cleaned JD text. Grouped only for readability.
VOCAB = [
    # product marketing / GTM
    "product marketing", "go-to-market", "gtm", "positioning", "messaging",
    "product launch", "launch", "competitive intelligence", "competitive analysis",
    "sales enablement", "buyer persona", "personas", "segmentation",
    "value proposition", "narrative", "storytelling", "thought leadership",
    "product adoption", "adoption", "battlecards", "win/loss", "market research",
    # demand / growth / lifecycle / brand
    "demand generation", "demand gen", "growth marketing", "lifecycle marketing",
    "lifecycle", "customer marketing", "field marketing", "partner marketing",
    "brand", "brand marketing", "content marketing", "email marketing",
    "campaign", "nurture", "abm", "account-based marketing",
    # analytics / ops / tools
    "analytics", "data-driven", "a/b testing", "experimentation", "attribution",
    "funnel", "conversion", "retention", "pipeline", "forecasting",
    "kpi", "roi", "reporting", "dashboard", "insights", "segmentation",
    "sql", "tableau", "power bi", "looker", "ga4", "google analytics",
    "excel", "python", "r ", "salesforce", "hubspot", "marketo", "eloqua",
    "pardot", "apollo", "clay", "sales navigator", "crm", "cms",
    # process / collaboration
    "cross-functional", "stakeholder management", "project management",
    "go to market", "revenue operations", "revops", "marketing operations",
    "product-led growth", "plg", "saas", "b2b", "enterprise",
]

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text):
    return _TAG_RE.sub(" ", text or "").lower()


def jd_keywords(job) -> list[str]:
    """In-vocabulary terms the job (title + description) asks for."""
    text = _clean(f"{job.get('title','')} {job.get('excerpt','')}")
    return sorted({t.strip() for t in VOCAB if t in text})


def variant_text(variant, profile) -> str:
    parts = [variant.get("summary", ""), variant.get("headline", "")]
    for g in variant.get("skills", []) or []:
        parts.extend(g.get("items", []) or [])
    for e in variant.get("experience", []) or []:
        # Experience titles/company appear on the rendered resume and carry
        # strong keyword signal (e.g. "Lead Consultant, Product Marketing").
        parts.append(e.get("title", ""))
        parts.append(e.get("company", ""))
        parts.extend(e.get("highlights", []) or [])
    return _clean(" ".join(parts))


def score_variant(requested: list[str], vtext: str):
    if not requested:
        return {"score": 0, "present": [], "missing": []}
    present = [k for k in requested if k in vtext]
    missing = [k for k in requested if k not in vtext]
    return {
        "score": round(100 * len(present) / len(requested)),
        "present": present,
        "missing": missing,
    }


def analyze(job, profile) -> dict:
    """Full gap analysis for one job across all resume variants."""
    requested = jd_keywords(job)
    per_variant = []
    for v in profile.variants:
        res = score_variant(requested, variant_text(v, profile))
        per_variant.append({
            "label": v.get("label", ""),
            "score": res["score"],
            "missing": res["missing"],
        })
    per_variant.sort(key=lambda x: x["score"], reverse=True)
    best = per_variant[0] if per_variant else {"label": None, "score": 0, "missing": []}
    return {
        "requested_keywords": requested,
        "ats_score": best["score"],
        "best_variant": best["label"],
        "missing_keywords": best["missing"],
        "per_variant": per_variant,
        "jd_available": bool((job.get("excerpt") or "").strip()),
    }
