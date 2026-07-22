#!/usr/bin/env python3
"""job-hunter worker — builds the static dashboard's data.

Runs the pipeline and writes two files:

  docs/jobs.json          PUBLIC, committed. Job data + ATS keyword-gap analysis
                          + resume import URL + a generic referral message. No
                          third-party PII (no connection names) — safe for a
                          public repo and for GitHub Actions to generate.
  data/private.local.json PRIVATE, git-ignored. Per-job in-network / Apollo
                          referral contacts derived from your LinkedIn export.
                          The dashboard loads this locally into localStorage.

Modes:
  python3 worker.py             # full run (local): public + private files
  python3 worker.py --public    # public file only (what GitHub Actions runs)
  python3 worker.py --no-discovery   # skip discovery + ATS fetch (offline/fast)

Nothing is applied or sent. Stdlib only.
"""

import argparse
import datetime as _dt
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import apollo as apollo_mod
from lib import ats as ats_mod
from lib import companies_gist as cgist_mod
from lib import discovery as disc_mod
from lib import gap as gap_mod
from lib import jobs as jobs_mod
from lib import match as match_mod
from lib import payload as payload_mod
from lib import persona as persona_mod
from lib import pool as pool_mod
from lib import profile as profile_mod
from lib import profile_roi as profile_roi_mod
from lib import referrals as ref_mod
from lib import resume_gist as resume_gist_mod

REPO_ROOT = Path(__file__).resolve().parent


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_config() -> dict:
    return json.loads((REPO_ROOT / "config.json").read_text())


def _too_old(job: dict, max_age_days: int, today: _dt.date) -> bool:
    """Auto-tidy: drop postings older than max_age_days. Unparseable/absent dates
    are kept (we don't guess). max_age_days <= 0 disables the filter."""
    if not max_age_days or max_age_days <= 0:
        return False
    raw = (job.get("posted_at") or "").strip()
    if not raw:
        return False
    try:
        d = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return False
    return (today - d).days > max_age_days


def _prev_profile_roi(path: Path):
    """Last profile-ROI report from the committed jobs.json (to carry forward / age)."""
    try:
        return json.loads(path.read_text()).get("meta", {}).get("profile_roi")
    except (OSError, json.JSONDecodeError):
        return None


def _roi_stale(prev, hours) -> bool:
    if not prev or not prev.get("ts"):
        return True
    try:
        t = _dt.datetime.fromisoformat(prev["ts"].replace("Z", "").replace(" ", "T").split("+")[0])
    except (ValueError, TypeError):
        return True
    return (_dt.datetime.now() - t).total_seconds() / 3600 >= float(hours)


def _enrich_sig(job: dict) -> str:
    import re
    c = re.sub(r"[^a-z0-9]+", " ", (job.get("company") or "").lower()).strip()
    t = re.sub(r"[^a-z0-9]+", " ", (job.get("title") or "").lower()).strip()
    return f"{c}|{t}"


def _apply_enrichment(jobs: list[dict]) -> int:
    """Merge data/enrichment.json (from scripts/enrich_jobs.py) into jobs. No file -> 0."""
    path = REPO_ROOT / "data" / "enrichment.json"
    try:
        store = json.loads(path.read_text()).get("by_sig", {})
    except (OSError, json.JSONDecodeError):
        return 0
    n = 0
    for j in jobs:
        e = store.get(_enrich_sig(j))
        if not e:
            continue
        j["enrichment"] = e
        if isinstance(e.get("boston_score"), int):
            j["boston_score"] = e["boston_score"]
        if e.get("sponsorship") in ("Yes", "No"):
            j["sponsorship"] = e["sponsorship"]
        n += 1
    return n


def _natural_key(job: dict) -> tuple:
    # Dedupe by company + title only. Many ATS feeds post the same role once per
    # location; keying on location too would leave those as duplicate cards.
    return ((job.get("company") or "").strip().lower(),
            (job.get("title") or "").strip().lower())


