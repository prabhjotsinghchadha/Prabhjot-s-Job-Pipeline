"""Jobs: list, read, update, delete, decide (skip / save), stats."""

from typing import Optional

from fastapi import APIRouter, HTTPException

from dashboard.routes import require_job, valid_kind
from utils.tracker import (
    STAGES, VALID_STATUSES, delete_job, get_all_jobs, get_companies, get_score_distribution,
    get_stats, get_timeline_stats, ignore_jobs, update_job_notes, update_job_status,
)

router = APIRouter(tags=["jobs"])


@router.get("/jobs")
async def list_jobs(
    status: Optional[str] = None, company: Optional[str] = None, min_score: Optional[int] = None,
    search: Optional[str] = None, sort_by: str = "discovered_at", sort_order: str = "desc",
    limit: int = 50, offset: int = 0, kind: str = "job",
) -> dict:
    """status may be a comma-separated list (e.g. applied,interviewing). kind = job | lead."""
    jobs, total = get_all_jobs(status=status, company=company, min_score=min_score, search=search,
                               sort_by=sort_by, sort_order=sort_order, limit=limit, offset=offset,
                               kind=valid_kind(kind))
    from utils.pipeline import _unpack_meta
    for j in jobs:
        _unpack_meta(j)
    return {"jobs": jobs, "total": total, "limit": limit, "offset": offset}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    from utils.pipeline import _unpack_meta
    job = require_job(job_id)
    _unpack_meta(job)
    return job


@router.patch("/jobs/{job_id}")
async def update_job(job_id: str, body: dict) -> dict:
    """Body: {status?, notes?}"""
    require_job(job_id)
    if "status" in body and not update_job_status(job_id, body["status"]):
        kind = require_job(job_id).get("kind", "job")
        raise HTTPException(status_code=400, detail=f"Invalid status for a {kind}. Valid: {STAGES[kind]}")
    if "notes" in body:
        update_job_notes(job_id, body["notes"] or "")
    return require_job(job_id)


@router.post("/jobs/{job_id}/skip")
async def skip_job(job_id: str, body: dict = {}) -> dict:
    """Skip a job and never show this title+company again."""
    require_job(job_id)
    ignore_jobs([job_id])
    if body.get("reason"):
        update_job_notes(job_id, body["reason"])
    return require_job(job_id)


@router.post("/jobs/{job_id}/save")
async def save_job(job_id: str) -> dict:
    """Save for later — stays out of the Inbox, shows under Saved."""
    require_job(job_id)
    update_job_status(job_id, "saved")
    return require_job(job_id)


@router.delete("/jobs/{job_id}")
async def remove_job(job_id: str) -> dict:
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True}


@router.get("/stats")
async def stats(kind: str = "job") -> dict:
    return get_stats(valid_kind(kind))


@router.get("/stats/timeline")
async def timeline(kind: str = "job") -> list:
    return get_timeline_stats(valid_kind(kind))


@router.get("/stats/scores")
async def scores(kind: str = "job") -> list:
    return get_score_distribution(valid_kind(kind))


@router.get("/companies")
async def companies(kind: str = "job") -> list:
    return get_companies(valid_kind(kind))


@router.get("/statuses")
async def statuses(kind: str = "") -> list:
    return STAGES[valid_kind(kind)] if kind else VALID_STATUSES
