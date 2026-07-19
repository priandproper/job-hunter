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


def main() -> int:
    doc = load_json(JOBS, {"jobs": []})
    seen = set(load_json(SEEN, []))
    cfg = load_json(ROOT / "config.json", {})
    alerts = cfg.get("alerts", {}) if isinstance(cfg, dict) else {}
    MIN = alerts.get("min_fit_for_alert", MIN_FIT)

    # "Send digest now" (mode=digest) force-sends the current top picks even if
    # they were already alerted; otherwise only genuinely-new jobs go out.
    force = os.environ.get("HUNT_MODE", "").strip() == "digest"
    if force:
        fresh = sorted((j for j in doc.get("jobs", []) if j.get("fit_score", 0) >= MIN),
                       key=lambda j: j.get("fit_score", 0), reverse=True)[:8]
    else:
        fresh = [j for j in doc.get("jobs", [])
                 if j["id"] not in seen and j.get("fit_score", 0) >= MIN]
    if not fresh:
        print("notify: no new jobs above the alert threshold")
        return 0

    lines = [f"Job Hunter found {len(fresh)} new match(es):\n"]
    for j in fresh:
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

    # Mark these ids as alerted so we don't re-send next run.
    seen.update(j["id"] for j in fresh)
    SEEN.write_text(json.dumps(sorted(seen), indent=0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
