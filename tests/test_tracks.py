"""Resume tracks — overlay, discovery merge, job assignment, per-track prefilter and DB filter."""
import asyncio
from types import SimpleNamespace

import pytest

from utils import tracks as T
from utils.prefilter import prefilter_reason, run_prefilter


def _profile():
    return {
        "personal": {"first_name": "Ada", "last_name": "L", "email": "a@x.io"},
        "preferences": {
            "roles": ["Senior Software Engineer", "Full Stack Engineer"],
            "keywords": ["Python"], "remote_only": True, "locations": ["Remote"],
            "min_match_score": 65, "prefilter": {"enabled": True, "max_age_days": 0},
        },
        "search": {"queries": ["Senior Software Engineer"]},
        "ideal_job_description": "Senior IC at a product company.",
        "resume_path": "./resumes/main.pdf",
        "resume": {"headline": "Senior Software Engineer", "summary": "IC.",
                   "experience": [{"company": "A", "title": "Senior Engineer", "bullets": ["Built X"]}]},
        "tracks": [{
            "id": "cto", "name": "CTO",
            "roles": ["CTO", "VP Engineering", "Head of Engineering"],
            "ideal_job_description": "Technical leader at a seed-to-B startup.",
            "min_match_score": 70,
            "resume": {"headline": "CTO", "summary": "Leader.",
                       "experience": [{"company": "A", "title": "Head of Engineering", "bullets": ["Grew team"]}]},
        }],
    }


def _job(title, track="", id="j1"):
    return {"id": id, "title": title, "company": "Acme", "location": "Remote", "description": "",
            "platform": "remotive", "track": track}


def test_list_tracks_main_first_named_after_first_role():
    ts = T.list_tracks(_profile())
    assert [t["id"] for t in ts] == ["main", "cto"]
    assert ts[0]["name"] == "Senior Software Engineer" and ts[0]["is_main"]
    assert ts[1]["name"] == "CTO" and not ts[1]["is_main"]


def test_profile_for_track_overlays_only_what_the_track_sets():
    eff = T.profile_for_track(_profile(), "cto")
    assert eff["preferences"]["roles"] == ["CTO", "VP Engineering", "Head of Engineering"]
    assert eff["search"]["queries"] == ["CTO", "VP Engineering", "Head of Engineering"]  # main's queries not reused
    assert eff["preferences"]["keywords"] == ["Python"]           # inherited
    assert eff["preferences"]["remote_only"] is True             # inherited
    assert eff["preferences"]["min_match_score"] == 70
    assert eff["ideal_job_description"].startswith("Technical leader")
    assert eff["resume"]["headline"] == "CTO"
    assert eff["resume_path"] == ""   # own builder, no PDF: never attach main's PDF
    assert eff["personal"]["first_name"] == "Ada"
    assert "tracks" not in eff


def test_profile_for_track_unknown_or_main_is_main_and_never_mutates():
    p = _profile()
    assert T.profile_for_track(p, "nope")["preferences"]["roles"] == p["preferences"]["roles"]
    eff = T.profile_for_track(p, "")
    eff["preferences"]["roles"].append("X")
    assert "X" not in p["preferences"]["roles"]


def test_track_without_own_resume_borrows_main():
    p = _profile()
    p["tracks"][0].pop("resume")
    eff = T.profile_for_track(p, "cto")
    assert eff["resume"]["headline"] == "Senior Software Engineer"
    assert eff["resume_path"] == "./resumes/main.pdf"


def test_discovery_profile_unions_roles_and_queries():
    p = _profile()
    d = T.discovery_profile(p, ["main", "cto"])
    assert d["preferences"]["roles"] == ["Senior Software Engineer", "Full Stack Engineer",
                                         "CTO", "VP Engineering", "Head of Engineering"]
    assert d["search"]["queries"] == ["Senior Software Engineer", "CTO", "VP Engineering", "Head of Engineering"]
    assert T.discovery_profile(p, ["cto"])["preferences"]["roles"][0] == "CTO"


def test_selected_filters_unknown_and_defaults_to_all():
    p = _profile()
    assert T.selected(p, None) == ["main", "cto"]
    assert T.selected(p, ["cto", "ghost"]) == ["cto"]
    assert T.selected(p, ["ghost"]) == ["main"]


