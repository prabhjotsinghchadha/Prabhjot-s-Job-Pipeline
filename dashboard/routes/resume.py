"""Resume: uploaded PDFs, the structured builder (PDF/DOCX/TXT), per-job tailoring, ATS checks."""

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from dashboard.routes import job_description, job_profile, load_profile, require_job, save_profile
from utils.paths import BASE_DIR, resumes_dir

router = APIRouter(tags=["resume"])


# ---------------------------------------------------------------- uploads

def _meta_path() -> Path:
    return resumes_dir() / "meta.json"


def _load_meta() -> dict:
    return json.loads(_meta_path().read_text()) if _meta_path().exists() else {}


def _save_meta(meta: dict) -> None:
    _meta_path().write_text(json.dumps(meta, indent=2))


def _rel(path: str) -> str:
    try:
        return "./" + str(Path(path).relative_to(BASE_DIR))
    except ValueError:
        return path


def _set_profile_resume_path(path: str) -> None:
    profile = load_profile()
    profile["resume_path"] = _rel(path)
    save_profile(profile)


def _same_file(a: str, b: str) -> bool:
    try:
        return bool(a and b) and Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def use_pdf_for_track(track: str, name: str) -> None:
    """Point a resume track at an uploaded PDF ('' name = none). Main = the default PDF."""
    from utils.tracks import MAIN, update_track
    meta = _load_meta()
    if name and name not in meta:
        raise HTTPException(status_code=404, detail="Resume not found")
    if (track or MAIN) == MAIN:
        for key in meta:
            meta[key]["is_default"] = key == name
        _save_meta(meta)
    profile = load_profile()
    update_track(profile, track or MAIN, {"resume_path": _rel(str(resumes_dir() / meta[name]["filename"])) if name else ""})
    save_profile(profile)


def _safe_name(name: str) -> str:
    safe = "".join(c for c in (name or "") if c.isalnum() or c in " -_").strip()
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid resume name")
    return safe


@router.get("/resumes")
async def list_resumes() -> list:
    """Uploaded PDFs; `used_by` lists the resume tracks that attach each one."""
    from utils.tracks import list_tracks
    try:
        tracks = list_tracks(load_profile())
    except HTTPException:
        tracks = []
    out = []
    for n, info in _load_meta().items():
        path = str(resumes_dir() / info["filename"])
        used_by = [t["id"] for t in tracks if _same_file(t.get("resume_path") or "", path)]
        out.append({"name": n, **info, "exists": Path(path).exists(), "used_by": used_by})
    return out


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
    from utils.tracks import list_tracks, update_track
    profile = load_profile()
    users = [t["id"] for t in list_tracks(profile) if not t["is_main"] and _same_file(t.get("resume_path") or "", str(path))]
    for tid in users:
        update_track(profile, tid, {"resume_path": ""})
    if users:
        save_profile(profile)
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
async def set_default_resume(name: str, track: str = "main") -> dict:
    """Use this PDF for a resume track (default: the main one)."""
    use_pdf_for_track(track, name)
    return {"ok": True, "default": name, "track": track}


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

def _builder_profile(job, track: str = "") -> dict:
    """The job's track when there is a job, else the requested track."""
    from utils.tracks import profile_for_track
    return job_profile(job) if job else profile_for_track(load_profile(), track)


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
async def resume_build(format: str = "pdf", job_id: str = "", track: str = ""):
    """Render the builder as pdf | docx | txt for a resume track. With job_id the job's
    track is used and its stored tailoring is overlaid."""
    from utils.resume_builder import build_resume, has_structured_resume
    job, tailored, _ = _job_context(job_id)
    profile = _builder_profile(job, track)
    if not has_structured_resume(profile):
        raise HTTPException(status_code=400, detail="The resume builder is empty. Fill it in or import your PDF first.")
    try:
        data, filename, mime = await asyncio.to_thread(build_resume, profile, format, tailored, _suffix(job))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/resume/ats")
async def resume_ats(job_id: str = "", source: str = "builder", track: str = "") -> dict:
    """ATS report for a track's builder (tailored when job_id given) or its uploaded PDF (source=uploaded)."""
    from utils.ats_checker import check_ats, check_ats_file, extract_keywords
    from utils.resume_builder import apply_tailoring, render_text, resume_from_profile
    job, tailored, desc = _job_context(job_id)
    profile = _builder_profile(job, track)
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
    """Transcribe an uploaded PDF into a track's builder with Claude. {name?, track?, save?: true}"""
    from utils.ats_checker import extract_pdf_text
    from utils.brain import ClaudeBrain
    from utils.resume_parser import extract_resume_text
    from utils.resume_tailor import parse_resume_to_structure
    from utils.tracks import MAIN, profile_for_track, update_track
    track = body.get("track") or MAIN
    profile = profile_for_track(load_profile(), track)
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
        base = load_profile()
        try:
            update_track(base, track, {"resume": data})
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown resume track")
        save_profile(base)
        saved = True
    return {"resume": data, "saved": saved, "source": str(path)}


@router.post("/resume/save-as")
async def resume_save_as(body: dict = {}) -> dict:
    """Render a track's builder (tailored for job_id if given) to a PDF in the uploads list.
    {name, job_id?, track?, set_default?} — set_default makes it that track's PDF."""
    from utils.resume_builder import build_resume, has_structured_resume
    from utils.tracks import MAIN
    track = body.get("track") or MAIN
    job, tailored, _ = _job_context(body.get("job_id") or "")
    profile = _builder_profile(job, track)
    if job:
        track = job.get("track") or MAIN
    if not has_structured_resume(profile):
        raise HTTPException(status_code=400, detail="The resume builder is empty")
    safe = _safe_name(body.get("name"))
    data, filename, _ = await asyncio.to_thread(build_resume, profile, "pdf", tailored, _suffix(job))
    stored = f"{safe.replace(' ', '_')}_{filename}"
    (resumes_dir() / stored).write_bytes(data)
    meta = _load_meta()
    set_default = bool(body.get("set_default")) or (track == MAIN and (
        len(meta) == 0 or meta.get(safe, {}).get("is_default", False)))
    meta[safe] = {"filename": stored, "original_name": filename, "is_default": meta.get(safe, {}).get("is_default", False),
                  "built": True, "job_id": job["id"] if job else "", "track": track,
                  "built_at": datetime.now().isoformat(timespec="seconds")}
    _save_meta(meta)
    if set_default:
        use_pdf_for_track(track, safe)
    return {"name": safe, "filename": stored, "is_default": set_default, "track": track, "bytes": len(data)}


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
    from utils.resume_tailor import tailor_resume
    from utils.tracker import update_tailored_resume
    from utils.tracks import track_resume_text
    job = require_job(job_id)
    profile = job_profile(job)
    resume_text = track_resume_text(profile)
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
        "SELECT id, title, company, status, match_score, track FROM applications "
        "WHERE tailored_resume != '' ORDER BY last_activity DESC, discovered_at DESC LIMIT 100").fetchall()
    conn.close()
    return [dict(r) for r in rows]
