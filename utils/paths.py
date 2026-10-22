"""
Where data lives. Single-user, local: everything sits in the repo root unless
DATA_DIR points somewhere else (Docker mounts a volume there).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    d = Path(os.environ.get("DATA_DIR", "")) if os.environ.get("DATA_DIR") else BASE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "applications.db"


def profile_path() -> Path:
    return data_dir() / "profile.yaml"


def resumes_dir() -> Path:
    d = data_dir() / "resumes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    d = data_dir() / ".cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path() -> Path:
    return data_dir() / "system_state.json"
