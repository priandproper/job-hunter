"""Phase 12 — funnel analytics over the application event log.

Turns the Phase 9 events (applied / recruiter_screen / interview / offer / rejected …)
joined to the job data into per-application records with every dimension the brief
tracks, the conversion rates (overall and by dimension), and — every 30 applications —
a plain-language diagnostic. Deterministic, stdlib-only, testable.

Stage vocabulary preserves the brief's distinction between an initial recruiter CALL
(recruiter_screen), a substantive INTERVIEW process (interview), a FINAL round
(interview whose metadata.round is final/onsite/last, or an offer implies it), and an
OFFER. Historical planning baseline: ~5 interviews per 120 applications (4.17%).
"""

import datetime as _dt

from lib import ranking as _ranking

BASELINE_APP_TO_INTERVIEW = 5 / 120     # 4.17%
REPORT_EVERY = 30
_CALL = {"recruiter_screen", "interview", "offer"}
_INTERVIEW = {"interview", "offer"}
_FINAL_ROUNDS = {"final", "onsite", "last", "panel"}


def _date(ts):
    try:
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00").split("T")[0]).date()
    except (ValueError, TypeError):
        return None


def _age_bucket(days):
    if days is None:
        return "unknown"
    if days <= 7:
        return "0-7"
    if days <= 14:
        return "8-14"
    if days <= 30:
        return "15-30"
    return "31+"


def _exp_bucket(years):
    if years is None:
        return "unstated"
    if years <= 2:
        return "0-2"
    if years <= 6:
        return "3-6"
    if years == 7:
        return "7"
    return "8+"


def _is_qualified(job, sponsorship):
    """Same gates as the cockpit's qualified-application test we can check server-side."""
    p = job.get("priority") or {}
    if p.get("band") == "Reject" or sponsorship == "red":
        return False
    basic = next((c for c in (p.get("components") or [])
                  if c.get("name") == "basic_qualifications"), None)
    if basic and basic.get("max") and basic["points"] / basic["max"] < 0.5:
        return False
    return True


def build_applications(events, jobs_by_id, today=None):
    """One record per job that has an 'applied' event, with all tracked dimensions."""
    today = today or _dt.date.today()
    by_job = {}
    for e in sorted(events or [], key=lambda x: str(x.get("timestamp") or "")):
        jid = e.get("job_id")
        if jid:
            by_job.setdefault(jid, []).append(e)

    apps = []
    for jid, evs in by_job.items():
        types = {e.get("event_type") for e in evs}
        if "applied" not in types:
            continue
        job = jobs_by_id.get(jid, {}) or {}
        applied = next((e for e in evs if e.get("event_type") == "applied"), None)
        applied_ts = applied.get("timestamp") if applied else None
        spons = (job.get("immigration") or {}).get("risk", "unknown")

        reached_final = "offer" in types or any(
            e.get("event_type") == "interview"
            and (e.get("metadata") or {}).get("round") in _FINAL_ROUNDS for e in evs)
        rej = next((e for e in evs if e.get("event_type") == "rejected"), None)
        rmeta = (rej.get("metadata") or {}) if rej else {}
        resume_ev = next((e for e in evs if e.get("event_type") == "resume_generated"), None)
        rmeta_resume = (resume_ev.get("metadata") or {}) if resume_ev else {}
        out_ev = next((e for e in evs if e.get("event_type") == "outreach_sent"), None)

        ad, pd = _date(applied_ts), _date(job.get("posted_at"))
        age = (ad - pd).days if (ad and pd) else None
        years = ((job.get("spec") or {}).get("required_years")
                 if job.get("spec") else job.get("min_years"))

        apps.append({
            "job_id": jid,
            "company": job.get("company", "") or "unknown",
            "source": job.get("source", "") or "unknown",
            "lane": _ranking.lane_of(job.get("title")),
            "sponsorship": spons,
            "warm": "outreach_sent" in types,
            "referral_type": (out_ev.get("metadata") or {}).get("type", "warm") if out_ev else "none",
            "resume_version": rmeta_resume.get("template") or rmeta_resume.get("version") or "unknown",
            "posting_age_days": age,
            "age_bucket": _age_bucket(age),
            "required_years": years,
            "exp_bucket": _exp_bucket(years),
            "qualified": _is_qualified(job, spons),
            "reached_call": bool(types & _CALL),
            "reached_interview": bool(types & _INTERVIEW),
            "reached_final": reached_final,
            "offer": "offer" in types,
            "rejected": "rejected" in types,
            "rejection_stage": rmeta.get("stage", "") if rej else "",
            "rejection_reason": rmeta.get("reason", "") if rej else "",
            "applied_ts": applied_ts,
        })
    return apps


def _pct(n, d):
    return round(100 * n / d, 1) if d else 0.0


