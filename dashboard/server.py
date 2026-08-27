"""
Prabhjot's Pipeline Dashboard — FastAPI server with REST API and WebSocket support.
Serves a local web dashboard at http://localhost:8080.

Architecture notes:
- REST endpoints handle all CRUD operations against the SQLite tracker.
- A single /ws WebSocket endpoint fans out EventBus events to every browser tab
  that is currently open, enabling live updates without polling.
- Background tasks (discover, rescore, score-all) are run via asyncio.create_task
  so the HTTP response returns immediately while work continues.
"""

import asyncio
from utils.browser import headed_supported
import base64
import copy
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("dashboard.server")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from utils.tracker import (
    get_all_jobs,
    get_job_by_id,
    update_job_status,
    update_job_notes,
    delete_job,
    get_stats,
    get_timeline_stats,
    get_score_distribution,
    get_companies,
    log_matched,
    log_skipped,
    get_unscored_jobs,
    VALID_STATUSES,
    ignore_jobs,
    purge_all,
    purge_everything,
    get_ignored_count,
)
from utils.events import EventBus

BASE_DIR = Path(__file__).parent
def _resumes_dir() -> Path:
    """Per-user resumes directory (repo-root resumes/ in legacy mode)."""
    from utils.usercontext import resumes_dir
    return resumes_dir()


def _resumes_meta() -> Path:
    return _resumes_dir() / "meta.json"


from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    """Start scheduler when server starts, stop when it shuts down."""
    try:
        from scheduler import setup_scheduler, start_scheduler
        setup_scheduler()
        start_scheduler()
    except Exception as e:
        print(f"  Scheduler start skipped: {e}")
    yield
    try:
        from scheduler import stop_scheduler
        stop_scheduler()
    except Exception:
        pass

app = FastAPI(title="Prabhjot's Pipeline", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Authentication — two modes:
#
#   Firebase (FIREBASE_PROJECT_ID set): multi-tenant. The frontend signs in
#     with the Firebase JS SDK and sends `Authorization: Bearer <id-token>`
#     on every request. Each verified user is bound to the request context,
#     which routes all data access to their own data directory.
#
#   Legacy: single-user. Optional HTTP Basic auth via DASHBOARD_PASSWORD
#     (required whenever hosted anywhere reachable); unset = open, local use.
# ---------------------------------------------------------------------------
import os
import secrets as _secrets

from fastapi.responses import Response, JSONResponse

from utils import usercontext
from utils.usercontext import multi_tenant_enabled
from utils.system_state import get_state as get_system_state, is_paused, set_paused

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# Reachable without credentials so container/platform healthchecks pass.
_AUTH_EXEMPT_PATHS = {"/api/health", "/api/config"}


def _basic_auth_ok(auth_header: Optional[str]) -> bool:
    if not DASHBOARD_PASSWORD:
        return True
    if not auth_header or not auth_header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
        user, _, password = decoded.partition(":")
    except Exception:
        return False
    user_ok = _secrets.compare_digest(user, DASHBOARD_USER)
    pass_ok = _secrets.compare_digest(password, DASHBOARD_PASSWORD)
    return user_ok and pass_ok


def _resolve_firebase_user(auth_header: Optional[str]) -> dict:
    """Verify the Bearer token and return the user. Raises AuthError."""
    from utils.auth import verify_firebase_token, AuthError

    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise AuthError("Missing bearer token")
    return verify_firebase_token(auth_header.split(" ", 1)[1])


# ---------------------------------------------------------------------------
# Global pause switch — while paused, endpoints that start automated or
# Claude-token-consuming work return 409. Reads, CRUD, cancel endpoints, and
# the pause/resume controls themselves stay available so the dashboard keeps
# working. Registered before the auth middleware so auth still runs first.
# ---------------------------------------------------------------------------
_PAUSE_GATED_EXACT = {
    "/api/discover",
    "/api/score-all",
    "/api/check-email",
    "/api/profile/score",
    "/api/yolo",
    "/api/apply-batch",
}
_PAUSE_GATED_PREFIXES = (
    "/api/rescore/",
    "/api/scheduler/trigger/",
    "/api/apply/",
    "/api/resolve-url/",
    "/api/email-apply/",
    "/api/bulk-apply/",
)
_PAUSE_EXEMPT = {
    "/api/apply/cancel",
    "/api/bulk-apply/cancel",
    "/api/email-apply/test",  # config check: no tokens, no browser, no send
}

# Who may flip the switch. The pause protects the deployer's Claude OAuth
# token, so in multi-tenant mode only the owner account(s) listed here can
# operate it — but a pause stops background work for EVERY user. Legacy
# single-user mode (the deployer is the only user) is always allowed.
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "prabhjottechs@gmail.com").split(",")
    if e.strip()
}


def _is_admin_user() -> bool:
    if not multi_tenant_enabled():
        return True
    try:
        return usercontext.get_current_user().get("email", "").lower() in ADMIN_EMAILS
    except Exception:
        return False


_PAUSED_DETAIL = (
    "System is paused — no automation or Claude usage until resumed. "
    "Press RESUME on the dashboard to continue."
)


def _pause_gated(path: str, method: str) -> bool:
    if method != "POST" or path in _PAUSE_EXEMPT:
        return False
    if path in _PAUSE_GATED_EXACT:
        return True
    if path.startswith("/api/jobs/") and path.endswith("/tailor"):
        return True
    return path.startswith(_PAUSE_GATED_PREFIXES)


@app.middleware("http")
async def _pause_middleware(request: Request, call_next):
    if _pause_gated(request.url.path, request.method) and is_paused():
        return JSONResponse({"detail": _PAUSED_DETAIL}, status_code=409)
    return await call_next(request)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path

    if multi_tenant_enabled():
        # The page shell, static assets, and config load before login;
        # they contain no user data. Every data route requires a token.
        if (
            path in _AUTH_EXEMPT_PATHS
            or path == "/"
            or path.startswith("/static/")
        ):
            return await call_next(request)
        from utils.auth import AuthError
        try:
            user = _resolve_firebase_user(request.headers.get("authorization"))
        except AuthError as e:
            return JSONResponse({"detail": str(e)}, status_code=401)
        usercontext.register_user(user["uid"], user["email"], user["name"])
        token = usercontext.set_current_user(user)
        try:
            return await call_next(request)
        finally:
            usercontext.reset_current_user(token)

    # Legacy single-user mode
    if DASHBOARD_PASSWORD and path not in _AUTH_EXEMPT_PATHS:
        if not _basic_auth_ok(request.headers.get("authorization")):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Prabhjot\'s Pipeline"'},
            )
    return await call_next(request)


@app.get("/api/config")
async def auth_config() -> dict:
    """
    Public bootstrap config for the frontend (pre-auth). Firebase web config
    values are public by design — security comes from token verification
    and the server-side allowlist, not from hiding these.
    """
    if multi_tenant_enabled():
        firebase_config = {}
        raw = os.environ.get("FIREBASE_WEB_CONFIG", "")
        if raw:
            try:
                firebase_config = json.loads(raw)
            except ValueError:
                logger.error("FIREBASE_WEB_CONFIG is not valid JSON")
        return {"auth": "firebase", "firebase": firebase_config}
    return {"auth": "basic" if DASHBOARD_PASSWORD else "none"}


@app.get("/api/health")
async def health() -> dict:
    """Health check for Docker and monitoring. Pre-auth: no per-user data."""
    if multi_tenant_enabled():
        return {"status": "ok"}
    return {"status": "ok", "profile": usercontext.profile_path().exists()}


# ---------------------------------------------------------------------------
# Static files and Jinja2 templates
# ---------------------------------------------------------------------------
app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------------------------------------------------------------------------
# WebSocket connection registry
# ---------------------------------------------------------------------------
# Connected dashboard tabs. In multi-tenant mode each socket is bound to the
# authenticated user's uid and only receives that user's events; in legacy
# mode the uid is LEGACY_UID and every event reaches every tab as before.
ws_clients: dict[WebSocket, str] = {}


