#!/usr/bin/env python3
"""Email alerts for newly-found jobs — run by GitHub Actions after the worker.

Compares the freshly-built docs/jobs.json against docs/seen.json (the ids we've
already alerted on) and emails a summary of new, high-fit jobs via Gmail.

Credentials come from GitHub Actions **Secrets** (never committed):
    GMAIL_USER   / GMAIL_USERNAME   your gmail address
    GMAIL_APP_PASSWORD / GMAIL_PASS a Gmail App Password (not your login password)
    ALERT_TO                        where to send alerts (defaults to the gmail user)

With no credentials present it prints what it *would* send and exits 0, so local
runs and forks don't fail.
"""

import json
import os
import re
import smtplib
import ssl
import unicodedata
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "docs" / "jobs.json"
SEEN = ROOT / "docs" / "seen.json"
MIN_FIT = 55


def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return default


# ---- dedup identity -----------------------------------------------------------
# We alert on a normalized, LOCATION-AGNOSTIC signature (company + title) rather
# than the raw per-(company,title,location) id. That way a repost, a re-listing at
# a different location, or a punctuation/casing/work-mode tweak of a role you were
# already alerted on does NOT generate a fresh alert.
def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


_COMPANY_SUFFIX = re.compile(
    r"[,\.]?\s+(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|"
    r"technologies|technology|labs|software|group|holdings|global)\.?$", re.I)

# Words that vary between repostings of the SAME role (location / work-mode /
# employment-type noise) — dropped from the title so they don't defeat dedup.
_TITLE_NOISE = {
    "remote", "hybrid", "onsite", "on", "site", "us", "usa", "united", "states",
    "u", "s", "fulltime", "full", "part", "time", "contract", "temporary", "temp",
    "the", "a", "an",
}


def _norm_company(name: str) -> str:
    s = _strip_accents((name or "").lower()).strip()
    prev = None
    while prev != s:            # strip stacked suffixes ("Acme Labs, Inc.")
        prev, s = s, _COMPANY_SUFFIX.sub("", s).strip()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _norm_title(title: str) -> str:
    t = _strip_accents((title or "").lower())
    t = re.sub(r"\([^)]*\)", " ", t)          # drop "(Remote)", "(New York)", etc.
    t = re.sub(r"[^a-z0-9]+", " ", t)          # punctuation -> space
    return " ".join(w for w in t.split() if w not in _TITLE_NOISE).strip()


def job_sig(job: dict) -> str:
    return _norm_company(job.get("company", "")) + "|" + _norm_title(job.get("title", ""))


def main() -> int:
    doc = load_json(JOBS, {"jobs": []})
    jobs_all = doc.get("jobs", [])
    seen = set(load_json(SEEN, []))
    cfg = load_json(ROOT / "config.json", {})
    alerts = cfg.get("alerts", {}) if isinstance(cfg, dict) else {}
    MIN = alerts.get("min_fit_for_alert", MIN_FIT)

    # One-time migration: seen.json used to store raw ids (no "|"). Switching to
    # signatures would make every current job look unseen and flood a huge email,
    # so seed the seen-set with EVERY currently-populated job — nothing already on
    # the dashboard or in a prior email gets re-tagged — and send nothing this run.
    if seen and not any("|" in s for s in seen):
        seeded = sorted({job_sig(j) for j in jobs_all})
        SEEN.write_text(json.dumps(seeded, indent=0))
        print(f"notify: migrated dedup to signatures — seeded {len(seeded)} "
              "already-populated job(s); no alerts sent this run")
        return 0

    # "Send digest now" (mode=digest) force-sends the current top picks even if
    # they were already alerted; otherwise only genuinely-new signatures go out.
    force = os.environ.get("HUNT_MODE", "").strip() == "digest"
    if force:
        cand = [j for j in jobs_all if j.get("fit_score", 0) >= MIN]
    else:
        cand = [j for j in jobs_all
                if job_sig(j) not in seen and j.get("fit_score", 0) >= MIN]

    # Strict per-signature dedup within the batch: keep only the highest-fit
    # instance of each signature, so one email never lists the same role twice.
    fresh, picked = [], set()
    for j in sorted(cand, key=lambda j: j.get("fit_score", 0), reverse=True):
        s = job_sig(j)
        if s in picked:
            continue
        picked.add(s)
        fresh.append(j)
    if force:
        fresh = fresh[:8]
    if not fresh:
        print("notify: no new jobs above the alert threshold")
        return 0

    # Cap the email to the strongest matches (all `fresh` are still marked seen below,
    # so a big backlog never floods a single email or re-alerts next run).
    MAX_IN_EMAIL = 25
    show = sorted(fresh, key=lambda j: j.get("fit_score", 0), reverse=True)[:MAX_IN_EMAIL]
    header = f"Job Hunter found {len(fresh)} new match(es)"
    if len(fresh) > len(show):
        header += f" (top {len(show)} shown)"
    lines = [header + ":\n"]
    for j in show:
        lines.append(f"• {j['company']} — {j['title']}")
        lines.append(f"    fit {j['fit_score']} · ATS {j['ats_score']}% ({j.get('best_variant','')})")
        if j.get("missing_keywords"):
            lines.append(f"    missing: {', '.join(j['missing_keywords'][:8])}")
        lines.append(f"    apply: {j.get('url','')}")
        lines.append("")
    body = "\n".join(lines)

    user = os.environ.get("GMAIL_USER") or os.environ.get("GMAIL_USERNAME")
    pw = os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get("GMAIL_PASS")
    # Recipients: env (secrets) win over config.json; a string may list several
    # addresses separated by comma/semicolon/space.
    def addrs(val):
        if not val:
            return []
        if isinstance(val, list):
            return [a.strip() for a in val if a and a.strip()]
        return [a.strip() for a in re.split(r"[,;\s]+", val) if a.strip()]

    to_list = addrs(os.environ.get("ALERT_TO") or alerts.get("to")) or ([user] if user else [])
    cc_list = addrs(os.environ.get("ALERT_CC") or alerts.get("cc"))

    if not (user and pw and to_list):
        print("notify: no Gmail credentials set — would have sent to "
              f"{', '.join(to_list) or '(nobody)'}"
              f"{' cc ' + ', '.join(cc_list) if cc_list else ''}:\n")
        print(body)
    else:
        msg = EmailMessage()
        msg["Subject"] = f"🎯 Job Hunter: {len(fresh)} new match(es)"
        msg["From"] = user
        msg["To"] = ", ".join(to_list)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(user, pw)
            s.send_message(msg)  # delivers to To + Cc automatically
        print(f"notify: emailed {len(fresh)} job(s) to {', '.join(to_list)}"
              f"{' (cc ' + ', '.join(cc_list) + ')' if cc_list else ''}")

    # Mark these signatures as alerted so we don't re-send next run.
    seen.update(job_sig(j) for j in fresh)
    SEEN.write_text(json.dumps(sorted(seen), indent=0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
