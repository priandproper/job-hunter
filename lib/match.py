"""Stage 2 — match a job to the profile and pick the best resume variant.

Two questions per job:
  1. Is this job a fit worth preparing? -> a fit score (0..100-ish, uncapped).
  2. Which of the profile's prebuilt resume variants fits it best?

Scoring is transparent and keyword-based (no ML, stdlib only), mirroring how the
existing scanner/tailor tools already reason about Priyanka's lanes. The score
combines: keyword hits in the job title (heavily weighted — titles are the
strongest signal), keyword hits in the job description excerpt, and how many of
the winning variant's own skill terms appear in the posting.
"""

import re

# Curated vocabulary for the candidate's target lanes. Reused from the scanner's
# tailor vocabulary so scoring stays consistent with the rest of the toolchain.
LANE_TERMS = [
    "product marketing", "go-to-market", "go to market", "gtm", "pmm",
    "positioning", "messaging", "product launch", "launch",
    "competitive intelligence", "competitive analysis", "sales enablement",
    "buyer persona", "segmentation", "value proposition", "thought leadership",
    "demand generation", "demand gen", "growth marketing", "lifecycle",
    "crm", "email marketing", "campaign", "nurture", "pipeline", "funnel",
    "conversion", "retention", "brand", "field marketing", "partner marketing",
    "customer marketing", "marketing analytics", "marketing analyst",
    "business analyst", "growth analyst", "revenue operations", "revops",
    "marketing operations", "marketing ops",
    "analytics", "data-driven", "a/b testing", "experimentation", "kpi", "roi",
    "reporting", "dashboard", "insights", "market research", "attribution",
    "sql", "tableau", "power bi", "looker", "ga4", "excel", "abm",
    "account-based marketing", "salesforce", "hubspot", "eloqua",
]

_TAG_RE = re.compile(r"<[^>]+>")

# Titles carrying any of these are dropped: too senior for a manager-level
# candidate, or a different function entirely (engineering/design). Mirrors the
# seniority/role exclusions the upstream scanner already used. Config can
# override via match.exclude_title_terms.
DEFAULT_EXCLUDE_TITLE_TERMS = [
    "vp", "v.p.", "vice president", "svp", "evp", "head of", "director",
    "chief", "cmo", "president", "principal", "intern", "internship",
    "co-op", "co op", "apprentice", "fellow", "trainee",
    "engineer", "engineering", "software", "data scientist", "designer",
    "architect",
]


def excluded_title(title: str | None, terms) -> bool:
    t = (title or "").lower()
    return any(term in t for term in terms)


# Location filtering (candidate needs US / US-remote roles for H-1B sponsorship).
# A role is dropped only if it names a non-US location AND has no US marker — so
# multi-location roles like "SF, NYC, Toronto, Remote in the US" are kept.
NONUS_LOCATION_TERMS = [
    "india", "london", "uk", "united kingdom", "ireland", "dublin", "emea",
    "apac", "germany", "berlin", "munich", "france", "paris", "spain", "madrid",
    "barcelona", "portugal", "lisbon", "poland", "krakow", "warsaw", "netherlands",
    "amsterdam", "australia", "sydney", "melbourne", "singapore", "tokyo", "japan",
    "korea", "seoul", "toronto", "canada", "vancouver", "ontario", "british columbia",
    "quebec", "montreal", "mexico", "guadalajara", "brazil", "sao paulo", "israel",
    "tel aviv", "dubai", "uae", "latam", "philippines", "manila", "hyderabad",
    "bangalore", "bengaluru", "pune", "delhi", "mumbai", "chennai", "gurgaon", "noida",
]
US_LOCATION_TERMS = [
    "united states", "usa", "u.s.", "u.s.a", ", ma", ", ny", ", ca", ", wa",
    ", il", ", tx", ", co", ", ga", ", dc", ", va", ", nc", ", az", ", or",
    ", fl", ", pa", ", mn", ", oh", ", ut", ", md", ", nj", ", tn",
    "massachusetts", "new york", "nyc", "boston", "cambridge", "san francisco",
    "seattle", "chicago", "austin", "denver", "atlanta", "los angeles", "brooklyn",
    "remote in the us", "remote - us", "us remote", "remote, us", "remote us",
]


def location_ok(location: str | None, nonus=None, us=None) -> bool:
    if not location:
        return True  # unknown location → keep rather than over-filter
    loc = location.lower()
    nonus = nonus if nonus is not None else NONUS_LOCATION_TERMS
    us = us if us is not None else US_LOCATION_TERMS
    if any(t in loc for t in nonus) and not any(t in loc for t in us):
        return False
    return True


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _TAG_RE.sub(" ", text).lower()


def _count_terms(text: str, terms) -> list[str]:
    return [t for t in terms if t in text]


def match_job(job: dict, profile) -> dict:
    """Return a match result: fit score, best variant, and the evidence behind it.

    Fit is normalized to ~0-100 and robust to missing excerpts (many ATS feeds
    omit the description body). A single on-lane job title carries most of the
    signal; the excerpt and variant-skill overlap refine it when present.
      - title keywords:  up to 60 pts (the strongest, most reliable signal)
      - excerpt keywords: up to 25 pts (only when a description body exists)
      - variant overlap:  up to 15 pts (how well the best resume variant fits)
    """
    title = _clean(job.get("title"))
    excerpt = _clean(job.get("excerpt"))
    full = f"{title} {excerpt}"

    title_hits = _count_terms(title, LANE_TERMS)
    excerpt_hits = _count_terms(excerpt, LANE_TERMS)

    title_pts = min(60, len(title_hits) * 30)
    excerpt_pts = min(25, len(set(excerpt_hits) - set(title_hits)) * 3)

    # Pick the best-fitting resume variant.
    best_variant = None
    best_variant_score = -1
    best_variant_hits: list[str] = []
    for v in profile.variants:
        vt = profile.variant_terms(v)
        hits = _count_terms(full, vt)
        vs = len(hits)
        if vs > best_variant_score:
            best_variant_score = vs
            best_variant = v
            best_variant_hits = hits

    variant_pts = min(15, max(best_variant_score, 0) * 3)
    fit = title_pts + excerpt_pts + variant_pts

    return {
        "fit_score": fit,
        "title_keywords": sorted(set(title_hits)),
        "matched_variant": best_variant.get("label") if best_variant else None,
        "variant_obj": best_variant,
        "variant_matched_terms": sorted(set(best_variant_hits))[:20],
    }


def passes_filters(job: dict, match: dict, cfg_match: dict) -> bool:
    if match["fit_score"] < cfg_match.get("min_fit_score", 30):
        return False
    if (job.get("sponsorship") or "").strip() in cfg_match.get("exclude_sponsorship", []):
        return False
    terms = cfg_match.get("exclude_title_terms", DEFAULT_EXCLUDE_TITLE_TERMS)
    if excluded_title(job.get("title"), terms):
        return False
    if not location_ok(job.get("location")):
        return False
    return True
