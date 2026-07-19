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
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import apollo as apollo_mod
from lib import ats as ats_mod
from lib import discovery as disc_mod
from lib import gap as gap_mod
from lib import jobs as jobs_mod
from lib import match as match_mod
from lib import payload as payload_mod
from lib import profile as profile_mod
from lib import referrals as ref_mod

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


def _natural_key(job: dict) -> tuple:
    # Dedupe by company + title only. Many ATS feeds post the same role once per
    # location; keying on location too would leave those as duplicate cards.
    return ((job.get("company") or "").strip().lower(),
            (job.get("title") or "").strip().lower())


def collect_jobs(cfg: dict, do_discovery: bool, log) -> list[dict]:
    jobs = jobs_mod.load_jobs(cfg, REPO_ROOT)  # tracker/scanner (empty in Actions)
    log(f"[2/6] ingest    — {len(jobs)} job(s) from scanner/tracker")
    if do_discovery:
        scannable = disc_mod.scannable_companies(cfg, REPO_ROOT)
        fetched = 0
        for c in scannable:
            new = ats_mod.fetch_company(c)
            fetched += len(new)
            jobs.extend(new)
        if fetched:
            log(f"        ingest    — +{fetched} from {len(scannable)} company ATS feed(s)")
    by_key: dict[tuple, dict] = {}
    for j in jobs:
        k = _natural_key(j)
        if k not in by_key or len(j.get("excerpt") or "") > len(by_key[k].get("excerpt") or ""):
            by_key[k] = j
    return list(by_key.values())


def run(cfg: dict, do_discovery: bool = True, public_only: bool = False, log=print) -> dict:
    now = _now()

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

    for job in all_jobs:
        m = match_mod.match_job(job, profile)
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
        resume_core = payload_mod.build_resume_input(job, m["variant_obj"], profile, contact_public)
        imp_url = payload_mod.import_url(resume_core, app_url, encoding)
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
            "variant_label": m.get("matched_variant"),
            "title_keywords": m.get("title_keywords", []),
            "ats_score": g["ats_score"],
            "best_variant": g["best_variant"],
            "missing_keywords": g["missing_keywords"],
            "requested_keywords": g["requested_keywords"],
            "per_variant": g["per_variant"],
            "jd_available": g["jd_available"],
            "import_url": imp_url,
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

    public_jobs.sort(key=lambda j: (j["fit_score"], j["ats_score"]), reverse=True)

    # Aggregate "what am I missing" analysis across all jobs.
    best = max(public_jobs, key=lambda j: j["ats_score"], default=None)
    doc = {
        "generated_at": now,
        "meta": {
            "job_count": len(public_jobs),
            "app_url": app_url,
            "min_fit_score": cfg["match"]["min_fit_score"],
        },
        "summary": {
            "best_match": ({
                "ats_score": best["ats_score"],
                "variant": best["best_variant"],
                "company": best["company"],
                "title": best["title"],
                "job_id": best["id"],
            } if best else None),
            "missing_leaderboard": [
                {"keyword": k, "count": c} for k, c in missing_counter.most_common(20)
            ],
        },
        "jobs": public_jobs,
    }

    pub_path = (REPO_ROOT / cfg["output"]["public_json"]).resolve()
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the dashboard data files.")
    ap.add_argument("--public", action="store_true", help="public jobs.json only (Actions mode)")
    ap.add_argument("--no-discovery", action="store_true", help="skip discovery + ATS fetch")
    ap.add_argument("--min-fit", type=int, default=None)
    args = ap.parse_args()
    cfg = load_config()
    if args.min_fit is not None:
        cfg["match"]["min_fit_score"] = args.min_fit
    run(cfg, do_discovery=not args.no_discovery, public_only=args.public)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
