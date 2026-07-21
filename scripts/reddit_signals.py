#!/usr/bin/env python3
"""Scan Reddit for hiring / referral signals and tie them back to your tracked jobs.

Reddit is the one platform with a usable free read API, so — unlike the paste-based
LinkedIn/X/community adapters — this runs on its own. It searches the subreddits you care
about (r/ProductMarketing, r/analytics, …) for "who's hiring" / referral posts, links each
to a matching job in your local jobs.json, and prints a digest. With --people it also adds
the posters (potential referrers) to a People CSV you import on the dashboard.

  python3 scripts/reddit_signals.py                       # digest to the terminal + data/reddit_signals.md
  python3 scripts/reddit_signals.py --people              # also -> data/people.local.csv (import on People)
  python3 scripts/reddit_signals.py --days 7 --limit 40   # tighter window, more results

Config (optional) in config.json under "reddit": { "subreddits": [...], "queries": [...],
"max_age_days": 30 }. Reads are unauthenticated (public .json) with a descriptive User-Agent;
if Reddit rate-limits/blocks the request it degrades gracefully. Stdlib only.
"""

import argparse
import csv
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib import secrets as secrets_mod  # noqa: E402

CONFIG = ROOT / "config.json"
SECRETS = ROOT / ".secrets.json"
LOCAL_JOBS = ROOT / "docs" / "jobs.json"
DIGEST_OUT = ROOT / "data" / "reddit_signals.md"
PEOPLE_OUT = ROOT / "data" / "people.local.csv"
COLUMNS = ["name", "title", "company", "past_companies", "seniority", "location",
           "linkedin", "email", "relationship", "tags", "how_known", "notes"]

DEFAULT_SUBREDDITS = ["ProductMarketing", "marketing", "analytics", "BusinessIntelligence",
                      "jobbit", "referrals", "recruitinghell"]
DEFAULT_QUERIES = ["hiring", "we're hiring", "referral", "product marketing", "marketing analyst",
                   "business analyst"]
# A post is a signal only if it reads like an offer to hire / refer (not someone asking).
_SIGNAL_RE = re.compile(r"\b(hiring|we['’]re hiring|open (role|req|position)|join (our|the) team|"
                        r"refer(ral)?|can refer|happy to refer|DM me|reach out|now hiring|"
                        r"looking for a|team is growing|opening for)\b", re.I)
_UA = "job-hunter/1.0 (personal job-search helper; contact: local user)"


def _load_cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text()).get("reddit", {}) or {}
    except Exception:
        return {}


