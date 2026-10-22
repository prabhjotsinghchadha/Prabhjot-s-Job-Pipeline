"""Clients mode: contract classifier, prospect builder, stage validation per kind."""

import pytest

from utils import leads


class TestContractClassifier:
    def test_contract_in_title(self):
        assert leads.is_contract_posting({"title": "Senior Engineer (Contract)", "description": ""})

    def test_employment_type_metadata(self):
        j = {"title": "Engineer", "description": "", "metadata": {"employment_type": "Freelance"}}
        assert leads.is_contract_posting(j) == "employment type: freelance"

    def test_metadata_as_json_string(self):
        j = {"title": "Engineer", "description": "", "metadata": '{"tags": ["contract", "remote"]}'}
        assert leads.is_contract_posting(j)

    def test_full_time_title_beats_body_mention(self):
        j = {"title": "Full-time Backend Engineer", "description": "We also hire contractors sometimes."}
        assert leads.is_contract_posting(j) is None

    def test_plain_job(self):
        assert leads.is_contract_posting({"title": "Staff Engineer", "description": "Great benefits."}) is None

    def test_seeking_freelancer(self):
        assert leads.is_contract_posting({"title": "SEEKING FREELANCER", "description": ""})


@pytest.fixture
def db(tmp_path, monkeypatch):
    import utils.paths as paths
    monkeypatch.setattr(paths, "db_path", lambda: tmp_path / "t.db")
    import utils.tracker as tracker
    monkeypatch.setattr(tracker, "_db_path", lambda: tmp_path / "t.db")
    return tracker


def _job(tracker, id, company, title, desc, email="", kind="job"):
    from utils.discovery import Job
    tracker.log_discovered(Job(id=id, title=title, company=company, location="Remote", url=f"https://x/{id}",
                               apply_url=f"https://x/{id}", platform="t", description=desc,
                               metadata={"kind": kind, "apply_email": email or None}))


PROFILE = {"offer": {"prospect_stacks": ["Next.js", "TypeScript"], "prospect_exclude": ["BigCo"]}}


class TestLeadIds:
    def test_gig_gets_own_id_prefix(self):
        from utils.discovery import Job
        j = Job(id="jobspy_abc", title="t", company="c", location="", url="", apply_url="", platform="x")
        assert leads._as_lead(j, "gig").id == "gig_jobspy_abc"
        assert leads._as_lead(j, "gig").id == "gig_jobspy_abc"  # idempotent

    def test_prospect_keeps_id(self):
        from utils.discovery import Job
        j = Job(id="prospect_acme", title="t", company="c", location="", url="", apply_url="", platform="x")
        assert leads._as_lead(j, "prospect").id == "prospect_acme"


class TestProspects:
    def test_groups_companies_hiring_in_stack(self, db):
        _job(db, "a1", "Acme", "Frontend Engineer", "We use Next.js and TypeScript.", email="jobs@acme.io")
        _job(db, "a2", "Acme", "Backend Engineer", "TypeScript services on Node.")
        _job(db, "b1", "BigCo", "Engineer", "Next.js everywhere")
        _job(db, "c1", "Other", "Engineer", "Rust and Go only")
        out = leads.build_prospects(PROFILE)
        assert [p.company for p in out] == ["Acme"]
        p = out[0]
        assert p.metadata["kind"] == "lead" and p.metadata["lead_type"] == "prospect"
        assert p.metadata["postings"] == 2
        assert p.metadata["apply_email"] == "jobs@acme.io"
        assert p.id == "prospect_acme"

    def test_existing_lead_company_not_repeated(self, db):
        _job(db, "a1", "Acme", "Frontend Engineer", "Next.js")
        _job(db, "l1", "Acme", "Prospect: hiring", "x", kind="lead")
        assert leads.build_prospects(PROFILE) == []

    def test_no_stacks_no_prospects(self, db):
        _job(db, "a1", "Acme", "Frontend Engineer", "Next.js")
        assert leads.build_prospects({"offer": {}}) == []


class TestStagesPerKind:
    def test_lead_rows_get_lead_kind_and_lead_stages(self, db):
        _job(db, "l1", "Acme", "Gig", "x", kind="lead")
        _job(db, "j1", "Acme", "Job", "x")
        assert db.get_job_by_id("l1")["kind"] == "lead"
        assert db.update_job_status("l1", "contacted") is True
        assert db.update_job_status("l1", "applied") is False
        assert db.update_job_status("j1", "contacted") is False
        assert db.update_job_status("j1", "applied") is True

    def test_contacted_schedules_follow_up(self, db):
        _job(db, "l1", "Acme", "Gig", "x", kind="lead")
        db.update_job_status("l1", "contacted")
        row = db.get_job_by_id("l1")
        assert row["follow_up_date"] and row["applied_at"]

    def test_stats_are_per_kind(self, db):
        _job(db, "l1", "Acme", "Gig", "x", kind="lead")
        _job(db, "j1", "Acme", "Job", "x")
        assert db.get_stats("lead")["discovered"] == 1
        assert db.get_stats("job")["discovered"] == 1
        assert db.get_all_jobs(kind="lead")[1] == 1

    def test_overdue_follow_ups_per_kind(self, db):
        _job(db, "l1", "Acme", "Gig", "x", kind="lead")
        db.update_job_status("l1", "contacted")
        db.set_follow_up("l1", days=-1)
        assert [r["id"] for r in db.get_overdue_follow_ups("lead")] == ["l1"]
        assert db.get_overdue_follow_ups("job") == []
