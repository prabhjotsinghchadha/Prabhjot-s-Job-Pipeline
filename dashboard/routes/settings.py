"""Settings: profile.yaml, scheduler, pause switch, Claude CLI check."""

import asyncio
import subprocess
from datetime import datetime

from fastapi import APIRouter, HTTPException

from dashboard.routes import save_profile
from utils.events import EventBus
from utils.paths import BASE_DIR, profile_path
from utils.system_state import get_state, set_paused

router = APIRouter(tags=["settings"])


def _deep_merge(base: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


@router.get("/profile")
async def get_profile() -> dict:
    """profile.yaml as JSON. The IMAP/SMTP app password is masked."""
    import yaml
    if not profile_path().exists():
        example = BASE_DIR / "profile.yaml.example"
        profile = yaml.safe_load(example.read_text()) if example.exists() else {}
        return {"needs_setup": True, **profile}
    with open(profile_path()) as f:
        profile = yaml.safe_load(f) or {}
    if (profile.get("email") or {}).get("app_password"):
        profile["email"]["app_password"] = "********"
    return profile


@router.patch("/profile")
async def update_profile(body: dict) -> dict:
    """Deep-merge into profile.yaml (created from the example on first save)."""
    import yaml
    if profile_path().exists():
        with open(profile_path()) as f:
            profile = yaml.safe_load(f) or {}
    else:
        example = BASE_DIR / "profile.yaml.example"
        profile = yaml.safe_load(example.read_text()) if example.exists() else {}
    if (body.get("email") or {}).get("app_password") == "********":
        body["email"].pop("app_password")
    _deep_merge(profile, body)
    save_profile(profile)
    try:
        from scheduler import setup_scheduler, start_scheduler
        setup_scheduler()
        start_scheduler()
    except Exception:
        pass
    return await get_profile()


@router.get("/scheduler/status")
async def scheduler_status() -> dict:
    try:
        from scheduler import get_scheduler_status
        return get_scheduler_status()
    except Exception as e:
        return {"running": False, "jobs": [], "last_results": {}, "error": str(e)}


@router.post("/scheduler/trigger/{job_name}")
async def trigger_job(job_name: str) -> dict:
    from scheduler import scheduler as sched
    job = sched.get_job(job_name)
    if not job:
        raise HTTPException(status_code=404, detail=f"No scheduled job named {job_name}")
    job.modify(next_run_time=datetime.now())
    return {"status": "triggered", "job": job_name}


@router.get("/system/state")
async def system_state() -> dict:
    return get_state()


@router.post("/system/pause")
async def system_pause() -> dict:
    state = set_paused(True, by="dashboard")
    EventBus.emit("system_state", state)
    return state


@router.post("/system/resume")
async def system_resume() -> dict:
    state = set_paused(False)
    EventBus.emit("system_state", state)
    return state


@router.get("/system/claude")
async def claude_check() -> dict:
    """Is the Claude CLI installed and logged in?"""
    def _check():
        try:
            r = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
            return {"ok": r.returncode == 0, "version": r.stdout.strip() or r.stderr.strip()}
        except FileNotFoundError:
            return {"ok": False, "error": "claude CLI not found. npm install -g @anthropic-ai/claude-code"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return await asyncio.to_thread(_check)