def _urlopen(url: str, timeout: int = 20):
    """GET with a descriptive UA; fall back to unverified TLS (macOS local cert issue)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLError) or "CERTIFICATE" in str(e).upper():
            return urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context())
        raise


def reddit_token() -> str:
    """Free Reddit OAuth (app-only). Needs a 'script' app's creds in .secrets.json:
    REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET (register at https://www.reddit.com/prefs/apps).
    Returns a bearer token, or '' if no creds / it fails (then we try the public JSON)."""
    cid = secrets_mod.get_key("REDDIT_CLIENT_ID", SECRETS)
    secret = secrets_mod.get_key("REDDIT_CLIENT_SECRET", SECRETS)
    if not cid or not secret:
        return ""
    import base64
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request("https://www.reddit.com/api/v1/access_token", data=body,
                                 headers={"User-Agent": _UA, "Authorization": "Basic " + auth})
    try:
        try:
            r = urllib.request.urlopen(req, timeout=20)
        except urllib.error.URLError:
            r = urllib.request.urlopen(req, timeout=20, context=ssl._create_unverified_context())
        with r:
            return json.loads(r.read().decode("utf-8", "replace")).get("access_token", "") or ""
    except Exception:
        return ""


def _search(subreddit: str, query: str, days: int, limit: int, token: str = "") -> list:
    params = urllib.parse.urlencode({
        "q": query, "restrict_sr": 1, "sort": "new",
        "t": "week" if days <= 7 else "month", "limit": limit})
    if token:
        url = f"https://oauth.reddit.com/r/{subreddit}/search?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Authorization": "bearer " + token})
        try:
            resp = urllib.request.urlopen(req, timeout=20)
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), ssl.SSLError) or "CERTIFICATE" in str(e).upper():
                resp = urllib.request.urlopen(req, timeout=20, context=ssl._create_unverified_context())
            else:
                raise
        with resp as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return _parse_listing(data, subreddit, days)
    url = f"https://www.reddit.com/r/{subreddit}/search.json?{params}"
    with _urlopen(url) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return _parse_listing(data, subreddit, days)


def _parse_listing(data: dict, subreddit: str, days: int) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    out = []
    for child in (data.get("data", {}) or {}).get("children", []):
        p = child.get("data", {}) or {}
        if (p.get("created_utc") or 0) < cutoff:
            continue
        out.append({
            "subreddit": subreddit,
            "title": p.get("title", "") or "",
            "body": (p.get("selftext", "") or "")[:1200],
            "author": p.get("author", "") or "",
            "permalink": "https://www.reddit.com" + (p.get("permalink", "") or ""),
            "created": p.get("created_utc") or 0,
            "score": p.get("score") or 0,
        })
    return out


def _load_jobs(live: bool) -> list:
    if live:
        try:
            cfg = json.loads(CONFIG.read_text())
            base = cfg.get("resume_builder", {}).get("app_url", "").rsplit("/", 1)[0] or ""
            url = "https://priandproper.github.io/job-hunter/jobs.json"
            with _urlopen(url) as r:
                return json.loads(r.read().decode("utf-8", "replace")).get("jobs", [])
        except Exception:
            pass
    try:
        return json.loads(LOCAL_JOBS.read_text()).get("jobs", [])
    except Exception:
        return []


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _match_jobs(text: str, jobs: list) -> list:
    """Company names from your tracked jobs that appear in the post."""
    hay = _norm(text)
    hits = []
    seen = set()
    for j in jobs:
        c = _norm(j.get("company", ""))
        if c and len(c) >= 3 and c in hay and c not in seen:
            seen.add(c)
            hits.append(j.get("company", ""))
    return hits


def _is_signal(post: dict) -> bool:
    return bool(_SIGNAL_RE.search(post["title"] + "\n" + post["body"]))


def main() -> int:
    cfg = _load_cfg()
    ap = argparse.ArgumentParser()
    ap.add_argument("--subreddits", nargs="*", default=cfg.get("subreddits") or DEFAULT_SUBREDDITS)
    ap.add_argument("--queries", nargs="*", default=cfg.get("queries") or DEFAULT_QUERIES)
    ap.add_argument("--days", type=int, default=int(cfg.get("max_age_days", 30)))
    ap.add_argument("--limit", type=int, default=25, help="results per subreddit×query")
    ap.add_argument("--people", action="store_true", help="also add posters to a People CSV")
    ap.add_argument("--out", default=str(PEOPLE_OUT), help="People CSV path (with --people)")
    ap.add_argument("--live", action="store_true", help="match against the deployed jobs.json")
    args = ap.parse_args()

    jobs = _load_jobs(args.live)
    token = reddit_token()
    print(f"reddit_signals: scanning {len(args.subreddits)} subreddit(s) × {len(args.queries)} "
          f"query(ies), last {args.days}d, against {len(jobs)} tracked job(s)"
          f"{' [OAuth]' if token else ' [public]'}…")

    seen_ids, signals, blocked = set(), [], 0
    for sub in args.subreddits:
        for q in args.queries:
            try:
                posts = _search(sub, q, args.days, args.limit, token)
            except Exception as e:
                blocked += 1
                print(f"  · r/{sub} '{q}': {e}")
                continue
            for p in posts:
                key = p["permalink"]
                if key in seen_ids or not _is_signal(p):
                    continue
                seen_ids.add(key)
                p["matched"] = _match_jobs(p["title"] + " " + p["body"], jobs)
                signals.append(p)

    if blocked and not signals:
        if token:
            print("reddit_signals: every request failed even with OAuth — try again shortly (rate limit) "
                  "or check your Reddit app creds.")
        else:
            print("reddit_signals: every request was blocked (Reddit gates unauthenticated reads). "
                  "Enable the free API: create a 'script' app at https://www.reddit.com/prefs/apps, then "
                  "add REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET to .secrets.json and re-run.")
        return 1

    signals.sort(key=lambda s: (bool(s["matched"]), s["score"], s["created"]), reverse=True)
    print(f"\nreddit_signals: {len(signals)} hiring/referral signal(s) "
          f"({sum(1 for s in signals if s['matched'])} touch a company you're tracking)\n")

    lines = ["# Reddit hiring / referral signals",
             f"_scanned {', '.join('r/'+s for s in args.subreddits)} · last {args.days} days_\n"]
    for s in signals[:60]:
        tag = f"  ·  **matches: {', '.join(s['matched'])}**" if s["matched"] else ""
        who = f"u/{s['author']}" if s["author"] and s["author"] != "[deleted]" else "(deleted)"
        print(f"  • r/{s['subreddit']} — {s['title'][:80]}{tag}")
        print(f"      {who} · {s['permalink']}")
        lines.append(f"- **r/{s['subreddit']}** — {s['title']}{tag}\n  - {who} · {s['permalink']}")
    DIGEST_OUT.parent.mkdir(parents=True, exist_ok=True)
    DIGEST_OUT.write_text("\n".join(lines) + "\n")
    print(f"\nreddit_signals: digest -> {DIGEST_OUT.relative_to(ROOT)}")

    if args.people:
        rows, seen_auth = [], set()
        for s in signals:
            a = s["author"]
            if not a or a in ("[deleted]", "AutoModerator") or a in seen_auth:
                continue
            seen_auth.add(a)
            rows.append({
                "name": f"u/{a}", "title": "", "company": "; ".join(s["matched"]),
                "past_companies": "", "seniority": "", "location": "", "linkedin": "", "email": "",
                "relationship": "unknown", "tags": "reddit; " + ("referral" if s["matched"] else "signal"),
                "how_known": f"Reddit r/{s['subreddit']}: {s['permalink']}",
                "notes": s["title"][:180]})
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(rows)
        print(f"reddit_signals: {len(rows)} poster(s) -> {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
        print("Next: dashboard -> People -> Import CSV.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
