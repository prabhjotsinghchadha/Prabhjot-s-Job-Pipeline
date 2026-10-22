"""
Application Tracker — SQLite-backed log of all job discovery and applications.
Prevents duplicate applications and provides stats.
Extended with dashboard-ready queries, schema migration, and event broadcasting.
"""

import sqlite3
import json
from datetime import datetime, date
from pathlib import Path

from utils.paths import db_path as _db_path

# All valid statuses
# Two pipelines share one table, told apart by `kind`:
#   job  — a posting you apply to
#   lead — a gig or a company you pitch (clients mode)
KINDS = ("job", "lead")
STAGES = {
    "job": ["discovered", "matched", "saved", "applied", "interviewing", "offer",
            "rejected", "skipped", "archived"],
    "lead": ["discovered", "matched", "saved", "contacted", "call", "proposal",
             "won", "lost", "skipped", "archived"],
}
VALID_STATUSES = list(dict.fromkeys(STAGES["job"] + STAGES["lead"]))
# Entering these stages schedules a follow-up reminder N days out.
FOLLOW_UP_DAYS = {"applied": 7, "contacted": 3, "proposal": 5}
# Stages where a reminder is still meaningful.
ACTIVE_STAGES = {"job": ("applied", "interviewing"), "lead": ("contacted", "call", "proposal")}
# Statuses from earlier versions are folded into the list above on connect.
_LEGACY_STATUS = {"failed": "skipped", "ignored": "skipped", "withdrawn": "archived"}


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            platform TEXT,
            url TEXT,
            apply_url TEXT,
            location TEXT,
            match_score INTEGER,
            reasoning TEXT,
            cover_letter TEXT,
            status TEXT DEFAULT 'discovered',
            applied_at TIMESTAMP,
            discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_status ON applications(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_company ON applications(company)
    """)
    # Ignore-list: hashes of jobs to skip on future discovery runs
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ignored_hashes (
            hash TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            ignored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Schema migration: add new columns if they don't exist
    _migrate_schema(conn)
    conn.commit()
    return conn


def _migrate_schema(conn: sqlite3.Connection):
    """Add new columns for extended tracking (safe to run multiple times)."""
    cursor = conn.execute("PRAGMA table_info(applications)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    migrations = {
        "salary_min": "INTEGER",
        "salary_max": "INTEGER",
        "date_posted": "TEXT",
        "source": "TEXT DEFAULT ''",
        "notes": "TEXT DEFAULT ''",
        "tags": "TEXT DEFAULT ''",
        "description": "TEXT DEFAULT ''",
        "tailored_resume": "TEXT DEFAULT ''",
        "follow_up_date": "TEXT",
        "last_activity": "TEXT",
        "follow_up_count": "INTEGER DEFAULT 0",
        "apply_email": "TEXT DEFAULT ''",
        "kind": "TEXT DEFAULT 'job'",
        "track": "TEXT DEFAULT ''",
    }

    for col, col_type in migrations.items():
        if col not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass  # Column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kind ON applications(kind)")
    for old, new in _LEGACY_STATUS.items():
        conn.execute("UPDATE applications SET status = ? WHERE status = ?", (new, old))


def is_already_seen(job_id: str) -> bool:
    """Check if we've already seen this job."""
    conn = get_db()
    row = conn.execute("SELECT id FROM applications WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return row is not None


def _emit(event_type: str, data=None):
    """Emit event if EventBus is available."""
    try:
        from utils.events import EventBus
        EventBus.emit(event_type, data)
    except Exception:
        pass


def log_discovered(job) -> None:
    """Log a newly discovered job. Skips if on the ignore list."""
    # Check ignore list first (fast hash lookup)
    if is_ignored(job.title, job.company):
        return

    # Cheap regex scan for a hiring inbox — enables email-based applying
    apply_email = ""
    try:
        from utils.email_apply import extract_apply_email
        apply_email = extract_apply_email(getattr(job, 'description', '') or '') or ""
    except Exception:
        pass

    conn = get_db()
    try:
        metadata = json.dumps(job.metadata) if isinstance(job.metadata, dict) else job.metadata
        meta = job.metadata if isinstance(job.metadata, dict) else {}
        kind = meta.get("kind", "job") if meta.get("kind") in KINDS else "job"
        if not apply_email and meta.get("apply_email"):
            apply_email = meta["apply_email"]
        track = str(meta.get("track") or "") if kind == "job" else ""
        conn.execute("""
            INSERT OR IGNORE INTO applications
            (id, title, company, platform, url, apply_url, location, description, source,
             salary_min, salary_max, date_posted, metadata, apply_email, kind, track)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job.id, job.title, job.company, job.platform, job.url, job.apply_url,
            job.location, getattr(job, 'description', ''),
            job.metadata.get('source', job.platform) if isinstance(job.metadata, dict) else job.platform,
            job.metadata.get('salary_min') if isinstance(job.metadata, dict) else None,
            job.metadata.get('salary_max') if isinstance(job.metadata, dict) else None,
            job.metadata.get('date_posted', '') if isinstance(job.metadata, dict) else '',
            metadata, apply_email, kind, track
        ))
        conn.commit()
    finally:
        conn.close()
    _emit("job_discovered", {"id": job.id, "title": job.title, "company": job.company, "kind": kind})


def log_matched(job_id: str, score: int, reasoning: str, cover_letter: str) -> None:
    """Update a job with its match results."""
    conn = get_db()
    conn.execute("""
        UPDATE applications
        SET match_score = ?, reasoning = ?, cover_letter = ?, status = 'matched'
        WHERE id = ?
    """, (score, reasoning, cover_letter, job_id))
    conn.commit()
    conn.close()
    _emit("job_matched", {"id": job_id, "score": score})


def log_applied(job_id: str, success: bool) -> None:
    """Mark a job as applied and set follow-up if successful."""
    status = "applied" if success else "skipped"
    now = datetime.now().isoformat()
    conn = get_db()
    if success:
        from datetime import timedelta
        follow_up = (datetime.now() + timedelta(days=7)).isoformat()
        conn.execute("""
            UPDATE applications
            SET status = ?, applied_at = ?, last_activity = ?, follow_up_date = ?
            WHERE id = ?
        """, (status, now, now, follow_up, job_id))
    else:
        conn.execute("""
            UPDATE applications
            SET status = ?, applied_at = ?
            WHERE id = ?
        """, (status, now, job_id))
    conn.commit()
    conn.close()
    _emit("job_applied", {"id": job_id, "success": success})


def log_skipped(job_id: str, reason: str) -> None:
    """Mark a job as skipped."""
    conn = get_db()
    conn.execute("""
        UPDATE applications
        SET status = 'skipped', reasoning = ?
        WHERE id = ?
    """, (reason, job_id))
    conn.commit()
    conn.close()


def _track_clause(track) -> tuple:
    """SQL fragment + params for an optional track filter (None = every track, '' = main)."""
    if track is None:
        return "", ()
    return " AND COALESCE(track, '') = ?", (track,)


def get_today_count(kind: str = "job", track=None) -> int:
    """Applications sent (or leads contacted) today."""
    tc, tp = _track_clause(track)
    conn = get_db()
    row = conn.execute(f"""
        SELECT COUNT(*) as cnt FROM applications
        WHERE kind = ? AND status IN ('applied', 'contacted') AND DATE(applied_at) = DATE('now'){tc}
    """, (kind, *tp)).fetchone()
    conn.close()
    return row["cnt"]


def get_stats(kind: str = "job", track=None) -> dict:
    """Counts per stage for one pipeline (optionally one resume track; '' = main)."""
    tc, tp = _track_clause(track)
    conn = get_db()
    stats = {"kind": kind}
    counts = {r["status"]: r["cnt"] for r in conn.execute(
        f"SELECT status, COUNT(*) as cnt FROM applications WHERE kind = ?{tc} GROUP BY status", (kind, *tp)
    ).fetchall()}
    for status in STAGES[kind]:
        stats[status] = counts.get(status, 0)

    stats["today"] = get_today_count(kind, track)

    row = conn.execute(f"""
        SELECT AVG(match_score) as avg_score FROM applications
        WHERE kind = ? AND status IN ('applied', 'contacted') AND match_score IS NOT NULL{tc}
    """, (kind, *tp)).fetchone()
    stats["avg_match_score"] = round(row["avg_score"] or 0, 1)

    conn.close()
    return stats


def print_stats():
    """Pretty print stats."""
    s = get_stats()
    print(f"\n📊 Application Stats:")
    print(f"   Discovered: {s['discovered']}")
    print(f"   Matched:    {s['matched']}")
    print(f"   Applied:    {s['applied']} (today: {s['today']})")
    print(f"   Skipped:    {s['skipped']}")
    if s['avg_match_score']:
        print(f"   Avg Score:  {s['avg_match_score']}")





def get_unscored_jobs(kind: str = "job") -> list:
    """
    Get all jobs that need scoring (discovered with no score).

    Ordered so the sources most likely to produce matches are scored first —
    scoring the full backlog takes hours, and this puts probable matches in
    front of the user early instead of grinding through old low-fit inventory.
    """
    conn = get_db()
    rows = conn.execute(
        f"""SELECT * FROM applications
           WHERE match_score IS NULL AND status = 'discovered' AND kind = ?
           ORDER BY CASE
               WHEN platform IN ('yc_jobs','remotive','himalayas','arbeitnow',
                                 'weworkremotely','web3career') THEN 0
               WHEN platform = 'remoteok' THEN 1
               WHEN platform LIKE 'jobspy%' THEN 2
               WHEN platform IN ('greenhouse','lever') THEN 3
               ELSE 4
           END,
           discovered_at DESC""", (kind,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_all_jobs(
    status: str = None,
    company: str = None,
    min_score: int = None,
    search: str = None,
    sort_by: str = "discovered_at",
    sort_order: str = "desc",
    limit: int = 100,
    offset: int = 0,
    kind: str = None,
    track: str = None,
) -> tuple:
    """Get jobs with filtering, sorting, and pagination. Returns (jobs, total_count).
    `track` filters to one resume track ('' = main, None = all)."""
    conn = get_db()
    try:
        conditions = []
        params = []

        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if track is not None:
            conditions.append("COALESCE(track, '') = ?")
            params.append(track)

        if status:
            wanted = [x.strip() for x in status.split(",") if x.strip()]
            conditions.append(f"status IN ({','.join('?' * len(wanted))})")
            params.extend(wanted)
        if company:
            conditions.append("company LIKE ?")
            params.append(f"%{company}%")
        if min_score is not None:
            conditions.append("match_score >= ?")
            params.append(min_score)
        if search:
            conditions.append("(title LIKE ? OR company LIKE ? OR location LIKE ?)")
            params.extend([f"%{search}%"] * 3)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # Validate sort column
        valid_sorts = ["discovered_at", "match_score", "company", "title", "status", "applied_at", "last_activity", "follow_up_date"]
        if sort_by not in valid_sorts:
            sort_by = "discovered_at"
        if sort_order not in ("asc", "desc"):
            sort_order = "desc"

        # Get total count
        count_row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM applications {where}", params
        ).fetchone()
        total = count_row["cnt"]

        # Get paginated results
        rows = conn.execute(
            f"SELECT * FROM applications {where} ORDER BY {sort_by} {sort_order} LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()
    finally:
        conn.close()

    return [dict(row) for row in rows], total


def get_job_by_id(job_id: str) -> dict:
    """Get a single job by ID."""
    conn = get_db()
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_job_status(job_id: str, status: str) -> bool:
    """Move a job/lead to a stage valid for its kind. Entering applied / contacted /
    proposal schedules the follow-up reminder."""
    from datetime import timedelta
    conn = get_db()
    try:
        row = conn.execute("SELECT kind FROM applications WHERE id = ?", (job_id,)).fetchone()
        kind = (row["kind"] if row and row["kind"] in KINDS else "job")
        if status not in STAGES[kind]:
            return False
        now = datetime.now().isoformat()
        if status in FOLLOW_UP_DAYS:
            follow_up = (datetime.now() + timedelta(days=FOLLOW_UP_DAYS[status])).isoformat()
            conn.execute(
                "UPDATE applications SET status = ?, last_activity = ?, follow_up_date = ?, "
                "applied_at = COALESCE(applied_at, ?) WHERE id = ?",
                (status, now, follow_up, now, job_id))
        else:
            conn.execute("UPDATE applications SET status = ?, last_activity = ? WHERE id = ?",
                         (status, now, job_id))
        conn.commit()
    finally:
        conn.close()
    _emit("job_status_changed", {"id": job_id, "status": status})
    return True


def set_job_track(job_id: str, track: str) -> bool:
    """Move a job to another resume track. Its cover letter and tailoring were
    written for the old resume, so they are cleared."""
    conn = get_db()
    cur = conn.execute(
        "UPDATE applications SET track = ?, cover_letter = '', tailored_resume = '' WHERE id = ?",
        (track or "", job_id))
    conn.commit()
    conn.close()
    _emit("job_track_changed", {"id": job_id, "track": track or ""})
    return cur.rowcount > 0


def track_counts(kind: str = "job") -> dict:
    """{track: {status: n}} for the resume-track switcher ('' = main)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT COALESCE(track, '') AS track, status, COUNT(*) AS cnt FROM applications "
        "WHERE kind = ? GROUP BY COALESCE(track, ''), status", (kind,)).fetchall()
    conn.close()
    out: dict = {}
    for r in rows:
        out.setdefault(r["track"], {})[r["status"]] = r["cnt"]
    return out


def clear_track(track: str) -> int:
    """A track was deleted: its jobs go back to the main resume."""
    conn = get_db()
    cur = conn.execute("UPDATE applications SET track = '' WHERE track = ?", (track,))
    conn.commit()
    conn.close()
    return cur.rowcount


def update_job_notes(job_id: str, notes: str) -> bool:
    """Update notes for a job."""
    conn = get_db()
    conn.execute("UPDATE applications SET notes = ? WHERE id = ?", (notes, job_id))
    conn.commit()
    conn.close()
    return True


def update_apply_email(job_id: str, apply_email: str) -> bool:
    """Store the extracted application email for a job."""
    conn = get_db()
    conn.execute("UPDATE applications SET apply_email = ? WHERE id = ?", (apply_email, job_id))
    conn.commit()
    conn.close()
    return True


def update_apply_url(job_id: str, apply_url: str) -> bool:
    """Update a job's apply_url (after URL resolution)."""
    conn = get_db()
    conn.execute("UPDATE applications SET apply_url = ? WHERE id = ?", (apply_url, job_id))
    conn.commit()
    conn.close()
    return True


def delete_job(job_id: str) -> bool:
    """Delete a single job."""
    conn = get_db()
    cursor = conn.execute("DELETE FROM applications WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def get_timeline_stats(kind: str = "job") -> list:
    """Get application counts grouped by date for charts."""
    conn = get_db()
    rows = conn.execute("""
        SELECT DATE(discovered_at) as date,
               COUNT(*) as total,
               SUM(CASE WHEN status IN ('applied', 'contacted') THEN 1 ELSE 0 END) as applied,
               SUM(CASE WHEN status = 'matched' THEN 1 ELSE 0 END) as matched
        FROM applications
        WHERE discovered_at IS NOT NULL AND kind = ?
        GROUP BY DATE(discovered_at)
        ORDER BY date DESC
        LIMIT 30
    """, (kind,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_score_distribution(kind: str = "job") -> list:
    """Get score distribution for charts."""
    conn = get_db()
    rows = conn.execute("""
        SELECT
            CASE
                WHEN match_score >= 90 THEN '90-100'
                WHEN match_score >= 80 THEN '80-89'
                WHEN match_score >= 70 THEN '70-79'
                WHEN match_score >= 60 THEN '60-69'
                WHEN match_score >= 50 THEN '50-59'
                WHEN match_score < 50 THEN '0-49'
                ELSE 'Unscored'
            END as bracket,
            COUNT(*) as count
        FROM applications
        WHERE kind = ?
        GROUP BY bracket
        ORDER BY bracket DESC
    """, (kind,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_companies(kind: str = "job") -> list:
    """Get distinct company names for filter dropdown."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT company FROM applications WHERE kind = ? ORDER BY company", (kind,)
    ).fetchall()
    conn.close()
    return [row["company"] for row in rows]


def update_cover_letter(job_id: str, cover_letter: str) -> bool:
    """Store an on-demand cover letter for a job (preferences.cover_letters = on_demand)."""
    conn = get_db()
    conn.execute(
        "UPDATE applications SET cover_letter = ? WHERE id = ?",
        (cover_letter, job_id)
    )
    conn.commit()
    conn.close()
    _emit("cover_letter_updated", {"id": job_id})
    return True


def update_tailored_resume(job_id: str, tailored_data: dict) -> bool:
    """Store tailored resume data for a job."""
    conn = get_db()
    conn.execute(
        "UPDATE applications SET tailored_resume = ? WHERE id = ?",
        (json.dumps(tailored_data), job_id)
    )
    conn.commit()
    conn.close()
    _emit("tailor_complete", {"id": job_id})
    return True


def get_tailored_resume(job_id: str) -> dict:
    """Get tailored resume data for a job."""
    conn = get_db()
    row = conn.execute(
        "SELECT tailored_resume FROM applications WHERE id = ?", (job_id,)
    ).fetchone()
    conn.close()
    if row and row["tailored_resume"]:
        try:
            return json.loads(row["tailored_resume"])
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


# ---------------------------------------------------------------------------
# Follow-up reminders & ghost detection
# ---------------------------------------------------------------------------


def set_follow_up(job_id: str, days: int = 7) -> bool:
    """Set a follow-up reminder date for a job."""
    from datetime import timedelta
    follow_up = (datetime.now() + timedelta(days=days)).isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE applications SET follow_up_date = ?, last_activity = ? WHERE id = ?",
        (follow_up, datetime.now().isoformat(), job_id)
    )
    conn.commit()
    conn.close()
    _emit("follow_up_set", {"id": job_id, "date": follow_up})
    return True


def get_overdue_follow_ups(kind: str = "job") -> list:
    """Rows past their follow-up date that are still in an active stage."""
    active = ACTIVE_STAGES[kind]
    conn = get_db()
    rows = conn.execute(f"""
        SELECT * FROM applications
        WHERE kind = ?
          AND follow_up_date IS NOT NULL
          AND follow_up_date <= datetime('now')
          AND status IN ({','.join('?' * len(active))})
        ORDER BY follow_up_date ASC
    """, (kind, *active)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_ghost_alerts(days: int = 14, kind: str = "job") -> list:
    """Applied (or contacted) more than N days ago with no activity since."""
    first_stage = "applied" if kind == "job" else "contacted"
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM applications
        WHERE kind = ? AND status = ?
          AND applied_at IS NOT NULL
          AND datetime(applied_at, '+' || ? || ' days') <= datetime('now')
          AND (last_activity IS NULL OR datetime(last_activity, '+' || ? || ' days') <= datetime('now'))
        ORDER BY applied_at ASC
    """, (kind, first_stage, days, days)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def increment_follow_up(job_id: str, days: int = 7) -> bool:
    """Mark a follow-up as done and schedule the next one."""
    from datetime import timedelta
    conn = get_db()
    conn.execute("""
        UPDATE applications
        SET follow_up_count = COALESCE(follow_up_count, 0) + 1,
            follow_up_date = ?,
            last_activity = ?
        WHERE id = ?
    """, (
        (datetime.now() + timedelta(days=days)).isoformat(),
        datetime.now().isoformat(),
        job_id,
    ))
    conn.commit()
    conn.close()
    _emit("follow_up_done", {"id": job_id})
    return True


def dismiss_follow_up(job_id: str) -> bool:
    """Clear follow-up for a job."""
    conn = get_db()
    conn.execute(
        "UPDATE applications SET follow_up_date = NULL WHERE id = ?",
        (job_id,)
    )
    conn.commit()
    conn.close()
    return True


# ---------------------------------------------------------------------------
# Ignore list — hash-based dedup across discovery runs
# ---------------------------------------------------------------------------

import hashlib

def _job_hash(title: str, company: str) -> str:
    """Generate a stable hash from normalized title + company."""
    key = f"{title.lower().strip()}|{company.lower().strip()}"
    return hashlib.md5(key.encode()).hexdigest()


def is_ignored(title: str, company: str) -> bool:
    """Check if a job (by title+company) is in the ignore list."""
    conn = get_db()
    h = _job_hash(title, company)
    row = conn.execute("SELECT hash FROM ignored_hashes WHERE hash = ?", (h,)).fetchone()
    conn.close()
    return row is not None


def ignore_jobs(job_ids: list) -> int:
    """Mark jobs as ignored and add their hashes to the ignore list."""
    conn = get_db()
    count = 0
    for jid in job_ids:
        row = conn.execute("SELECT title, company FROM applications WHERE id = ?", (jid,)).fetchone()
        if row:
            h = _job_hash(row["title"], row["company"])
            conn.execute(
                "INSERT OR IGNORE INTO ignored_hashes (hash, title, company) VALUES (?, ?, ?)",
                (h, row["title"], row["company"])
            )
            conn.execute("UPDATE applications SET status = 'skipped' WHERE id = ?", (jid,))
            count += 1
    conn.commit()
    conn.close()
    _emit("jobs_ignored", {"count": count})
    return count