def collect_jobs(cfg: dict, do_discovery: bool, log) -> list[dict]:
    """Fetch fresh postings from every source, then union into the ever-expanding pool."""
    jobs = jobs_mod.load_jobs(cfg, REPO_ROOT)  # tracker/scanner (empty in Actions)
    log(f"[2/6] ingest    — {len(jobs)} job(s) from scanner/tracker")
    if do_discovery:
        scannable = disc_mod.scannable_companies(cfg, REPO_ROOT)
        log(f"        ingest    — fetching {len(scannable)} company ATS feed(s)… "
            f"(this is the slow part — ~1–2 min, no output between updates)")
        fetched = 0
        for i, c in enumerate(scannable, 1):
            new = ats_mod.fetch_company(c)
            fetched += len(new)
            jobs.extend(new)
            if i % 15 == 0 or i == len(scannable):   # heartbeat so it never looks frozen
                log(f"        ingest    — {i}/{len(scannable)} companies · {fetched} postings so far")
        if fetched:
            log(f"        ingest    — +{fetched} from {len(scannable)} company ATS feed(s)")
        else:
            log("        ingest    — 0 from ATS feeds (check network; see fetch errors above)")
        postings = disc_mod.fetch_postings(cfg, REPO_ROOT, log)  # JSearch job boards
        jobs.extend(postings)

    # Relevance gate: pool ONLY jobs that pass the match filter. The boards return
    # thousands of off-lane roles (engineering, sales, etc.) that never surface in
    # jobs.json; pooling them just bloats the committed file and — worse — lets the
    # size cap evict a genuinely-good, high-fit job (whose board had a transient miss
    # that run) to make room for off-lane noise. It then reappears next run with a
    # reset _first_seen: the "job vanished after deploy" flicker. Keeping only passers
    # holds the pool to the set the user actually sees, so the cap never bites and good
    # jobs persist across runs. cfg["match"] already has the persona folded in here.
    m = cfg.get("match", {})
    profile = profile_mod.load_profile(cfg, REPO_ROOT)
    extra_terms = m.get("extra_lane_terms", [])

    def _passes(job: dict) -> bool:
        res = match_mod.match_job(job, profile, extra_terms)
        return match_mod.passes_filters(job, res, m)

    pool_path = (REPO_ROOT / cfg.get("pool_file", "data/job_pool.json")).resolve()
    prior = pool_mod.load_pool(pool_path)
    fresh_relevant = [j for j in jobs if _passes(j)]
    # Re-gate the accumulated pool too, so off-lane jobs pooled before this change (or
    # under a broader persona) are pruned rather than lingering forever.
    prior_relevant = [j for j in prior if _passes(j)]
    dropped = len(prior) - len(prior_relevant)
    merged, stats = pool_mod.merge(
        prior_relevant, fresh_relevant, _now(),
        max_age_days=cfg.get("pool_max_age_days", m.get("max_age_days", 45)),
        max_size=cfg.get("pool_max_size", 4000))
    pool_mod.save_pool(pool_path, merged, _now())
    log(f"        pool      — {stats['total']} in pool "
        f"(+{stats['added']} new, -{stats['aged_out']} aged out, "
        f"-{dropped} off-lane pruned this run)")
    return merged


