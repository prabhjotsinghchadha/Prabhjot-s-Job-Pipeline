"""
Background scheduler — runs the pipeline on the intervals set in profile.yaml.
setup_scheduler() configures the jobs; start_scheduler() must be called from
inside a running event loop (the FastAPI lifespan does this).
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from utils import pipeline
from utils.system_state import is_paused

scheduler = AsyncIOScheduler()
_configured = False


def _paused(name: str) -> bool:
    if is_paused():
        print(f"[Scheduler] Paused — skipping {name}")
        return True
    return False


def _clients_on(profile: dict) -> bool:
    return bool((profile.get("offer") or {}).get("enabled"))


async def job_discover():
    profile = pipeline.load_profile()
    if profile and not _paused("discovery"):
        r = await pipeline.run_discovery(profile)
        print(f"[Scheduler] Discovery: {r}")
        if _clients_on(profile) and not _paused("lead discovery"):
            r = await pipeline.run_lead_discovery(profile)
            print(f"[Scheduler] Lead discovery: {r}")


async def job_score():
    profile = pipeline.load_profile()
    if profile and not _paused("scoring"):
        r = await pipeline.run_scoring(profile)
        print(f"[Scheduler] Scoring: {r}")
        if _clients_on(profile) and not _paused("lead scoring"):
            r = await pipeline.run_scoring(profile, kind="lead")
            print(f"[Scheduler] Lead scoring: {r}")


async def job_email():
    profile = pipeline.load_profile()
    if profile and not _paused("email check"):
        import asyncio
        await asyncio.to_thread(pipeline.run_email_check, profile)


async def job_follow_up():
    pipeline.run_follow_up_check()


JOBS = {
    "discover": ("Find jobs", job_discover),
    "score": ("Score jobs", job_score),
    "email": ("Check email", job_email),
    "follow_up": ("Follow-up check", job_follow_up),
}


def setup_scheduler():
    global _configured
    profile = pipeline.load_profile()
    if profile is None:
        print("[Scheduler] No profile.yaml yet — scheduler idle until Settings are saved")
        return
    cfg = profile.get("schedule", {})
    if not cfg.get("enabled", True):
        print("[Scheduler] Disabled in profile.yaml")
        return
    discover_hours = cfg.get("discover_interval_hours", 6)
    score_minutes = cfg.get("score_interval_minutes", 30)
    email_hours = profile.get("email", {}).get("check_interval_hours", 12)

    scheduler.add_job(job_discover, IntervalTrigger(hours=discover_hours), id="discover",
                      name=JOBS["discover"][0], replace_existing=True)
    scheduler.add_job(job_score, IntervalTrigger(minutes=score_minutes), id="score",
                      name=JOBS["score"][0], replace_existing=True)
    if profile.get("email", {}).get("enabled", False):
        scheduler.add_job(job_email, IntervalTrigger(hours=email_hours), id="email",
                          name=JOBS["email"][0], replace_existing=True)
    scheduler.add_job(job_follow_up, IntervalTrigger(hours=6), id="follow_up",
                      name=JOBS["follow_up"][0], replace_existing=True)
    _configured = True
    print(f"[Scheduler] Discovery every {discover_hours}h, scoring every {score_minutes}m")


def start_scheduler():
    if _configured and not scheduler.running:
        scheduler.start()
        print("[Scheduler] Started")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


def get_scheduler_status() -> dict:
    jobs = []
    try:
        for job in scheduler.get_jobs():
            jobs.append({"id": job.id, "name": job.name,
                         "next_run": job.next_run_time.isoformat() if job.next_run_time else None})
    except Exception:
        pass
    return {
        "running": bool(getattr(scheduler, "running", False)),
        "paused": is_paused(),
        "jobs": jobs,
        "last_results": pipeline.last_results,
        "busy": {k: pipeline.is_running(k) for k in ("discover", "score", "discover_lead", "score_lead")},
    }
