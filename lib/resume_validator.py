"""Phase 5 — programmatic résumé validator ("don't trust the LLM to be truthful").

Given a generated résumé (resume-builder JSON), the candidate's BASE resume, the
target job, and the verified fact bank, this checks — deterministically, stdlib-only
— the rules the brief says a validator must FAIL generation on:

  • a number appears that the fact bank / base resume doesn't support
  • employment dates, employers, or titles differ from the real resume
  • an unsupported technology/tool appears
  • ownership is upgraded (a "contributed" fact rewritten as "led/owned")
  • a company or product from ANOTHER application is left in
  • a bullet cannot be traced to a verified fact (or, until facts are verified, to
    the base resume — so wholesale-invented bullets are still caught)

It also reports requirement coverage, genuine gaps, the fact_ids used, and a change
log. One-page fit is a heuristic warning; true rendering / PDF-ATS / hyperlink checks
need a renderer and are reported as not-checked rather than faked. The base resume is
the ground truth of what the candidate actually has; verified facts tighten the bar.
"""

import re

from lib import factbank as _fb
from lib import jobspec as _jobspec

# Ownership verb classes — an upgrade from "contributed" to "led" is a fabrication
# of ownership even when every other word is true.
_LED = re.compile(r"\b(led|lead|owned?|drove|driven|spearheaded|directed|founded|"
                  r"built|launched|created|established|architected|headed|ran)\b", re.I)
_CONTRIB = re.compile(r"\b(contributed|supported|assisted|helped|participated|"
                      r"collaborated|partnered|aided|coordinated)\b", re.I)

# Tool / technology vocabulary — a term here that appears in the tailored résumé but
# NOT anywhere in the base resume is an unsupported technology.
_TOOLS = ["sql", "tableau", "power bi", "looker", "ga4", "google analytics", "excel",
          "salesforce", "hubspot", "eloqua", "marketo", "pardot", "jira", "asana",
          "figma", "python", "r", "sas", "spss", "amplitude", "mixpanel", "segment",
          "snowflake", "dbt", "airflow", "kubernetes", "docker", "aws", "gcp", "azure",
          "java", "javascript", "react", "tensorflow", "pytorch", "hadoop", "spark",
          "adobe analytics", "google ads", "facebook ads", "outreach", "gong", "6sense"]

_WORD = re.compile(r"[a-z0-9][a-z0-9\+/\.#-]*")
_STOP = set("the a an and or of to in for on with at by from as is are be this that "
            "you your we our their they it its into across over per using use used "
            "including etc via which who whom whose than then".split())


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def _all_highlights(resume: dict) -> list[str]:
    out = []
    for e in resume.get("experience", []) or []:
        out += [h for h in (e.get("highlights") or []) if h]
    for p in resume.get("projects", []) or []:
        out += [h for h in (p.get("highlights") or []) if h]
    return out


def _resume_text(resume: dict) -> str:
    parts = [resume.get("summary", "")]
    parts += _all_highlights(resume)
    for grp in resume.get("skills", []) or []:
        parts += (grp.get("items") or [])
    return " ".join(parts)


def _skills_text(resume: dict) -> str:
    out = []
    for grp in resume.get("skills", []) or []:
        out.append(grp.get("name", ""))
        out += (grp.get("items") or [])
    return " ".join(out)