def run(cfg: dict, do_discovery: bool = True, public_only: bool = False, log=print) -> dict:
    now = _now()

    # Fold in the dashboard-published persona (public Gist) so the scrape keeps only
    # jobs that fit you. No PERSONA_GIST_ID / no net -> unchanged.
    persona = persona_mod.load_persona()
    if persona:
        cfg["match"] = persona_mod.apply(persona, cfg["match"])
        log(f"[0/6] persona   — applied ({len(persona.get('roles', []))} role(s), "
            f"{len(persona.get('skills', []))} skill(s), min_fit={cfg['match'].get('min_fit_score')})")

    # Fold user-added companies (published to a Gist from the dashboard) into the scan
    # list, so a company you add in the app becomes scannable. No id/net -> no-op.
    cg = cgist_mod.merge(cfg, REPO_ROOT)
    if cg["added"]:
        log(f"[0/6] companies — +{cg['added']} user-added from gist ({cg['listed']} listed)")

    if do_discovery:
        summary = disc_mod.discover_companies(cfg, REPO_ROOT, log)
        log(f"[1/6] discover  — +{summary['added']} companies "
            f"({summary['verified']} H-1B-verified)" if summary["added"]
            else "[1/6] discover  — no new companies this run")
    else:
        log("[1/6] discover  — skipped")

    all_jobs = collect_jobs(cfg, do_discovery, log)

    profile = profile_mod.load_profile(cfg, REPO_ROOT)
    ref_cfg = cfg["referrals"]
    titles = ref_cfg.get("target_titles", [])
    # Only non-PII contact fields (name, location, linkedin, github) go into the
    # committed/public data. Email + phone live in a git-ignored local file and
    # are merged into the resume only client-side.
    contact_public = cfg["contact_public"]
    contact_name = contact_public["fullName"]
    app_url = cfg["resume_builder"]["app_url"]
    encoding = cfg["resume_builder"].get("import_encoding", "raw")

    connections = []
    contact_pii = {}
    if not public_only:
        conn_path = (REPO_ROOT / ref_cfg["connections_csv"]).resolve()
        connections = ref_mod.load_connections(conn_path)
        pii_path = (REPO_ROOT / cfg.get("contact_pii_file", "")).resolve() if cfg.get("contact_pii_file") else None
        if pii_path and pii_path.exists():
            try:
                contact_pii = json.loads(pii_path.read_text())
            except (json.JSONDecodeError, OSError):
                contact_pii = {}

    public_jobs = []
    private = {}
    missing_counter = Counter()
    kept = 0
    total_ref = 0
    tidied = 0
    today = _dt.date.today()
    max_age = cfg["match"].get("max_age_days", 0)

    extra_terms = cfg["match"].get("extra_lane_terms", [])
    for job in all_jobs:
        m = match_mod.match_job(job, profile, extra_terms)
        if not match_mod.passes_filters(job, m, cfg["match"]):
            continue
        if _too_old(job, max_age, today):   # auto-tidy stale postings
            tidied += 1
            continue
        kept += 1

        g = gap_mod.analyze(job, profile)
        for kw in g["missing_keywords"]:
            missing_counter[kw] += 1

        # resume_core carries NO email/phone (contact_public only). The dashboard
        # merges those in-browser from your loaded private data before opening.
        # NOTE: the builder import URL is NOT stored here — it's fully derivable from
        # resume_core, and the base64 blob was ~half of jobs.json. The dashboard
        # builds it client-side (buildImportUrl), which frees room for the full JD.
        resume_core = payload_mod.build_resume_input(job, m["variant_obj"], profile, contact_public)
        message = ref_mod.draft_message({"name": ""}, job, contact_name)
        search_link = ref_mod.linkedin_search_url(job["company"], titles)

        public_jobs.append({
            "id": job["id"],
            "company": job.get("company", ""),
            "title": job.get("title", ""),
            "location": job.get("location", ""),
            "url": job.get("url", ""),
            "source": job.get("source", ""),
            "posted_at": job.get("posted_at", ""),
            "sponsorship": job.get("sponsorship", "Unknown"),
            "fit_score": m["fit_score"],
            "min_years": match_mod.extract_years(job.get("excerpt")),
            "variant_label": m.get("matched_variant"),
            "title_keywords": m.get("title_keywords", []),
            "ats_score": g["ats_score"],
            "best_variant": g["best_variant"],
            "missing_keywords": g["missing_keywords"],
            "requested_keywords": g["requested_keywords"],
            "per_variant": g["per_variant"],
            "jd_available": g["jd_available"],
            "excerpt": job.get("excerpt", ""),   # (near-)full JD text — what to align to
            "resume_core": resume_core,
            "referral_message": message,
            "linkedin_search": search_link,
        })

        if not public_only:
            apollo_people = apollo_mod.find_people(job["company"], titles, cfg, REPO_ROOT)
            referrers = ref_mod.build_referrals(job["company"], connections, ref_cfg, apollo_people)
            if referrers:
                private[job["id"]] = referrers
                total_ref += len(referrers)

    # Re-merge locally-computed enrichment (scripts/enrich_jobs.py, keyed by company+
    # title signature) so it survives cloud rebuilds. No file -> no-op.
    n_enriched = _apply_enrichment(public_jobs)
    if n_enriched:
        log(f"        enrich    — merged enrichment into {n_enriched} job(s)")

    public_jobs.sort(key=lambda j: (j["fit_score"], j["ats_score"]), reverse=True)

    # Company watchlist — the set the scraper is looking at, surfaced so the dashboard
    # can list it (and link into the Jobs company filter). Slim + PII-free.
    watch = disc_mod.load_companies((REPO_ROOT / cfg["companies_file"]).resolve())
    meta_companies = [{
        "name": c.get("name", ""), "ats": c.get("ats", ""),
        "active": bool(c.get("active")), "source": c.get("source", ""),
        "careers_url": cgist_mod.careers_url(c),
    } for c in watch if c.get("name")]
    meta_companies.sort(key=lambda c: c["name"].lower())

    # Aggregate "what am I missing" analysis across all jobs.
    best = max(public_jobs, key=lambda j: j["ats_score"], default=None)
    leaderboard = [{"keyword": k, "count": c} for k, c in missing_counter.most_common(20)]

    # Profile-ROI — resume-aware "what to work on", gated to ~once/day (or on digest)
    # to bound Claude cost. Carries the previous report forward when not refreshed.
    pub_path = (REPO_ROOT / cfg["output"]["public_json"]).resolve()
    roi_cfg = cfg.get("profile_roi", {}) or {}
    profile_roi = _prev_profile_roi(pub_path)
    force_roi = os.environ.get("HUNT_MODE", "").strip() == "digest"
    if roi_cfg.get("enabled", True) and (force_roi or _roi_stale(profile_roi, roi_cfg.get("min_interval_hours", 20))):
        resume = resume_gist_mod.load_resume(roi_cfg.get("gist_env", "RESUME_GIST_ID"))
        fresh = profile_roi_mod.analyze(cfg, REPO_ROOT, public_jobs, leaderboard,
                                        resume, persona or {}, now, log)
        if fresh:
            profile_roi = fresh
            log(f"        profile-roi — refreshed (score "
                f"{fresh['report'].get('completeness_score')}, {fresh['jobs']} jobs)")

    doc = {
        "generated_at": now,
        "meta": {
            "job_count": len(public_jobs),
            "app_url": app_url,
            "min_fit_score": cfg["match"]["min_fit_score"],
            "github": cfg.get("github", {}),
            "companies": meta_companies,
            "company_count": len(meta_companies),
            "profile_roi": profile_roi,
        },
        "summary": {
            "best_match": ({
                "ats_score": best["ats_score"],
                "variant": best["best_variant"],
                "company": best["company"],
                "title": best["title"],
                "job_id": best["id"],
            } if best else None),
            "missing_leaderboard": leaderboard,
        },
        "jobs": public_jobs,
    }

    pub_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.write_text(json.dumps(doc, indent=2))
    log(f"[3/6] match     — {len(public_jobs)} job(s) pass fit >= {cfg['match']['min_fit_score']}"
        + (f"; auto-tidied {tidied} stale" if tidied else ""))
    log(f"[4/6] gap       — best ATS {best['ats_score'] if best else 0}% "
        f"({best['best_variant'] if best else '—'}); "
        f"{len(missing_counter)} distinct missing keyword(s)")
    log(f"[5/6] public    — wrote {pub_path}")

    if not public_only:
        priv_payload = {
            "referrals": private,
            "connections_loaded": len(connections),
            "contact": contact_pii,   # {email, phone} — merged into resumes in-browser only
        }
        priv_path = (REPO_ROOT / cfg["output"]["private_json"]).resolve()
        priv_path.parent.mkdir(parents=True, exist_ok=True)
        priv_path.write_text(json.dumps({"generated_at": now, **priv_payload}, indent=2))

        # One-off script: paste into the dashboard console to load PII into
        # localStorage. Git-ignored; regenerated each run. Never committed.
        inject_path = (REPO_ROOT / "scripts" / "inject.local.js").resolve()
        inject_path.write_text(
            "// One-off: open your dashboard, paste this whole file into the browser\n"
            "// console (F12 -> Console), press Enter. It loads your private data into\n"
            "// localStorage on THIS browser only. Git-ignored — never committed.\n"
            "localStorage.setItem('job-hunter:private:v1', JSON.stringify("
            + json.dumps(priv_payload) + "));\n"
            "location.reload();\n"
        )
        log(f"[6/6] private   — {total_ref} contact(s) across {len(private)} job(s); "
            f"console script -> {inject_path}")
    else:
        log("[6/6] private   — skipped (--public)")

    return {"jobs": len(public_jobs), "referrals": total_ref, "at": now,
            "best_ats": best["ats_score"] if best else 0}


