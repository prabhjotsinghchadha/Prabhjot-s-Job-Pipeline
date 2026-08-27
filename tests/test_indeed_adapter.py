"""Tests for adapters/indeed.py — native Indeed Apply flow (pure logic)."""

from adapters.indeed import (
    CONTINUE_RX,
    IGNORE_BUTTON_RX,
    SUBMIT_RX,
    is_indeed_job_url,
    _classify_apply_candidates,
    _is_indeed_host,
)


class TestIsIndeedJobUrl:
    def test_viewjob(self):
        assert is_indeed_job_url("https://www.indeed.com/viewjob?jk=5087fc206cc0738e")

    def test_country_subdomain(self):
        assert is_indeed_job_url("https://au.indeed.com/viewjob?jk=60cfc8d242fde8ed")

    def test_smartapply(self):
        assert is_indeed_job_url(
            "https://smartapply.indeed.com/beta/indeedapply/form/contact-info")

    def test_rc_clk_redirect(self):
        assert is_indeed_job_url("https://www.indeed.com/rc/clk?jk=abc&from=x")

    def test_company_page_not_job(self):
        assert not is_indeed_job_url("https://www.indeed.com/cmp/Acme-Corp")

    def test_other_domains(self):
        assert not is_indeed_job_url("https://boards.greenhouse.io/acme/jobs/123")
        assert not is_indeed_job_url("https://www.linkedin.com/jobs/view/123")
        # lookalike domain must not match
        assert not is_indeed_job_url("https://notindeed.com/viewjob?jk=abc")

    def test_garbage(self):
        assert not is_indeed_job_url("")
        assert not is_indeed_job_url("not a url")


class TestSubmitGuard:
    def test_matches_submit_buttons(self):
        assert SUBMIT_RX.search("Submit your application")
        assert SUBMIT_RX.search("Submit")
        assert SUBMIT_RX.search("Send application")

    def test_does_not_match_navigation(self):
        assert not SUBMIT_RX.search("Continue")
        assert not SUBMIT_RX.search("Next")
        assert not SUBMIT_RX.search("Save and continue")
        assert not SUBMIT_RX.search("Review your application")


class TestStepNavigation:
    def test_continue_matches_step_buttons(self):
        assert CONTINUE_RX.match("Continue")
        assert CONTINUE_RX.match("Next")
        assert CONTINUE_RX.match("Save and continue")
        assert CONTINUE_RX.match("Review your application")

    def test_continue_does_not_match_submit(self):
        assert not CONTINUE_RX.match("Submit your application")

    def test_ignores_wizard_chrome(self):
        for text in ("Skip to main content", "Save and close",
                     "Report an issue", "Accept Terms", "Privacy",
                     "1 new update"):
            assert IGNORE_BUTTON_RX.search(text), text

    def test_does_not_ignore_navigation(self):
        assert not IGNORE_BUTTON_RX.search("Continue")
        assert not IGNORE_BUTTON_RX.search("Submit your application")


class TestClassifyApplyCandidates:
    PAGE = "https://www.indeed.com/viewjob?jk=abc"

    def test_native_by_id(self):
        result = _classify_apply_candidates(
            [{"tag": "button", "id": "indeedApplyButton", "testid": "",
              "text": "Apply now", "href": "", "cls": ""}], self.PAGE)
        assert result["mode"] == "native"
        assert result["selector"] == '[id="indeedApplyButton"]'

    def test_native_by_text(self):
        result = _classify_apply_candidates(
            [{"tag": "button", "id": "", "testid": "",
              "text": "Apply now", "href": "", "cls": "css-xyz"}], self.PAGE)
        assert result["mode"] == "native"

    def test_native_apply_with_indeed_hashed_id(self):
        # Real markup seen on viewjob pages: hashed id, "Apply with Indeed"
        result = _classify_apply_candidates(
            [{"tag": "button", "id": "aa19c224432c7b3a", "testid":
              "aa19c224432c7b3a-test", "text": "Apply with Indeed",
              "href": "", "cls": "css-abc"}], self.PAGE)
        assert result["mode"] == "native"
        # attribute selector, not #id — hashed ids can start with a digit
        assert result["selector"] == '[id="aa19c224432c7b3a"]'

    def test_native_fallback_generic_apply_button(self):
        result = _classify_apply_candidates(
            [{"tag": "button", "id": "", "testid": "",
              "text": "Apply", "href": "", "cls": ""}], self.PAGE)
        assert result["mode"] == "native"

    def test_external_company_site_link(self):
        result = _classify_apply_candidates(
            [{"tag": "a", "id": "", "testid": "",
              "text": "Apply on company site",
              "href": "https://boards.greenhouse.io/acme/jobs/1",
              "cls": ""}], self.PAGE)
        assert result["mode"] == "external"
        assert "greenhouse.io" in result["url"]

    def test_external_button_without_href(self):
        result = _classify_apply_candidates(
            [{"tag": "button", "id": "", "testid": "",
              "text": "Apply on company site", "href": "", "cls": ""}],
            self.PAGE)
        assert result["mode"] == "external_button"

    def test_native_preferred_over_external(self):
        result = _classify_apply_candidates(
            [{"tag": "a", "id": "", "testid": "", "text": "Apply here",
              "href": "https://acme.com/careers", "cls": ""},
             {"tag": "button", "id": "indeedApplyButton", "testid": "",
              "text": "Apply now", "href": "", "cls": ""}], self.PAGE)
        assert result["mode"] == "native"

    def test_indeed_internal_link_not_external(self):
        # A relative or indeed.com href must never count as an external ATS
        result = _classify_apply_candidates(
            [{"tag": "a", "id": "", "testid": "", "text": "Apply with Indeed",
              "href": "/promo/resume", "cls": ""}], self.PAGE)
        assert result["mode"] != "external"

    def test_nothing_found(self):
        result = _classify_apply_candidates([], self.PAGE)
        assert result["mode"] == "none"


class TestIsIndeedHost:
    def test_hosts(self):
        assert _is_indeed_host("https://www.indeed.com/viewjob")
        assert _is_indeed_host("https://smartapply.indeed.com/x")
        assert not _is_indeed_host("https://boards.greenhouse.io/x")
        assert not _is_indeed_host("https://notindeed.com/x")
