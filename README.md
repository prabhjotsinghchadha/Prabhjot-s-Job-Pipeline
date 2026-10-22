# Pipeline

A local pipeline for finding work, in two modes that share one app:

- **Jobs**: find postings from a dozen sources, score them against your
  profile with Claude, decide in the Inbox, apply, track.
- **Clients**: find contract gigs and companies that might buy your services,
  score them against your offer, pitch, track the deal.

```
Jobs     Find (13 sources) → Prefilter (0 tokens) → Score (10 per Claude call)
         → Inbox: Apply / Save / Skip → Applications: Applied → Interviewing → Offer

Clients  Find (gig boards + prospects from your own jobs data) → Prefilter → Score
         → Leads: Pitch / Save / Skip → Pipeline: Contacted → Call → Proposal → Won / Lost
```

Switch with the **Jobs | Clients** control in the top bar. Nothing is ever
sent or submitted without you pressing the button, in either mode.

## Screens

| Screen | What it is for |
|---|---|
| **Inbox / Leads** (`/`) | Scored jobs (or leads) above your threshold, best first. Apply (or Pitch), Save for later, or Skip. |
| **Applications / Pipeline** (`/applications`) | Everything you applied to (or pitched), by stage. Follow-up reminders, ghost alerts, notes. |
| **Resume** (`/resume`) | One or more resumes (e.g. Senior SWE and CTO), each with its target roles. Structured builder → ATS-friendly PDF/DOCX. Per-job tailoring. ATS check. |
| **Settings** (`/settings`) | Who you are, what you want, where to look, schedule, email. Edits `profile.yaml`. |

The top bar has the two actions that matter (**Find jobs**, **Score**), a
theme switch (system / light / dark) and a **Pause** switch that stops all
automation and Claude usage. With more than one resume it also has an
**As …** picker (see below).

### Several resumes

Applying for different kinds of role (say Senior Software Engineer *and* CTO)?
On the Resume screen press **+ New resume**. Each resume has its own name,
target roles, optional search queries / keywords / ideal-job paragraph /
minimum score, its own builder content and its own PDF. Anything it leaves
empty is borrowed from the main resume (the top-level profile).

- **Find jobs** searches for every resume at once, or only the one chosen in
  the top-bar **As …** picker. Each new job is matched to the resume whose
  roles fit its title best (a "Head of Engineering" posting goes to CTO, a
  "Senior Backend Engineer" one to the IC resume).
- Each job is prefiltered and scored against **its** resume: the CTO resume
  keeps director/VP titles that the IC one drops.
- The Inbox and Applications show the picked resume's jobs (or all, with a
  chip naming the resume). The Apply drawer says which resume you are applying
  as, and you can switch it there; the cover letter, tailoring and attached
  PDF follow.

### Applying

Press **Apply** on an Inbox card:

- **Posting gives a hiring email** → an email draft is composed (your stored
  cover letter, or Claude writes one). You edit it, press **Send**, and it goes
  from your own Gmail with the resume attached.
- **Otherwise** → the apply kit: the real application URL (aggregator links
  are resolved to the company's form), a cover letter, your contact details and
  common answers with copy buttons, and the tailored resume to download. You
  fill the form in your browser and press **Mark as applied**.

### Clients mode

Fill in **Settings → Clients**: what you sell, engagement types, rates and
minimum budget, ideal client, red flags, proof points, gig search queries and
the stacks that make a company a prospect. Then switch to Clients and press
**Find clients**. Leads come from:

- the monthly HN "Freelancer? Seeking freelancer?" thread,
- Indeed / LinkedIn searched with `job_type=contract` and your gig queries,
- contract-looking postings on the regular boards (title, tags or employment type),
- **prospects**: companies already in your jobs data that are hiring in your
  stack. No network; built from what discovery already found.

Claude scores each lead against the offer (budget fit, engagement type, a
one-line pitch angle). **Pitch** opens the same drawer as Apply: an email
draft you review and send when there is an address, or the pitch to copy
plus contact channels (posting, LinkedIn people search, company search) and
**Mark as contacted**. Contacted leads get a 3-day follow-up, proposals 5 days.

Set `offer.enabled: true` to include clients in the scheduled runs; otherwise
they only run when you press the button.

## Quick start

Requirements: Python 3.11, the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code)
logged in (`claude auth`), and a Chromium for Playwright.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python main.py server            # http://localhost:8080
```

Open **Settings**, fill in your name, email and the roles you want, save, then
press **Find jobs**. Discovery runs every 6 hours and scoring every 30 minutes
while the server is up (change this in Settings → Schedule).

Docker: `docker compose up -d --build` does the same on port 8080 with
`profile.yaml`, `resumes/`, `applications.db` and `.cache/` mounted from the
repo. Put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or
`ANTHROPIC_API_KEY` in `.env`.

### CLI

```bash
python main.py discover    # find new jobs from every enabled source, then score
python main.py discover --track cto   # only for one resume track (repeatable)
python main.py score       # score the unscored backlog
python main.py stats       # counts per stage
python main.py server      # dashboard + scheduler
```

## How it works

```
main.py              CLI
scheduler.py         APScheduler: discover / score / email / follow-ups on intervals
dashboard/
  app.py             FastAPI app, 4 screens, WebSocket relay, pause switch
  routes/            jobs, discover, apply, resume, tracks, tracking, settings
  templates/         base + one HTML file per screen (Jinja + Alpine.js, no build step)
  static/            app.css (theme), common.js (top bar, API, WebSocket), one JS per screen
