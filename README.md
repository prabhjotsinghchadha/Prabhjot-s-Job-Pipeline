# Pipeline

A local job-search pipeline. It finds postings from a dozen sources, scores
them against your profile with Claude, and gives you one screen to decide on
each one. You apply; it keeps track.

```
Find (13 sources)  →  Prefilter (0 tokens)  →  Score (10 jobs per Claude call)
        →  Inbox: Apply / Save / Skip  →  Applications: Applied → Interviewing → Offer
```

Nothing is ever sent or submitted without you pressing the button.

## Screens

| Screen | What it is for |
|---|---|
| **Inbox** (`/`) | Scored jobs above your threshold, best first. Apply, Save for later, or Skip. |
| **Applications** (`/applications`) | Everything you applied to, by stage. Follow-up reminders, ghost alerts, notes. |
| **Resume** (`/resume`) | Structured resume builder → ATS-friendly PDF/DOCX. Per-job tailoring. ATS check. |
| **Settings** (`/settings`) | Who you are, what you want, where to look, schedule, email. Edits `profile.yaml`. |

The top bar has the two actions that matter (**Find jobs**, **Score**), a
theme switch (system / light / dark) and a **Pause** switch that stops all
automation and Claude usage.

### Applying

Press **Apply** on an Inbox card:

- **Posting gives a hiring email** → an email draft is composed (your stored
  cover letter, or Claude writes one). You edit it, press **Send**, and it goes
  from your own Gmail with the resume attached.
- **Otherwise** → the apply kit: the real application URL (aggregator links
  are resolved to the company's form), a cover letter, your contact details and
  common answers with copy buttons, and the tailored resume to download. You
  fill the form in your browser and press **Mark as applied**.

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
  routes/            jobs, discover, apply, resume, tracking, settings
  templates/         base + one HTML file per screen (Jinja + Alpine.js, no build step)
  static/            app.css (theme), common.js (top bar, API, WebSocket), one JS per screen
utils/
  pipeline.py        the one code path: run_discovery, run_scoring (batched), email, follow-ups
  discovery.py       source registry + Greenhouse/Lever; other sources in *_source.py
  prefilter.py       zero-token rules (remote, location, seniority, title, age)
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

Statuses: `discovered → matched | skipped`, then `saved`, `applied`,
`interviewing`, `offer`, `rejected`, `archived`.

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

All under `/api`. The dashboard uses only these.

```
GET   /jobs?status=matched,saved&min_score=&search=&sort_by=&limit=&offset=
GET   /jobs/{id}        PATCH /jobs/{id} {status|notes}        DELETE /jobs/{id}
POST  /jobs/{id}/skip   POST /jobs/{id}/save   POST /jobs/{id}/score   POST /jobs/manual
POST  /discover         POST /score            POST /prefilter {dry_run}   GET /sources   POST /ingest {jobs}
POST  /jobs/{id}/apply-kit   POST /jobs/{id}/applied   POST /jobs/{id}/cover-letter   POST /jobs/{id}/resolve-url
POST  /jobs/{id}/email-draft POST /jobs/{id}/email-send {to,subject,body}   GET /email/config   POST /email/test
GET   /resume/build?format=pdf|docx|txt[&job_id=]   GET /resume/ats[?job_id=]   POST /resume/import   POST /resume/save-as
GET   /jobs/{id}/tailor      POST /jobs/{id}/tailor   GET /resume/tailored   GET|POST /resumes   DELETE /resumes/{name}
GET   /follow-ups       POST /jobs/{id}/follow-up   POST /jobs/{id}/dismiss-follow-up   POST /check-email
GET   /profile          PATCH /profile (deep merge)   GET /scheduler/status   POST /scheduler/trigger/{job}
GET   /system/state     POST /system/pause   POST /system/resume   GET /system/claude   GET /stats
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
