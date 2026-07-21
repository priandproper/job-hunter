#!/usr/bin/env python3
"""Scan your job-application Gmail inbox and PROPOSE status updates for the app.

Runs entirely on your machine. It reads the applying inbox over IMAP, classifies
messages (application received / interview / rejection / offer), matches each to a
tracked job in docs/jobs.json, and writes a git-ignored one-off script:

    scripts/inbox.local.js

Open the dashboard, hit "Run script", load that file — the detected changes show up
as *suggestions* you Accept or Dismiss. Nothing is applied automatically, and nothing
leaves your machine or touches the public repo.

Credentials (env var OR .secrets.json, both git-ignored):
    IMAP_USER            the applying gmail address (defaults to your contact email)
    IMAP_APP_PASSWORD    a Gmail App Password with IMAP enabled (NOT your login password)

Usage:
    python3 scripts/inbox_scan.py               # scan last 30 days -> inbox.local.js
    python3 scripts/inbox_scan.py --days 60
    python3 scripts/inbox_scan.py --print       # also print the proposals as JSON
    python3 scripts/inbox_scan.py --demo        # no IMAP; run the classifier on samples
"""

import argparse
import email
import imaplib
import json
import re
import sys
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime, parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.secrets import get_key  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "docs" / "jobs.json"
SECRETS = ROOT / ".secrets.json"
CONTACT = ROOT / "data" / "contact.local.json"
OUT = ROOT / "scripts" / "inbox.local.js"
INBOX_KEY = "job-hunter:inbox:v1"
MAIL_KEY = "job-hunter:mail:v1"

# Applicant-tracking-system senders: the company is in the subject/body, not the domain.
ATS_DOMAINS = {
    "greenhouse.io", "greenhouse-mail.io", "us.greenhouse-mail.io", "lever.co",
    "hire.lever.co", "myworkday.com", "myworkdayjobs.com", "ashbyhq.com",
    "smartrecruiters.com", "icims.com", "jobvite.com", "taleo.net", "successfactors.com",
    "workable.com", "breezy.hr", "bamboohr.com",
}

# Ordered strongest-signal-first, and the ORDER is load-bearing. A rejection is
# definitive. A GENUINE interview invite sits ABOVE the "applied" receipt so a real
# invite that politely opens with "thanks for applying" is still an interview. The
# SOFT interview hints sit BELOW "applied" so an automated application receipt — which
# routinely carries boilerplate like "next steps" or "we'll schedule a conversation" —
# is not mistaken for an interview.
# Each rule: (status, confidence, [regexes]) — matched against subject + body.
RULES = [
    ("rejected", 0.9, [
        r"we('| ha)ve decided (to|not)", r"not (be )?(moving|proceeding|advancing) forward",
        r"move forward with other candidates", r"will not be moving forward",
        r"unfortunately,? (we|after)", r"position has been filled",
        r"pursuing other candidates", r"not (be )?selected for", r"regret to inform",
    ]),
    ("offer", 0.75, [
        r"pleased to (extend|offer)", r"offer of employment", r"we('| a)re excited to offer",
        r"your (job )?offer", r"formal offer",
    ]),
    # STRONG interview signals: a real person inviting THIS candidate to interview.
    ("interview", 0.85, [
        r"invite you (to|for) (an? )?(interview|phone screen|conversation|call)",
        r"we('| woul)d (like|love) to (interview|meet|speak) (with )?you",
        r"interview (invitation|request)",
        r"(please |kindly )?(share|send|provide|confirm|let us know) your availability",
        r"(phone|video|onsite|technical) (screen|interview) (is|has been) scheduled",
        r"(schedule|set up|book) (your|a|an) (interview|phone screen)",
        r"book a time (with|on|using)", r"calendly\.com",
    ]),
    # Application RECEIPT (automated). Definitive — beats the soft interview hints below.
    ("applied", 0.6, [
        r"thank(s| you) for (applying|your (application|interest))",
        r"we('| ha)ve received your application", r"application (has been )?received",
        r"successfully (applied|submitted)", r"received your application",
        r"your application (to|for|has been)",
    ]),
    # SOFT interview hints — only reached when the email is NOT an application receipt.
    ("interview", 0.65, [
        r"(schedule|set up|book|arrange) (a|your|some) (time|call|chat|conversation)",
        r"like to (speak|chat|connect)", r"phone screen",
        r"next (round|step|stage)", r"availability (for|this)", r"move you (forward|to the next)",
    ]),
]