def _json_safe(obj):
    """Make an object JSON-serializable (datetimes, sets, etc.)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if hasattr(obj, "__dict__"):
        return str(obj)
    return str(obj)


async def broadcast_event(event: dict) -> None:
    """
    Send a JSON event to connected WebSocket clients.

    Multi-tenant: the event's "uid" (stamped by EventBus, or bound here from
    the current context) selects which user's tabs receive it — never all.
    Legacy: every tab gets every event, as before.

    Dead connections are collected and removed after each broadcast
    to avoid growing the registry with stale entries.
    """
    target_uid = event.pop("uid", None)
    if multi_tenant_enabled() and target_uid is None:
        try:
            target_uid = usercontext.current_uid()
        except Exception:
            target_uid = None  # No owner — deliver to nobody rather than leak

    # Pre-serialize to catch any JSON issues before sending
    try:
        payload = json.dumps(event, default=_json_safe)
    except (TypeError, ValueError) as exc:
        logger.error("broadcast_event: JSON serialization failed: %s", exc)
        # Send a simplified error event instead
        payload = json.dumps({
            "type": event.get("type", "unknown"),
            "data": {"error": f"Serialization error: {exc}"},
        })

    dead: list[WebSocket] = []
    for ws, ws_uid in list(ws_clients.items()):
        if multi_tenant_enabled() and ws_uid != target_uid:
            continue
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.pop(ws, None)


# ---------------------------------------------------------------------------
# Bridge EventBus (sync) -> WebSocket broadcast (async)
# ---------------------------------------------------------------------------
def _on_event(event: dict) -> None:
    """
    Callback registered with EventBus.

    EventBus.emit() is called from synchronous tracker functions, so we must
    schedule the coroutine on whatever event loop is currently running.
    asyncio.ensure_future is safe to call from sync code that runs within an
    async context (i.e. while uvicorn's loop is active).
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(broadcast_event(event))
    except RuntimeError:
        pass  # No event loop — running outside server context, skip


EventBus.subscribe(_on_event)


# ===========================================================================
# HTML Route
# ===========================================================================

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Serve the single-page dashboard."""
    return templates.TemplateResponse(request, "index.html")


# ===========================================================================
# REST API — Jobs
# ===========================================================================

@app.get("/api/jobs")
async def list_jobs(
    status: Optional[str] = None,
    company: Optional[str] = None,
    min_score: Optional[int] = None,
    search: Optional[str] = None,
    sort_by: str = "discovered_at",
    sort_order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """
    Return a paginated, filtered list of jobs.

    Query parameters:
        status      — filter by exact status string
        company     — partial match against company name
        min_score   — only jobs with match_score >= this value
        search      — full-text search across title, company, location
        sort_by     — column name (discovered_at, match_score, company, title,
                      status, applied_at)
        sort_order  — asc | desc
        limit       — page size (default 50)
        offset      — pagination offset (default 0)
    """
    jobs, total = get_all_jobs(
        status=status,
        company=company,
        min_score=min_score,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    return {"jobs": jobs, "total": total, "limit": limit, "offset": offset}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    """Return a single job record by ID."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.patch("/api/jobs/{job_id}")
async def update_job(job_id: str, body: dict) -> dict:
    """
    Partially update a job.

    Accepted body keys:
        status  — must be a member of VALID_STATUSES
        notes   — free-text string
    """
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if "status" in body:
        if not update_job_status(job_id, body["status"]):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status. Valid values: {VALID_STATUSES}",
            )

    if "notes" in body:
        update_job_notes(job_id, body["notes"])

    return get_job_by_id(job_id)


@app.delete("/api/jobs/{job_id}")
async def remove_job(job_id: str) -> dict:
    """Permanently delete a job record."""
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True}


# ===========================================================================
# REST API — Ignore & Purge
# ===========================================================================

@app.post("/api/jobs/ignore")
async def ignore_selected(body: dict) -> dict:
    """Mark selected jobs as ignored. They won't reappear on future discovery runs.
    Body: {"job_ids": ["id1", "id2", ...]}
    """
    job_ids = body.get("job_ids", [])
    if not job_ids:
        raise HTTPException(status_code=400, detail="No job_ids provided")
    count = ignore_jobs(job_ids)
    return {"ok": True, "ignored": count}


@app.post("/api/purge")
async def purge(body: dict = {}) -> dict:
    """Nuke discovery data.
    Body: {"keep_ignore_list": true} — preserves ignore list (default).
          {"keep_ignore_list": false} — full factory reset.
    """
    keep = body.get("keep_ignore_list", True)
    if keep:
        result = purge_all()
    else:
        result = purge_everything()
    return {"ok": True, **result}


@app.get("/api/ignored/count")
async def ignored_count() -> dict:
    """Return the number of ignored job hashes."""
    return {"count": get_ignored_count()}


# ===========================================================================
# REST API — Stats and metadata
# ===========================================================================

@app.get("/api/stats")
async def stats() -> dict:
    """Return aggregate counts and average score."""
    return get_stats()


@app.get("/api/stats/timeline")
async def timeline() -> list:
    """Return per-day discovery and application counts (last 30 days)."""
    return get_timeline_stats()


@app.get("/api/stats/scores")
async def scores() -> list:
    """Return match-score distribution bucketed into score brackets."""
    return get_score_distribution()


@app.get("/api/companies")
async def companies() -> list:
    """Return distinct company names for the filter dropdown."""
    return get_companies()


@app.get("/api/statuses")
async def statuses() -> list:
    """Return all valid status strings."""
    return VALID_STATUSES


# ===========================================================================
# REST API — Follow-ups & Ghost Detection
# ===========================================================================

@app.get("/api/follow-ups")
async def get_follow_ups() -> dict:
    """Get overdue follow-ups and ghost alerts."""
    from utils.tracker import get_overdue_follow_ups, get_ghost_alerts
    return {
        "overdue": get_overdue_follow_ups(),
        "ghosts": get_ghost_alerts(days=14),
    }


@app.post("/api/jobs/{job_id}/follow-up")
async def mark_follow_up(job_id: str, body: dict = {}) -> dict:
    """Mark follow-up done and reschedule."""
    from utils.tracker import increment_follow_up
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    days = body.get("next_days", 7)
    increment_follow_up(job_id, days=days)
    return {"ok": True, "job_id": job_id, "next_days": days}


@app.post("/api/jobs/{job_id}/dismiss-follow-up")
async def dismiss_follow_up_endpoint(job_id: str) -> dict:
    """Clear follow-up reminder for a job."""
    from utils.tracker import dismiss_follow_up
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    dismiss_follow_up(job_id)
    return {"ok": True, "job_id": job_id}


# ===========================================================================
# REST API — Profile
# ===========================================================================

@app.get("/api/profile")
async def get_profile() -> dict:
    """Return the current profile.yaml as JSON, or signal setup needed."""
    import yaml
    profile_path = usercontext.profile_path()
    if not profile_path.exists():
        return {"needs_setup": True}
    with open(profile_path) as f:
        return yaml.safe_load(f)


@app.get("/api/sources")
async def get_sources() -> list:
    """Catalog of discovery sources with their current on/off state.
    Toggle via PATCH /api/profile with {"sources": {"<key>": bool}}."""
    import yaml
    from utils.discovery import SOURCE_REGISTRY, source_enabled
    profile = {}
    profile_path = usercontext.profile_path()
    if profile_path.exists():
        with open(profile_path) as f:
            profile = yaml.safe_load(f) or {}
    return [
        {**entry, "enabled": source_enabled(profile, entry["key"])}
        for entry in SOURCE_REGISTRY
    ]


