"""
Per-user data context — the core of multi-tenant isolation.

Every user gets their own data directory (own SQLite DB, profile.yaml,
resumes/, .cache/), so isolation holds by construction: there is no shared
table where a missed WHERE clause could leak one user's data to another.

Modes:
  Legacy (FIREBASE_PROJECT_ID unset): single-user, paths resolve to the
    repo/app root exactly as before — CLI, docker-compose bind mounts, and
    the existing Railway deploy all keep working unchanged.
  Multi-tenant (FIREBASE_PROJECT_ID set): the authenticated Firebase user's
    uid selects DATA_DIR/users/<uid>/. The current user travels in a
    contextvar, which asyncio copies into tasks created during a request,
    so background work started by an endpoint stays bound to that user.
"""

import contextvars
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent.parent

LEGACY_UID = "local"

_current_user: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "current_user", default=None
)


def multi_tenant_enabled() -> bool:
    return bool(os.environ.get("FIREBASE_PROJECT_ID"))


def data_root() -> Path:
    return Path(os.environ.get("DATA_DIR") or BASE_DIR)


def set_current_user(user: Optional[dict]):
    """Bind {uid, email, name} to the current context. Returns a reset token."""
    return _current_user.set(user)


def reset_current_user(token) -> None:
    _current_user.reset(token)


def get_current_user() -> dict:
    user = _current_user.get()
    if user is None:
        if multi_tenant_enabled():
            raise RuntimeError(
                "No user bound to this context — data access before auth"
            )
        return {"uid": LEGACY_UID, "email": "", "name": ""}
    return user


def current_uid() -> str:
    return get_current_user()["uid"]


def user_data_dir(uid: Optional[str] = None) -> Path:
    """Resolve (and create) the data directory for a user."""
    if not multi_tenant_enabled():
        return BASE_DIR
    uid = uid or current_uid()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", uid):
        raise ValueError(f"Invalid uid: {uid!r}")
    d = data_root() / "users" / uid
    (d / "resumes").mkdir(parents=True, exist_ok=True)
    (d / ".cache").mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return user_data_dir() / "applications.db"


def profile_path() -> Path:
    return user_data_dir() / "profile.yaml"


def resumes_dir() -> Path:
    d = user_data_dir() / "resumes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    d = user_data_dir() / ".cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# User registry — DATA_DIR/users.json maps uid -> {email, name, created_at}.
# The scheduler iterates this to run discovery/scoring for every user.
# Friends-scale (atomic whole-file writes); revisit if accounts grow large.
# ---------------------------------------------------------------------------

def _registry_path() -> Path:
    return data_root() / "users.json"


def list_users() -> dict:
    p = _registry_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def register_user(uid: str, email: str, name: str = "") -> None:
    """Record a user on first login. Idempotent; refreshes email/name."""
    users = list_users()
    entry = users.get(uid, {"created_at": datetime.utcnow().isoformat()})
    entry.update({"email": email, "name": name})
    users[uid] = entry
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(users, indent=2))
    tmp.replace(p)
