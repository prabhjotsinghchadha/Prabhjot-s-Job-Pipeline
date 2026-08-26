"""
Background scheduler — Runs discovery and scoring on configurable intervals.
Integrates with FastAPI server lifecycle via setup_scheduler() -> deferred start.

Multi-tenant mode: each scheduled job iterates every registered user
sequentially, binding the user's context so all data access (DB, profile,
resumes, cache) lands in that user's own directory. Sequential on purpose —
all tenants share one egress IP, and job boards must see one polite client.

Legacy mode: behaves exactly as before — one user, one profile.yaml.
"""

import asyncio
import yaml
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from utils import usercontext

scheduler = AsyncIOScheduler()
# Keyed by uid so one tenant's run summary is never shown to another.
_last_results: dict = {}
_configured = False


def _results() -> dict:
    """Per-user slot in _last_results for the current context."""
    uid = usercontext.current_uid()
    return _last_results.setdefault(uid, {"discover": None, "score": None})


def get_profile():
    """Load the current user's profile. Returns None if missing."""
    path = usercontext.profile_path()
    if not path.exists():
        return None
    with open(path) as f:
        return yaml.safe_load(f)


async def _for_each_user(fn) -> None:
    """
    Run fn() once per registered user (multi-tenant) or once as the local
    user (legacy). One user's failure never blocks the others.
    """
    if not usercontext.multi_tenant_enabled():
        await fn()
        return
    for uid, info in usercontext.list_users().items():
        token = usercontext.set_current_user({
            "uid": uid,
            "email": info.get("email", ""),
            "name": info.get("name", ""),
        })
        try:
            await fn()
        except Exception as e:
            print(f"[Scheduler] {fn.__name__} failed for user {uid}: {e}")
        finally:
            usercontext.reset_current_user(token)


async def _discover_for_current_user():
    """Discover new jobs from all sources for the bound user."""
    try:
        from utils.discovery import discover_all_jobs
        from utils.tracker import is_already_seen, log_discovered

        profile = get_profile()
        if profile is None:
            return

        # Persist each source's results as it finishes — a restart mid-run
        # keeps everything discovered so far.
        seen_keys: set = set()
        counts = {"total": 0, "new": 0}

        async def on_source_jobs(source: str, jobs: list) -> None:
            for job in jobs:
                key = (job.title.lower().strip(), job.company.lower().strip())
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                counts["total"] += 1
                if not is_already_seen(job.id):
                    log_discovered(job)
                    counts["new"] += 1

        await discover_all_jobs(profile, on_source_jobs=on_source_jobs)

        _results()["discover"] = {
            "total": counts["total"],
            "new": counts["new"],
            "timestamp": __import__("datetime").datetime.now().isoformat()
        }
        print(f"[Scheduler] Discovery complete: {counts['new']} new jobs from {counts['total']} total")
    except Exception as e:
        print(f"[Scheduler] Discovery failed: {e}")
        _results()["discover"] = {"error": str(e)}


async def _score_for_current_user():
    """Score all unscored jobs for the bound user."""
    try:
        from utils.tracker import get_unscored_jobs, log_matched, log_skipped
        from utils.brain import ClaudeBrain

        profile = get_profile()
        if profile is None:
            return
        unscored = get_unscored_jobs()
        if not unscored:
            return

        brain = ClaudeBrain(verbose=False, profile=profile)
        from utils.resume_parser import extract_resume_text
        resume_text = extract_resume_text(profile.get("resume_path", ""))
        min_score = profile["preferences"].get("min_match_score", 65)
        scored = 0

        for job_row in unscored:
            try:
                desc = job_row.get("description", "") or f"Job: {job_row['title']} at {job_row['company']}"
                result = brain.match_job(desc, profile, resume_text=resume_text)
                score = result.get("score", 0)
                log_matched(job_row["id"], score, result.get("reasoning", ""), result.get("cover_letter", ""))
                if score < min_score:
                    log_skipped(job_row["id"], f"Score {score} < {min_score}")
                scored += 1
            except Exception:
                pass

        _results()["score"] = {
            "scored": scored,
            "timestamp": __import__("datetime").datetime.now().isoformat()
        }
        print(f"[Scheduler] Scored {scored} jobs")
    except Exception as e:
        print(f"[Scheduler] Scoring failed: {e}")
        _results()["score"] = {"error": str(e)}


