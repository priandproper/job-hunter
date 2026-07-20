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

## Highest-leverage keywords (Cockpit)

The Cockpit shows a **Highest-leverage keywords** card: the skills job descriptions
request most across all scraped roles that **none of your résumé variants cover yet**
(from `docs/jobs.json` → `summary.missing_leaderboard`, built by `lib/gap.py`). Ranked by
how many jobs ask for each, with a bar per keyword — click one to see those jobs. Adding
these to a résumé variant is the single biggest lever to raise match/ATS scores.

## Tailor a resume to a job with Claude (`Settings → Claude API`)

Each job page has a **✨ Tailor with Claude** button. It sends your current resume for
that job plus the job's title/company/keywords to the **Claude API** (Messages API,
`claude-opus-4-8` by default) and gets back a resume rewritten to align with the role —
summary, experience bullets, headline, and skill emphasis, without inventing anything.
The result opens straight in the resume builder or copies as JSON.

- Add your **Anthropic API key** in Settings — it's stored only in this browser (never
  committed) and sent only to `api.anthropic.com` (direct browser access). It's a
  billing credential; only use a key you're comfortable keeping in the browser.
- Output is constrained with structured outputs to the builder's exact resume schema,
  so it's guaranteed-valid JSON. Contact and education are kept factual; only genuinely
  applicable keywords are woven in.

## Your profile / persona (tune what gets scraped)

**Settings → Your profile** lets you set what a good job looks like — years, seniority,
target roles, skills/tools, projects, title exclusions, min fit. It works two ways:

- **In the app, instantly** — a *Best for me* sort ranks by persona-relevance, a
  *Hide poor fits* toggle drops jobs that don't match your roles/skills, and
  **✂ Prune off-persona** (Jobs header) permanently hides jobs that don't fit
  (reversible — they move to a **Pruned** tab with *Restore all*).
- **In the cloud scrape** — click **Publish to Gist** and the app writes `persona.json`
  to a **public Gist** via your token (needs **Gists: read/write** on the token). The
  runner reads it (`lib/persona.py`) and folds it into matching — roles + skills raise
  relevance, exclusions hard-drop titles, min-fit tightens the keep threshold — so it
  stops surfacing jobs that don't fit you.

One-time runner setup: after publishing, add an Actions **variable**
`PERSONA_GIST_ID = <gist id>` (repo → Settings → Secrets and variables → Actions →
Variables). Without it, the scrape runs unchanged.

## Sync statuses from your application inbox

Keep job statuses in step with your email — always in your browser or on your machine,
never in the cloud/public repo. Either path classifies messages (application received /
interview / rejection / offer), matches each to a tracked job, and surfaces them on the
Cockpit as **Inbox updates** you Accept or Dismiss. Nothing is applied automatically,
and a status you set by hand is never silently overwritten.

### Option A — In the browser (`Settings → Application inbox`)

One-click, no terminal. Uses the **Gmail API over OAuth** (read-only) — the token lives
in your browser, calls go straight from your browser to Google, proposals land in
`localStorage`. Free for personal use; no billing account. One-time setup:

1. **[console.cloud.google.com](https://console.cloud.google.com)** → create a project → **APIs & Services → Library** → enable **Gmail API**.
2. **OAuth consent screen** → User type **External** → add the `.../auth/gmail.readonly` scope → add your Gmail as a **Test user** (keeps the app free in "Testing" mode; you re-consent ~weekly).
3. **Credentials → Create credentials → OAuth client ID** → type **Web application** → under *Authorized JavaScript origins* add `https://priandproper.github.io` → create.
4. Copy the **Client ID** (ends in `.apps.googleusercontent.com`) and paste it into **Settings → Application inbox → Save Client ID**, then hit **Scan inbox**.

The Client ID is public by design (not a secret) and is stored only in `localStorage`.
Note: OAuth needs a real web origin, so this works on the deployed Pages site, not a
`file://` copy.

### Option B — Local script, no OAuth (`scripts/inbox_scan.py`)

Reads your applying Gmail over **IMAP** with a Gmail **App Password**, writes a
git-ignored `scripts/inbox.local.js` you load via the dashboard's **Run script** modal.

```bash
# creds via env or .secrets.json: IMAP_USER + IMAP_APP_PASSWORD (a Gmail App Password)
python3 scripts/inbox_scan.py            # scan last 30 days
python3 scripts/inbox_scan.py --demo     # try the classifier on built-in samples
```

## Track referral outreach (no LinkedIn scraping)

LinkedIn is **never** scraped or automated (see below). Instead, outreach is tracked
two ToS-safe ways, both surfaced in each job's **Outreach** section on its page:

- **Log sends in-app** — draft the referral message, send it yourself on LinkedIn/email,
  then hit *Log as sent* (records recipient + text + date; first log bumps status to
  `messaged`). Stored in `localStorage`.
- **Import your LinkedIn data export** — download *your own* messages
  (LinkedIn → Settings → Data Privacy → Get a copy of your data → Messages), then:

  ```bash
  python3 scripts/linkedin_import.py --messages ~/Downloads/Messages.csv
  python3 scripts/linkedin_import.py --demo      # synthetic messages, real jobs
  ```

  It matches the messages *you sent* to jobs by company and writes a git-ignored
  `scripts/outreach.local.js` — load it via **Run script** (merges, de-duped by sig).

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
