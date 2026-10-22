"""
Applying. Two paths, both human-in-the-loop:

  email   draft -> you review -> POST send           (SMTP, resume attached)
  manual  apply-kit (real URL + answers + letter) -> you fill the form -> POST applied

Nothing is ever sent or submitted without a click from the user.
"""

import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException

from dashboard.routes import job_description, load_profile, require_job
from utils.events import EventBus
from utils.system_state import is_paused
from utils.tracker import log_applied, update_apply_email, update_apply_url, update_cover_letter, update_job_notes

router = APIRouter(tags=["apply"])


def _brain(profile: dict):
    from utils.brain import ClaudeBrain
    return ClaudeBrain(verbose=False, profile=profile)


async def _cover_letter(job: dict, profile: dict, force: bool = False) -> str:
    existing = (job.get("cover_letter") or "").strip()
    if existing and not force:
        return existing
    from utils.resume_parser import extract_resume_text
    resume_text = extract_resume_text(profile.get("resume_path", ""))
    letter = await asyncio.to_thread(
        _brain(profile).write_cover_letter, job_description(job), profile, resume_text,
        job.get("company") or "", job.get("title") or "",
    )
    if letter:
        update_cover_letter(job["id"], letter)
    return letter


@router.post("/jobs/{job_id}/cover-letter")
async def cover_letter(job_id: str, body: dict = {}) -> dict:
    """Write (or rewrite with {force: true}) the cover letter for one job."""
    job = require_job(job_id)
    letter = await _cover_letter(job, load_profile(), force=bool(body.get("force")))
    if not letter:
        raise HTTPException(status_code=502, detail="Claude returned an empty cover letter")
    return {"job_id": job_id, "cover_letter": letter}


@router.post("/jobs/{job_id}/resolve-url")
async def resolve_url(job_id: str) -> dict:
    """Turn an aggregator link into the real application page (headless browser)."""
    from utils.url_resolver import is_ats_url, resolve_apply_url
    job = require_job(job_id)
    current = job.get("apply_url") or job.get("url") or ""
    if is_ats_url(current):
        return {"status": "already_ats", "url": current}
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await (await browser.new_context(user_agent="Mozilla/5.0 (Macintosh) Chrome/120")).new_page()
            result = await resolve_apply_url(page, job_url=current, company=job.get("company", ""),
                                             title=job.get("title", ""), description=job.get("description", ""),
                                             platform=job.get("platform", ""))
            await browser.close()
        if result["resolved_url"] != current:
            update_apply_url(job_id, result["resolved_url"])
        if result.get("apply_email") and not job.get("apply_email"):
            update_apply_email(job_id, result["apply_email"])
        return {"status": result["resolution"], "url": result["resolved_url"],
                "apply_email": result.get("apply_email")}
    except Exception as e:
        return {"status": "error", "url": current, "error": str(e)}


@router.post("/jobs/{job_id}/apply-kit")
async def apply_kit(job_id: str, body: dict = {}) -> dict:
    """
    Everything needed to apply by hand on the company's form: the best apply
    URL, your contact details, the common answers, the cover letter (written
    now if missing) and which resume PDF to attach. {resolve: true} also
    resolves aggregator links first.
    """
    job = require_job(job_id)
    profile = load_profile()
    if body.get("resolve"):
        await resolve_url(job_id)
        job = require_job(job_id)
    letter = (job.get("cover_letter") or "").strip()
    if not letter and body.get("cover_letter", True) and not is_paused():
        try:
            letter = await _cover_letter(job, profile)
        except Exception as exc:
            letter = f"(cover letter failed: {exc})"
    from utils.resume_builder import resume_path_for_job
    resume = await asyncio.to_thread(resume_path_for_job, profile, job)
    personal = profile.get("personal", {})
    return {
        "job": require_job(job_id),
        "apply_url": job.get("apply_url") or job.get("url"),
        "apply_email": job.get("apply_email") or "",
        "contact": {k: personal.get(k, "") for k in
                    ("first_name", "last_name", "email", "phone", "location", "linkedin", "github", "portfolio")},
        "answers": profile.get("common_answers", {}),
        "cover_letter": letter,
        "resume_path": resume,
        "resume_tailored": bool(resume and "tailored" in str(resume)),
    }