def _rates(apps):
    n = len(apps)
    calls = sum(a["reached_call"] for a in apps)
    interviews = sum(a["reached_interview"] for a in apps)
    finals = sum(a["reached_final"] for a in apps)
    offers = sum(a["offer"] for a in apps)
    return {
        "applications": n,
        "qualified": sum(a["qualified"] for a in apps),
        "calls": calls, "interviews": interviews, "finals": finals, "offers": offers,
        "app_to_call": _pct(calls, n),
        "app_to_interview": _pct(interviews, n),
        "interview_to_final": _pct(finals, interviews),
        "final_to_offer": _pct(offers, finals),
        "interview_to_offer": _pct(offers, interviews),
    }


def _group(apps, key):
    out = {}
    for a in apps:
        out.setdefault(a[key], []).append(a)
    return {k: _rates(v) for k, v in sorted(out.items())}


def conversions(apps):
    """Overall funnel + conversion broken down by each dimension the brief lists."""
    return {
        "overall": _rates(apps),
        "by_lane": _group(apps, "lane"),
        "by_resume": _group(apps, "resume_version"),
        "by_source": _group(apps, "source"),
        "by_warm_cold": _group([dict(a, warm=("warm" if a["warm"] else "cold")) for a in apps], "warm"),
        "by_sponsorship": _group(apps, "sponsorship"),
        "by_age": _group(apps, "age_bucket"),
        "by_experience": _group(apps, "exp_bucket"),
    }


def should_report(n_apps):
    return n_apps > 0 and n_apps % REPORT_EVERY == 0


def _best_worst(by, metric, min_n=3):
    elig = {k: v for k, v in by.items() if v["applications"] >= min_n}
    if not elig:
        return None, None
    best = max(elig, key=lambda k: elig[k][metric])
    worst = min(elig, key=lambda k: elig[k][metric])
    return (best, elig[best]), (worst, elig[worst])


def diagnostic(apps, conv=None, today=None):
    """The brief's after-every-30 diagnostic, as plain-language answers."""
    conv = conv or conversions(apps)
    ov = conv["overall"]
    n = ov["applications"]
    out = {}

    best_lane, worst_lane = _best_worst(conv["by_lane"], "app_to_interview")
    out["what_is_converting"] = (
        f"{best_lane[0]} lane leads at {best_lane[1]['app_to_interview']}% app→interview "
        f"({best_lane[1]['interviews']}/{best_lane[1]['applications']})." if best_lane
        else "Not enough volume per lane yet to say.")
    dead = [k for k, v in conv["by_lane"].items() if v["applications"] >= 3 and v["interviews"] == 0]
    out["what_is_not_converting"] = ("No interviews from: " + ", ".join(dead)
                                     if dead else "No lane has clearly stalled yet.")

    # biggest drop-off stage
    stages = [("apply→call", ov["app_to_call"]), ("call→interview",
              _pct(ov["interviews"], ov["calls"])), ("interview→offer", ov["interview_to_offer"])]
    weakest = min(stages, key=lambda s: s[1])
    out["where_funnel_fails"] = f"Weakest step: {weakest[0]} at {weakest[1]}%."

    # lane to give more volume: high conversion but low share
    if best_lane and n:
        share = _pct(best_lane[1]["applications"], n)
        out["which_lane_more_volume"] = (
            f"Send more to {best_lane[0]} — it converts best ({best_lane[1]['app_to_interview']}%) "
            f"but is only {share}% of applications.")
    else:
        out["which_lane_more_volume"] = "Insufficient data."
    _, worst_resume = _best_worst(conv["by_resume"], "app_to_call")
    out["which_resume_needs_revision"] = (
        f"'{worst_resume[0]}' résumé has the lowest app→call ({worst_resume[1]['app_to_call']}%)."
        if worst_resume else "Résumé version not recorded per application yet.")

    yellow = conv["by_sponsorship"].get("yellow", {}).get("applications", 0)
    red = conv["by_sponsorship"].get("red", {}).get("applications", 0)
    out["immigration_filtered_early_enough"] = (
        f"{red} application(s) went to red (hard-stop) roles — should be filtered earlier."
        if red else f"No hard-stop roles applied to. {yellow} yellow (unverified) — verify sponsorship sooner."
        if yellow else "Sponsorship posture looks clean on applications.")

    ages = [a["posting_age_days"] for a in apps if a["posting_age_days"] is not None]
    med = sorted(ages)[len(ages) // 2] if ages else None
    out["applications_fast_enough"] = (
        f"Median posting age at apply is {med}d — {'too slow, apply sooner' if med and med > 14 else 'good pace'}."
        if med is not None else "Posting age at apply unknown.")

    out["vs_baseline"] = (
        f"App→interview {ov['app_to_interview']}% vs {round(BASELINE_APP_TO_INTERVIEW*100,2)}% baseline — "
        + ("ahead of plan." if ov["app_to_interview"] >= BASELINE_APP_TO_INTERVIEW * 100 else "below plan."))
    return out
