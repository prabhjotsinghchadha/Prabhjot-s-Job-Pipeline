"""
The pipeline — one code path for all background work, used by the
scheduler, the dashboard API and the CLI alike:

    discover  ->  prefilter (0 tokens)  ->  score (10 jobs per Claude call)
    email check (IMAP)  ->  follow-up check

Every function emits progress on the EventBus (the dashboard relays it over
WebSocket) and honours the global pause switch.
"""

import asyncio
import json
from datetime import datetime
from typing import Callable, Optional

import yaml

from utils.events import EventBus
from utils.paths import profile_path
from utils.system_state import is_paused

BATCH_SIZE = 10
_running: dict[str, bool] = {"discover": False, "score": False, "discover_lead": False, "score_lead": False}
last_results: dict[str, Optional[dict]] = {"discover": None, "score": None, "email": None, "follow_up": None,
                                          "discover_lead": None, "score_lead": None}


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

async def run_discovery(profile: dict, tracks: Optional[list] = None) -> dict:
    """
    Run every enabled source, save jobs as each source finishes, prefilter.
    `tracks` picks which resume tracks to search for (None = all). The sources
    run once with every selected track's roles; each new job is tagged with
    the track it fits best.
    """
    if _running["discover"]:
        return {"status": "already_running"}
    _running["discover"] = True
    from utils.discovery import discover_all_jobs
    from utils.prefilter import run_prefilter
    from utils.tracker import is_already_seen, log_discovered
    from utils.tracks import assign_track, discovery_profile, get_track, selected

    seen: set = set()
    counts = {"total": 0, "new": 0}
    ids = selected(profile, tracks)
    per_track: dict = {}

    async def on_source_jobs(source: str, jobs: list) -> None:
        new_here = 0
        for job in jobs:
            key = (job.title.lower().strip(), job.company.lower().strip())
            if key in seen:
                continue
            seen.add(key)
            counts["total"] += 1
            if not is_already_seen(job.id):
                if not isinstance(job.metadata, dict):
                    job.metadata = {}
                job.metadata["track"] = assign_track(job, profile, ids)
                log_discovered(job)
                new_here += 1
                counts["new"] += 1
                per_track[job.metadata["track"]] = per_track.get(job.metadata["track"], 0) + 1
        EventBus.emit("discovery_progress",
                      {"source": source, "found": len(jobs), "new": new_here, "total_new": counts["new"]})

    names = [(get_track(profile, i) or {}).get("name", i) for i in ids]
    try:
        EventBus.emit("discovery_started", {"kind": "job", "tracks": names})
        await discover_all_jobs(discovery_profile(profile, ids), on_source_jobs=on_source_jobs)
        pf = await asyncio.to_thread(run_prefilter, profile)
        result = {"total": counts["total"], "new": counts["new"], "tracks": names,
                  "new_by_track": {(get_track(profile, t or "main") or {}).get("name", t): n
                                   for t, n in per_track.items()},
                  "prefiltered": pf["skipped"], "kind": "job", "timestamp": _now()}
        EventBus.emit("discovery_complete", result)
        return result
    except Exception as exc:
        result = {"error": str(exc), "kind": "job", "timestamp": _now()}
        EventBus.emit("discovery_error", result)
        return result
    finally:
        _running["discover"] = False
        last_results["discover"] = result


# --------------------------------------------------------------------------
# Scoring (batched)
# --------------------------------------------------------------------------

def _score_batch(brain, batch: list, profile: dict, resume_text: str, kind: str = "job") -> dict:
    """Score a batch; fall back to one-by-one if the batched call fails (jobs only)."""
    try:
        if kind == "lead":
            return brain.match_leads(batch, profile)
        return brain.match_jobs(batch, profile, resume_text)
    except Exception as exc:
        print(f"  Batch scoring failed ({exc}); scoring {len(batch)} jobs individually")
    if kind == "lead":
        return {}
    out = {}
    for j in batch:
        desc = j.get("description") or f"Job: {j['title']} at {j['company']}. Location: {j.get('location', '')}"
        try:
            out[str(j["id"])] = brain.match_job(desc, profile, resume_text)
        except Exception as exc:
            print(f"  Scoring failed for {j['id']}: {exc}")
    return out


