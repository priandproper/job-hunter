# Networking & referrals strategy

How job-hunter turns cold job discovery into warm intros → referrals. The app is the
central place you *initiate* from; the goal is an organic arc that leads to referrals,
intros to hiring managers, and networking, not one-off cold blasts.

## The core model: a warm-path pipeline

Referrals rarely come from a cold "please refer me." They come from a short arc:

> **identify a good person → find the warmest path to them → make a genuine first touch → then ask**

The app is already good at *identify* (the boolean "Find referrers" search, `linkedin_people.py`
paste → People CSV, mutual-connection capture). The value we're adding is everything *after*
"identified": ranking the warmest path per job and walking you through the arc — a lightweight
personal CRM for the job hunt.

## Where the people are (warmest → coldest)

| Source | How people use it for referrals | App hook |
| --- | --- | --- |
| **Mutual connections (2nd°)** | Ask the shared person for an intro — highest conversion | We already capture mutuals (`how_known`); rank by "has a mutual" + draft the intro-request to the *mutual* |
| **Alumni** | Same-school people reply far more | Alumni-lane: LinkedIn Alumni URL per company + tag imported people who share a school |
| **Function peers at the company** | Non-transactional DM → rapport → referral | Surfaced by "Find referrers"; sequencer drives the follow-up |
| **Recruiters / Talent** | Direct, legitimate inbound | Already: recruiter-vs-referral ask in `reach_out.py` |
| **Communities** (Slack/Discord/newsletters) | "Who's hiring" + member referrals: Product Marketing Alliance, Sharebird, RevGenius, Women in Product, Elpha, Pavilion, MBA cohort Slacks | Person-source paste adapter (paste a member list / thread) |
| **Reddit** | Hiring/referral threads; company subs; poster is often a referrer | `reddit_signals.py` (real free API) → posters as potential referrers linked to jobs |
| **X / Twitter** | Active PMMs/founders open to DMs; "who's hiring" | Person-source paste adapter (paste a search) |
| **Events / webinars** | Attend, ask a smart question, follow up warm | Event radar: paste a Luma/Meetup/company-events page → tasks |
| **Glassdoor / Levels.fyi / Blind** | Intel: interview Qs, team names, comp, org changes | Intel enrichment into outreach + interview prep (not for direct contact) |
| **Their work** (GitHub/Kaggle/Substack) | Reach out about the thing they made (analyst/technical) | Person-source paste adapter |

## The plays that convert (what to operationalize)

- **Warm-path first** — mutual/alumni before cold, always.
- **Engagement-before-ask** — comment on their post / cite a mutual or alumni tie, *then* ask a few days later.
- **Non-transactional first touch** — "could I ask you 2 questions about the team?" beats "refer me."
- **Specificity from intel** — name a recent launch/news so it's clearly not a spray.
- **The follow-up** — most referrals land on touch #2; a timed nudge is the highest-ROI missing feature.
- **Network broadcast** — one post/message to *your* people: "targeting these 10 companies, anyone connected?" (the app already knows your target companies).

## Guardrails (non-negotiable)

- **No LinkedIn/Glassdoor scrapers.** They block it, it violates ToS, and it can get *your* account
  restricted. Keep the paste-based, "copy what you can see" model. **Reddit has a proper free read API** —
  that one we can automate; paste the rest.
- **Volume backfires.** LinkedIn caps ~100–200 connection requests/week; cold blasts burn your account and
  email deliverability. Warm paths convert ~10× cold — push quality + sequencing, cap cold sends.
- **Authenticity is the product.** Features should make genuine, specific, non-transactional outreach
  *easier* — never enable mass spam.

## Build roadmap (tracking)

- [x] **Alumni lane** — school-based LinkedIn Alumni URL per company on the job page; tag same-school people.
- [ ] **Person-source paste adapters** — generalize `linkedin_people.py` to Reddit threads / community
      member lists / X searches / event attendee pages (Cmd-C → People CSV).
- [ ] **Reddit signal ingestion** — `reddit_signals.py` watches chosen subreddits for hiring/referral posts,
      matches company+role to your jobs, adds the poster as a potential referrer.
- [ ] **Warm-path ranker + outreach sequencer** — per-job "best 3 paths to a referral" (mutual/alumni/peer/
      recruiter) + a follow-up tracker with stages (identified → engaged → connected → replied → intro →
      referred), next-step + due date, and drafted messages. *(Highest ROI; uses data we already collect.)*
- [ ] **Intel enrichment** *(later)* — Glassdoor interview Qs / recent news into drafts + interview prep.
- [ ] **Network broadcast generator** *(later)* — draft the "anyone connected at these companies?" post.