def _publish() -> None:
    """Commit the refreshed data and push, so the live dashboard updates. This is the
    step people forget after a local run — --publish folds it into the worker."""
    import subprocess
    files = ["docs/jobs.json", "data/job_pool.json", "docs/seen.json", "data/companies.json"]
    subprocess.run(["git", "add", *files], cwd=REPO_ROOT)
    st = subprocess.run(["git", "status", "--porcelain", *files], cwd=REPO_ROOT,
                        capture_output=True, text=True).stdout.strip()
    if not st:
        print("publish     — nothing changed since the last publish; skipping.")
        return
    # NOTE: message must never contain the literal CI-skip token, or the deploy is skipped.
    msg = "refresh: publish latest local job refresh"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=REPO_ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("publish     — commit failed:", (r.stderr or r.stdout).strip()); return
    p = subprocess.run(["git", "push"], cwd=REPO_ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        print("publish     — push failed:", (p.stderr or p.stdout).strip()); return
    print("publish     — pushed. The live dashboard will update in ~1–2 min "
          "(hard-refresh the browser with Cmd+Shift+R).")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the dashboard data files.")
    ap.add_argument("--public", action="store_true", help="public jobs.json only (Actions mode)")
    ap.add_argument("--no-discovery", action="store_true", help="skip discovery + ATS fetch")
    ap.add_argument("--no-publish", action="store_true",
                    help="local only — do NOT commit/push (default is to publish so the live site updates)")
    ap.add_argument("--publish", action="store_true", help="(deprecated: publishing is the default now)")
    ap.add_argument("--min-fit", type=int, default=None)
    args = ap.parse_args()
    cfg = load_config()
    if args.min_fit is not None:
        cfg["match"]["min_fit_score"] = args.min_fit
    run(cfg, do_discovery=not args.no_discovery, public_only=args.public)
    # Publish by DEFAULT: "refresh" and "make it live" are one step now, because the
    # split kept catching people out. Skip only for --no-publish or Actions (--public).
    if not args.no_publish and not args.public:
        _publish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