utils/
  pipeline.py        the one code path: run_discovery, run_lead_discovery, run_scoring (batched), email, follow-ups
  leads.py           clients mode: contract classifier, gig sources, prospects from the jobs DB
  discovery.py       source registry + Greenhouse/Lever; other sources in *_source.py
  prefilter.py       zero-token rules (remote, location, seniority, title, age)
  tracks.py          several resumes: profile_for_track overlay, job → track assignment
  brain.py           Claude CLI calls: match_jobs (batch), match_job, write_cover_letter
  tracker.py         SQLite (WAL) CRUD + EventBus emits
  email_apply.py     hiring-inbox detection, draft composition, SMTP send
  email_checker.py   IMAP: rejections / interview invites → status updates
  url_resolver.py    aggregator link → real ATS form
  resume_builder.py  profile.resume → PDF/DOCX/TXT; tailoring overlay
  resume_tailor.py   Claude rewrites bullets/summary per posting (validated, never invents)
  ats_checker.py     deterministic ATS score
  paths.py           where data lives (DATA_DIR, default: repo root)
profile.yaml         your configuration (gitignored). See profile.yaml.example.
applications.db      SQLite, gitignored.
```

Every row has `kind` = `job` or `lead`. Stages:

- job: `discovered → matched | skipped`, then `saved`, `applied`, `interviewing`, `offer`, `rejected`, `archived`
- lead: `discovered → matched | skipped`, then `saved`, `contacted`, `call`, `proposal`, `won`, `lost`, `archived`

### Sources

Greenhouse and Lever boards you list, Indeed / LinkedIn / Google (python-jobspy),
RemoteOK, YC Jobs, Remotive, Himalayas, Arbeitnow, WeWorkRemotely, web3.career,
Adzuna (needs a free key), HN Who is Hiring, and any career-page URL. Each is
independently optional and toggled in Settings → Where to look. One failing
never stops the others.

### Scoring

The prefilter drops obvious misses for free (wrong location, junior/management
titles, titles unrelated to your roles, stale posts). Survivors go to Claude ten
at a time; each gets a 0–100 score and a one-line reason. Jobs at or above
`preferences.min_match_score` land in the Inbox.

## API

All under `/api`. Every list, stats and action endpoint takes `kind=job|lead`
(default job); the dashboard adds it from the current mode.

```
GET   /jobs?status=matched,saved&min_score=&search=&sort_by=&limit=&offset=&track=
GET   /jobs/{id}        PATCH /jobs/{id} {status|notes|track}  DELETE /jobs/{id}
POST  /jobs/{id}/skip   POST /jobs/{id}/save   POST /jobs/{id}/score   POST /jobs/manual
POST  /discover {tracks?}   POST /score            POST /prefilter {dry_run}   GET /sources   POST /ingest {jobs}
POST  /jobs/{id}/apply-kit   POST /jobs/{id}/applied   POST /jobs/{id}/cover-letter   POST /jobs/{id}/resolve-url
POST  /jobs/{id}/email-draft POST /jobs/{id}/email-send {to,subject,body}   GET /email/config   POST /email/test
POST  /jobs/{id}/pitch {force?}   POST /jobs/{id}/contacted {channel,note}     (leads; apply-kit returns the outreach kit)
GET   /resume/build?format=pdf|docx|txt[&job_id=|&track=]   GET /resume/ats[?job_id=|track=]   POST /resume/import {track?}   POST /resume/save-as {track?}
GET   /jobs/{id}/tailor      POST /jobs/{id}/tailor   GET /resume/tailored   GET|POST /resumes   DELETE /resumes/{name}
GET   /tracks           POST /tracks {name,copy_from?}   PATCH /tracks/{id} {name,roles,…,resume,resume_file}   DELETE /tracks/{id}
GET   /follow-ups       POST /jobs/{id}/follow-up   POST /jobs/{id}/dismiss-follow-up   POST /check-email
GET   /profile          PATCH /profile (deep merge)   GET /scheduler/status   POST /scheduler/trigger/{job}
GET   /system/state     POST /system/pause   POST /system/resume   GET /system/claude   GET /stats[?track=]
```

Live updates arrive on `ws://localhost:8080/ws` as `{type, data}` events.

## Safety rules

- No email is sent and no application is marked without a click in the dashboard.
- Scoring, cover letters, tailoring and resume import are the only things that
  spend Claude tokens, and **Pause** stops all of them (scheduled runs included).
- Tailoring only rephrases what is in your resume builder. It never invents
  employers, titles, dates, metrics or skills.
- `profile.yaml`, `applications.db`, `resumes/` and `.env` are gitignored.

## Tests

```bash
pip install pytest pytest-asyncio
python -m pytest tests/
```

MIT licensed.
