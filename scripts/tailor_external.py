#!/usr/bin/env python3
"""Tailor your resume to an EXTERNAL job — one that ISN'T in the scan (a referral, a
link a friend sent, any posting). Same engine as tailor_resume.py, but the job comes
from YOU instead of jobs.json: you supply the base resume and paste the job description.

  # JD in a file:
  python3 scripts/tailor_external.py --resume data/master-analyst.json \
      --company "AWS" --title "Sales Compensation Systems Analyst" --jd-file jd.txt

  # or pipe the JD straight from your clipboard (macOS):
  pbpaste | python3 scripts/tailor_external.py --resume data/master-analyst.json \
      --company AWS --title "Sales Comp Systems Analyst"

  # steer emphasis (same as tailor_resume.py):
  python3 scripts/tailor_external.py --resume data/master-gtm.json --company Datadog \
      --title "GTM Manager" --jd-file jd.txt -p "lead with pipeline ownership and forecasting"

Writes data/tailored.<company>-<title>.json and prints a resume-builder import URL
(open it and the resume loads into the builder). The FROZEN facts (contact, employers,
titles, dates, education) are re-applied from your real resume, so nothing can drift.
Pick the MASTER that matches the role's track — the tailoring sharpens language, it does
not manufacture positioning your base resume doesn't already have.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import tailor_resume as tr  # noqa: E402  (reuse the whole engine)


def read_jd(args) -> str:
    if args.jd:
        return args.jd
    if args.jd_file:
        return Path(args.jd_file).read_text()
    if not sys.stdin.isatty():           # JD piped in (e.g. `pbpaste | ...`)
        return sys.stdin.read()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", required=True,
                    help="base resume JSON (your track master, in builder shape — e.g. Export JSON)")
    ap.add_argument("--company", required=True, help="target company (used for the label)")
    ap.add_argument("--title", default="", help="target job title (used for the label)")
    ap.add_argument("--jd-file", dest="jd_file", default="", help="file containing the job description")
    ap.add_argument("--jd", default="", help="job description inline")
    ap.add_argument("-p", "--prompt", dest="extra", default="",
                    help="extra freeform steering (emphasis/angle only; never overrides truthfulness)")
    ap.add_argument("extra_words", nargs="*", help="trailing words are also treated as steering")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--out", default="", help="output JSON file (default data/tailored.<slug>.json)")
    args = ap.parse_args()

    if not shutil.which("claude"):
        print("tailor_external: the `claude` CLI is required (and you must be logged in).")
        return 2

    jd = read_jd(args).strip()
    if not jd:
        print("tailor_external: no job description. Pass --jd-file <f>, --jd \"...\", or pipe it via stdin.")
        return 2

    try:
        base = json.loads(Path(args.resume).read_text())
    except Exception as e:
        print(f"tailor_external: couldn't read --resume ({e}).")
        return 1
    if not base.get("experience"):
        print("tailor_external: base resume has no experience — is --resume a builder-shape resume JSON?")
        return 1

    # A synthetic 'job' so we can reuse tailor_resume's prompt + enforcement verbatim.
    job = {
        "title": args.title,
        "company": args.company,
        "excerpt": jd,
        "requested_keywords": [],
        "missing_keywords": [],
    }

    extra = " ".join(x for x in ([args.extra] + list(args.extra_words)) if x).strip()
    print(f"Tailoring for: {args.company} — {args.title or '(title from JD)'}")
    if extra:
        print(f"  steering: {extra}")
    print(f"  drafting with Claude CLI ({args.model})…")

    prompt = tr.build_prompt(job, base, extra)
    core, warns, fails = None, [], []
    for attempt in range(2):
        p = prompt if not fails else (
            prompt + "\n\nYour previous draft broke these HARD REQUIREMENTS. Fix ALL of them "
            "(truthfully, no fabrication) and return the full JSON again:\n- " + "\n- ".join(fails))
        try:
            raw = tr.claude_json(p, args.model)
        except Exception as e:
            print(f"tailor_external: Claude failed ({e})")
            return 1
        if not (raw.get("contact") is not None and raw.get("experience")):
            print("tailor_external: model output didn't look like a resume — try again.")
            return 1
        core, warns = tr.enforce_frozen(base, raw, job)
        core["summary"] = tr._trim_summary(core.get("summary", ""))
        fails = tr._requirement_failures(core)
        if not fails:
            break
        if attempt == 0:
            print(f"  re-drafting to meet requirements ({'; '.join(fails)})…")
    warns += fails

    # Give each external tailor a UNIQUE, stable id. URL-import (#import=) preserves the
    # id, so without this every tailor from the same master would share the master's id
    # and overwrite each other in the builder. Deterministic per company+title means
    # re-running the same role updates its own resume instead of duplicating it.
    core["id"] = "res_ext_" + (tr._slug(args.company) + "-" + tr._slug(args.title)).strip("-")

    out = Path(args.out) if args.out else (
        ROOT / "data" / f"tailored.{tr._slug(args.company)}-{tr._slug(args.title)}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(core, indent=2))
    rel = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out

    url = tr.import_url(core)
    clipped = tr._clip(url)
    print(f"\n✓ Tailored resume written -> {rel}")
    for w in warns:
        print(f"  ⚠ {w}")
    print("\n✓ Import link copied to your clipboard — paste it in your browser:\n"
          if clipped else "\nOpen this URL to load the resume into the builder:\n")
    print(url + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
