"""Resume: uploaded PDFs, the structured builder (PDF/DOCX/TXT), per-job tailoring, ATS checks."""

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from dashboard.routes import job_description, load_profile, require_job, save_profile
from utils.paths import BASE_DIR, resumes_dir

router = APIRouter(tags=["resume"])


# ---------------------------------------------------------------- uploads

def _meta_path() -> Path:
    return resumes_dir() / "meta.json"


def _load_meta() -> dict:
    return json.loads(_meta_path().read_text()) if _meta_path().exists() else {}


def _save_meta(meta: dict) -> None:
    _meta_path().write_text(json.dumps(meta, indent=2))


def _set_profile_resume_path(path: str) -> None:
    try:
        path = "./" + str(Path(path).relative_to(BASE_DIR))
    except ValueError:
        pass
    profile = load_profile()
    profile["resume_path"] = path
    save_profile(profile)


def _safe_name(name: str) -> str:
    safe = "".join(c for c in (name or "") if c.isalnum() or c in " -_").strip()
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid resume name")
    return safe


@router.get("/resumes")
async def list_resumes() -> list:
    return [{"name": n, **info, "exists": (resumes_dir() / info["filename"]).exists()}
            for n, info in _load_meta().items()]


@router.post("/resumes")
async def upload_resume(file: UploadFile = File(...), name: str = Form(...)) -> dict:
    meta = _load_meta()
    safe = _safe_name(name)
    filename = f"{safe.replace(' ', '_')}_{file.filename}"
    with open(resumes_dir() / filename, "wb") as f:
        shutil.copyfileobj(file.file, f)
    is_default = len(meta) == 0 or meta.get(safe, {}).get("is_default", False)
    meta[safe] = {"filename": filename, "original_name": file.filename, "is_default": is_default}
    _save_meta(meta)
    if is_default:
        _set_profile_resume_path(str(resumes_dir() / filename))
    return {"name": safe, "filename": filename, "is_default": is_default}


@router.delete("/resumes/{name}")
async def delete_resume(name: str) -> dict:
    meta = _load_meta()
    if name not in meta:
        raise HTTPException(status_code=404, detail="Resume not found")
    path = resumes_dir() / meta[name]["filename"]
    was_default = meta[name].get("is_default", False)
    if path.exists():
        path.unlink()
    del meta[name]
    if was_default and meta:
        first = next(iter(meta))
        meta[first]["is_default"] = True
        _set_profile_resume_path(str(resumes_dir() / meta[first]["filename"]))
    _save_meta(meta)
    return {"ok": True}


@router.patch("/resumes/{name}/default")
async def set_default_resume(name: str) -> dict:
    meta = _load_meta()
    if name not in meta:
        raise HTTPException(status_code=404, detail="Resume not found")
    for key in meta:
        meta[key]["is_default"] = key == name
    _save_meta(meta)
    _set_profile_resume_path(str(resumes_dir() / meta[name]["filename"]))
    return {"ok": True, "default": name}


@router.get("/resumes/{name}/download")
async def download_resume(name: str):
    meta = _load_meta()
    if name not in meta:
        raise HTTPException(status_code=404, detail="Resume not found")
    path = resumes_dir() / meta[name]["filename"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(path, filename=meta[name].get("original_name", meta[name]["filename"]))


# ---------------------------------------------------------------- builder

def _job_context(job_id: str):
    """(job, tailored, description) or (None, None, '')."""
    if not job_id:
        return None, None, ""
    from utils.tracker import get_tailored_resume
    job = require_job(job_id)
    return job, (get_tailored_resume(job_id) or None), job_description(job)


def _suffix(job) -> str:
    if not job:
        return ""
    company = (job.get("company") or "").strip()
    return company if company and company.lower() != "unknown" else (job.get("title") or "").strip()


@router.get("/resume/build")
async def resume_build(format: str = "pdf", job_id: str = ""):
    """Render the builder as pdf | docx | txt. With job_id the stored tailoring is overlaid."""
    from utils.resume_builder import build_resume, has_structured_resume
    profile = load_profile()
    if not has_structured_resume(profile):
        raise HTTPException(status_code=400, detail="The resume builder is empty. Fill it in or import your PDF first.")
    job, tailored, _ = _job_context(job_id)
    try:
        data, filename, mime = await asyncio.to_thread(build_resume, profile, format, tailored, _suffix(job))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/resume/ats")
async def resume_ats(job_id: str = "", source: str = "builder") -> dict:
    """ATS report for the builder (tailored when job_id given) or the uploaded PDF (source=uploaded)."""
    from utils.ats_checker import check_ats, check_ats_file, extract_keywords
    from utils.resume_builder import apply_tailoring, render_text, resume_from_profile
    profile = load_profile()
    job, tailored, desc = _job_context(job_id)
    if job:
        keywords = extract_keywords(desc, exclude=[job.get("company") or ""])
        for kw in (tailored or {}).get("keywords_to_include") or []:
            if str(kw).strip() and str(kw).lower() not in {k.lower() for k in keywords}:
                keywords.append(str(kw).strip())
        keywords = keywords[:50]
    else:
        prefs = profile.get("preferences", {})
        keywords = list(dict.fromkeys([*(prefs.get("keywords") or []),
                                       *((profile.get("skills") or {}).get("primary") or [])])) or None
    if source == "uploaded":
        path = profile.get("resume_path", "")
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail="No uploaded resume PDF found")
        report = await asyncio.to_thread(check_ats_file, path, desc, keywords)
    else:
        report = check_ats(render_text(apply_tailoring(resume_from_profile(profile), tailored)), desc, keywords)
        report["source"] = "builder" + (" (tailored)" if tailored and job else "")
    report["job_id"] = job_id or None
    report["tailored"] = bool(tailored and job)
    return report


