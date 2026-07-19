# job-hunter

A **static job-search dashboard** fed by a background worker. Every matching job
arrives with a tailored resume, an ATS keyword-gap analysis, a drafted referral
message, and who can refer you — all pre-built. You review and hit send.
**Nothing is applied or sent automatically.**

- **Front-end only** — a static page (`docs/index.html`) reading a JSON file. No
  database, no server. Runs on GitHub Pages or `python3 -m http.server`.
- **State + PII live in your browser** (`localStorage`), never in the repo.
- **A GitHub Actions cron** rebuilds the data every ~30 min and **emails you**
  about new high-fit jobs.

## The dashboard (`docs/index.html`)

Everything is on one page:

- **Top stats** — matching jobs · **best resume match** (top ATS score + which
  variant + which job) · # missing keywords · private-data status.
- **📊 What you're missing** — the keywords the job descriptions ask for that
  *none* of your resume variants cover, most-requested first. Your highest-leverage
  skills to add, aggregated across all jobs (a data-driven gap analysis from the
  JDs themselves — not from scraping anyone).
- **Per job** — an **ATS-match %** and **fit** score, the **missing keywords** for
  that role, **Open resume in builder →** (loads the tailored variant into
  `priandproper.github.io/resume-builder`), the **referral message** (Copy button;
  click *use* by a contact to personalize), **who can refer you**, a **status**
  (sticky in localStorage), and **Apply ↗**.

## How the data is built (`worker.py`)

1. **Discover** — grows the H-1B-sponsoring company list from job boards (JSearch)
   + public DOL LCA data. *LinkedIn is not scraped — ToS-blocked and flags accounts.*
2. **Ingest** — jobs from discovered companies' Greenhouse/Lever/Ashby feeds (and,
   locally, your existing scanner/tracker).
3. **Match** — fit score + best resume variant.
4. **Gap** — for each job, scores each resume variant against the **job description**,
   picks the best, and lists the missing keywords (`lib/gap.py`).
5. **Prepare** — resume import URL (base64) + a drafted referral message.
6. **Persist** — writes two files:
   - **`docs/jobs.json`** — PUBLIC, committed. Job data + gap analysis + resume URLs
     + generic message. **No third-party PII.**
   - **`data/private.local.json`** — PRIVATE, git-ignored. In-network / Apollo
     referral contacts from your LinkedIn export. Loaded into the browser locally.

## Your private data (PII) never leaves your machine

Nothing sensitive is ever committed. The public `docs/jobs.json` contains resume
*content* but **no email, no phone, and no referrer names**. Those live only in
git-ignored local files (`data/contact.local.json`, `data/private.local.json`) and
get into the dashboard two ways, both browser-local:

- **In-app script runner (primary).** Each `worker.py` run regenerates
  `scripts/inject.local.js` (git-ignored) with your PII baked in. On the dashboard
  click **▶ Run one-off script**, paste the file's contents, and hit **Run script** —
  no dev tools needed. It writes your PII to `localStorage` on that browser only,
  nothing is uploaded. (The same modal has a **Load from file…** shortcut.)

Once loaded, the dashboard merges your email/phone into each resume link **in the
browser** when you click *Open resume in builder* — so the full resume opens, but
your contact PII was never in the committed/public data. Statuses/notes also live
in `localStorage`.

## Sync statuses from your application inbox (`scripts/inbox_scan.py`)

Keep job statuses in step with your email — locally, never in the cloud. The scanner
reads your applying Gmail over IMAP, classifies messages (application received /
interview / rejection / offer), matches each to a tracked job, and writes a
git-ignored `scripts/inbox.local.js`:

```bash
# creds via env or .secrets.json: IMAP_USER + IMAP_APP_PASSWORD (a Gmail App Password)
python3 scripts/inbox_scan.py            # scan last 30 days
python3 scripts/inbox_scan.py --demo     # try the classifier on built-in samples
```

Load the generated file through the dashboard's **Run script** modal. Detected
changes appear on the Cockpit as **Inbox updates** you Accept or Dismiss — nothing is
applied automatically, and a status you set by hand is never silently overwritten.

## Setup