@pytest.mark.parametrize("title,expected", [
    ("Senior Software Engineer, Payments", ""),
    ("Chief Technology Officer", "cto"),
    ("VP of Engineering", "cto"),
    ("Head of Engineering", "cto"),
    ("Founding CTO", "cto"),
    ("Full Stack Engineer", ""),
])
def test_assign_track_by_title(title, expected):
    p = _profile()
    assert T.assign_track(_job(title), p, ["main", "cto"]) == expected


def test_assign_track_single_selection_is_that_track():
    assert T.assign_track(_job("Senior Software Engineer"), _profile(), ["cto"]) == "cto"


def test_prefilter_judges_each_job_against_its_own_track():
    p = _profile()
    # Management titles are skipped for the IC resume but kept for the CTO resume.
    assert prefilter_reason(_job("VP Engineering"), p) == "management/executive title"
    out = run_prefilter(p, [_job("VP Engineering", track="", id="a"),
                            _job("VP Engineering", track="cto", id="b")], dry_run=True)
    assert out["skipped"] == 1 and out["samples"][0]["id"] == "a"


def test_add_update_remove_track():
    p = _profile()
    t = T.add_track(p, "Staff Engineer", copy_from="main")
    assert t["id"] == "staff-engineer" and t["roles"] == ["Staff Engineer"]
    assert t["resume"]["headline"] == "Staff Engineer"
    assert T.add_track(p, "Staff Engineer")["id"] == "staff-engineer-2"
    T.update_track(p, "staff-engineer", {"roles": ["Staff Engineer", "Principal Engineer"], "min_match_score": ""})
    assert T.get_track(p, "staff-engineer")["roles"][1] == "Principal Engineer"
    T.update_track(p, "main", {"name": "IC", "roles": ["Engineer"]})
    assert p["main_track_name"] == "IC" and p["preferences"]["roles"] == ["Engineer"]
    T.remove_track(p, "staff-engineer")
    assert "staff-engineer" not in T.track_ids(p)
    with pytest.raises(ValueError):
        T.remove_track(p, "main")
    with pytest.raises(KeyError):
        T.update_track(p, "ghost", {"name": "x"})


@pytest.fixture
def db(tmp_path, monkeypatch):
    import utils.paths as paths
    import utils.tracker as tracker
    monkeypatch.setattr(paths, "db_path", lambda: tmp_path / "t.db")
    monkeypatch.setattr(tracker, "_db_path", lambda: tmp_path / "t.db")
    return tracker


def _discovered(id, title, track):
    return SimpleNamespace(id=id, title=title, company="Acme", platform="remotive", url="u", apply_url="u",
                           location="Remote", description="", metadata={"track": track})


def test_tracker_stores_and_filters_by_track(db):
    db.log_discovered(_discovered("a", "Senior Software Engineer", ""))
    db.log_discovered(_discovered("b", "CTO", "cto"))
    assert [j["id"] for j in db.get_all_jobs(track="cto")[0]] == ["b"]
    assert [j["id"] for j in db.get_all_jobs(track="")[0]] == ["a"]
    assert db.get_all_jobs(track=None)[1] == 2
    assert db.get_stats("job", "cto")["discovered"] == 1
    assert db.track_counts("job") == {"": {"discovered": 1}, "cto": {"discovered": 1}}
    db.update_cover_letter("b", "Dear…")
    db.set_job_track("b", "")
    j = db.get_job_by_id("b")
    assert j["track"] == "" and j["cover_letter"] == ""
    db.set_job_track("a", "cto")
    assert db.clear_track("cto") == 1


def test_run_discovery_tags_jobs_with_their_track(db, monkeypatch):
    import utils.discovery as discovery
    import utils.pipeline as pipeline
    seen_profiles = []

    async def fake_discover(profile, on_source_jobs=None):
        seen_profiles.append(profile)
        jobs = [_discovered("x1", "Senior Software Engineer", None), _discovered("x2", "Head of Engineering", None)]
        for j in jobs:
            j.metadata = {}
        await on_source_jobs("fake", jobs)
        return jobs

    monkeypatch.setattr(discovery, "discover_all_jobs", fake_discover)
    r = asyncio.run(pipeline.run_discovery(_profile(), None))
    assert r["new"] == 2 and r["new_by_track"] == {"Senior Software Engineer": 1, "CTO": 1}
    assert "CTO" in seen_profiles[0]["preferences"]["roles"]
    assert db.get_job_by_id("x1")["track"] == "" and db.get_job_by_id("x2")["track"] == "cto"
    # Neither is skipped: each passed the prefilter of its own track.
    assert db.get_job_by_id("x2")["status"] == "discovered"
