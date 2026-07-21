#!/usr/bin/env python3
"""Add Y Combinator companies to the scan list (a "YC company tool").

There is no clean, key-free YC *jobs* API (Work at a Startup hides its jobs behind a
secured, rotating Algolia key), but the official YC *companies* API is stable and
key-free. So this tool pairs it with the ATS adapters we already have:

  1. Pull YC companies from api.ycombinator.com (newest batches first, Active only).
  2. For each new one, find its public job board (Greenhouse / Lever / Ashby) by
     scanning the company website + a few careers paths.
  3. Add the scannable ones to data/companies.json, so the normal worker run fetches
     their marketing-lane roles via lib/ats.py (the global lane filter trims the rest).

Incremental: companies already in the list are skipped. Preview with --dry, then commit
data/companies.json. Stdlib only.

  python3 scripts/yc_discover.py --dry                   # preview (established cos, team>=40)
  python3 scripts/yc_discover.py                         # add them to data/companies.json
  python3 scripts/yc_discover.py --min-team 100 --pages 60   # cast a wider/bigger net
"""

import argparse
import json
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib import ats as ats_mod          # noqa: E402
from lib import discovery as disc_mod   # noqa: E402

COMPANIES = ROOT / "data" / "companies.json"
YC_API = "https://api.ycombinator.com/v0.1/companies?page={page}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) job-hunter/1.0"
_CTX = ssl._create_unverified_context()   # macOS Python often lacks a CA bundle
CAREERS_PATHS = ("", "/careers", "/jobs", "/company/careers", "/about/careers", "/careers/jobs")

# Board URLs we can actually fetch (Greenhouse / Lever / Ashby).
_BOARD_RE = re.compile(
    r"https?://(?:job-boards\.|boards\.)?greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9][a-z0-9\-]{1,40})"
    r"|https?://jobs\.lever\.co/([a-z0-9][a-z0-9\-]{1,40})"
    r"|https?://jobs\.ashbyhq\.com/([a-z0-9][a-z0-9\-]{1,40})", re.I)


def _fetch(url: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


def _detect_board(html: str):
    m = _BOARD_RE.search(html or "")
    if not m:
        return None
    g, l, a = m.group(1), m.group(2), m.group(3)
    if g and g.lower() not in ("embed", "job_board"):
        return ("greenhouse", g.lower())
    if l:
        return ("lever", l.lower())
    if a and a.lower() != "job":
        return ("ashby", a.lower())
    return None


def verify_board(ats: str, slug: str) -> bool:
    """Confirm the detected board is live (returns postings) — filters stale links
    (a company that moved off an ATS still links the old board on its site)."""
    urls = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
    }
    url = urls.get(ats)
    if not url:
        return False
    try:
        data = json.loads(_fetch(url))
        if ats == "lever":
            return isinstance(data, list) and len(data) > 0
        return len(data.get("jobs", []) or []) > 0
    except Exception:
        return False


def find_board(website: str):
    """Best-effort: find a company's Greenhouse/Lever/Ashby board from its site."""
    site = (website or "").rstrip("/")
    if not site:
        return None
    if not site.startswith("http"):
        site = "https://" + site
    for path in CAREERS_PATHS:
        try:
            board = _detect_board(_fetch(site + path))
            if board:
                return board
        except Exception:
            continue
    return None


def yc_companies(max_pages: int, batches, min_team: int):
    """Yield Active YC companies matching the batch/team-size filters. The API is
    newest-first and the newest batches are pre-launch (tiny teams, no ATS board), so
    we scan OLDEST-first — established companies (that have marketing roles + boards)
    live in the older batches. Cheap: only the YC API is hit here; the expensive board
    lookups happen on the yielded (established) companies only."""
    want = {b.strip().upper() for b in batches} if batches else None
    try:
        first = json.loads(_fetch(YC_API.format(page=1)))
    except Exception as e:
        print(f"yc_discover: YC API unreachable ({e})")
        return
    total = first.get("totalPages") or 1
    for page in list(range(total, 0, -1))[:max_pages]:      # oldest -> newer
        try:
            data = json.loads(_fetch(YC_API.format(page=page)))
        except Exception:
            continue
        for c in data.get("companies", []) or []:
            if (c.get("status") or "").lower() != "active":
                continue
            if want and (c.get("batch") or "").upper() not in want:
                continue
            if (c.get("teamSize") or 0) < min_team:
                continue
            yield c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=80, help="cap on how many companies to board-check (network)")
    ap.add_argument("--min-team", type=int, default=40, help="only established companies (min team size)")
    ap.add_argument("--pages", type=int, default=40, help="YC API pages to scan (25 companies each)")
    ap.add_argument("--batches", default="", help="comma list, e.g. S22,W22 (default: any)")
    ap.add_argument("--dry", action="store_true", help="preview only — don't write companies.json")
    ap.add_argument("--no-verify", action="store_true", help="skip the live-board check (faster, noisier)")
    ap.add_argument("--out", default=str(COMPANIES))
    args = ap.parse_args()

    comp_path = Path(args.out)
    companies = disc_mod.load_companies(comp_path)
    known_names = {(c.get("name") or "").strip().lower() for c in companies}
    known_slugs = {(c.get("ats"), c.get("slug")) for c in companies}
    batches = [b for b in args.batches.split(",") if b.strip()]

    print(f"yc_discover: scanning up to {args.pages} pages for Active YC companies "
          f"(team >= {args.min_team}"
          + (f", batches {', '.join(batches)}" if batches else "") + f"), board-checking up to {args.max}…")
    added, checked, skipped = [], 0, 0
    for c in yc_companies(args.pages, batches, args.min_team):
        name = (c.get("name") or "").strip()
        if not name or name.lower() in known_names:
            skipped += 1
            continue
        if checked >= args.max:
            break
        checked += 1
        board = find_board(c.get("website", ""))
        time.sleep(0.15)   # be polite to the sites
        if not board:
            continue
        ats, slug = board
        if (ats, slug) in known_slugs:
            skipped += 1
            continue
        if not args.no_verify and not verify_board(ats, slug):
            continue                       # stale link (e.g. moved off the ATS)
        locs = c.get("locations") or []
        entry = {
            "name": name, "ats": ats, "slug": slug,
            "h1b": None, "h1b_note": f"YC {c.get('batch', '')}".strip(),
            "hq": (locs[0] if locs else ""), "source": f"yc:{c.get('batch', '')}".rstrip(":"),
            "active": True,
        }
        companies.append(entry)
        known_names.add(name.lower())
        known_slugs.add((ats, slug))
        added.append(entry)
        print(f"  + {name:28.28s} [{c.get('batch','')}] -> {ats}/{slug}")

    print(f"\nyc_discover: checked {checked}, added {len(added)} scannable YC "
          f"compan{'y' if len(added)==1 else 'ies'} ({skipped} already known/dupes).")
    if added and not args.dry:
        disc_mod.save_companies(comp_path, companies)
        print(f"  wrote {comp_path.relative_to(ROOT) if comp_path.is_relative_to(ROOT) else comp_path}"
              " — commit it, then the next worker run scans them.")
    elif added:
        print("  (--dry: not written. Re-run without --dry to add them.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