@app.post("/api/setup")
async def run_setup(body: dict) -> dict:
    """First-run wizard: create profile.yaml from wizard data."""
    import yaml
    profile_path = usercontext.profile_path()
    example_path = BASE_DIR.parent / "profile.yaml.example"

    # Load defaults from example
    defaults = {}
    if example_path.exists():
        with open(example_path) as f:
            defaults = yaml.safe_load(f) or {}

    # Deep merge wizard data over defaults
    def deep_merge(base, updates):
        for key, value in updates.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                deep_merge(base[key], value)
            else:
                base[key] = value

    deep_merge(defaults, body)

    with open(profile_path, "w") as f:
        yaml.dump(defaults, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    # Try to start scheduler now that profile exists
    try:
        from scheduler import setup_scheduler, start_scheduler
        setup_scheduler()
        start_scheduler()
    except Exception:
        pass

    return defaults


@app.patch("/api/profile")
async def update_profile(body: dict) -> dict:
    """Partially update profile.yaml with recursive deep merge."""
    import yaml
    profile_path = usercontext.profile_path()
    with open(profile_path) as f:
        profile = yaml.safe_load(f)

    def deep_merge(base, updates):
        for key, value in updates.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                deep_merge(base[key], value)
            else:
                base[key] = value

    deep_merge(profile, body)

    with open(profile_path, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    # Clear LLM backend cache so new ai config takes effect
    try:
        from utils.llm import clear_backend_cache
        clear_backend_cache()
    except ImportError:
        pass

    return profile


@app.post("/api/profile/score")
async def score_profile_endpoint() -> dict:
    """
    AI-powered profile and resume analysis.
    Reads profile.yaml + resume PDF, sends to Claude for scoring.
    Returns strengths, gaps, resume suggestions, role fit rankings.
    """
    import yaml

    profile_path = usercontext.profile_path()
    with open(profile_path) as f:
        profile = yaml.safe_load(f)

    async def _do_score():
        try:
            from utils.brain import ClaudeBrain
            from utils.resume_parser import extract_resume_text

            await broadcast_event({"type": "profile_score_started", "data": {}})

            brain = ClaudeBrain(verbose=False, profile=profile)
            resume_path = profile.get("resume_path", "")
            resume_text = extract_resume_text(resume_path) if resume_path else ""

            result = brain.score_profile(profile, resume_text)

            await broadcast_event({
                "type": "profile_score_complete",
                "data": result,
            })
        except Exception as exc:
            await broadcast_event({
                "type": "profile_score_error",
                "data": {"error": str(exc)},
            })

    asyncio.create_task(_do_score())
    return {"status": "started"}


# ===========================================================================
# REST API — Resume Management
# ===========================================================================

def _load_resume_meta() -> dict:
    _resumes_dir().mkdir(exist_ok=True)
    if _resumes_meta().exists():
        return json.loads(_resumes_meta().read_text())
    return {}

def _save_resume_meta(meta: dict):
    _resumes_dir().mkdir(exist_ok=True)
    _resumes_meta().write_text(json.dumps(meta, indent=2))

def _update_profile_resume_path(path: str):
    import yaml
    # Store relative to the app root: profile.yaml is bind-mounted into Docker,
    # so an absolute path like /app/resumes/x.pdf would break the host-side CLI.
    try:
        path = "./" + str(Path(path).relative_to(usercontext.BASE_DIR))
    except ValueError:
        pass
    profile_path = usercontext.profile_path()
    with open(profile_path) as f:
        profile = yaml.safe_load(f)
    profile["resume_path"] = path
    with open(profile_path, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


@app.get("/api/resumes")
async def list_resumes() -> list:
    """List all stored resumes with metadata."""
    meta = _load_resume_meta()
    return [
        {"name": name, **info, "exists": (_resumes_dir() / info["filename"]).exists()}
        for name, info in meta.items()
    ]


@app.post("/api/resumes")
async def upload_resume(file: UploadFile = File(...), name: str = Form(...)) -> dict:
    """Upload a resume PDF with a display name."""
    _resumes_dir().mkdir(exist_ok=True)
    meta = _load_resume_meta()

    safe_name = "".join(c for c in name if c.isalnum() or c in " -_").strip()
    if not safe_name:
        raise HTTPException(400, "Invalid resume name")

    filename = f"{safe_name.replace(' ', '_')}_{file.filename}"
    dest = _resumes_dir() / filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Re-uploading under an existing name (e.g. the wizard's "Replace File")
    # must keep that entry's default status instead of dropping it.
    is_default = len(meta) == 0 or meta.get(safe_name, {}).get("is_default", False)
    meta[safe_name] = {
        "filename": filename,
        "original_name": file.filename,
        "is_default": is_default,
    }
    _save_resume_meta(meta)

    if is_default:
        _update_profile_resume_path(str(dest))

    return {"name": safe_name, "filename": filename, "is_default": is_default}


@app.delete("/api/resumes/{name}")
async def delete_resume(name: str) -> dict:
    """Remove a resume by display name."""
    meta = _load_resume_meta()
    if name not in meta:
        raise HTTPException(404, "Resume not found")
    filepath = _resumes_dir() / meta[name]["filename"]
    was_default = meta[name].get("is_default", False)
    if filepath.exists():
        filepath.unlink()
    del meta[name]
    if was_default and meta:
        first_key = next(iter(meta))
        meta[first_key]["is_default"] = True
        _update_profile_resume_path(str(_resumes_dir() / meta[first_key]["filename"]))
    _save_resume_meta(meta)
    return {"ok": True}


@app.patch("/api/resumes/{name}/default")
async def set_default_resume(name: str) -> dict:
    """Set a resume as the default for applications."""
    meta = _load_resume_meta()
    if name not in meta:
        raise HTTPException(404, "Resume not found")
    for key in meta:
        meta[key]["is_default"] = (key == name)
    _save_resume_meta(meta)
    _update_profile_resume_path(str(_resumes_dir() / meta[name]["filename"]))
    return {"ok": True, "default": name}


@app.get("/api/resumes/{name}/download")
async def download_resume(name: str):
    """Download a resume file."""
    meta = _load_resume_meta()
    if name not in meta:
        raise HTTPException(404, "Resume not found")
    filepath = _resumes_dir() / meta[name]["filename"]
    if not filepath.exists():
        raise HTTPException(404, "File not found on disk")
    return FileResponse(filepath, filename=meta[name].get("original_name", meta[name]["filename"]))


# ===========================================================================
# REST API — Background actions
# ===========================================================================

# Only one discovery run at a time — overlapping runs hammer the job boards
# (rate-limit risk) and multiply an already multi-minute scrape.
_discovery_running = False


def _incremental_saver(broadcast: bool = True):
    """
    Build an on_source_jobs callback for discover_all_jobs that persists each
    source's results to SQLite as soon as that source finishes, so jobs appear
    on the dashboard within seconds instead of after the full run — and survive
    a mid-run crash or container restart.

    Returns (callback, counts) where counts accumulates {"total", "new"}.
    """
    from utils.tracker import is_already_seen, log_discovered

    seen_keys: set = set()
    counts = {"total": 0, "new": 0}

    async def on_source_jobs(source: str, jobs: list) -> None:
        new_in_source = 0
        for job in jobs:
            key = (job.title.lower().strip(), job.company.lower().strip())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            counts["total"] += 1
            if not is_already_seen(job.id):
                log_discovered(job)
                new_in_source += 1
                counts["new"] += 1
        if broadcast:
            await broadcast_event(
                {
                    "type": "discovery_progress",
                    "data": {
                        "source": source,
                        "found": len(jobs),
                        "new": new_in_source,
                        "total_new": counts["new"],
                    },
                }
            )

    return on_source_jobs, counts


@app.post("/api/discover")
async def trigger_discover() -> dict:
    """
    Launch a full discovery run in the background.

    The endpoint returns immediately with {"status": "started"}, or
    {"status": "already_running"} if a run is in progress.
    Progress events (discovery_started, discovery_progress per source,
    discovery_complete, discovery_error) are broadcast over the WebSocket,
    and jobs are saved incrementally as each source finishes.
    """
    import yaml

    global _discovery_running
    if _discovery_running:
        return {"status": "already_running"}
    _discovery_running = True

    profile_path = usercontext.profile_path()
    with open(profile_path) as f:
        profile = yaml.safe_load(f)

    async def _run_discovery() -> None:
        global _discovery_running
        try:
            await broadcast_event({"type": "discovery_started", "data": {}})

            from utils.discovery import discover_all_jobs

            on_source_jobs, counts = _incremental_saver()
            await discover_all_jobs(profile, on_source_jobs=on_source_jobs)

            await broadcast_event(
                {
                    "type": "discovery_complete",
                    "data": {"total": counts["total"], "new": counts["new"]},
                }
            )
        except Exception as exc:
            await broadcast_event(
                {"type": "discovery_error", "data": {"error": str(exc)}}
            )
        finally:
            _discovery_running = False

    asyncio.create_task(_run_discovery())
    return {"status": "started"}


@app.post("/api/rescore/{job_id}")
async def rescore_job(job_id: str) -> dict:
    """
    Re-score a single job with the Claude brain.

    The endpoint returns immediately. A rescore_complete or rescore_error
    WebSocket event is emitted when finished.
    """
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def _do_rescore() -> None:
        try:
            import yaml
            from utils.brain import ClaudeBrain

            profile_path = usercontext.profile_path()
            with open(profile_path) as f:
                profile = yaml.safe_load(f)

            brain = ClaudeBrain(verbose=False, profile=profile)
            from utils.resume_parser import extract_resume_text
            resume_text = extract_resume_text(profile.get("resume_path", ""))
            desc = (
                job.get("description", "")
                or f"Job: {job['title']} at {job['company']}. Location: {job['location']}"
            )
            result = await asyncio.to_thread(brain.match_job, desc, profile, resume_text=resume_text)
            score: int = result.get("score", 0)
            reasoning: str = result.get("reasoning", "")
            cover_letter: str = result.get("cover_letter", "")

            log_matched(job_id, score, reasoning, cover_letter)

            min_score: int = profile["preferences"].get("min_match_score", 65)
            if score < min_score:
                log_skipped(job_id, f"Score {score} < {min_score}")

            await broadcast_event(
                {"type": "rescore_complete", "data": {"id": job_id, "score": score}}
            )
        except Exception as exc:
            await broadcast_event(
                {
                    "type": "rescore_error",
                    "data": {"id": job_id, "error": str(exc)},
                }
            )

    asyncio.create_task(_do_rescore())
    return {"status": "started"}


@app.post("/api/jobs/{job_id}/tailor")
async def tailor_job(job_id: str) -> dict:
    """Generate tailored resume content for a specific job."""
    from utils.tracker import get_tailored_resume, update_tailored_resume

    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def _do_tailor():
        try:
            import yaml
            from utils.resume_tailor import tailor_resume
            from utils.resume_parser import extract_resume_text
            from utils.brain import ClaudeBrain

            profile_path = usercontext.profile_path()
            with open(profile_path) as f:
                profile = yaml.safe_load(f)

            resume_text = extract_resume_text(profile.get("resume_path", ""))
            desc = job.get("description", "") or f"Job: {job['title']} at {job['company']}"

            brain = ClaudeBrain(verbose=False, profile=profile)
            result = tailor_resume(desc, resume_text, profile, brain=brain)

            update_tailored_resume(job_id, result)

            await broadcast_event({
                "type": "tailor_complete",
                "data": {"id": job_id, "has_content": bool(result.get("tailored_summary"))}
            })
        except Exception as exc:
            await broadcast_event({
                "type": "tailor_error",
                "data": {"id": job_id, "error": str(exc)}
            })

    asyncio.create_task(_do_tailor())
    return {"status": "started", "job_id": job_id}


@app.get("/api/jobs/{job_id}/tailor")
async def get_tailor(job_id: str) -> dict:
    """Get tailored resume content for a job."""
    from utils.tracker import get_tailored_resume
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return get_tailored_resume(job_id)


@app.post("/api/score-all")
async def score_all_unscored() -> dict:
    """
    Score every job that currently has no match_score.

    Returns {"status": "started", "count": N} immediately.
    A score_all_complete or score_all_error WebSocket event follows.
    """
    unscored = get_unscored_jobs()

    async def _do_score_all() -> None:
        try:
            import yaml
            from utils.brain import ClaudeBrain

            profile_path = usercontext.profile_path()
            with open(profile_path) as f:
                profile = yaml.safe_load(f)

            brain = ClaudeBrain(verbose=False, profile=profile)
            from utils.resume_parser import extract_resume_text
            resume_text = extract_resume_text(profile.get("resume_path", ""))
            min_score: int = profile["preferences"].get("min_match_score", 65)

            scored_ok = 0
            consecutive_failures = 0
            last_error = ""
            for job_row in unscored:
                if is_paused():
                    break
                try:
                    desc = (
                        job_row.get("description", "")
                        or (
                            f"Job: {job_row['title']} at {job_row['company']}. "
                            f"Location: {job_row['location']}"
                        )
                    )
                    result = await asyncio.to_thread(brain.match_job, desc, profile, resume_text=resume_text)
                    score: int = result.get("score", 0)
                    log_matched(
                        job_row["id"],
                        score,
                        result.get("reasoning", ""),
                        result.get("cover_letter", ""),
                    )
                    if score < min_score:
                        log_skipped(job_row["id"], f"Score {score} < {min_score}")
                    scored_ok += 1
                    consecutive_failures = 0
                except Exception as exc:
                    # Tolerate isolated failures, but a backend that fails on every
                    # job (bad auth token, CLI missing) would otherwise silently
                    # churn for hours and then report "complete".
                    consecutive_failures += 1
                    last_error = str(exc)[:300]
                    # Abort on a dead backend: immediately when nothing has ever
                    # scored, or after a sustained mid-run failure streak (auth
                    # expiry, usage-limit exhaustion) — never churn silently.
                    if (scored_ok == 0 and consecutive_failures >= 3) or consecutive_failures >= 15:
                        msg = (
                            f"AI backend failed {consecutive_failures} times in a row "
                            f"({scored_ok} scored before that) — aborting. "
                            "Check CLAUDE_CODE_OAUTH_TOKEN / usage limits, then re-run. "
                            f"Last error: {last_error}"
                        )
                        print(f"  ✗ Score-all aborted: {msg}")
                        await broadcast_event(
                            {"type": "score_all_error", "data": {"error": msg}}
                        )
                        return

            await broadcast_event(
                {"type": "score_all_complete", "data": {"count": len(unscored)}}
            )
        except Exception as exc:
            await broadcast_event(
                {"type": "score_all_error", "data": {"error": str(exc)}}
            )

    asyncio.create_task(_do_score_all())
    return {"status": "started", "count": len(unscored)}


# ===========================================================================
# REST API — Apply (Form Filling)
# ===========================================================================

# Track active apply sessions so we can report status / cancel
# Per-user apply-session state (keyed by uid; one entry total in legacy mode).
_apply_states: dict = {}


def _apply_state() -> dict:
    uid = usercontext.current_uid()
    return _apply_states.setdefault(uid, {
        "running": False,
        "job_id": None,
        "progress": [],  # list of {job_id, status, message}
        "cancel_requested": False,
    })


@app.post("/api/apply/{job_id}")
async def apply_single_job(job_id: str, body: dict = {}) -> dict:
    """
    Apply to a single job by ID. Launches Playwright, fills the form, submits.

    Body options:
        dry_run  — bool, default True. If True, fills form but doesn't submit.
    """
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.get("apply_url"):
        raise HTTPException(status_code=400, detail="Job has no apply URL")
    if _apply_state()["running"]:
        raise HTTPException(status_code=409, detail="An apply session is already running")

    dry_run = body.get("dry_run", True)

    async def _do_apply():
        _apply_state()["running"] = True
        _apply_state()["job_id"] = job_id
        _apply_state()["cancel_requested"] = False

        try:
            import yaml
            from playwright.async_api import async_playwright
            from utils.brain import ClaudeBrain
            from adapters.stagehand_adapter import apply_smart
            from utils.tracker import log_applied

            profile_path = usercontext.profile_path()
            with open(profile_path) as f:
                profile = yaml.safe_load(f)

            brain = ClaudeBrain(verbose=False, profile=profile)
            cover_letter = job.get("cover_letter", "")

            await broadcast_event({
                "type": "apply_started",
                "data": {
                    "job_id": job_id,
                    "title": job["title"],
                    "company": job["company"],
                    "dry_run": dry_run,
                }
            })

            async with async_playwright() as p:
                from utils.browser import launch_apply_browser
                page, close_browser = await launch_apply_browser(p)

                platform = job.get("platform", "")
                apply_url = job["apply_url"]

                success = await apply_smart(
                    page, apply_url, profile, brain,
                    cover_letter=cover_letter, dry_run=dry_run,
                    platform=platform,
                    company=job.get("company", ""),
                    title=job.get("title", ""),
                    description=job.get("description", ""),
                )

                if not dry_run:
                    log_applied(job_id, success)

                await broadcast_event({
                    "type": "apply_complete",
                    "data": {
                        "job_id": job_id,
                        "success": success,
                        "dry_run": dry_run,
                        "title": job["title"],
                        "company": job["company"],
                    }
                })

                # Keep browser open for a bit so user can review
                await asyncio.sleep(5)
                await close_browser()

        except Exception as exc:
            await broadcast_event({
                "type": "apply_error",
                "data": {"job_id": job_id, "error": str(exc)}
            })
        finally:
            _apply_state()["running"] = False
            _apply_state()["job_id"] = None

    asyncio.create_task(_do_apply())
    return {"status": "started", "job_id": job_id, "dry_run": dry_run}


@app.post("/api/apply-batch")
async def apply_batch(body: dict = {}) -> dict:
    """
    Apply to all matched jobs above the score threshold — or, when `job_ids`
    is given, to exactly that user-selected set (bulk apply).

    Body options:
        dry_run   — bool, default True
        max_count — int, max applications this batch (default 10)
        min_score — int, override minimum score (default from profile)
        job_ids   — list[str], apply to these specific jobs (skips the
                    score filter; the user picked them explicitly)
    """
    if _apply_state()["running"]:
        raise HTTPException(status_code=409, detail="An apply session is already running")

    dry_run = body.get("dry_run", True)
    max_count = body.get("max_count", 10)
    min_score_override = body.get("min_score")
    job_ids = body.get("job_ids")

    import yaml
    profile_path = usercontext.profile_path()
    with open(profile_path) as f:
        profile = yaml.safe_load(f)

    min_score = min_score_override or profile["preferences"].get("min_match_score", 65)
    rate_limits = profile.get("rate_limits", {})
    max_per_day = rate_limits.get("max_applications_per_day", 25)

    from utils.tracker import get_all_jobs as get_jobs_filtered, get_today_count

    if job_ids:
        # Explicit selection: the user chose these — no score gate
        selected = [get_job_by_id(jid) for jid in job_ids]
        eligible = [
            j for j in selected
            if j and j.get("apply_url") and j.get("status") not in ("applied", "ignored")
        ][:max(max_count, len(job_ids))]
    else:
        # Get matched jobs that haven't been applied to yet
        matched_jobs, _ = get_jobs_filtered(status="matched", sort_by="match_score", sort_order="desc", limit=500)
        # Filter to only jobs with apply URLs and above score threshold
        eligible = [
            j for j in matched_jobs
            if j.get("apply_url")
            and (j.get("match_score") or 0) >= min_score
        ][:max_count]

    if not eligible:
        return {"status": "no_eligible_jobs", "count": 0}

    today_count = get_today_count()
    remaining_today = max(0, max_per_day - today_count)
    eligible = eligible[:remaining_today]

    if not eligible:
        return {"status": "daily_limit_reached", "today": today_count, "max": max_per_day}

    async def _do_batch():
        import random
        _apply_state()["running"] = True
        _apply_state()["progress"] = []
        _apply_state()["cancel_requested"] = False

        try:
            from playwright.async_api import async_playwright
            from utils.brain import ClaudeBrain
            from adapters.stagehand_adapter import apply_smart
            from utils.tracker import log_applied

            brain = ClaudeBrain(verbose=False, profile=profile)
            min_delay = rate_limits.get("min_delay_seconds", 60)
            max_delay = rate_limits.get("max_delay_seconds", 180)

            await broadcast_event({
                "type": "apply_batch_started",
                "data": {"count": len(eligible), "dry_run": dry_run}
            })

            async with async_playwright() as p:
                from utils.browser import launch_apply_browser
                page, close_browser = await launch_apply_browser(p)

                applied_count = 0
                for i, job in enumerate(eligible):
                    if _apply_state()["cancel_requested"] or is_paused():
                        await broadcast_event({
                            "type": "apply_batch_cancelled",
                            "data": {"applied": applied_count, "cancelled_at": i}
                        })
                        break

                    job_id = job["id"]
                    _apply_state()["job_id"] = job_id

                    await broadcast_event({
                        "type": "apply_progress",
                        "data": {
                            "current": i + 1,
                            "total": len(eligible),
                            "job_id": job_id,
                            "title": job["title"],
                            "company": job["company"],
                            "dry_run": dry_run,
                        }
                    })

                    try:
                        cover_letter = job.get("cover_letter", "")
                        platform = job.get("platform", "")
                        apply_url = job["apply_url"]

                        success = await apply_smart(
                            page, apply_url, profile, brain,
                            cover_letter=cover_letter, dry_run=dry_run,
                            platform=platform,
                            company=job.get("company", ""),
                            title=job.get("title", ""),
                            description=job.get("description", ""),
                        )

                        if not dry_run:
                            log_applied(job_id, success)

                        applied_count += 1
                        _apply_state()["progress"].append({
                            "job_id": job_id, "status": "success" if success else "failed",
                            "title": job["title"], "company": job["company"],
                        })

                    except Exception as exc:
                        if not dry_run:
                            log_applied(job_id, False)
                        _apply_state()["progress"].append({
                            "job_id": job_id, "status": "error",
                            "error": str(exc), "title": job["title"],
                            "company": job["company"],
                        })

                    # Rate limiting between applications
                    if i < len(eligible) - 1:
                        delay = random.randint(min_delay, max_delay)
                        await broadcast_event({
                            "type": "apply_waiting",
                            "data": {"seconds": delay, "next_index": i + 2}
                        })
                        await asyncio.sleep(delay)

                await close_browser()

            await broadcast_event({
                "type": "apply_batch_complete",
                "data": {
                    "applied": applied_count,
                    "total": len(eligible),
                    "dry_run": dry_run,
                    "results": _apply_state()["progress"],
                }
            })

        except Exception as exc:
            await broadcast_event({
                "type": "apply_batch_error",
                "data": {"error": str(exc)}
            })
        finally:
            _apply_state()["running"] = False
            _apply_state()["job_id"] = None

    asyncio.create_task(_do_batch())
    return {
        "status": "started",
        "count": len(eligible),
        "dry_run": dry_run,
        "daily_remaining": remaining_today,
    }


@app.get("/api/apply/status")
async def apply_status() -> dict:
    """Get the current apply session status."""
    return {
        "running": _apply_state()["running"],
        "current_job_id": _apply_state()["job_id"],
        "progress": _apply_state()["progress"],
    }


@app.post("/api/apply/cancel")
async def cancel_apply() -> dict:
    """Request cancellation of the current batch apply."""
    if not _apply_state()["running"]:
        return {"status": "not_running"}
    _apply_state()["cancel_requested"] = True
    return {"status": "cancel_requested"}


@app.post("/api/resolve-url/{job_id}")
async def resolve_url(job_id: str) -> dict:
    """
    Resolve a job's aggregator URL to a real ATS application form URL.
    Updates the job's apply_url in the database if resolved.
    """
    from utils.url_resolver import resolve_apply_url, is_ats_url
    from utils.tracker import get_job_by_id, update_apply_url

    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    current_url = job.get("apply_url") or job.get("url", "")
    if is_ats_url(current_url):
        return {"status": "already_ats", "url": current_url}

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            result = await resolve_apply_url(
                page,
                job_url=current_url,
                company=job.get("company", ""),
                title=job.get("title", ""),
                description=job.get("description", ""),
                platform=job.get("platform", ""),
            )

            await browser.close()

        if result["resolved_url"] != current_url:
            update_apply_url(job_id, result["resolved_url"])

        return {
            "status": result["resolution"],
            "original_url": result["original_url"],
            "resolved_url": result["resolved_url"],
            "apply_email": result.get("apply_email"),
            "company_careers": result.get("company_careers"),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ===========================================================================
# REST API — Email Apply & Bulk Apply
#
# Flow: prepare (extract emails + compose drafts) → user reviews/edits every
# draft in the dashboard → send (explicit confirmation). Emails are NEVER
# sent without the user pressing send. Jobs without a hiring inbox fall back
# to the browser pipeline via /api/apply-batch {job_ids}.
# ===========================================================================

# Per-user bulk-apply state (keyed by uid; one entry total in legacy mode).
_bulk_states: dict = {}


def _bulk_state() -> dict:
    uid = usercontext.current_uid()
    return _bulk_states.setdefault(uid, {
        "running": False,
        "phase": None,          # "prepare" | "send" | None
        "drafts": [],           # [{job_id, title, company, to, subject, body}]
        "browser_jobs": [],     # jobs with an apply_url but no email
        "skipped": [],          # jobs with neither
        "progress": [],         # send results
        "cancel_requested": False,
    })


def _load_profile() -> dict:
    import yaml
    with open(usercontext.profile_path()) as f:
        return yaml.safe_load(f)


@app.post("/api/scan-emails")
async def scan_apply_emails() -> dict:
    """Regex-backfill apply_email for all stored jobs. No tokens, no network."""
    from utils.email_apply import backfill_apply_emails
    result = await asyncio.to_thread(backfill_apply_emails)
    await broadcast_event({"type": "email_scan_complete", "data": result})
    return result


@app.get("/api/email-apply/config")
async def email_apply_config() -> dict:
    """Whether outbound email is configured (never exposes the password)."""
    from utils.email_apply import smtp_config
    try:
        cfg = smtp_config(_load_profile())
        return {"configured": True, "from": cfg["email"],
                "server": cfg["server"], "port": cfg["port"]}
    except Exception as e:
        return {"configured": False, "reason": str(e)}


@app.get("/api/browser-login/status")
async def browser_login_status() -> dict:
    """
    Saved browser sessions from `main.py login` — domain names and age only,
    never the tokens themselves.
    """
    from utils.browser import login_state_path
    path = login_state_path()
    if not path.exists():
        return {"exists": False}
    try:
        state = json.loads(path.read_text())
        cookies = state.get("cookies", [])
        domains = sorted({c.get("domain", "").lstrip(".")
                          for c in cookies if c.get("domain")})
        return {
            "exists": True,
            "saved_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
            "cookies": len(cookies),
            "domains": domains[:20],
        }
    except FileNotFoundError:
        return {"exists": False}  # deleted between check and read
    except Exception as e:
        return {"exists": True, "error": str(e)}


@app.post("/api/browser-login/upload")
async def browser_login_upload(body: dict) -> dict:
    """
    Session sync for hosted deployments: accept a Playwright storage_state
    export (the `.cache/login_state.json` written by `main.py login` on the
    user's machine) and store it in this instance's per-user cache, so
    browser applies here start logged in. Auth middleware already scopes
    this to the calling user.
    """
    from utils.browser import login_state_path

    cookies = body.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise HTTPException(
            status_code=400,
            detail="Not a login-state export — expected the JSON written by "
                   "`python3.11 main.py login` (.cache/login_state.json)",
        )
    if len(cookies) > 5000:
        raise HTTPException(status_code=400, detail="Too many cookies (max 5000)")
    for c in cookies:
        if not (isinstance(c, dict) and c.get("name") and "value" in c):
            raise HTTPException(status_code=400, detail="Malformed cookie entry")

    state = {"cookies": cookies, "origins": body.get("origins", [])}
    path = login_state_path()
    path.write_text(json.dumps(state))
    os.chmod(path, 0o600)

    domains = sorted({c.get("domain", "").lstrip(".")
                      for c in cookies if c.get("domain")})
    await broadcast_event({
        "type": "browser_login_updated",
        "data": {"cookies": len(cookies), "domains": domains[:20]},
    })
    return {"status": "saved", "cookies": len(cookies), "domains": domains[:20]}


@app.delete("/api/browser-login")
async def browser_login_clear() -> dict:
    """Delete the stored login sessions for this user."""
    from utils.browser import login_state_path
    path = login_state_path()
    existed = path.exists()
    if existed:
        path.unlink()
    await broadcast_event({"type": "browser_login_updated",
                           "data": {"cookies": 0, "domains": []}})
    return {"status": "cleared" if existed else "nothing_to_clear"}


@app.post("/api/email-apply/test")
async def email_apply_test() -> dict:
    """Log in to the SMTP server to verify credentials. Sends nothing."""
    from utils.email_apply import test_smtp_login
    try:
        result = await asyncio.to_thread(test_smtp_login, _load_profile())
        return {"ok": True, **result}
    except ValueError as e:  # not configured
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False,
                "error": f"SMTP login failed: {e}. For Gmail, make sure you're "
                         f"using an App Password (not your account password)."}


@app.post("/api/email-apply/{job_id}/draft")
async def email_apply_draft(job_id: str) -> dict:
    """
    Build (but do not send) an application email draft for one job.
    Uses the stored cover letter when present; AI compose otherwise.
    """
    from utils.email_apply import extract_apply_email, compose_application_email
    from utils.tracker import update_apply_email

    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    apply_email = job.get("apply_email") or ""
    if not apply_email:
        apply_email = extract_apply_email(job.get("description") or "") or ""
        if apply_email:
            update_apply_email(job_id, apply_email)
            job["apply_email"] = apply_email
    if not apply_email:
        raise HTTPException(
            status_code=400,
            detail="No application email found in this posting. "
                   "Use the browser APPLY instead.",
        )

    profile = _load_profile()
    brain = None
    if not (job.get("cover_letter") or "").strip():
        from utils.brain import ClaudeBrain
        brain = ClaudeBrain(verbose=False, profile=profile)

    draft = await asyncio.to_thread(compose_application_email, job, profile, brain)
    return {
        "job_id": job_id,
        "title": job.get("title"),
        "company": job.get("company"),
        **draft,
        "resume": profile.get("resume_path", ""),
    }


@app.post("/api/email-apply/{job_id}/send")
async def email_apply_send(job_id: str, body: dict) -> dict:
    """
    Send ONE user-reviewed application email. The dashboard only calls this
    after the user has seen the draft and pressed send.

    Body: {to, subject, body}
    """
    from utils.email_apply import send_application_email, smtp_config
    from utils.tracker import log_applied, update_job_notes

    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    to = (body.get("to") or "").strip()
    subject = (body.get("subject") or "").strip()
    text = (body.get("body") or "").strip()
    if not to or not subject or not text:
        raise HTTPException(status_code=400, detail="to, subject, and body are required")

    profile = _load_profile()
    try:
        smtp_config(profile)  # fail fast with the config message
        resume = profile.get("resume_path", "")
        await asyncio.to_thread(
            send_application_email, profile, to, subject, text,
            resume if resume and Path(resume).exists() else "",
        )
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log_applied(job_id, False)
        raise HTTPException(status_code=502, detail=f"Send failed: {e}")

    log_applied(job_id, True)
    note = f"Applied via email to {to} on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    existing = (job.get("notes") or "").strip()
    update_job_notes(job_id, f"{existing}\n{note}".strip())

    await broadcast_event({
        "type": "email_apply_sent",
        "data": {"job_id": job_id, "to": to,
                 "title": job.get("title"), "company": job.get("company")},
    })
    return {"status": "sent", "job_id": job_id, "to": to}


@app.post("/api/bulk-apply/prepare")
async def bulk_apply_prepare(body: dict) -> dict:
    """
    Stage 1 of bulk apply: for each selected job, find its hiring inbox and
    compose an email draft; jobs without one are queued for the browser
    pipeline. Runs in the background; results land in /api/bulk-apply/status
    and are broadcast over the WebSocket. Nothing is sent from here.

    Body: {job_ids: [...]}
    """
    job_ids = body.get("job_ids") or []
    if not job_ids:
        raise HTTPException(status_code=400, detail="job_ids is required")
    if _bulk_state()["running"] or _apply_state()["running"]:
        raise HTTPException(status_code=409, detail="An apply session is already running")

    profile = _load_profile()

    async def _do_prepare():
        from utils.email_apply import extract_apply_email, compose_application_email
        from utils.tracker import update_apply_email
        from utils.brain import ClaudeBrain

        state = _bulk_state()
        state.update(running=True, phase="prepare", drafts=[], browser_jobs=[],
                     skipped=[], progress=[], cancel_requested=False)
        brain = None  # created lazily — only needed for jobs without a cover letter

        try:
            await broadcast_event({
                "type": "bulk_prepare_started",
                "data": {"total": len(job_ids)},
            })

            for i, jid in enumerate(job_ids):
                if state["cancel_requested"] or is_paused():
                    break
                job = get_job_by_id(jid)
                if not job:
                    state["skipped"].append({"job_id": jid, "reason": "not found"})
                    continue

                entry = {"job_id": jid, "title": job.get("title"),
                         "company": job.get("company")}

                apply_email = job.get("apply_email") or ""
                if not apply_email:
                    apply_email = extract_apply_email(job.get("description") or "") or ""
                    if apply_email:
                        update_apply_email(jid, apply_email)
                        job["apply_email"] = apply_email

                if apply_email:
                    if brain is None and not (job.get("cover_letter") or "").strip():
                        brain = ClaudeBrain(verbose=False, profile=profile)
                    draft = await asyncio.to_thread(
                        compose_application_email, job, profile,
                        brain if not (job.get("cover_letter") or "").strip() else None,
                    )
                    state["drafts"].append({**entry, **draft})
                elif job.get("apply_url"):
                    state["browser_jobs"].append({**entry, "apply_url": job["apply_url"]})
                else:
                    state["skipped"].append({**entry, "reason": "no email and no apply URL"})

                await broadcast_event({
                    "type": "bulk_prepare_progress",
                    "data": {"current": i + 1, "total": len(job_ids),
                             "emails": len(state["drafts"]),
                             "browser": len(state["browser_jobs"])},
                })

            await broadcast_event({
                "type": "bulk_prepare_complete",
                "data": {"emails": len(state["drafts"]),
                         "browser": len(state["browser_jobs"]),
                         "skipped": len(state["skipped"])},
            })
        except Exception as exc:
            await broadcast_event({"type": "bulk_prepare_error", "data": {"error": str(exc)}})
        finally:
            state["running"] = False
            state["phase"] = None

    asyncio.create_task(_do_prepare())
    return {"status": "started", "count": len(job_ids)}


@app.get("/api/bulk-apply/status")
async def bulk_apply_status() -> dict:
    """Current bulk-apply state: drafts to review, browser fallbacks, progress."""
    state = _bulk_state()
    return {k: state[k] for k in
            ("running", "phase", "drafts", "browser_jobs", "skipped", "progress")}


@app.post("/api/bulk-apply/send")
async def bulk_apply_send(body: dict) -> dict:
    """
    Stage 2 of bulk apply: send the user-reviewed email drafts. The dashboard
    only calls this after the user has reviewed every draft and pressed the
    send button — that click is the explicit confirmation for this batch.

    Body: {items: [{job_id, to, subject, body}, ...]}
    """
    from utils.email_apply import smtp_config
    from utils.tracker import get_today_count

    items = body.get("items") or []
    if not items:
        raise HTTPException(status_code=400, detail="items is required")
    if _bulk_state()["running"] or _apply_state()["running"]:
        raise HTTPException(status_code=409, detail="An apply session is already running")

    profile = _load_profile()
    try:
        smtp_config(profile)  # surface config problems before starting
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    max_per_day = profile.get("rate_limits", {}).get("max_applications_per_day", 25)

    async def _do_send():
        import random
        from utils.email_apply import send_application_email
        from utils.tracker import log_applied, update_job_notes

        state = _bulk_state()
        state.update(running=True, phase="send", progress=[], cancel_requested=False)

        try:
            await broadcast_event({
                "type": "bulk_send_started", "data": {"total": len(items)},
            })
            resume = profile.get("resume_path", "")
            resume = resume if resume and Path(resume).exists() else ""

            for i, item in enumerate(items):
                if state["cancel_requested"] or is_paused():
                    await broadcast_event({
                        "type": "bulk_send_cancelled",
                        "data": {"sent": len([r for r in state["progress"] if r["status"] == "sent"])},
                    })
                    break
                if get_today_count() >= max_per_day:
                    state["progress"].append({
                        "job_id": item.get("job_id"), "status": "skipped",
                        "error": f"daily limit reached ({max_per_day})",
                    })
                    break

                jid = item.get("job_id")
                job = get_job_by_id(jid) or {}
                result = {"job_id": jid, "title": job.get("title"),
                          "company": job.get("company"), "to": item.get("to")}
                try:
                    await asyncio.to_thread(
                        send_application_email, profile,
                        (item.get("to") or "").strip(),
                        (item.get("subject") or "").strip(),
                        (item.get("body") or "").strip(),
                        resume,
                    )
                    log_applied(jid, True)
                    note = (f"Applied via email to {item.get('to')} on "
                            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
                    existing = (job.get("notes") or "").strip()
                    update_job_notes(jid, f"{existing}\n{note}".strip())
                    result["status"] = "sent"
                except Exception as exc:
                    log_applied(jid, False)
                    result["status"] = "failed"
                    result["error"] = str(exc)

                state["progress"].append(result)
                await broadcast_event({
                    "type": "bulk_send_progress",
                    "data": {"current": i + 1, "total": len(items), **result},
                })

                # Small human-ish gap between sends (also avoids SMTP rate flags)
                if i < len(items) - 1:
                    await asyncio.sleep(random.randint(4, 12))

            sent = len([r for r in state["progress"] if r["status"] == "sent"])
            failed = len([r for r in state["progress"] if r["status"] == "failed"])
            await broadcast_event({
                "type": "bulk_send_complete",
                "data": {"sent": sent, "failed": failed, "total": len(items),
                         "results": state["progress"]},
            })
        except Exception as exc:
            await broadcast_event({"type": "bulk_send_error", "data": {"error": str(exc)}})
        finally:
            state["running"] = False
            state["phase"] = None

    asyncio.create_task(_do_send())
    return {"status": "started", "count": len(items)}


@app.post("/api/bulk-apply/cancel")
async def bulk_apply_cancel() -> dict:
    """Stop the current bulk prepare/send after the item in flight."""
    if not _bulk_state()["running"]:
        return {"status": "not_running"}
    _bulk_state()["cancel_requested"] = True
    return {"status": "cancel_requested"}


# ===========================================================================
# REST API — YOLO Mode (Fully Autonomous Pipeline)
# ===========================================================================

# Per-user YOLO state (keyed by uid; one entry total in legacy mode).
_yolo_states: dict = {}


def _yolo_state() -> dict:
    uid = usercontext.current_uid()
    return _yolo_states.setdefault(uid, {
        "running": False,
        "phase": None,       # "discover" | "score" | "apply" | "waiting"
        "cancel_requested": False,
        "log": [],           # Full action log
        "cycle": 0,          # Which cycle we're on
        "continuous": False,  # Keep looping?
    })


@app.post("/api/yolo")
async def start_yolo(body: dict = {}) -> dict:
    """
    YOLO Mode — Fully autonomous: discover → score → apply → repeat.

    Body options:
        dry_run      — bool, default True. Safety net.
        continuous   — bool, default False. If True, loops forever with interval.
        interval_min — int, minutes between cycles (default 360 = 6 hours).
        max_apply    — int, max applications per cycle (default 10).
        min_score    — int, override minimum score threshold.
    """
    if _yolo_state()["running"]:
        raise HTTPException(status_code=409, detail="YOLO mode already running")
    if _apply_state()["running"]:
        raise HTTPException(status_code=409, detail="An apply session is already running")

    dry_run = body.get("dry_run", True)
    continuous = body.get("continuous", False)
    interval_min = body.get("interval_min", 360)
    max_apply = body.get("max_apply", 10)
    min_score_override = body.get("min_score")

    async def _yolo_pipeline():
        import random
        import yaml

        _yolo_state()["running"] = True
        _yolo_state()["cancel_requested"] = False
        _yolo_state()["continuous"] = continuous
        _yolo_state()["log"] = []
        _yolo_state()["cycle"] = 0

        def ylog(msg, level="info"):
            """Append to YOLO action log and broadcast."""
            entry = {
                "time": __import__("datetime").datetime.now().isoformat(),
                "msg": msg,
                "level": level,
                "cycle": _yolo_state()["cycle"],
            }
            _yolo_state()["log"].append(entry)
            # Keep log bounded
            if len(_yolo_state()["log"]) > 500:
                _yolo_state()["log"] = _yolo_state()["log"][-500:]

        try:
            while True:
                if _yolo_state()["cancel_requested"] or is_paused():
                    ylog("YOLO cancelled by user.", "warn")
                    await broadcast_event({"type": "yolo_cancelled", "data": {}})
                    break

                _yolo_state()["cycle"] += 1
                cycle = _yolo_state()["cycle"]

                await broadcast_event({
                    "type": "yolo_cycle_start",
                    "data": {"cycle": cycle, "dry_run": dry_run, "continuous": continuous}
                })
                ylog(f"=== CYCLE {cycle} START {'[DRY RUN]' if dry_run else '[LIVE]'} ===")

                # --- PHASE 1: DISCOVER ---
                _yolo_state()["phase"] = "discover"
                ylog("Phase 1: Discovering jobs...")
                await broadcast_event({"type": "yolo_phase", "data": {"phase": "discover", "cycle": cycle}})

                profile_path = usercontext.profile_path()
                with open(profile_path) as f:
                    profile = yaml.safe_load(f)

                try:
                    from utils.discovery import discover_all_jobs

                    on_source_jobs, counts = _incremental_saver()
                    await discover_all_jobs(profile, on_source_jobs=on_source_jobs)
                    ylog(f"Discovered {counts['new']} new jobs from {counts['total']} total.")
                    await broadcast_event({
                        "type": "yolo_discover_done",
                        "data": {"total": counts["total"], "new": counts["new"], "cycle": cycle}
                    })
                except Exception as e:
                    ylog(f"Discovery error: {e}", "error")
                    await broadcast_event({"type": "yolo_error", "data": {"phase": "discover", "error": str(e)}})

                if _yolo_state()["cancel_requested"] or is_paused():
                    break

                # --- PHASE 2: SCORE ---
                _yolo_state()["phase"] = "score"
                ylog("Phase 2: Scoring unscored jobs...")
                await broadcast_event({"type": "yolo_phase", "data": {"phase": "score", "cycle": cycle}})

                try:
                    from utils.brain import ClaudeBrain
                    from utils.resume_parser import extract_resume_text

                    brain = ClaudeBrain(verbose=False, profile=profile)
                    resume_text = extract_resume_text(profile.get("resume_path", ""))
                    unscored = get_unscored_jobs()
                    min_score = min_score_override or profile["preferences"].get("min_match_score", 65)
                    scored_count = 0

                    for job_row in unscored:
                        if _yolo_state()["cancel_requested"] or is_paused():
                            break
                        try:
                            desc = (
                                job_row.get("description", "")
                                or f"Job: {job_row['title']} at {job_row['company']}. Location: {job_row['location']}"
                            )
                            result = await asyncio.to_thread(brain.match_job, desc, profile, resume_text=resume_text)
                            score = result.get("score", 0)
                            log_matched(job_row["id"], score, result.get("reasoning", ""), result.get("cover_letter", ""))
                            if score < min_score:
                                log_skipped(job_row["id"], f"Score {score} < {min_score}")
                            scored_count += 1
                            ylog(f"  Scored: {job_row['title']} @ {job_row['company']} = {score}")
                        except Exception as e:
                            ylog(f"  Score failed: {job_row['title']}: {e}", "error")

                    ylog(f"Scored {scored_count} jobs.")
                    await broadcast_event({
                        "type": "yolo_score_done",
                        "data": {"scored": scored_count, "cycle": cycle}
                    })
                except Exception as e:
                    ylog(f"Scoring error: {e}", "error")
                    await broadcast_event({"type": "yolo_error", "data": {"phase": "score", "error": str(e)}})

                if _yolo_state()["cancel_requested"] or is_paused():
                    break

                # --- PHASE 3: APPLY ---
                _yolo_state()["phase"] = "apply"
                ylog("Phase 3: Applying to matched jobs...")
                await broadcast_event({"type": "yolo_phase", "data": {"phase": "apply", "cycle": cycle}})

                try:
                    from playwright.async_api import async_playwright
                    from adapters.stagehand_adapter import apply_smart
                    from utils.tracker import log_applied, get_today_count

                    rate_limits = profile.get("rate_limits", {})
                    max_per_day = rate_limits.get("max_applications_per_day", 25)
                    min_delay = rate_limits.get("min_delay_seconds", 60)
                    max_delay = rate_limits.get("max_delay_seconds", 180)
                    min_score = min_score_override or profile["preferences"].get("min_match_score", 65)

                    # Get matched jobs not yet applied
                    matched, _ = get_all_jobs(status="matched", sort_by="match_score", sort_order="desc", limit=500)
                    eligible = [
                        j for j in matched
                        if j.get("apply_url") and (j.get("match_score") or 0) >= min_score
                    ][:max_apply]

                    today_count = get_today_count()
                    remaining = max(0, max_per_day - today_count)
                    eligible = eligible[:remaining]

                    if not eligible:
                        ylog(f"No eligible jobs to apply to (today: {today_count}/{max_per_day}).")
                    else:
                        ylog(f"Applying to {len(eligible)} jobs...")
                        _apply_state()["running"] = True

                        async with async_playwright() as p:
                            from utils.browser import launch_apply_browser
                            page, close_browser = await launch_apply_browser(p)
                            applied_count = 0

                            for i, job in enumerate(eligible):
                                if _yolo_state()["cancel_requested"] or is_paused():
                                    break

                                job_id = job["id"]
                                _apply_state()["job_id"] = job_id
                                ylog(f"  [{i+1}/{len(eligible)}] {job['title']} @ {job['company']}")

                                await broadcast_event({
                                    "type": "yolo_applying",
                                    "data": {
                                        "current": i + 1, "total": len(eligible),
                                        "title": job["title"], "company": job["company"],
                                        "dry_run": dry_run,
                                    }
                                })

                                try:
                                    cover_letter = job.get("cover_letter", "")
                                    platform = job.get("platform", "")
                                    apply_url = job["apply_url"]

                                    success = await apply_smart(
                                        page, apply_url, profile, brain,
                                        cover_letter=cover_letter, dry_run=dry_run,
                                        platform=platform,
                                        company=job.get("company", ""),
                                        title=job.get("title", ""),
                                        description=job.get("description", ""),
                                    )

                                    if not dry_run:
                                        log_applied(job_id, success)

                                    status_str = "OK" if success else "FAIL"
                                    ylog(f"    -> {status_str}")
                                    applied_count += 1

                                except Exception as e:
                                    ylog(f"    -> ERROR: {e}", "error")
                                    if not dry_run:
                                        log_applied(job_id, False)

                                # Rate limit
                                if i < len(eligible) - 1:
                                    delay = random.randint(min_delay, max_delay)
                                    ylog(f"    Waiting {delay}s...")
                                    await asyncio.sleep(delay)

                            await close_browser()

                        _apply_state()["running"] = False
                        _apply_state()["job_id"] = None
                        ylog(f"Applied to {applied_count}/{len(eligible)} jobs.")

                    await broadcast_event({
                        "type": "yolo_apply_done",
                        "data": {"applied": applied_count if eligible else 0, "cycle": cycle, "dry_run": dry_run}
                    })

                except Exception as e:
                    ylog(f"Apply error: {e}", "error")
                    _apply_state()["running"] = False
                    _apply_state()["job_id"] = None
                    await broadcast_event({"type": "yolo_error", "data": {"phase": "apply", "error": str(e)}})

                # --- CYCLE COMPLETE ---
                ylog(f"=== CYCLE {cycle} COMPLETE ===")
                await broadcast_event({
                    "type": "yolo_cycle_complete",
                    "data": {"cycle": cycle, "log_size": len(_yolo_state()["log"])}
                })

                if not continuous:
                    break

                # Wait for next cycle
                _yolo_state()["phase"] = "waiting"
                ylog(f"Next cycle in {interval_min} minutes...")
                await broadcast_event({
                    "type": "yolo_waiting",
                    "data": {"minutes": interval_min, "next_cycle": cycle + 1}
                })

                # Sleep in small increments so cancel is responsive
                for _ in range(interval_min * 6):  # check every 10 seconds
                    if _yolo_state()["cancel_requested"] or is_paused():
                        break
                    await asyncio.sleep(10)

        except Exception as e:
            ylog(f"YOLO fatal error: {e}", "error")
            await broadcast_event({"type": "yolo_error", "data": {"phase": "fatal", "error": str(e)}})
        finally:
            _yolo_state()["running"] = False
            _yolo_state()["phase"] = None
            _apply_state()["running"] = False

    asyncio.create_task(_yolo_pipeline())
    return {
        "status": "started",
        "dry_run": dry_run,
        "continuous": continuous,
        "interval_min": interval_min,
        "max_apply": max_apply,
    }


@app.get("/api/yolo/status")
async def yolo_status() -> dict:
    """Get current YOLO mode status and action log."""
    return {
        "running": _yolo_state()["running"],
        "phase": _yolo_state()["phase"],
        "cycle": _yolo_state()["cycle"],
        "continuous": _yolo_state()["continuous"],
        "log_count": len(_yolo_state()["log"]),
        "recent_log": _yolo_state()["log"][-20:],  # Last 20 entries
    }


@app.get("/api/yolo/log")
async def yolo_log(offset: int = 0, limit: int = 100) -> dict:
    """Get full YOLO action log with pagination."""
    log = _yolo_state()["log"]
    return {
        "total": len(log),
        "entries": log[offset:offset + limit],
    }


@app.post("/api/yolo/cancel")
async def cancel_yolo() -> dict:
    """Cancel YOLO mode after current action completes."""
    if not _yolo_state()["running"]:
        return {"status": "not_running"}
    _yolo_state()["cancel_requested"] = True
    _apply_state()["cancel_requested"] = True  # Also cancel any active apply
    return {"status": "cancel_requested"}


# ===========================================================================
# REST API — MCP-powered discovery (ingestion endpoint)
# ===========================================================================

@app.post("/api/ingest")
async def ingest_mcp_jobs(body: dict) -> dict:
    """
    Ingest job results discovered via MCP tools (WebSearch, Playwright, etc.).

    Accepts: {"jobs": [{"title": "...", "company": "...", "url": "...", ...}]}
    This is called by Claude Code agents after running MCP-powered searches.
    """
    from utils.mcp_source import ingest_jobs
    job_dicts = body.get("jobs", [])
    if not job_dicts:
        raise HTTPException(status_code=400, detail="No jobs provided")
    result = ingest_jobs(job_dicts)
    await broadcast_event({
        "type": "mcp_ingest_complete",
        "data": result
    })
    return result


# ===========================================================================
# REST API — System pause switch
# ===========================================================================

@app.get("/api/system/state")
async def system_state() -> dict:
    """Global pause state + whether the caller may operate the switch."""
    state = get_system_state()
    state["pause_control"] = _is_admin_user()
    return state


@app.post("/api/system/pause")
async def system_pause() -> dict:
    """
    Pause everything, for every user: scheduled jobs skip their runs,
    in-flight scoring / YOLO / batch-apply loops stop at their next
    checkpoint, and endpoints that would spend Claude tokens or drive a
    browser return 409 until resumed. Owner accounts only.
    """
    if not _is_admin_user():
        raise HTTPException(status_code=403, detail="Only the system owner can pause the system")
    by = ""
    try:
        by = usercontext.get_current_user().get("email", "")
    except Exception:
        pass
    state = set_paused(True, by=by)
    await broadcast_event({"type": "system_state", "data": state})
    print(f"[System] PAUSED by {by or 'owner'} — all automation and Claude usage stopped")
    return state


@app.post("/api/system/resume")
async def system_resume() -> dict:
    """Resume normal operation — the scheduler picks up at each job's next tick."""
    if not _is_admin_user():
        raise HTTPException(status_code=403, detail="Only the system owner can resume the system")
    state = set_paused(False)
    await broadcast_event({"type": "system_state", "data": state})
    print("[System] RESUMED — automation active again")
    return state


# ===========================================================================
# REST API — Scheduler
# ===========================================================================

@app.get("/api/scheduler/status")
async def scheduler_status() -> dict:
    """Return scheduler state: running flag, job next-run times, last results."""
    try:
        from scheduler import get_scheduler_status
        status = get_scheduler_status()
    except Exception:
        status = {"running": False, "jobs": [], "last_results": {}, "paused": is_paused()}
    status["pause_control"] = _is_admin_user()
    return status


@app.post("/api/scheduler/trigger/{job_name}")
async def trigger_scheduler_job(job_name: str) -> dict:
    """Manually trigger a scheduler job (discover, score, email)."""
    try:
        from scheduler import scheduler as sched
        job = sched.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_name}' not found")
        job.modify(next_run_time=__import__("datetime").datetime.now())
        return {"status": "triggered", "job": job_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================================================
# REST API — Email checking
# ===========================================================================

@app.post("/api/check-email")
async def check_email_now() -> dict:
    """Trigger an email check for application status updates."""
    import yaml
    profile_path = usercontext.profile_path()
    with open(profile_path) as f:
        profile = yaml.safe_load(f)

    async def _do_check():
        try:
            from utils.email_checker import check_emails
            results = check_emails(profile)
            await broadcast_event({
                "type": "email_check_complete",
                "data": {"results": results, "count": len(results)}
            })
        except Exception as exc:
            await broadcast_event({
                "type": "email_check_error",
                "data": {"error": str(exc)}
            })

    asyncio.create_task(_do_check())
    return {"status": "started"}


# ===========================================================================
# WebSocket endpoint
# ===========================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """
    Maintain a persistent connection with each browser tab.

    The client may send "ping" to keep the connection alive through
    proxies or firewalls that enforce idle timeouts. All other inbound
    messages are silently ignored — this is a server-push channel.

    Auth (the HTTP middleware does not see WebSocket upgrades):
      Firebase mode — the client's FIRST message must be
      {"type": "auth", "token": "<firebase-id-token>"}; anything else, or an
      invalid token, closes the socket. The verified uid scopes which events
      this socket receives.
      Legacy mode — same Basic check as HTTP; browsers re-send cached
      credentials on the same-origin upgrade request.
    """
    if multi_tenant_enabled():
        await websocket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            msg = json.loads(first)
            assert msg.get("type") == "auth"
            from utils.auth import verify_firebase_token
            user = verify_firebase_token(msg.get("token", ""))
        except Exception:
            await websocket.close(code=1008)
            return
        uid = user["uid"]
        await websocket.send_text(json.dumps({"type": "auth_ok"}))
    else:
        if not _basic_auth_ok(websocket.headers.get("authorization")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        uid = usercontext.LEGACY_UID

    ws_clients[websocket] = uid
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WebSocket error: %s", exc)
    finally:
        ws_clients.pop(websocket, None)


# ===========================================================================
# Entry point
# ===========================================================================

def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Start the uvicorn server.  Called from main.py or directly."""
    import uvicorn

    print(f"\n  Dashboard running at: http://localhost:{port}")
    print(f"  Press Ctrl+C to stop\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
