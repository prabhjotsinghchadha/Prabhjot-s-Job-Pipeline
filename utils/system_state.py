"""
Global pause switch — one flag that halts every automated, token-consuming
operation: scheduled jobs, discovery, scoring, YOLO cycles, and applies.

Deliberately global rather than per-user: scoring runs through the deployer's
Claude CLI session, so any tenant's background work spends the same OAuth
token budget. Persisted as a tiny JSON file in the data root so a pause
survives server restarts and redeploys (on Railway, keep DATA_DIR on a
volume so the flag outlives the container).
"""

import json
from datetime import datetime
from pathlib import Path

from utils.usercontext import data_root

_DEFAULT_STATE = {"paused": False, "paused_at": None, "paused_by": ""}


def _state_path() -> Path:
    return data_root() / "system_state.json"


def get_state() -> dict:
    p = _state_path()
    if not p.exists():
        return dict(_DEFAULT_STATE)
    try:
        raw = json.loads(p.read_text())
        return {
            "paused": bool(raw.get("paused", False)),
            "paused_at": raw.get("paused_at"),
            "paused_by": raw.get("paused_by", ""),
        }
    except Exception:
        # Unreadable state file must never take the system down — run normally.
        return dict(_DEFAULT_STATE)


def is_paused() -> bool:
    return get_state()["paused"]


def set_paused(paused: bool, by: str = "") -> dict:
    state = {
        "paused": paused,
        "paused_at": datetime.now().isoformat() if paused else None,
        "paused_by": by if paused else "",
    }
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)
    return state
