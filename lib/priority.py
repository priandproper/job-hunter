"""Phase 6 — transparent job priority score with hard gates.

Replaces the opaque keyword `fit_score` as the headline ranking signal with a
weighted, fully explainable score. Every component is deterministic (stdlib-only)
and reports its own evidence and confidence, so the UI can show WHY a role landed
where it did — never a single number presented as scientific precision.

Weighted dimensions (max 100):
    basic-qualification match      25
    direct experience alignment    20
    immigration viability          20
    product / customer alignment   15
    referral / internal access     10
    posting recency                 5
    location compatibility          5

Bands: A 75-100, B 60-74, C 45-59, Reject <45 — and ALWAYS Reject on a hard stop
(immigration red / citizenship / clearance), regardless of the number.
"""

import datetime as _dt
import re

from lib import jobspec as _jobspec

_WORD = re.compile(r"[a-z0-9][a-z0-9\+/\.#-]*")
_STOP = set("the a an and or of to in for on with at by from as is are be this that you "
            "your we our their they it its into across over per using use used including "
            "etc via which who whom whose than then years year experience degree bachelor "
            "master plus strong ability able work working role team".split())

# Default domain vocabulary for product/customer alignment (the candidate's world:
# B2B SaaS marketing, fintech/banking, analytics). Worker can extend via ctx["domains"].
DEFAULT_DOMAINS = {"b2b", "saas", "enterprise", "marketing", "product marketing", "gtm",
                   "go-to-market", "demand generation", "growth", "analytics", "fintech",
                   "banking", "payments", "financial services", "data", "platform", "ai"}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2}


def _spec(job: dict) -> dict:
    return job.get("spec") or _jobspec.structure(job, include_full=False, max_bullets=12)


def _band(total: int, hard_stop: bool) -> str:
    if hard_stop:
        return "Reject"
    if total >= 75:
        return "A"
    if total >= 60:
        return "B"
    if total >= 45:
        return "C"
    return "Reject"


# --- components (each returns points, confidence, evidence) --------------------
def _c_basic_quals(job, ctx):
    spec = _spec(job)
    quals = spec.get("basic_qualifications", []) or []
    have = ctx.get("skills", set()) | ctx.get("experience_terms", set())
    if not quals:
        return 15.0, "low", "no basic-qualifications parsed from the JD — neutral estimate"
    covered = 0
    gaps = []
    for q in quals:
        qt = _tokens(q)
        ratio = len(qt & have) / len(qt) if qt else 0.0
        if ratio >= 0.30:
            covered += 1
        else:
            gaps.append(q[:60])
    frac = covered / len(quals)
    points = round(25 * frac, 1)
    ev = f"{covered}/{len(quals)} basic quals evidenced"
    if gaps:
        ev += "; gaps: " + "; ".join(gaps[:2])
    # Experience sweet spot (e.g. 2–4 yrs): a role asking MORE than the sweet-spot high
    # is down-ranked ~3 pts/year over, so lower-experience roles float to the top without
    # being hidden. Roles within/below the sweet spot are unpenalized.
    yrs = spec.get("required_years")
    low, high = ctx.get("sweet_spot", (0, 99))
    if yrs is not None and yrs > high:
        over = yrs - high
        penalty = min(points, round(over * 3.0, 1))
        points = round(points - penalty, 1)
        ev += f"; asks {yrs}y — {over}y over your {low}-{high}y sweet spot (−{penalty})"
    elif yrs is not None and low <= yrs <= high:
        ev += f"; asks {yrs}y (in your {low}-{high}y sweet spot)"
    return points, ("high" if len(quals) >= 3 else "medium"), ev


def _c_direct_experience(job, ctx):
    spec = _spec(job)
    jd_terms = _tokens(" ".join([job.get("title", "")] + spec.get("responsibilities", [])
                                + spec.get("basic_qualifications", [])))
    skills = ctx.get("skills", set()) | ctx.get("experience_terms", set())
    # of the role's own skill/responsibility vocabulary, how much does the candidate's
    # ACTUAL experience cover (evidence-based, not raw title keyword overlap)?
    key = {t for t in jd_terms if t in (ctx.get("skill_vocab", set()) or skills)}
    if not key:
        return 12.0, "low", "no matchable skill vocabulary in the JD — neutral estimate"
    overlap = len(key & skills) / len(key)
    matched = sorted(key & skills)[:5]
    return round(20 * overlap, 1), "medium", (f"{len(key & skills)}/{len(key)} role skills in your "
                                              f"experience" + (f": {', '.join(matched)}" if matched else ""))


def _c_immigration(job, immigration):
    imm = immigration or job.get("immigration") or {}
    risk = imm.get("risk", "yellow")
    if risk == "green":
        return 20.0, "medium", "posting indicates sponsorship (confirm in writing)"
    if risk == "red":
        return 0.0, "high", f"HARD STOP: {imm.get('hard_stop_reason','sponsorship prohibited')}"
    return 10.0, "low", "sponsorship not stated — verify with recruiter before hiring-manager stage"


