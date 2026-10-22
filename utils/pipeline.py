"""
The pipeline — one code path for all background work, used by the
scheduler, the dashboard API and the CLI alike:

    discover  ->  prefilter (0 tokens)  ->  score (10 jobs per Claude call)
    email check (IMAP)  ->  follow-up check

Every function emits progress on the EventBus (the dashboard relays it over
WebSocket) and honours the global pause switch.
"""

import asyncio
from datetime import datetime
from typing import Callable, Optional

import yaml

from utils.events import EventBus
from utils.paths import profile_path
from utils.system_state import is_paused

BATCH_SIZE = 10
_running: dict[str, bool] = {"discover": False, "score": False}
last_results: dict[str, Optional[dict]] = {"discover": None, "score": None, "email": None, "follow_up": None}


def load_profile() -> Optional[dict]:
    p = profile_path()
    if not p.exists():
        return None
    with open(p) as f:
        return yaml.safe_load(f) or {}


def is_running(what: str) -> bool:
    return _running.get(what, False)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

async def run_discovery(profile: dict) -> dict:
    """Run every enabled source, save jobs as each source finishes, prefilter."""
    if _running["discover"]:
        return {"status": "already_running"}
    _running["discover"] = True
    from utils.discovery import discover_all_jobs
    from utils.prefilter import run_prefilter
    from utils.tracker import is_already_seen, log_discovered

    seen: set = set()
    counts = {"total": 0, "new": 0}

    async def on_source_jobs(source: str, jobs: list) -> None:
        new_here = 0
        for job in jobs:
            key = (job.title.lower().strip(), job.company.lower().strip())
            if key in seen:
                continue
            seen.add(key)
            counts["total"] += 1
            if not is_already_seen(job.id):
                log_discovered(job)
                new_here += 1
                counts["new"] += 1
        EventBus.emit("discovery_progress",
                      {"source": source, "found": len(jobs), "new": new_here, "total_new": counts["new"]})

    try:
        EventBus.emit("discovery_started", {})
        await discover_all_jobs(profile, on_source_jobs=on_source_jobs)
        pf = await asyncio.to_thread(run_prefilter, profile)
        result = {"total": counts["total"], "new": counts["new"],
                  "prefiltered": pf["skipped"], "timestamp": _now()}
        EventBus.emit("discovery_complete", result)
        return result
    except Exception as exc:
        result = {"error": str(exc), "timestamp": _now()}
        EventBus.emit("discovery_error", result)
        return result
    finally:
        _running["discover"] = False
        last_results["discover"] = result


# --------------------------------------------------------------------------
# Scoring (batched)
# --------------------------------------------------------------------------

def _score_batch(brain, batch: list, profile: dict, resume_text: str) -> dict:
    """Score a batch; fall back to one-by-one if the batched call fails."""
    try:
        return brain.match_jobs(batch, profile, resume_text)
    except Exception as exc:
        print(f"  Batch scoring failed ({exc}); scoring {len(batch)} jobs individually")
    out = {}
    for j in batch:
        desc = j.get("description") or f"Job: {j['title']} at {j['company']}. Location: {j.get('location', '')}"
        try:
            out[str(j["id"])] = brain.match_job(desc, profile, resume_text)
        except Exception as exc:
            print(f"  Scoring failed for {j['id']}: {exc}")
    return out


async def run_scoring(profile: dict, jobs: Optional[list] = None, force: bool = False,
                      should_stop: Callable[[], bool] = is_paused) -> dict:
    """
    Score unscored jobs (or the given list). Prefilter first unless `force`,
    then send BATCH_SIZE postings per Claude call. Jobs that clear
    preferences.min_match_score become `matched`, the rest `skipped`.
    """
    if _running["score"]:
        return {"status": "already_running"}
    _running["score"] = True
    from utils.brain import ClaudeBrain
    from utils.prefilter import run_prefilter
    from utils.resume_parser import extract_resume_text
    from utils.tracker import get_unscored_jobs, log_matched, log_skipped

    result: dict = {}
    try:
        targets = jobs if jobs is not None else get_unscored_jobs()
        prefiltered = 0
        if not force and targets:
            pf = await asyncio.to_thread(run_prefilter, profile, targets)
            prefiltered = pf["skipped"]
            if jobs is None:
                targets = get_unscored_jobs()
            else:
                skipped_ids = {s["id"] for s in pf.get("samples", [])}
                targets = [t for t in targets if t["id"] not in skipped_ids] if prefiltered else targets

        total = len(targets)
        EventBus.emit("scoring_started", {"total": total, "prefiltered": prefiltered})
        if not targets:
            result = {"scored": 0, "matched": 0, "prefiltered": prefiltered, "timestamp": _now()}
            EventBus.emit("scoring_complete", result)
            return result

        brain = ClaudeBrain(verbose=False, profile=profile)
        resume_text = extract_resume_text(profile.get("resume_path", ""))
        min_score = int(profile.get("preferences", {}).get("min_match_score", 65))

        scored = matched = 0
        failed_batches = 0
        for i in range(0, total, BATCH_SIZE):
            if should_stop():
                EventBus.emit("scoring_paused", {"scored": scored, "total": total})
                break
            batch = targets[i:i + BATCH_SIZE]
            results = await asyncio.to_thread(_score_batch, brain, batch, profile, resume_text)
            if not results:
                failed_batches += 1
                if failed_batches >= 3 and scored == 0:
                    raise RuntimeError(
                        "Claude returned nothing for 3 batches in a row. "
                        "Check `claude auth` / usage limits, then run scoring again.")
                continue
            failed_batches = 0
            for j in batch:
                r = results.get(str(j["id"]))
                if not r:
                    continue
                score = int(r.get("score", 0) or 0)
                log_matched(j["id"], score, r.get("reasoning", ""), "")
                if score < min_score:
                    log_skipped(j["id"], f"Score {score} < {min_score}: {r.get('reasoning', '')}")
                else:
                    matched += 1
                scored += 1
            EventBus.emit("scoring_progress", {"scored": scored, "matched": matched, "total": total})

        result = {"scored": scored, "matched": matched, "prefiltered": prefiltered,
                  "total": total, "timestamp": _now()}
        EventBus.emit("scoring_complete", result)
        return result
    except Exception as exc:
        result = {"error": str(exc), "timestamp": _now()}
        EventBus.emit("scoring_error", result)
        return result
    finally:
        _running["score"] = False
        last_results["score"] = result


# --------------------------------------------------------------------------
# Email + follow-ups
# --------------------------------------------------------------------------

def run_email_check(profile: dict) -> dict:
    from utils.email_checker import check_emails
    if not profile.get("email", {}).get("enabled", False):
        result = {"checked": 0, "skipped": "email disabled", "timestamp": _now()}
    else:
        try:
            updates = check_emails(profile)
            result = {"checked": len(updates), "updates": updates, "timestamp": _now()}
            EventBus.emit("email_check_complete", {"checked": len(updates)})
        except Exception as exc:
            result = {"error": str(exc), "timestamp": _now()}
            EventBus.emit("email_check_error", result)
    last_results["email"] = result
    return result


def run_follow_up_check() -> dict:
    from utils.tracker import get_ghost_alerts, get_overdue_follow_ups
    overdue, ghosts = get_overdue_follow_ups(), get_ghost_alerts(days=14)
    result = {"overdue": len(overdue), "ghosts": len(ghosts), "timestamp": _now()}
    if overdue or ghosts:
        EventBus.emit("follow_ups_due", result)
    last_results["follow_up"] = result
    return result