def _tools_in(text: str) -> set[str]:
    tl = (text or "").lower()
    return {t for t in _TOOLS if re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", tl)}


def _ownership_class(text: str) -> str:
    if _LED.search(text or ""):
        return "led"
    if _CONTRIB.search(text or ""):
        return "contributed"
    return "neutral"


def _best_match(bullet: str, candidates: list[str]) -> tuple[float, str]:
    bt = _tokens(bullet)
    best, best_s = "", 0.0
    for c in candidates:
        s = _jaccard(bt, _tokens(c))
        if s > best_s:
            best_s, best = s, c
    return best_s, best


# --- individual rule checks ----------------------------------------------------
def _check_numbers(tailored: dict, base: dict, verified_facts: list) -> list[str]:
    """Every number in the tailored résumé must be supported by the base resume or a
    verified fact. A new statistic is a fabrication."""
    supported = _fb.numbers_in(_resume_text(base))
    for f in verified_facts:
        supported |= _fb.fact_numbers(f)
    bad = []
    for h in _all_highlights(tailored) + [tailored.get("summary", "")]:
        for n in _fb.numbers_in(h):
            if n in supported:
                continue
            # Only CLAIM-BEARING figures are checked: a percentage, a currency amount,
            # a k/m/b/x/+ suffix, comma-thousands, or a decimal. A bare integer — a plain
            # count ("3 teams") or a year ("by 2030") — is not a verifiable statistic and
            # is never a fail on its own.
            if re.fullmatch(r"\d+", n.rstrip(".")):
                continue
            bad.append(f"unsupported number '{n}' in: {h[:70]}")
    return bad


def _check_frozen(tailored: dict, base: dict) -> list[str]:
    """Employers, titles and dates must match the base resume exactly."""
    out = []
    be, te = base.get("experience", []) or [], tailored.get("experience", []) or []
    if len(be) != len(te):
        out.append(f"experience entry count changed ({len(be)}→{len(te)})")
        return out
    for i, (b, t) in enumerate(zip(be, te)):
        for field, label in (("company", "employer"), ("title", "title"),
                             ("startDate", "start date"), ("endDate", "end date")):
            if (b.get(field) or "").strip() != (t.get(field) or "").strip():
                out.append(f"{label} changed on entry {i+1}: "
                           f"{b.get(field)!r} → {t.get(field)!r}")
    return out


def _check_tools(tailored: dict, base: dict) -> list[str]:
    have = _tools_in(_resume_text(base))
    used = _tools_in(_resume_text(tailored))
    return [f"unsupported technology '{t}' not in the base resume" for t in sorted(used - have)]


def _check_ownership(tailored: dict, base: dict) -> list[str]:
    base_h = _all_highlights(base)
    out = []
    for h in _all_highlights(tailored):
        if _ownership_class(h) != "led":
            continue
        score, match = _best_match(h, base_h)
        if score >= 0.35 and _ownership_class(match) == "contributed":
            out.append(f"ownership upgraded (contributed→led): {h[:70]}")
    return out


def _check_contamination(tailored: dict, allowed_names: set, other_names: set) -> list[str]:
    text = _resume_text(tailored).lower()
    out = []
    for name in other_names:
        n = (name or "").strip().lower()
        if len(n) < 3 or n in allowed_names:
            continue
        if re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", text):
            out.append(f"leftover company/product from another application: {name!r}")
    return out


# Below INVENT_MAX a bullet has essentially no basis in the real resume → invented.
# Between INVENT_MAX and TRACE_MIN it's a heavy paraphrase — allowed until a verified
# fact bank exists, at which point it must trace to a verified fact.
_INVENT_MAX = 0.15
_TRACE_MIN = 0.30


def _check_traceability(tailored: dict, base: dict, verified_facts: list):
    """Trace each bullet. Returns (hard_untraceable, soft_untraceable, fact_ids_used,
    enforcing). Wholesale-invented bullets are ALWAYS hard. Once verified facts exist,
    a bullet that doesn't trace to one is hard; before that, a heavy paraphrase of the
    base resume is a soft warning (the number/tool/ownership rules catch clear fakes)."""
    base_h = _all_highlights(base)
    fact_actions = [(f.get("fact_id"), f.get("action", "")) for f in verified_facts]
    enforcing = bool(fact_actions)
    hard, soft, used = [], [], set()
    for h in _all_highlights(tailored):
        fid_hit = next((fid for fid, action in fact_actions
                        if _jaccard(_tokens(h), _tokens(action)) >= _TRACE_MIN), None)
        if fid_hit:
            used.add(fid_hit)
            continue
        score, _ = _best_match(h, base_h)
        if score < _INVENT_MAX:
            hard.append(f"no basis in the real resume: {h[:70]}")     # invented
        elif enforcing:
            hard.append(f"not tied to a verified fact: {h[:60]}")     # must trace to a fact
        elif score < _TRACE_MIN:
            soft.append(f"weak trace to the base resume (heavy paraphrase): {h[:60]}")
    return hard, soft, sorted(used), enforcing


def _one_page_estimate(resume: dict) -> tuple[int, bool]:
    """Rough line estimate (~55 lines ≈ one page). Heuristic — real fit needs a renderer."""
    lines = len(re.findall(r"[.!?]", resume.get("summary", ""))) + 1
    for e in resume.get("experience", []) or []:
        lines += 2 + len(e.get("highlights") or [])          # header + subhead + bullets
    for p in resume.get("projects", []) or []:
        lines += 1 + len(p.get("highlights") or [])
    lines += len(resume.get("skills", []) or []) + len(resume.get("education", []) or []) + 3
    return lines, lines > 55


def _requirement_coverage(tailored: dict, job: dict):
    """Which basic_quals the résumé evidences vs genuine gaps (Phase 3 spec)."""
    spec = job.get("spec") or _jobspec.structure(job, include_full=False, max_bullets=12)
    quals = spec.get("basic_qualifications", []) or []
    rtext = _tokens(_resume_text(tailored))
    covered, gaps = [], []
    for q in quals:
        qt = _tokens(q) - {"years", "experience", "degree", "bachelor", "master", "plus"}
        overlap = len(qt & rtext) / len(qt) if qt else 0.0
        (covered if overlap >= 0.30 else gaps).append(q[:90])
    return {"covered": covered, "gaps": gaps,
            "coverage_ratio": round(len(covered) / len(quals), 2) if quals else None}


def _change_log(tailored: dict, base: dict) -> list[str]:
    log = []
    if (base.get("summary") or "") != (tailored.get("summary") or ""):
        log.append("summary rewritten")
    for i, (b, t) in enumerate(zip(base.get("experience", []) or [],
                                   tailored.get("experience", []) or [])):
        bh, th = b.get("highlights") or [], t.get("highlights") or []
        if bh != th:
            log.append(f"{t.get('company','entry '+str(i+1))}: bullets edited/reordered "
                       f"({len(bh)}→{len(th)})")
    bs = [g.get("name") for g in base.get("skills", []) or []]
    ts = [g.get("name") for g in tailored.get("skills", []) or []]
    if bs != ts:
        log.append("skills regrouped/reordered")
    return log


def validate(tailored: dict, base: dict, job: dict,
             facts: list | None = None, other_names=None, filename: str = "") -> dict:
    """Run every programmatic check and return the full validation report.
    `passed` is False when any HARD rule is violated."""
    verified = _fb.usable(facts or [])
    allowed = {(e.get("company") or "").strip().lower()
               for e in base.get("experience", []) or []}
    allowed |= {(job.get("company") or "").strip().lower()}
    other = set(other_names or []) - {""}

    unsupported_numbers = _check_numbers(tailored, base, verified)
    frozen_drift = _check_frozen(tailored, base)
    unsupported_tools = _check_tools(tailored, base)
    ownership_upgrades = _check_ownership(tailored, base)
    contamination = _check_contamination(tailored, allowed, other)
    hard_untraceable, soft_untraceable, fact_ids_used, trace_enforcing = \
        _check_traceability(tailored, base, verified)
    est_lines, over_one_page = _one_page_estimate(tailored)

    hard = []
    hard += [("number", x) for x in unsupported_numbers]
    hard += [("dates/employer/title", x) for x in frozen_drift]
    hard += [("technology", x) for x in unsupported_tools]
    hard += [("ownership", x) for x in ownership_upgrades]
    hard += [("contamination", x) for x in contamination]
    hard += [("traceability", x) for x in hard_untraceable]
    untraceable = hard_untraceable + soft_untraceable

    warnings = []
    warnings += soft_untraceable
    if over_one_page:
        warnings.append(f"may exceed one page (~{est_lines} lines by heuristic — verify in the builder)")
    if not verified:
        warnings.append("fact bank has no verified facts yet — bullets checked against the base "
                        "resume only; verify facts (scripts/seed_facts.py) to enforce fact-level tracing")

    return {
        "filename": filename,
        "passed": not hard,
        "hard_failures": hard,
        "warnings": warnings,
        "fact_ids_used": fact_ids_used,
        "requirement_coverage": _requirement_coverage(tailored, job),
        "change_log": _change_log(tailored, base),
        "unsupported_numbers": unsupported_numbers,
        "unsupported_tools": unsupported_tools,
        "ownership_upgrades": ownership_upgrades,
        "contamination": contamination,
        "date_title_drift": frozen_drift,
        "untraceable_bullets": untraceable,
        "trace_enforcing_on_facts": trace_enforcing,
        "not_checked": [
            "one-page rendering (needs the resume-builder/PDF renderer)",
            "PDF text ATS-readability (needs PDF extraction)",
            "hyperlink liveness (needs network checks)",
        ],
    }


def format_report(rep: dict) -> str:
    """Human-readable validation report for the CLI (the brief's 'validation results')."""
    lines = [f"Validation: {'PASS ✓' if rep['passed'] else 'FAIL ✗'}"]
    for kind, msg in rep["hard_failures"]:
        lines.append(f"  ✗ [{kind}] {msg}")
    for w in rep["warnings"]:
        lines.append(f"  ⚠ {w}")
    cov = rep["requirement_coverage"]
    if cov.get("coverage_ratio") is not None:
        lines.append(f"  requirement coverage: {cov['coverage_ratio']} "
                     f"({len(cov['covered'])} met, {len(cov['gaps'])} gap(s))")
    if cov.get("gaps"):
        lines.append("  genuine gaps: " + "; ".join(cov["gaps"][:4]))
    if rep["fact_ids_used"]:
        lines.append(f"  fact_ids used: {', '.join(rep['fact_ids_used'])}")
    if rep["change_log"]:
        lines.append("  change log: " + "; ".join(rep["change_log"]))
    lines.append("  not checked (needs a renderer): one-page fit, PDF ATS text, hyperlinks")
    return "\n".join(lines)