def _c_product_customer(job, ctx):
    spec = _spec(job)
    jd = (job.get("excerpt") or "") + " " + " ".join(spec.get("responsibilities", []))
    jd_l = jd.lower()
    domains = ctx.get("domains") or DEFAULT_DOMAINS
    matched = sorted({d for d in domains if d in jd_l})
    if not jd.strip():
        return 9.0, "low", "no JD body — neutral estimate"
    ratio = min(1.0, len(matched) / 3.0)
    return round(15 * ratio, 1), ("medium" if matched else "low"), (
        ("domain overlap: " + ", ".join(matched[:5])) if matched else "little product/customer overlap")


def _c_referral(job, referral_count):
    n = referral_count or 0
    if n >= 3:
        return 10.0, "high", f"{n} potential referrer(s) in your network"
    if n >= 1:
        return 5.0, "high", f"{n} potential referrer(s) in your network"
    return 0.0, "medium", "no referral path found yet"


def _c_recency(job, today):
    raw = (job.get("posted_at") or "").strip()
    if not raw:
        return 2.0, "low", "posting date unknown"
    try:
        d = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00").split("T")[0]).date()
    except (ValueError, TypeError):
        return 2.0, "low", "posting date unparseable"
    age = (today - d).days
    if age <= 7:
        return 5.0, "high", f"posted {age}d ago"
    if age <= 21:
        return 3.0, "high", f"posted {age}d ago"
    if age <= 45:
        return 1.0, "high", f"posted {age}d ago"
    return 0.0, "high", f"posted {age}d ago (stale)"


_BOSTON = re.compile(r"boston|cambridge|massachusetts|\bma\b|remote", re.I)
_US = re.compile(r"united states|usa|u\.s\.|remote", re.I)


def _c_location(job, today=None):
    loc = job.get("location") or ""
    if not loc:
        return 2.0, "low", "location unknown"
    if _BOSTON.search(loc):
        return 5.0, "high", f"Boston-friendly / remote: {loc[:40]}"
    if _US.search(loc) or _jobspec._locations(loc):
        return 3.0, "medium", f"US location: {loc[:40]}"
    return 2.0, "low", loc[:40]


def score(job: dict, ctx: dict | None = None, immigration: dict | None = None,
          referral_count: int = 0, today: _dt.date | None = None) -> dict:
    """Compute the transparent priority score for a job. Returns total, band,
    per-component breakdown (points/max/confidence/evidence), hard stops, an overall
    uncertainty level, and a one-line 'why'."""
    ctx = ctx or {}
    today = today or _dt.date.today()

    specs = [
        ("basic_qualifications", 25, *_c_basic_quals(job, ctx)),
        ("direct_experience", 20, *_c_direct_experience(job, ctx)),
        ("immigration", 20, *_c_immigration(job, immigration)),
        ("product_customer", 15, *_c_product_customer(job, ctx)),
        ("referral_access", 10, *_c_referral(job, referral_count)),
        ("recency", 5, *_c_recency(job, today)),
        ("location", 5, *_c_location(job)),
    ]
    components = []
    hard_stops = []
    total = 0.0
    low_conf = []
    for name, mx, pts, conf, ev in specs:
        total += pts
        components.append({"name": name, "points": pts, "max": mx,
                           "confidence": conf, "evidence": ev})
        if conf == "low":
            low_conf.append(name)
    imm = immigration or job.get("immigration") or {}
    if imm.get("risk") == "red":
        hard_stops.append(imm.get("hard_stop_reason") or "sponsorship prohibited")

    total = int(round(total))
    band = _band(total, bool(hard_stops))

    # Uncertainty is qualitative on purpose — the score is a guide, not a measurement.
    if hard_stops:
        uncertainty = "n/a (hard stop)"
    elif len(low_conf) >= 3 or "basic_qualifications" in low_conf:
        uncertainty = "high"
    elif low_conf:
        uncertainty = "medium"
    else:
        uncertainty = "low"

    top = sorted([c for c in components if c["name"] != "immigration"],
                 key=lambda c: c["points"] / c["max"], reverse=True)[:2]
    gap = min(components, key=lambda c: c["points"] / c["max"])
    if hard_stops:
        why = f"Reject — hard stop: {', '.join(hard_stops)}."
    else:
        why = (f"Priority {band} ({total}/100). Strong on "
               + " & ".join(c["name"].replace("_", " ") for c in top)
               + f"; weakest on {gap['name'].replace('_',' ')}. "
               + (f"Confidence limited by: {', '.join(low_conf)}." if low_conf else "High confidence."))

    return {
        "total": total, "band": band, "hard_stops": hard_stops,
        "uncertainty": uncertainty, "why": why, "components": components,
    }
