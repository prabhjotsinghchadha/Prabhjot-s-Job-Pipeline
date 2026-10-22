"""
Global pause switch — one flag that halts every automated, token-consuming
operation: scheduled jobs, discovery, scoring, tailoring. Persisted as a tiny
JSON file in the data dir so a pause survives restarts.
"""

import json
from datetime import datetime
from pathlib import Path

from utils.paths import state_path as _state_path

_DEFAULT_STATE = {"paused": False, "paused_at": None, "paused_by": ""}



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
