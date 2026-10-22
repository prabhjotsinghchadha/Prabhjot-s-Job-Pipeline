"""
Dashboard server — FastAPI app, four HTML screens, REST routers, WebSocket.

    /             Inbox         scored jobs to decide on
    /applications Applications  everything you applied to
    /resume       Resume        builder, tailoring, ATS
    /settings     Settings      profile.yaml

Every mutation in utils/ emits on the EventBus; this module relays those
events to every open browser tab over /ws.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from utils.events import EventBus
from utils.system_state import is_paused

logger = logging.getLogger("dashboard")
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ASSET_V = datetime.now().strftime("%Y%m%d%H%M%S")  # cache-busts static files per server start


@asynccontextmanager
async def lifespan(app):
    try:
        from scheduler import setup_scheduler, start_scheduler, stop_scheduler
        setup_scheduler()
        start_scheduler()
    except Exception as e:
        print(f"  Scheduler not started: {e}")
    yield
    try:
        stop_scheduler()
    except Exception:
        pass


app = FastAPI(title="Job Pipeline", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# --------------------------------------------------------------------------
# Pause switch: anything that spends Claude tokens returns 409 while paused.
# --------------------------------------------------------------------------
_PAUSE_GATED_SUFFIXES = ("/score", "/cover-letter", "/tailor")


@app.middleware("http")
async def pause_middleware(request: Request, call_next):
    path = request.url.path
    if request.method == "POST" and is_paused():
        gated = path in ("/api/discover", "/api/score", "/api/resume/import") or (
            path.startswith("/api/jobs/") and path.endswith(_PAUSE_GATED_SUFFIXES)
        )
        if gated:
            return JSONResponse(
                {"detail": "Paused. Press Resume in the header to run anything that uses Claude."},
                status_code=409,
            )
    return await call_next(request)


# --------------------------------------------------------------------------
# WebSocket relay
# --------------------------------------------------------------------------
ws_clients: set[WebSocket] = set()


def _json_safe(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (set, bytes)):
        return list(obj) if isinstance(obj, set) else obj.decode("utf-8", "replace")
    return str(obj)


async def broadcast_event(event: dict) -> None:
    payload = json.dumps(event, default=_json_safe)
    dead = []
    for ws in list(ws_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.discard(ws)


def _on_event(event: dict) -> None:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(broadcast_event(event))
    except RuntimeError:
        pass


EventBus.subscribe(_on_event)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    ws_clients.add(websocket)
    try:
        while True:
            if await websocket.receive_text() == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WebSocket error: %s", exc)
    finally:
        ws_clients.discard(websocket)


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------
SCREENS = {
    "/": ("inbox", "Inbox"),
    "/applications": ("applications", "Applications"),
    "/resume": ("resume", "Resume"),
    "/settings": ("settings", "Settings"),
}


def _screen(name: str, title: str):
    async def view(request: Request) -> HTMLResponse:
        from utils.pipeline import load_profile
        return templates.TemplateResponse(
            request, f"{name}.html",
            {"screen": name, "title": title, "has_profile": load_profile() is not None, "asset_v": ASSET_V},
        )
    return view


for path, (name, title) in SCREENS.items():
    app.get(path, response_class=HTMLResponse, include_in_schema=False)(_screen(name, title))


@app.get("/api/health")
async def health() -> dict:
    from utils.paths import profile_path
    return {"status": "ok", "profile": profile_path().exists()}


# --------------------------------------------------------------------------
# Routers
# --------------------------------------------------------------------------
from dashboard.routes import apply, discover, jobs, resume, settings, tracking, tracks  # noqa: E402

for router in (jobs.router, discover.router, apply.router, resume.router, tracks.router, tracking.router, settings.router):
    app.include_router(router, prefix="/api")


def run_server(host: str = "127.0.0.1", port: int = 8080) -> None:
    import uvicorn
    print(f"\n  Dashboard: http://localhost:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
