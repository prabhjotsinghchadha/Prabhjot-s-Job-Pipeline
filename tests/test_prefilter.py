"""Deterministic prefilter — no network, no DB, no tokens."""
from datetime import date, timedelta

from utils.prefilter import prefilter_reason, run_prefilter, role_keywords, reason_key


def _profile(**over):
    p = {
        "preferences": {
            "roles": ["Senior Full Stack Engineer", "Founding Engineer", "Web3 Engineer"],
            "remote_only": True,
            "locations": ["Remote", "United States", "United Kingdom"],
            "exclude_companies": ["Meta"],
            "prefilter": {"enabled": True, "max_age_days": 60},
        }
    }
    p["preferences"].update(over)
    return p


def _job(title="Senior Full Stack Engineer", company="Acme", location="Remote",
         description="", platform="greenhouse", date_posted=None, id="j1"):
    return {"id": id, "title": title, "company": company, "location": location,
            "description": description, "platform": platform, "date_posted": date_posted}


def test_clean_remote_engineer_passes():
    assert prefilter_reason(_job(), _profile()) is None


def test_excluded_company_whole_word_only():
    assert "excluded company" in prefilter_reason(_job(company="Meta"), _profile())
    assert "excluded company" in prefilter_reason(_job(company="Meta Platforms, Inc."), _profile())
    assert prefilter_reason(_job(company="Metabase"), _profile()) is None


def test_non_engineering_title_blocklisted():
    assert "non-engineering" in prefilter_reason(_job(title="Account Executive, AI Native"), _profile())
    assert "non-engineering" in prefilter_reason(_job(title="Sales Engineer"), _profile())


def test_unrelated_title_needs_role_keyword():
    assert "unrelated" in prefilter_reason(_job(title="Product Designer"), _profile())
    assert prefilter_reason(_job(title="Fullstack Developer (React/Node)"), _profile()) is None
    assert prefilter_reason(_job(title="Web3 Solidity Engineer"), _profile()) is None
    off = _profile(prefilter={"require_role_keyword": False})
    assert prefilter_reason(_job(title="Product Designer"), off) is None


def test_allowlist_overrides_title_rules():
    p = _profile(prefilter={"title_allowlist": ["designer"]})
    assert prefilter_reason(_job(title="Product Designer"), p) is None


def test_seniority_rules():
    assert "junior" in prefilter_reason(_job(title="Software Engineering Intern"), _profile())
    assert "management" in prefilter_reason(_job(title="Director of Engineering"), _profile())
    # A profile targeting junior roles must not skip them.
    jr = _profile(roles=["Junior Software Engineer"])
    assert prefilter_reason(_job(title="Junior Software Engineer"), jr) is None
    # A profile targeting leadership keeps leadership titles.
    mgr = _profile(roles=["Director of Engineering"])
    assert prefilter_reason(_job(title="Director of Engineering"), mgr) is None


def test_remote_only_rules():
    p = _profile()
    assert prefilter_reason(_job(location="Remote - US"), p) is None
    assert prefilter_reason(_job(location="Worldwide", platform="remoteok"), p) is None
    assert prefilter_reason(_job(location="London", description="This role is fully remote in the UK"), p) is None
    assert "on-site" in prefilter_reason(_job(location="San Francisco, CA (Hybrid)"), p)
    assert "on-site" in prefilter_reason(_job(location="SF", description="3 days a week in office, hybrid"), p)
    assert "no remote" in prefilter_reason(_job(location="New York, NY", description="We are in-person in NYC."), p)
    assert "not remote" in prefilter_reason(_job(location="Remote", description="This is not a remote role."), p)
    # Unknown description + plain city: not enough evidence either way → score it.
    assert prefilter_reason(_job(location="Austin, TX", description=""), p) is None


def test_location_rule_when_not_remote_only():
    p = _profile(remote_only=False, locations=["San Francisco", "Remote"])
    assert prefilter_reason(_job(location="San Francisco, CA"), p) is None
    assert prefilter_reason(_job(location="Remote - Europe"), p) is None
    assert "location not in preferences" in prefilter_reason(_job(location="Berlin, Germany"), p)


def test_stale_posting():
    old = (date.today() - timedelta(days=90)).isoformat()
    fresh = (date.today() - timedelta(days=3)).isoformat()
    assert "posted 90 days ago" in prefilter_reason(_job(date_posted=old), _profile())
    assert prefilter_reason(_job(date_posted=fresh), _profile()) is None
    assert prefilter_reason(_job(date_posted="garbage"), _profile()) is None
    assert prefilter_reason(_job(date_posted=old), _profile(prefilter={"max_age_days": 0})) is None


def test_disabled_passes_everything():
    assert prefilter_reason(_job(title="Account Executive", company="Meta"), _profile(prefilter={"enabled": False})) is None
    assert prefilter_reason(_job(title="Account Executive", company="Meta"), _profile(prefilter=False)) is None


def test_run_prefilter_dry_run_summary():
    jobs = [_job(id="a"), _job(id="b", company="Meta"), _job(id="c", title="Recruiter"),
            _job(id="d", title="Staff Software Engineer", location="Remote")]
    s = run_prefilter(_profile(), jobs, dry_run=True)
    assert (s["checked"], s["skipped"], s["kept"]) == (4, 2, 2)
    assert s["reasons"] == {"excluded company": 1, "non-engineering title": 1}
    assert {x["id"] for x in s["samples"]} == {"b", "c"}


def test_role_keywords_expand_families():
    kws = role_keywords(_profile())
    assert {"engineer", "developer", "full stack", "fullstack", "founding", "web3", "solidity"} <= kws
    assert "senior" not in kws


def test_reason_key_strips_detail():
    assert reason_key("excluded company (Meta)") == "excluded company"
    assert reason_key("junior/intern title") == "junior/intern title"


if __name__ == "__main__":  # runnable without pytest
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"  ok   {fn.__name__}")
        except Exception as e:
            failed += 1; print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"{len(fns) - failed}/{len(fns)} passed"); sys.exit(1 if failed else 0)