```bash
# 1. Your contact PII (kept out of the repo — used only in your browser):
cp data/contact.example.json data/contact.local.json    # then edit email/phone

# 2. In-network referrals: LinkedIn -> Settings -> Data Privacy ->
#    Get a copy of your data -> Connections. Save the CSV:
cp ~/Downloads/Connections.csv data/connections.csv

# 3. (Optional) richer discovery + named out-of-network people:
#    echo '{"JSEARCH_API_KEY":"...","APOLLO_API_KEY":"..."}' > .secrets.json

# 4. Build the data and view it locally:
python3 worker.py                       # writes docs/jobs.json (public) + local PII files
cd docs && python3 -m http.server 8777  # open http://localhost:8777
#    then paste scripts/inject.local.js into the browser console to load your PII
```

## Deploy (hosted dashboard + cron + email alerts)

1. **Push to GitHub.** `.gitignore` keeps your PII and secrets out.
2. **Enable Pages**: repo Settings → Pages → **Source: GitHub Actions** (already
   set). The `hunt` workflow builds `docs/` and deploys it via the Pages artifact
   flow — no branch folder needed. Referrer PII loads only in your own browser.
3. **Email alerts**: create a Gmail **App Password**
   (myaccount.google.com → Security → App passwords — needs 2FA), then in the repo
   Settings → **Secrets and variables → Actions**, add:
   - `GMAIL_USER` — your gmail address
   - `GMAIL_APP_PASSWORD` — the app password (**never put this in a file/commit**)
   - `ALERT_TO` — where to send alerts (optional; defaults to `GMAIL_USER`)
4. The **`hunt` workflow** (`.github/workflows/hunt.yml`) then runs every ~30 min:
   rebuild `docs/jobs.json`, email new jobs with fit ≥ `alerts.min_fit_for_alert`,
   persist state, and **deploy the dashboard to Pages**. Trigger the first run
   manually from the **Actions** tab (or just push) to publish it immediately.

## Manual commands

```bash
python3 worker.py                 # full local build (public + private)
python3 worker.py --public        # public jobs.json only (what Actions runs)
python3 worker.py --no-discovery  # offline/fast: skip discovery + ATS fetch
python3 worker.py --min-fit 45    # raise the match bar (default 30)
./scripts/sync-profile.sh         # refresh data/profile.json from the resume-builder
./schedule.sh install             # local cron alternative to GitHub Actions
```

> **Note:** the worker reads resume variants from `data/profile.json`, a vendored
> copy of the resume-builder's library — so it works in GitHub Actions, where the
> sibling `../resume-builder` repo isn't checked out. After editing your resumes in
> the builder, run `./scripts/sync-profile.sh` and commit the change.

## Files

| Path | Responsibility |
|---|---|
| `docs/index.html` | The static dashboard (reads `jobs.json`; PII + status in localStorage) |
| `docs/jobs.json` | Public, committed job data + gap analysis (worker output) |
| `docs/seen.json` | Ids already emailed (so alerts don't repeat) |
| `worker.py` | Pipeline: discover → ingest → match → gap → prepare → persist |
| `scripts/notify.py` | Emails new jobs via Gmail (Actions Secrets) |
| `.github/workflows/hunt.yml` | Cron: rebuild data, email alerts, commit |
| `lib/gap.py` | ATS keyword-gap analysis (JD vs each resume variant) |
| `lib/match.py` | Fit scoring + resume-variant selection |
| `lib/discovery.py` | Company discovery (job boards) + H-1B verification (DOL data) |
| `lib/ats.py` | Greenhouse/Lever/Ashby feed fetch (network-guarded) |
| `lib/referrals.py` | In-network matching, LinkedIn search links, drafted messages |
| `lib/apollo.py` | Optional out-of-network people via the Apollo.io API |
| `lib/payload.py` | Build the resume-builder `ResumeInput` + base64 import URL |
| `lib/jobs.py` / `lib/profile.py` / `lib/secrets.py` | Ingest / profile library / key loader |
| `data/companies.json` | The growable H-1B company list (seeded from the scanner) |

## What this deliberately does NOT do

- **Scrape LinkedIn** (your feed, or other people's profiles/resumes) — it's against
  LinkedIn's terms, gets accounts flagged, and harvesting third parties' data isn't
  something this does. The gap analysis uses **job descriptions** instead — a
  higher-signal, legitimate source for "what am I missing." "Hidden" postings are
  partly covered by JSearch, which aggregates many LinkedIn/Indeed listings.
- **Store secrets in the repo** — Gmail/API credentials live in GitHub Actions
  Secrets or a git-ignored `.secrets.json`, never in committed files.
