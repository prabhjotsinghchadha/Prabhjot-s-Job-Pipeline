# Pipeline — notes for Claude sessions

A local, single-user pipeline with two modes sharing one table (`kind` column):
- **job**: find postings → prefilter → score → user decides in the Inbox → they apply (email draft or assisted manual) → track.
- **lead** (Clients mode): gigs from boards + prospects built from the jobs data → prefilter → score against `profile.offer` → user pitches (email draft or outreach kit) → Contacted → Call → Proposal → Won/Lost.
The UI switch (Jobs | Clients) lives in localStorage; `common.js` adds `kind=` to every API call. Dashboard at `http://localhost:8080`.

## Map

```
main.py                   CLI: discover | score | stats | server
scheduler.py              APScheduler jobs → utils/pipeline.py
dashboard/app.py          FastAPI app, 4 screens (/ /applications /resume /settings), /ws relay, pause gate
dashboard/routes/         jobs.py discover.py apply.py resume.py tracking.py settings.py  (thin; logic in utils/)
dashboard/templates/      base.html + inbox/applications/resume/settings.html + apply_drawer.html
dashboard/static/         app.css (theme tokens), common.js (shell: top bar, api, bus), <screen>.js
utils/pipeline.py         run_discovery, run_lead_discovery, run_scoring(kind) (prefilter + 10-item batches), run_email_check, run_follow_up_check
utils/leads.py            is_contract_posting, discover_gigs (HN freelancer thread, jobspy contract, board contract filter), build_prospects
utils/brain.py            ClaudeBrain: ask, ask_json, match_jobs / match_leads (batch), match_job, write_cover_letter, write_pitch
utils/discovery.py        SOURCE_REGISTRY, discover_all_jobs(profile, on_source_jobs)
utils/prefilter.py        run_prefilter(profile, jobs=None, dry_run=False)
utils/tracker.py          SQLite WAL; VALID_STATUSES; log_discovered/log_matched/log_skipped/log_applied; EventBus emits
utils/email_apply.py      extract_apply_email, compose_application_email, send_application_email
utils/resume_builder.py   build_resume(profile, fmt, tailored), resume_path_for_job
utils/resume_tailor.py    tailor_resume (validated structured rewrites)
utils/paths.py            data_dir() from DATA_DIR (default repo root): db_path, profile_path, resumes_dir, cache_dir
profile.yaml              user config (gitignored); schema in profile.yaml.example
```

Stages per kind are `utils/tracker.STAGES`; `update_job_status` validates against the row's kind and schedules follow-ups (applied 7d, contacted 3d, proposal 5d).

## Invariants

1. `utils/brain.py` runs `claude -p --output-format json` with the `CLAUDECODE` env var stripped. Never add it back (nested-session crash).
2. Nothing is sent or submitted without the user's click, in either mode. `POST /api/jobs/{id}/email-send` is only called after they reviewed the draft; `POST /api/jobs/{id}/applied` and `/contacted` only record what they did themselves. There is no browser auto-fill and there must not be.
3. Never commit `profile.yaml`, `applications.db`, `resumes/`, `.env`, `system_state.json`.
4. Every discovery source is optional and wrapped in try/except; one failing never stops the others.
5. SQLite stays in WAL mode (CLI and dashboard share the file).
6. Strip HTML from descriptions before scoring; deduplicate by (title, company) before insert.
7. The scheduler starts inside the FastAPI lifespan, never before the event loop.
8. Tailoring never invents employers, titles, dates, metrics or skills.
9. Everything that spends Claude tokens honours the pause switch (`utils/system_state.is_paused`).

## Working on it

- Run: `.venv/bin/python main.py server` (or `preview_start` name `mr-jobs-dashboard`). Static files are cache-busted per server start, so restart after editing templates/JS/CSS.
- Tests: `.venv/bin/python -m pytest tests/` (needs `pytest pytest-asyncio` in the venv).
- To try the UI with real data without touching the user's DB: copy a DB and `profile.yaml` into a scratch folder, run `DATA_DIR=<folder> python main.py server --port 8081`, and pause it (`POST /api/system/pause`) so the scheduler spends nothing.
- Frontend is plain Jinja + Alpine.js from CDN. No build step, no Tailwind. Theme tokens live on `:root` in `app.css`; the top-bar switch sets `data-theme` (light/dark) or clears it for system.
- New endpoint: add it to the right router in `dashboard/routes/`, keep logic in `utils/`, document it in README's API block.

## When the user asks you to

- **Find clients**: `POST /api/discover?kind=lead` then `POST /api/score?kind=lead`. Needs `profile.offer.services`. Prospects come from the jobs already in the DB, so run jobs discovery first.
- **Find jobs**: `POST /api/discover` (or `python main.py discover`), then `POST /api/score`. You can also WebSearch and `POST /api/ingest {jobs:[...]}` (helpers in `utils/mcp_source.py`). Report new count, matches, top scores.
- **Check status**: `POST /api/check-email` (IMAP) and read `GET /api/follow-ups`. Update statuses with `PATCH /api/jobs/{id}`.
- **Apply**: never do it for them. Prepare: `POST /api/jobs/{id}/apply-kit` or `/email-draft`, show the result, and let them press Send / Mark as applied in the dashboard.
- **Change config**: `PATCH /api/profile` deep-merges into `profile.yaml`; lists replace wholesale.
