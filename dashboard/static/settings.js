/* Settings: edit profile.yaml. Lists are comma-separated inputs. */

function settings() {
  const defaults = () => ({
    personal: { first_name: "", last_name: "", email: "", phone: "", location: "", linkedin: "", github: "", portfolio: "" },
    preferences: { roles: [], keywords: [], locations: [], remote_only: true, min_match_score: 65, exclude_companies: [],
                   prefilter: { enabled: true, require_role_keyword: true, skip_junior: true, skip_management: true, max_age_days: 60, title_blocklist: [], title_allowlist: [] } },
    skills: { primary: [], secondary: [] },
    ideal_job_description: "", favorite_companies: [],
    target_boards: { greenhouse: [], lever: [] },
    custom_career_pages: [],
    search: { enabled: true, queries: [], locations: [], distance_miles: 50, results_per_query: 25, salary_min: 0 },
    sources: {},
    schedule: { enabled: true, discover_interval_hours: 6, score_interval_minutes: 30 },
    email: { enabled: false, imap_server: "imap.gmail.com", email: "", app_password: "", check_interval_hours: 12 },
    common_answers: {},
    offer: { enabled: false, services: [], engagement_types: ["fixed-scope", "retainer", "hourly"], rate_min: null, rate_max: null,
             min_budget: null, availability: "", ideal_client: "", red_flags: ["equity only", "unpaid trial", "revenue share"],
             case_studies: [], gig_queries: [], prospect_stacks: [], prospect_exclude: [], min_match_score: null,
             sources: { hn_freelancer: true, jobspy_contract: true, boards_contract: true, prospects_from_jobs: true } },
  });
  const merge = (base, over) => {
    for (const [k, v] of Object.entries(over || {})) {
      if (v && typeof v === "object" && !Array.isArray(v) && base[k] && typeof base[k] === "object" && !Array.isArray(base[k])) merge(base[k], v);
      else if (v !== undefined && v !== null) base[k] = v;
    }
    return base;
  };
  return {
    p: defaults(), sources: [], dirty: false, saving: false, testing: false, needsSetup: false, newAnswer: "",
    claude: { ok: null },
    async init() {
      const [profile, sources, claude] = await Promise.all([api.get("/api/profile"), api.get("/api/sources"), api.get("/api/system/claude")]);
      this.needsSetup = !!profile.needs_setup;
      delete profile.needs_setup;
      const resume = profile.resume; delete profile.resume;
      this.p = merge(defaults(), profile);
      this.sources = sources; this.claude = claude;
    },
    async save() {
      this.saving = true;
      try {
        const body = JSON.parse(JSON.stringify(this.p));
        for (const k of ["rate_min", "rate_max", "min_budget", "min_match_score"]) if (body.offer[k] === null || body.offer[k] === "") delete body.offer[k];
        await api.patch("/api/profile", body);
        this.dirty = false; this.needsSetup = false; toast("Saved");
      } finally { this.saving = false; }
    },
    async testEmail() {
      this.testing = true;
      try { const r = await api.post("/api/email/test"); toast(r.ok ? "Email login works" : r.error, r.ok ? "" : "error"); }
      finally { this.testing = false; }
    },
    addAnswer() {
      const k = this.newAnswer.trim().toLowerCase().replace(/\s+/g, "_");
      if (k && !(k in this.p.common_answers)) { this.p.common_answers[k] = ""; this.dirty = true; }
      this.newAnswer = "";
    },
  };
}
