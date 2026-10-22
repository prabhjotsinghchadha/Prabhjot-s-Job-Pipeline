"""
Resume tracks — one person, several resumes aimed at different roles
(e.g. "Senior Software Engineer" and "CTO").

The top-level profile is the *main* track. Every entry under `tracks:` only
overrides what it sets; everything else (personal details, locations,
sources, common answers) is shared:

    tracks:
      - id: cto
        name: CTO
        roles: [CTO, VP Engineering, Head of Engineering]   # prefilter + scoring + search
        search_queries: [CTO, VP of Engineering]            # optional, defaults to roles
        keywords: [leadership, architecture]                # optional
        ideal_job_description: ...                          # optional
        min_match_score: 70                                 # optional
        resume_path: ./resumes/CTO_cv.pdf                   # optional uploaded PDF
        resume: {headline: ..., experience: [...]}          # optional builder content

`profile_for_track()` turns that into an ordinary profile dict, so discovery,
the prefilter, scoring, tailoring and applying need no track awareness of
their own. Jobs carry the track they were found for in the `track` column
('' = main).
"""

import copy
import re
from typing import Iterable, Optional

MAIN = "main"

# Track keys and where they live on the main (top-level) profile.
FIELDS = ("roles", "search_queries", "keywords", "ideal_job_description",
          "min_match_score", "resume_path", "resume")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:40] or "track"


def _extra(profile: dict) -> list:
    return [t for t in ((profile or {}).get("tracks") or []) if isinstance(t, dict) and t.get("id")]


def _main_name(profile: dict) -> str:
    p = profile or {}
    return (str(p.get("main_track_name") or "").strip()
            or str(((p.get("preferences") or {}).get("roles") or [""])[0]).strip()
            or str((p.get("resume") or {}).get("headline") or "").strip()
            or "Main")


def main_track(profile: dict) -> dict:
    """The top-level profile seen as a track."""
    p = profile or {}
    prefs = p.get("preferences") or {}
    return {
        "id": MAIN, "name": _main_name(p), "is_main": True,
        "roles": list(prefs.get("roles") or []),
        "search_queries": list((p.get("search") or {}).get("queries") or []),
        "keywords": list(prefs.get("keywords") or []),
        "ideal_job_description": p.get("ideal_job_description") or "",
        "min_match_score": prefs.get("min_match_score"),
        "resume_path": p.get("resume_path") or "",
        "resume": p.get("resume") or {},
    }


def list_tracks(profile: dict) -> list:
    """Main first, then the extra tracks in file order."""
    out = [main_track(profile)]
    for t in _extra(profile):
        out.append({**{k: t.get(k) for k in FIELDS}, "id": str(t["id"]),
                    "name": t.get("name") or t["id"], "is_main": False})
    return out


def get_track(profile: dict, track_id: Optional[str]) -> Optional[dict]:
    track_id = track_id or MAIN
    return next((t for t in list_tracks(profile) if t["id"] == track_id), None)


def track_ids(profile: dict) -> list:
    return [t["id"] for t in list_tracks(profile)]


def norm_id(track_id: Optional[str]) -> str:
    """'' and 'main' both mean the main track; stored as '' in the DB."""
    return "" if not track_id or track_id == MAIN else str(track_id)


def profile_for_track(profile: dict, track_id: Optional[str]) -> dict:
    """
    A full profile dict for one track. Unknown or empty ids give the main
    profile. A track with its own builder content but no PDF of its own does
    not inherit the main PDF (that would score/attach the wrong resume).
    """
    track_id = track_id or MAIN
    eff = copy.deepcopy(profile or {})
    if track_id == MAIN:
        return eff
    t = next((x for x in _extra(profile) if str(x["id"]) == track_id), None)
    if not t:
        return eff
    eff.pop("tracks", None)
    prefs = eff.setdefault("preferences", {})
    if t.get("roles"):
        prefs["roles"] = list(t["roles"])
        # Main's explicit queries are for main's roles; never search them for this track.
        eff.setdefault("search", {})["queries"] = list(t.get("search_queries") or t["roles"])
    elif t.get("search_queries"):
        eff.setdefault("search", {})["queries"] = list(t["search_queries"])
    if t.get("keywords"):
        prefs["keywords"] = list(t["keywords"])
    if t.get("ideal_job_description"):
        eff["ideal_job_description"] = t["ideal_job_description"]
    if t.get("min_match_score") not in (None, ""):
        prefs["min_match_score"] = t["min_match_score"]
    own_resume = t.get("resume") or {}
    if own_resume.get("experience") or own_resume.get("summary"):
        eff["resume"] = copy.deepcopy(own_resume)
        eff["resume_path"] = t.get("resume_path") or ""
    elif t.get("resume_path"):
        eff["resume_path"] = t["resume_path"]
    eff["_track"] = {"id": track_id, "name": t.get("name") or track_id}
    return eff


def profile_for_job(profile: dict, job: dict) -> dict:
    return profile_for_track(profile, (job or {}).get("track"))


def selected(profile: dict, wanted: Optional[Iterable[str]]) -> list:
    """Valid track ids from `wanted` (None/empty = all tracks), in profile order."""
    all_ids = track_ids(profile)
    want = {MAIN if not w else str(w) for w in (wanted or [])}
    return [i for i in all_ids if not want or i in want] or [MAIN]


