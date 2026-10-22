"""Tracking: follow-ups, ghost alerts, inbox checks for status updates."""

import asyncio

from fastapi import APIRouter

from dashboard.routes import load_profile, require_job
from utils import pipeline
from utils.tracker import dismiss_follow_up, get_ghost_alerts, get_overdue_follow_ups, increment_follow_up

router = APIRouter(tags=["tracking"])


@router.get("/follow-ups")
async def follow_ups() -> dict:
    return {"overdue": get_overdue_follow_ups(), "ghosts": get_ghost_alerts(days=14)}


@router.post("/jobs/{job_id}/follow-up")
async def mark_follow_up(job_id: str, body: dict = {}) -> dict:
    """Followed up: push the next reminder out {next_days: 7}."""
    require_job(job_id)
    days = int(body.get("next_days", 7))
    increment_follow_up(job_id, days=days)
    return require_job(job_id)


@router.post("/jobs/{job_id}/dismiss-follow-up")
async def dismiss(job_id: str) -> dict:
    require_job(job_id)
    dismiss_follow_up(job_id)
    return require_job(job_id)


@router.post("/check-email")
async def check_email() -> dict:
    """Read the inbox (IMAP) for rejections / interview invites and update statuses."""
    profile = load_profile()
    return await asyncio.to_thread(pipeline.run_email_check, profile)