@router.post("/jobs/{job_id}/applied")
async def mark_applied(job_id: str, body: dict = {}) -> dict:
    """You submitted the form yourself: record it. {note?}"""
    job = require_job(job_id)
    log_applied(job_id, True)
    note = f"Applied manually on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    if body.get("note"):
        note += f": {body['note']}"
    existing = (job.get("notes") or "").strip()
    update_job_notes(job_id, f"{existing}\n{note}".strip())
    return require_job(job_id)


# ----------------------------------------------------------------- email

@router.get("/email/config")
async def email_config() -> dict:
    """Is outbound email set up? Never returns the password."""
    from utils.email_apply import smtp_config
    try:
        cfg = smtp_config(load_profile())
        return {"configured": True, "from": cfg["email"], "server": cfg["server"], "port": cfg["port"]}
    except Exception as e:
        return {"configured": False, "reason": str(e)}


@router.post("/email/test")
async def email_test() -> dict:
    """Log in to SMTP to check the credentials. Sends nothing."""
    from utils.email_apply import test_smtp_login
    try:
        return {"ok": True, **(await asyncio.to_thread(test_smtp_login, load_profile()))}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"SMTP login failed: {e}. Gmail needs an App Password."}


@router.post("/jobs/{job_id}/email-draft")
async def email_draft(job_id: str) -> dict:
    """Compose (but never send) the application email for a job with a hiring inbox."""
    from utils.email_apply import compose_application_email, extract_apply_email
    job = require_job(job_id)
    to = job.get("apply_email") or extract_apply_email(job.get("description") or "") or ""
    if not to:
        raise HTTPException(status_code=400, detail="No application email in this posting. Use the apply kit instead.")
    if to != job.get("apply_email"):
        update_apply_email(job_id, to)
        job["apply_email"] = to
    profile = load_profile()
    # Stored letter -> no tokens. Otherwise Claude writes the body, unless paused (plain template then).
    brain = None if ((job.get("cover_letter") or "").strip() or is_paused()) else _brain(profile)
    draft = await asyncio.to_thread(compose_application_email, job, profile, brain)
    from utils.resume_builder import resume_path_for_job
    resume = await asyncio.to_thread(resume_path_for_job, profile, job)
    return {"job_id": job_id, "title": job.get("title"), "company": job.get("company"), **draft, "resume": resume}


@router.post("/jobs/{job_id}/email-send")
async def email_send(job_id: str, body: dict) -> dict:
    """Send ONE reviewed draft: {to, subject, body}. Only called after the user presses Send."""
    from utils.email_apply import send_application_email, smtp_config
    job = require_job(job_id)
    to, subject, text = (body.get("to") or "").strip(), (body.get("subject") or "").strip(), (body.get("body") or "").strip()
    if not (to and subject and text):
        raise HTTPException(status_code=400, detail="to, subject and body are required")
    profile = load_profile()
    try:
        smtp_config(profile)
        from utils.resume_builder import resume_path_for_job
        resume = await asyncio.to_thread(resume_path_for_job, profile, job)
        await asyncio.to_thread(send_application_email, profile, to, subject, text,
                                resume if resume and Path(resume).exists() else "")
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Send failed: {e}")
    log_applied(job_id, True)
    note = f"Applied by email to {to} on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    update_job_notes(job_id, f"{(job.get('notes') or '').strip()}\n{note}".strip())
    EventBus.emit("email_sent", {"job_id": job_id, "to": to, "title": job.get("title"), "company": job.get("company")})
    return {"status": "sent", "job_id": job_id, "to": to}


@router.post("/email/scan")
async def email_scan() -> dict:
    """Regex-scan every stored posting for a hiring inbox. No tokens, no network."""
    from utils.email_apply import backfill_apply_emails
    return await asyncio.to_thread(backfill_apply_emails)