async def _email_check_for_current_user():
    """Check email for application status updates, if this user enabled it."""
    try:
        from utils.email_checker import check_emails
        profile = get_profile()
        if profile is None or not profile.get("email", {}).get("enabled", False):
            return
        results = check_emails(profile)
        _results()["email"] = {
            "checked": len(results),
            "timestamp": __import__("datetime").datetime.now().isoformat()
        }
    except Exception as e:
        print(f"[Scheduler] Email check failed: {e}")


async def _follow_up_check_for_current_user():
    """Check for overdue follow-ups and log count."""
    try:
        from utils.tracker import get_overdue_follow_ups, get_ghost_alerts
        overdue = get_overdue_follow_ups()
        ghosts = get_ghost_alerts(days=14)
        if overdue or ghosts:
            print(f"[Scheduler] Follow-up check: {len(overdue)} overdue, {len(ghosts)} ghosts")
        _results()["follow_up"] = {
            "overdue": len(overdue),
            "ghosts": len(ghosts),
            "timestamp": __import__("datetime").datetime.now().isoformat()
        }
    except Exception as e:
        print(f"[Scheduler] Follow-up check failed: {e}")


async def scheduled_discover():
    await _for_each_user(_discover_for_current_user)


async def scheduled_score():
    await _for_each_user(_score_for_current_user)


async def scheduled_email_check():
    await _for_each_user(_email_check_for_current_user)


async def scheduled_follow_up_check():
    await _for_each_user(_follow_up_check_for_current_user)


def setup_scheduler():
    """
    Configure scheduler jobs (but don't start yet).
    Call start_scheduler() after the event loop is running (e.g., in FastAPI lifespan).
    """
    global _configured

    if usercontext.multi_tenant_enabled():
        # No single profile to read intervals from — platform defaults.
        discover_hours, score_minutes = 6, 30
        email_enabled = True  # Per-user opt-in is checked inside the job
    else:
        profile = get_profile()
        if profile is None:
            print("[Scheduler] No profile.yaml found — skipping scheduler setup (waiting for wizard)")
            return
        schedule_config = profile.get("schedule", {})
        if not schedule_config.get("enabled", True):
            print("[Scheduler] Disabled in profile.yaml")
            return
        discover_hours = schedule_config.get("discover_interval_hours", 6)
        score_minutes = schedule_config.get("score_interval_minutes", 30)
        email_enabled = profile.get("email", {}).get("enabled", False)

    scheduler.add_job(
        scheduled_discover,
        trigger=IntervalTrigger(hours=discover_hours),
        id="discover",
        name="Job Discovery",
        replace_existing=True
    )

    scheduler.add_job(
        scheduled_score,
        trigger=IntervalTrigger(minutes=score_minutes),
        id="score",
        name="Job Scoring",
        replace_existing=True
    )

    if email_enabled:
        scheduler.add_job(
            scheduled_email_check,
            trigger=IntervalTrigger(hours=12),
            id="email",
            name="Email Check",
            replace_existing=True
        )

    # Follow-up reminder & ghost detection check
    scheduler.add_job(
        scheduled_follow_up_check,
        trigger=IntervalTrigger(hours=6),
        id="follow_up",
        name="Follow-up Check",
        replace_existing=True
    )

    _configured = True
    print(f"[Scheduler] Configured — Discovery every {discover_hours}h, Scoring every {score_minutes}m")


def start_scheduler():
    """Start the scheduler. Must be called from within a running event loop."""
    if _configured and not scheduler.running:
        scheduler.start()
        print("[Scheduler] Started")


def stop_scheduler():
    """Stop the scheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        print("[Scheduler] Stopped")


def get_scheduler_status() -> dict:
    """Scheduler status for the dashboard — last results scoped to the caller."""
    jobs_info = []
    try:
        for job in scheduler.get_jobs():
            jobs_info.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
            })
    except Exception:
        pass
    return {
        "running": scheduler.running if hasattr(scheduler, 'running') else False,
        "jobs": jobs_info,
        "last_results": _results(),
    }
