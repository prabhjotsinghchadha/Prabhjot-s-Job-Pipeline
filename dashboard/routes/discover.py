"""Find and score jobs. Long work runs in the background; progress arrives over /ws."""

import asyncio

from fastapi import APIRouter, HTTPException

from dashboard.routes import load_profile, require_job
from utils import pipeline

router = APIRouter(tags=["discover"])


@router.post("/discover")
async def discover() -> dict:
    """Run every enabled source, then prefilter. Returns at once."""
    profile = load_profile()
    if pipeline.is_running("discover"):
        return {"status": "already_running"}
    asyncio.create_task(pipeline.run_discovery(profile))
    return {"status": "started"}


@router.post("/score")
async def score() -> dict:
    """Prefilter, then score the unscored backlog in batches. Returns at once."""
    profile = load_profile()
    if pipeline.is_running("score"):
        return {"status": "already_running"}
    from utils.tracker import get_unscored_jobs
    count = len(get_unscored_jobs())
    asyncio.create_task(pipeline.run_scoring(profile))
    return {"status": "started", "count": count}


@router.post("/jobs/{job_id}/score")
async def score_one(job_id: str) -> dict:
    """Re-score one job now (skips the prefilter)."""
    job = require_job(job_id)
    profile = load_profile()
    result = await pipeline.run_scoring(profile, jobs=[job], force=True)
    if result.get("status") == "already_running":
        raise HTTPException(status_code=409, detail="Scoring is already running; try again when it finishes.")
    return {**result, "job": require_job(job_id)}


@router.post("/prefilter")
async def prefilter(body: dict = {}) -> dict:
    """Zero-token rules over the unscored backlog. {dry_run: true} previews."""
    from utils.prefilter import run_prefilter
    profile = load_profile()
    dry_run = bool(body.get("dry_run", False))
    return {"dry_run": dry_run, **(await asyncio.to_thread(run_prefilter, profile, None, dry_run))}


@router.get("/sources")
async def sources() -> list:
    """Discovery source catalog with on/off state (toggle via PATCH /api/profile {sources: {...}})."""
    from utils.discovery import SOURCE_REGISTRY, source_enabled
    try:
        profile = load_profile()
    except HTTPException:
        profile = {}
    return [{**entry, "enabled": source_enabled(profile, entry["key"])} for entry in SOURCE_REGISTRY]


@router.post("/ingest")
async def ingest(body: dict) -> dict:
    """Add jobs found elsewhere (e.g. by a Claude session with WebSearch). {jobs: [{title, company, url, ...}]}"""
    from utils.mcp_source import ingest_jobs
    jobs = body.get("jobs") or []
    if not jobs:
        raise HTTPException(status_code=400, detail="No jobs provided")
    return ingest_jobs(jobs)


@router.post("/jobs/manual")
async def add_manual_job(body: dict) -> dict:
    """Paste a posting by hand: {title, company, url?, description}. Optionally fetches the URL for text."""
    import hashlib
    import re
    from utils.mcp_source import ingest_jobs
    from utils.tracker import get_db

    title = (body.get("title") or "").strip()
    company = (body.get("company") or "").strip() or "Unknown"
    url = (body.get("url") or "").strip()
    description = (body.get("description") or "").strip()
    if not description and url:
        try:
            description = await asyncio.to_thread(_fetch_posting_text, url)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Could not fetch that URL ({exc}). Paste the description instead.")
        if len(description) < 300:
            raise HTTPException(status_code=422, detail="That page had too little text. Paste the description instead.")
    if not description:
        raise HTTPException(status_code=400, detail="Paste the job description or give a URL.")
    if not title:
        m = re.search(r"^\s*([^\n]{4,80})", description)
        title = (m.group(1).strip() if m else "Custom role")[:80]

    job_id = "manual_" + hashlib.md5(f"{title.lower()}|{company.lower()}|{url.lower()}".encode()).hexdigest()[:16]
    ingest_jobs([{"id": job_id, "title": title, "company": company, "url": url, "apply_url": url,
                  "location": body.get("location") or "", "description": description,
                  "platform": "manual", "source": "manual"}])
    conn = get_db()
    conn.execute("UPDATE applications SET description = ?, tailored_resume = '' WHERE id = ?", (description, job_id))
    conn.commit()
    conn.close()
    return require_job(job_id)


def _fetch_posting_text(url: str) -> str:
    import html as _html
    import re
    import httpx
    r = httpx.get(url, follow_redirects=True, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    t = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", t, flags=re.I)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()[:12000]
