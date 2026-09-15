# job-hunter — operating guide

The operating model: help complete ~**120 credible, sponsor-viable applications per 30
days** and improve the qualified-application → recruiter-conversation rate — for an
international STEM-MBA candidate on F-1 OPT who will need future H-1B sponsorship.

**Nothing is ever applied to or sent automatically.** Every résumé, message, and ranking
is prepared for you to review and send. The pipeline never fabricates facts.

---

## 1. Installation

```bash
git clone <your fork>            # this repo
cd job-hunter
python3 -m pip install -r requirements.txt   # certifi (verified TLS) — required
# optional: the Claude CLI (`claude`) logged into your plan, for the coach & tailoring
```
Python 3.11+ (stdlib only besides `certifi`). Node is optional (only to run the JS tests).

## 2. Daily operation

```bash
python3 worker.py                 # refresh jobs, score, rank, write docs/*, commit + push
python3 scripts/coach_rank.py --publish   # Claude re-ranks (falls back to deterministic)
```
- The launchd agent (`scripts/install_auto_refresh.sh`) runs both at 8:00 daily.
- Open `docs/index.html` (GitHub Pages, or `python3 -m http.server` then `/docs/`).
- **Cockpit** = today's plan (2 Priority-A + 4 Priority-B applications, 5 outreach, 1
  interim-OPT action) + operating metrics vs the 120/30 goal.
- Per Priority-A/B job: `📦 Full packet` (button copies `python3 scripts/packet.py --id <id>`).

## 3. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Dashboard shows a red/amber banner | A run failed/was partial or data is stale — see `docs/health.json`; re-run `python3 worker.py`. |
| Worker fetched 0 jobs | Missing certs — `pip install -r requirements.txt` (certifi). Sources that fail are listed in health, never silently 0. |
| Coach says "deterministic fallback" | The `claude` CLI was logged out / rate-limited. Ranking still works; re-run when Claude is back. |
| "another run holds data/.worker.lock" | A run is in progress (lock auto-clears after 2h if a run crashed). |
| Repeated recommendations | Export state (below) so Python can see what you've applied to/dismissed. |

## 4. Data schemas

- `docs/jobs.json` — **public**: `meta` + `jobs[]`, each with `priority` (Phase 6),
  `immigration` (Phase 2), `spec` (Phase 3 structured JD), `duplicate` (Phase 11), and
  `resume_core` (contact_public + résumé; email/phone stripped).
- `docs/coach.json` — Claude/deterministic ranking: `briefing`, `ranked[]`, `flagged[]`,
  `model`, `coach_status`, `generated_at`.
- `docs/health.json` — run status (Phase 13): `run_id`, `status`, counts,
  `sources_failed`, `coach_status`, `last_full_success`.
- `data/facts.local.json` — **private**: the verified career fact bank (Phase 4).
- `data/state.local.json` — **private**: exported event log (Phase 9),
  `{job_id, event_type, timestamp, metadata}`.
- `data/alerts.local.json` — **private**: email-alert recipients (Phase 16).

## 5. Privacy model

- Public files (`docs/`) carry **job-market data + your chosen public contact fields
  only** (name, city, LinkedIn, GitHub). Email/phone are stripped from `jobs.json`.
- Everything sensitive is git-ignored `*.local.*` / `data/` (contacts, fact bank, state,
  alert recipients, secrets). GitHub Actions deploys `docs/` only — never `data/`.
- `worker.py` runs `lib/privacy.scan_public()` before publishing and refuses to hide a
  leak (logs it + marks the run partial). Tests assert the public payload excludes
  restricted fields.
- The résumé-builder `#import=` URL holds your résumé in the URL *hash*: never sent to a
  server, but stored in browser history and readable by tab-scoped extensions — use it
  only in the builder tab.
- **Open item:** `docs/jobs.json` currently embeds `resume_core` (your résumé content) —
  public by design; can be moved to a private local file if you prefer.

## 6. Backup and recovery

- The job pool (`data/job_pool.json`) and public files are in git — `git revert`/checkout
  restores any prior state.
- Private files live only on your machine: back up `data/*.local.*` yourself (they are
  never committed). Re-exporting from the dashboard regenerates `state.local.json`.
- A failed run never overwrites a good board (empty-guard); `docs/health.json` records
  `last_full_success` so you always know the last clean state.

## 7. How to verify candidate facts

```bash
python3 scripts/seed_facts.py           # seed drafts from data/profile.json (needs_clarification)
```
Open `data/facts.local.json`; set `verification_status` to `verified` for facts you can
stand behind, `do_not_use` to block one. **Only `verified` facts generate résumé bullets**,
and every bullet cites the fact_id(s) it rests on. Dates/numbers are immutable. Re-running
the seeder never clobbers your edits.

## 8. How to add or modify role lanes

Edit `config.json → match`:
- `target_role_terms` — the title allowlist (`lib/match.DEFAULT_TARGET_ROLE_TERMS` is the
  default). Add a phrase to admit a new family.
- `extra_lane_terms` — scoring vocabulary. `experience.exclude_at_years` (default 8) is
  the experience ceiling; targeting is ~3–6 years (Phase 1). Add discovery queries under
  `discovery.queries` so the postings get fetched.

## 9. How to interpret immigration risk

Each job's `immigration.risk` (Phase 2): **green** = the posting indicates sponsorship
(still confirm in writing); **yellow** = not stated — verify with the recruiter before the
hiring-manager stage; **red** = explicit prohibition or a citizenship/clearance
requirement (hard stop — filtered out). Company H-1B history is *evidence, never proof*;
it can never make a role green.

## 10. How to recover when Claude fails

Nothing to do — `scripts/coach_rank.py` automatically falls back to the deterministic
active-queue ranking, writes a valid `coach.json` (`model: deterministic-fallback`,
`coach_status: fallback`), and the dashboard labels it. Re-run when Claude is available for
the judgment layer. Résumé tailoring (`scripts/tailor_resume.py`) requires Claude and will
tell you if it's unavailable; it never fabricates to compensate.

## 11. How to migrate localStorage state

In the cockpit, click **⬇ Export state for Python** → save the downloaded
`state.local.json` into `data/`. The worker/coach then skip roles you've applied to,
dismissed, or snoozed. The exporter writes an append-only event log plus a snapshot; the
Python side reads either (snapshot is the fallback for pre-event-log exports).

## 12. How to run tests

```bash
python3 tests/run_all.py          # all Python + node tests, combined total
python3 tests/test_match_phase1.py    # or any single file
```
Zero framework required (each file has a built-in runner; all are pytest-collectable too).

## 13. How to inspect daily health

Read `docs/health.json` (or watch the dashboard banner): `status` (success/partial/
failed), `jobs_matched`, `sources_failed`, `coach_status`, `duration_seconds`,
`last_full_success`. The worker also prints a `health —` line each run.

---

## Scheduler

The daily process stays on **launchd** (local) for now — Claude auth and all private data
are local, and cloud migration would need secrets + privacy changes without solving the
biggest constraints. Local scheduling already has: a run lock, stale-run recovery (2h lock
reclaim), a manual refresh (`python3 worker.py`), success/failure surfaced via
`health.json` + the dashboard banner, one atomic publish, and empty-run recovery. A future
split (public ATS collection in Actions, private ranking local) is possible but not yet
warranted.
