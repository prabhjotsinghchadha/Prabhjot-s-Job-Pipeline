"""REST routers. Each file owns one area; business logic lives in utils/."""

import yaml
from fastapi import HTTPException

from utils.paths import profile_path
from utils.tracker import get_job_by_id


def load_profile() -> dict:
    p = profile_path()
    if not p.exists():
        raise HTTPException(status_code=400, detail="No profile yet. Fill in Settings first.")
    with open(p) as f:
        return yaml.safe_load(f) or {}


def save_profile(profile: dict) -> None:
    with open(profile_path(), "w") as f:
        yaml.dump(profile, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def require_job(job_id: str) -> dict:
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def valid_kind(kind: str) -> str:
    from utils.tracker import KINDS
    return kind if kind in KINDS else "job"


def job_description(job: dict) -> str:
    return job.get("description") or f"Job: {job.get('title')} at {job.get('company')}. Location: {job.get('location', '')}"