def discovery_profile(profile: dict, ids: list) -> dict:
    """One profile whose roles and search queries cover every selected track,
    so the sources run once instead of once per track."""
    if ids == [MAIN]:
        return copy.deepcopy(profile)
    effs = [profile_for_track(profile, i) for i in ids]
    out = copy.deepcopy(effs[0])
    roles, queries, keywords = [], [], []
    for e in effs:
        prefs = e.get("preferences") or {}
        roles += prefs.get("roles") or []
        queries += (e.get("search") or {}).get("queries") or prefs.get("roles") or []
        keywords += prefs.get("keywords") or []
    out.setdefault("preferences", {})["roles"] = list(dict.fromkeys(roles))
    out["preferences"]["keywords"] = list(dict.fromkeys(keywords))
    out.setdefault("search", {})["queries"] = list(dict.fromkeys(queries))
    out.pop("_track", None)
    return out


def assign_track(job, profile: dict, ids: list) -> str:
    """
    Which selected track a discovered job belongs to: among the tracks whose
    prefilter it passes, the one whose roles match the title best; ties go to
    the earlier track. If it passes none, the first selected track (the
    prefilter will skip it there). Returns the DB value ('' = main).
    """
    if len(ids) == 1:
        return norm_id(ids[0])
    from utils.prefilter import _has_phrase, _norm, job_to_dict, prefilter_reason, role_keywords
    j = job_to_dict(job)
    title = _norm(j.get("title"))
    best, best_score = ids[0], -1
    for tid in ids:
        eff = profile_for_track(profile, tid)
        if prefilter_reason(j, eff) is not None:
            continue
        roles = (eff.get("preferences") or {}).get("roles") or []
        # Whole role phrases count most, then individual role words/synonyms.
        score = sum(10 * len(r.split()) for r in roles if _has_phrase(title, r))
        score += sum(len(kw.split()) for kw in role_keywords(eff) if _has_phrase(title, kw))
        if score > best_score:
            best, best_score = tid, score
    return norm_id(best)


def track_resume_text(eff: dict) -> str:
    """Resume text Claude scores against: the track's PDF, else its builder content."""
    from utils.resume_parser import extract_resume_text
    text = extract_resume_text(eff.get("resume_path") or "") if eff.get("resume_path") else ""
    if text.strip():
        return text
    from utils.resume_builder import has_structured_resume, render_text, resume_from_profile
    return render_text(resume_from_profile(eff)) if has_structured_resume(eff) else ""


# ------------------------------------------------------------------ editing

def update_track(profile: dict, track_id: str, changes: dict) -> dict:
    """Apply `changes` (track field names) to the profile in place. Returns the track."""
    changes = {k: v for k, v in (changes or {}).items() if k in FIELDS or k == "name"}
    if (track_id or MAIN) == MAIN:
        prefs = profile.setdefault("preferences", {})
        if "name" in changes:
            profile["main_track_name"] = str(changes["name"] or "").strip()
        if "roles" in changes:
            prefs["roles"] = list(changes["roles"] or [])
        if "search_queries" in changes:
            profile.setdefault("search", {})["queries"] = list(changes["search_queries"] or [])
        if "keywords" in changes:
            prefs["keywords"] = list(changes["keywords"] or [])
        if "ideal_job_description" in changes:
            profile["ideal_job_description"] = changes["ideal_job_description"] or ""
        if "min_match_score" in changes and changes["min_match_score"] not in (None, ""):
            prefs["min_match_score"] = int(changes["min_match_score"])
        if "resume_path" in changes:
            profile["resume_path"] = changes["resume_path"] or ""
        if "resume" in changes:
            profile["resume"] = changes["resume"] or {}
        return main_track(profile)
    t = next((x for x in _extra(profile) if str(x["id"]) == track_id), None)
    if t is None:
        raise KeyError(track_id)
    for k, v in changes.items():
        if k == "min_match_score":
            v = int(v) if v not in (None, "") else None
        if v in (None, "", [], {}):
            t.pop(k, None)
        else:
            t[k] = v
    return get_track(profile, track_id)


def add_track(profile: dict, name: str, copy_from: Optional[str] = None, **fields) -> dict:
    """Append a track. `copy_from` copies that track's builder content (not its PDF:
    the copy gets reshaped for other roles, so it renders from its own builder
    until it is given a PDF of its own)."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("Give the resume a name")
    existing = set(track_ids(profile))
    base = slugify(name)
    tid, n = base, 2
    while tid in existing:
        tid, n = f"{base}-{n}", n + 1
    entry: dict = {"id": tid, "name": name}
    if copy_from:
        src = get_track(profile, copy_from)
        if src:
            if src.get("resume"):
                entry["resume"] = copy.deepcopy(src["resume"])
                if name and isinstance(entry["resume"], dict):
                    entry["resume"]["headline"] = name
    entry["roles"] = list(fields.get("roles") or [name])
    profile.setdefault("tracks", []).append(entry)
    rest = {k: v for k, v in fields.items() if k in FIELDS and k != "roles"}
    if rest:
        update_track(profile, tid, rest)
    return get_track(profile, tid)


def remove_track(profile: dict, track_id: str) -> None:
    if (track_id or MAIN) == MAIN:
        raise ValueError("The main resume cannot be deleted")
    before = len(profile.get("tracks") or [])
    profile["tracks"] = [t for t in (profile.get("tracks") or []) if str(t.get("id")) != track_id]
    if len(profile["tracks"]) == before:
        raise KeyError(track_id)
    if not profile["tracks"]:
        profile.pop("tracks")
