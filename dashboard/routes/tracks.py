"""Resume tracks: several resumes aimed at different roles (e.g. Senior SWE and CTO).
The main track is the top-level profile; the others live under `tracks:`."""

from fastapi import APIRouter, HTTPException

from dashboard.routes import load_profile, save_profile
from utils import tracks as T

router = APIRouter(tags=["tracks"])


def _with_counts(items: list) -> list:
    from utils.tracker import track_counts
    counts = track_counts("job")
    return [{**t, "counts": counts.get(T.norm_id(t["id"]), {})} for t in items]


@router.get("/tracks")
async def list_tracks() -> list:
    """Every resume track, main first, with job counts per status."""
    try:
        profile = load_profile()
    except HTTPException:
        profile = {}
    return _with_counts(T.list_tracks(profile))


@router.post("/tracks")
async def create_track(body: dict) -> dict:
    """{name, roles?, copy_from?} — copy_from copies that track's builder content."""
    profile = load_profile()
    try:
        track = T.add_track(profile, body.get("name"), body.get("copy_from"),
                            **{k: v for k, v in body.items() if k in T.FIELDS})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    save_profile(profile)
    return track


@router.patch("/tracks/{track_id}")
async def update_track(track_id: str, body: dict) -> dict:
    """Change any of name, roles, search_queries, keywords, ideal_job_description,
    min_match_score, resume (builder content). {resume_file: <uploaded PDF name or ''>}
    picks the PDF this track attaches."""
    if "resume_file" in body:
        from dashboard.routes.resume import use_pdf_for_track
        use_pdf_for_track(track_id, body["resume_file"] or "")
    profile = load_profile()
    try:
        track = T.update_track(profile, track_id, {k: v for k, v in body.items() if k != "resume_file"})
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown resume track")
    save_profile(profile)
    return _with_counts([track])[0]


@router.delete("/tracks/{track_id}")
async def delete_track(track_id: str) -> dict:
    """Remove a track. Its jobs move to the main resume."""
    from utils.tracker import clear_track
    profile = load_profile()
    try:
        T.remove_track(profile, track_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown resume track")
    save_profile(profile)
    return {"ok": True, "moved_to_main": clear_track(track_id)}