@router.post("/resume/import")
async def resume_import(body: dict = {}) -> dict:
    """Transcribe an uploaded PDF into the structured builder with Claude. {name?, save?: true}"""
    from utils.ats_checker import extract_pdf_text
    from utils.brain import ClaudeBrain
    from utils.resume_parser import extract_resume_text
    from utils.resume_tailor import parse_resume_to_structure
    profile = load_profile()
    meta = _load_meta()
    name = (body.get("name") or "").strip()
    if name:
        if name not in meta:
            raise HTTPException(status_code=404, detail="Resume not found")
        path = resumes_dir() / meta[name]["filename"]
    else:
        path = Path(profile.get("resume_path", "") or "")
    if not str(path) or not path.exists():
        raise HTTPException(status_code=404, detail="No resume PDF to import. Upload one first.")
    text = extract_resume_text(str(path)) or extract_pdf_text(path)
    if not text.strip():
        raise HTTPException(status_code=422, detail="Could not read any text from that PDF")
    data = await asyncio.to_thread(parse_resume_to_structure, text, ClaudeBrain(verbose=False, profile=profile), profile)
    if not data or not (data.get("experience") or data.get("summary")):
        raise HTTPException(status_code=502, detail="Claude could not structure this resume. Try again.")
    saved = False
    if body.get("save", True):
        data["use_tailored_when_applying"] = (profile.get("resume") or {}).get("use_tailored_when_applying", True)
        profile["resume"] = data
        save_profile(profile)
        saved = True
    return {"resume": data, "saved": saved, "source": str(path)}


@router.post("/resume/save-as")
async def resume_save_as(body: dict = {}) -> dict:
    """Render the builder (tailored for job_id if given) to a PDF in the uploads list. {name, job_id?, set_default?}"""
    from utils.resume_builder import build_resume, has_structured_resume
    profile = load_profile()
    if not has_structured_resume(profile):
        raise HTTPException(status_code=400, detail="The resume builder is empty")
    safe = _safe_name(body.get("name"))
    job, tailored, _ = _job_context(body.get("job_id") or "")
    data, filename, _ = await asyncio.to_thread(build_resume, profile, "pdf", tailored, _suffix(job))
    stored = f"{safe.replace(' ', '_')}_{filename}"
    (resumes_dir() / stored).write_bytes(data)
    meta = _load_meta()
    set_default = bool(body.get("set_default")) or len(meta) == 0 or meta.get(safe, {}).get("is_default", False)
    if set_default:
        for key in meta:
            meta[key]["is_default"] = False
    meta[safe] = {"filename": stored, "original_name": filename, "is_default": set_default, "built": True,
                  "job_id": job["id"] if job else "", "built_at": datetime.now().isoformat(timespec="seconds")}
    _save_meta(meta)
    if set_default:
        _set_profile_resume_path(str(resumes_dir() / stored))
    return {"name": safe, "filename": stored, "is_default": set_default, "bytes": len(data)}


# --------------------------------------------------------------- tailoring

@router.get("/jobs/{job_id}/tailor")
async def get_tailor(job_id: str) -> dict:
    from utils.tracker import get_tailored_resume
    require_job(job_id)
    return get_tailored_resume(job_id)


@router.post("/jobs/{job_id}/tailor")
async def tailor(job_id: str, body: dict = {}) -> dict:
    """Rewrite the builder's bullets/summary for this posting. {notes?} Blocks until done (~30-60s)."""
    from utils.brain import ClaudeBrain
    from utils.resume_parser import extract_resume_text
    from utils.resume_tailor import tailor_resume
    from utils.tracker import update_tailored_resume
    job = require_job(job_id)
    profile = load_profile()
    resume_text = extract_resume_text(profile.get("resume_path", ""))
    result = await asyncio.to_thread(tailor_resume, job_description(job), resume_text, profile,
                                     ClaudeBrain(verbose=False, profile=profile),
                                     (body.get("notes") or ""), job.get("company") or "")
    update_tailored_resume(job_id, result)
    return {"job_id": job_id, "mode": result.get("mode", ""), "ats_score": (result.get("ats") or {}).get("score"),
            "suggested_skills": result.get("suggested_skills") or [], "error": result.get("error", ""),
            "tailored": result}


@router.get("/resume/tailored")
async def list_tailored() -> list:
    """Jobs that have a tailored resume, newest first."""
    from utils.tracker import get_db
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, company, status, match_score FROM applications "
        "WHERE tailored_resume != '' ORDER BY last_activity DESC, discovered_at DESC LIMIT 100").fetchall()
    conn.close()
    return [dict(r) for r in rows]