async def run_scoring(profile: dict, jobs: Optional[list] = None, force: bool = False,
                      should_stop: Callable[[], bool] = is_paused, kind: str = "job") -> dict:
    """
    Score unscored jobs or leads (or the given list). Prefilter first unless
    `force`, then send BATCH_SIZE items per Claude call. Items that clear the
    threshold become `matched`, the rest `skipped`.
    """
    slot = "score" if kind == "job" else "score_lead"
    if _running[slot]:
        return {"status": "already_running"}
    _running[slot] = True
    from utils.brain import ClaudeBrain
    from utils.prefilter import run_prefilter
    from utils.tracker import get_unscored_jobs, log_matched, log_skipped
    from utils.tracks import profile_for_track, track_resume_text

    result: dict = {}
    try:
        targets = jobs if jobs is not None else get_unscored_jobs(kind)
        for t in targets:
            _unpack_meta(t)
        prefiltered = 0
        if not force and targets:
            pf = await asyncio.to_thread(run_prefilter, profile, targets, False, kind)
            prefiltered = pf["skipped"]
            if jobs is None:
                targets = get_unscored_jobs(kind)
                for t in targets:
                    _unpack_meta(t)
            else:
                skipped_ids = {s["id"] for s in pf.get("samples", [])}
                targets = [t for t in targets if t["id"] not in skipped_ids] if prefiltered else targets

        total = len(targets)
        EventBus.emit("scoring_started", {"total": total, "prefiltered": prefiltered, "kind": kind})
        if not targets:
            result = {"scored": 0, "matched": 0, "prefiltered": prefiltered, "kind": kind, "timestamp": _now()}
            EventBus.emit("scoring_complete", result)
            return result

        brain = ClaudeBrain(verbose=False, profile=profile)

        # Jobs are scored against the resume track they belong to, so batches
        # never mix tracks. Leads have one offer and one group.
        groups: dict = {}
        for t in targets:
            groups.setdefault((t.get("track") or "") if kind == "job" else "", []).append(t)
        batches = []
        for track, items in groups.items():
            eff = profile_for_track(profile, track) if track else profile
            resume_text = track_resume_text(eff) if kind == "job" else ""
            if kind == "lead":
                floor = int(profile.get("preferences", {}).get("min_match_score", 65))
                min_score = int((profile.get("offer") or {}).get("min_match_score") or floor)
            else:
                min_score = int(eff.get("preferences", {}).get("min_match_score", 65))
            for i in range(0, len(items), BATCH_SIZE):
                batches.append((items[i:i + BATCH_SIZE], eff, resume_text, min_score))

        scored = matched = 0
        failed_batches = 0
        for batch, eff, resume_text, min_score in batches:
            if should_stop():
                EventBus.emit("scoring_paused", {"scored": scored, "total": total})
                break
            results = await asyncio.to_thread(_score_batch, brain, batch, eff, resume_text, kind)
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
                if kind == "lead":
                    _merge_meta(j["id"], {k: r.get(k) for k in ("pitch_angle", "budget_fit", "engagement") if r.get(k)})
                if score < min_score:
                    log_skipped(j["id"], f"Score {score} < {min_score}: {r.get('reasoning', '')}")
                else:
                    matched += 1
                scored += 1
            EventBus.emit("scoring_progress", {"scored": scored, "matched": matched, "total": total, "kind": kind})

        result = {"scored": scored, "matched": matched, "prefiltered": prefiltered,
                  "total": total, "kind": kind, "timestamp": _now()}
        EventBus.emit("scoring_complete", result)
        return result
    except Exception as exc:
        result = {"error": str(exc), "kind": kind, "timestamp": _now()}
        EventBus.emit("scoring_error", result)
        return result
    finally:
        _running[slot] = False
        last_results[slot] = result


def _unpack_meta(row: dict) -> None:
    """Expose lead_type / pitch_angle from the metadata JSON on tracker rows."""
    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if isinstance(meta, dict):
        for k in ("lead_type", "pitch_angle", "budget_fit", "engagement"):
            if k in meta and k not in row:
                row[k] = meta[k]


def _merge_meta(job_id: str, extra: dict) -> None:
    if not extra:
        return
    from utils.tracker import get_db
    conn = get_db()
    row = conn.execute("SELECT metadata FROM applications WHERE id = ?", (job_id,)).fetchone()
    try:
        meta = json.loads(row["metadata"] or "{}") if row else {}
    except Exception:
        meta = {}
    meta.update(extra)
    conn.execute("UPDATE applications SET metadata = ? WHERE id = ?", (json.dumps(meta), job_id))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Clients mode: leads
# --------------------------------------------------------------------------

async def run_lead_discovery(profile: dict) -> dict:
    """Gigs from the boards + prospects from the jobs DB, then the lead prefilter."""
    if _running["discover_lead"]:
        return {"status": "already_running"}
    _running["discover_lead"] = True
    from utils.leads import build_prospects, discover_gigs, offer_source_enabled
    from utils.prefilter import run_prefilter
    from utils.tracker import is_already_seen, log_discovered

    result: dict = {}
    try:
        EventBus.emit("discovery_started", {"kind": "lead"})
        gigs = await discover_gigs(profile)
        prospects = await asyncio.to_thread(build_prospects, profile) if offer_source_enabled(profile, "prospects_from_jobs") else []
        new = {"gig": 0, "prospect": 0}
        for lead in gigs + prospects:
            if not is_already_seen(lead.id):
                log_discovered(lead)
                new[lead.metadata.get("lead_type", "gig")] += 1
        EventBus.emit("discovery_progress", {"source": "leads", "found": len(gigs) + len(prospects),
                                             "new": sum(new.values()), "total_new": sum(new.values()), "kind": "lead"})
        pf = await asyncio.to_thread(run_prefilter, profile, None, False, "lead")
        result = {"total": len(gigs) + len(prospects), "new": sum(new.values()), "gigs": new["gig"],
                  "prospects": new["prospect"], "prefiltered": pf["skipped"], "kind": "lead", "timestamp": _now()}
        EventBus.emit("discovery_complete", result)
        return result
    except Exception as exc:
        result = {"error": str(exc), "kind": "lead", "timestamp": _now()}
        EventBus.emit("discovery_error", result)
        return result
    finally:
        _running["discover_lead"] = False
        last_results["discover_lead"] = result


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