SUFFIX = re.compile(r"[,\.]?\s+(inc|llc|ltd|corp|co|company|technologies|technology|labs|"
                    r"software|group|holdings|global)\.?$", re.I)


def norm_company(name: str) -> str:
    s = (name or "").lower().strip()
    s = SUFFIX.sub("", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def decode(s) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def body_text(msg) -> str:
    """Best-effort plain-text body (falls back to crudely stripped HTML)."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                parts.append(_payload(part))
        if not parts:  # no plain part — degrade from HTML
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    parts.append(re.sub(r"<[^>]+>", " ", _payload(part)))
    else:
        raw = _payload(msg)
        parts.append(re.sub(r"<[^>]+>", " ", raw) if msg.get_content_type() == "text/html" else raw)
    return re.sub(r"\s+", " ", " ".join(parts))[:4000]


def _payload(part) -> str:
    try:
        return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""


def classify(subject: str, body: str):
    hay = f"{subject}\n{body}".lower()
    for status, conf, pats in RULES:
        if any(re.search(p, hay) for p in pats):
            return status, conf
    return None, 0.0


def match_job(from_name: str, from_domain: str, subject: str, body: str, jobs):
    """Find the tracked job this email is about, using the known company set."""
    domain_word = from_domain.split(".")[-2] if from_domain.count(".") >= 1 else from_domain
    is_ats = from_domain in ATS_DOMAINS or any(from_domain.endswith("." + d) for d in ATS_DOMAINS)
    hay = f"{from_name} {subject} {body[:1200]}".lower()
    best, best_score = None, 0
    for j in jobs:
        cn = norm_company(j.get("company", ""))
        if not cn:
            continue
        score = 0
        if not is_ats and cn.replace(" ", "") == norm_company(domain_word).replace(" ", ""):
            score += 5                                    # sender domain IS the company
        if cn in hay:
            score += 3                                    # company name appears in the text
        if score:
            # tie-break toward the role whose title words show up in the email
            title_words = [w for w in re.findall(r"[a-z]+", j.get("title", "").lower()) if len(w) > 3]
            score += sum(1 for w in set(title_words) if w in hay) * 0.1
        if score > best_score:
            best, best_score = j, score
    return best if best_score >= 3 else None


def scan_imap(user: str, pw: str, days: int):
    M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    M.login(user, pw)
    M.select("INBOX")
    typ, data = M.search(None, f'(SINCE {_since(days)})')
    ids = data[0].split() if typ == "OK" and data and data[0] else []
    out = []
    for num in ids:
        typ, msg_data = M.fetch(num, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        out.append(msg)
    M.close()
    M.logout()
    return out


def _since(days: int) -> str:
    # IMAP wants DD-Mon-YYYY; compute without importing datetime.now at module load.
    from datetime import datetime, timedelta
    return (datetime.utcnow() - timedelta(days=days)).strftime("%d-%b-%Y")


def messages_to_proposals(msgs, jobs):
    by_job = {}
    for msg in msgs:
        subject = decode(msg.get("Subject"))
        from_name, from_addr = parseaddr(decode(msg.get("From")))
        from_domain = from_addr.split("@")[-1].lower() if "@" in from_addr else ""
        body = body_text(msg)
        status, conf = classify(subject, body)
        if not status:
            continue
        job = match_job(from_name, from_domain, subject, body, jobs)
        if not job:
            continue
        try:
            ts = parsedate_to_datetime(msg.get("Date")).astimezone().isoformat()
        except Exception:
            ts = ""
        cand = {"id": job["id"], "company": job.get("company", ""), "title": job.get("title", ""),
                "detected": status, "confidence": conf, "email_ts": ts,
                "subject": subject[:160], "from": from_addr}
        prev = by_job.get(job["id"])
        # keep the strongest signal per job; tie-break on newer email
        if not prev or conf > prev["confidence"] or (conf == prev["confidence"] and ts > prev["email_ts"]):
            by_job[job["id"]] = cand
    return sorted(by_job.values(), key=lambda p: p["email_ts"], reverse=True)


def write_inject(proposals):
    payload = json.dumps(proposals)
    OUT.write_text(
        "// One-off: open your dashboard, hit \"Run script\", load this file. It stages\n"
        "// inbox-detected status changes as suggestions to Accept/Dismiss. Applies nothing\n"
        "// on its own. Git-ignored — never committed.\n"
        f"var P = {payload};\n"
        f"localStorage.setItem('{INBOX_KEY}', JSON.stringify(P));\n"
        "// also record a persistent per-job signal the single-job page reads:\n"
        "(function(){try{var log=JSON.parse(localStorage.getItem('" + MAIL_KEY + "'))||{};"
        "P.forEach(function(p){log[p.id]={detected:p.detected,email_ts:p.email_ts,subject:p.subject,from:p.from};});"
        "localStorage.setItem('" + MAIL_KEY + "',JSON.stringify(log));}catch(e){}})();\n"
        "location.reload();\n"
    )


DEMO = [
    ("no-reply@greenhouse.io", "Datadog Recruiting",
     "Thank you for applying to Datadog",
     "Hi Priyanka, we've received your application for Senior Product Marketing Manager and our team is reviewing it."),
    ("careers@stripe.com", "Stripe",
     "Update on your Stripe application",
     "Thank you for your interest. After careful consideration we have decided not to move forward with your application at this time."),
    ("recruiting@notion.so", "Notion Talent",
     "Next steps — Product Marketing at Notion",
     "We'd love to invite you to interview. Could you share your availability for a phone screen next week?"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--print", dest="show", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    doc = json.loads(JOBS.read_text()) if JOBS.exists() else {"jobs": []}
    jobs = doc.get("jobs", [])

    if args.demo:
        msgs = []
        for addr, name, subj, body in DEMO:
            m = EmailMessage()
            m["From"] = f"{name} <{addr}>"
            m["Subject"] = subj
            m["Date"] = "Fri, 18 Jul 2026 10:00:00 -0400"
            m.set_content(body)
            msgs.append(m)
    else:
        user = get_key("IMAP_USER", SECRETS) or _contact_email()
        pw = get_key("IMAP_APP_PASSWORD", SECRETS)
        if not (user and pw):
            print("inbox_scan: set IMAP_USER + IMAP_APP_PASSWORD (env or .secrets.json).\n"
                  "  IMAP_APP_PASSWORD must be a Gmail App Password with IMAP enabled —\n"
                  "  not your normal password. See https://myaccount.google.com/apppasswords",
                  file=sys.stderr)
            return 2
        try:
            msgs = scan_imap(user, pw, args.days)
        except imaplib.IMAP4.error as e:
            print(f"inbox_scan: IMAP login/scan failed: {e}", file=sys.stderr)
            return 1
        print(f"inbox_scan: scanned {len(msgs)} message(s) from the last {args.days} days")

    proposals = messages_to_proposals(msgs, jobs)
    write_inject(proposals)
    print(f"inbox_scan: {len(proposals)} proposal(s) -> {OUT.relative_to(ROOT)}")
    for p in proposals:
        print(f"  • {p['company']} — {p['detected']}  ({p['subject']})")
    if args.show:
        print(json.dumps(proposals, indent=2))
    return 0


def _contact_email():
    try:
        return json.loads(CONTACT.read_text()).get("email")
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
