/* Inbox: decide on scored jobs. Apply / Save / Skip. */

function applyDrawer() {
  return {
    apply: { open: false, job: {}, mode: "manual", draft: null, kit: null, loading: false, loadingText: "",
             error: "", sending: false, tailoring: false, resolving: false, writing: false, note: "", hasBuilder: true },
    async openApply(job) {
      this.apply = { ...this.apply, open: true, job, draft: null, kit: null, error: "", note: "",
                     mode: job.apply_email ? "email" : "manual" };
      document.body.style.overflow = "hidden";
      if (this.apply.mode === "email") await this.loadDraft(); else await this.loadKit();
    },
    closeApply() { this.apply.open = false; document.body.style.overflow = ""; },
    async loadDraft() {
      if (this.apply.draft) return;
      this.apply.loading = true; this.apply.loadingText = "Writing the draft…"; this.apply.error = "";
      try { this.apply.draft = await api.post(`/api/jobs/${this.apply.job.id}/email-draft`, {}, { quiet: true }); }
      catch (e) { this.apply.error = e.message; }
      this.apply.loading = false;
    },
    async loadKit() {
      if (this.apply.kit) return;
      this.apply.loading = true; this.apply.loadingText = "Preparing the apply kit (cover letter takes ~20s)…"; this.apply.error = "";
      try { this.apply.kit = await api.post(`/api/jobs/${this.apply.job.id}/apply-kit`, {}, { quiet: true }); }
      catch (e) { this.apply.error = e.message; }
      this.apply.loading = false;
    },
    async resolveUrl() {
      this.apply.resolving = true;
      try {
        const r = await api.post(`/api/jobs/${this.apply.job.id}/resolve-url`);
        if (r.url) this.apply.kit.apply_url = r.url;
        toast(r.status === "already_ats" ? "That is already the company's form" : r.error ? "Could not resolve: " + r.error : "Found: " + fmt.host(r.url));
      } finally { this.apply.resolving = false; }
    },
    async rewriteLetter() {
      this.apply.writing = true;
      try {
        const r = await api.post(`/api/jobs/${this.apply.job.id}/cover-letter`, { force: true });
        this.apply.kit.cover_letter = r.cover_letter;
      } finally { this.apply.writing = false; }
    },
    async tailor() {
      this.apply.tailoring = true;
      try {
        const r = await api.post(`/api/jobs/${this.apply.job.id}/tailor`);
        if (r.error) toast("Tailoring: " + r.error, "error");
        else toast(`Resume tailored (ATS ${r.ats_score ?? "?"})`);
        if (this.apply.kit) { this.apply.kit = null; await this.loadKit(); }
        if (this.apply.draft) { this.apply.draft = null; await this.loadDraft(); }
        this.apply.job.tailored_resume = "1";
      } finally { this.apply.tailoring = false; }
    },
    async sendEmail() {
      const d = this.apply.draft;
      if (!confirm(`Send this application to ${d.to}?\n\nThis sends a real email from your account.`)) return;
      this.apply.sending = true;
      try {
        await api.post(`/api/jobs/${this.apply.job.id}/email-send`, { to: d.to, subject: d.subject, body: d.body });
        toast("Sent. Moved to Applications.");
        this.afterApplied(this.apply.job);
        this.closeApply();
      } finally { this.apply.sending = false; }
    },
    async markApplied() {
      await api.post(`/api/jobs/${this.apply.job.id}/applied`, { note: this.apply.note });
      toast("Marked as applied");
      this.afterApplied(this.apply.job);
      this.closeApply();
    },
    async skipFromDrawer() {
      await api.post(`/api/jobs/${this.apply.job.id}/skip`);
      this.afterApplied(this.apply.job);
      this.closeApply();
    },
    afterApplied(job) { if (this.jobs) this.jobs = this.jobs.filter((j) => j.id !== job.id); if (this.load) this.load(); },
  };
}

function inbox() {
  return {
    ...applyDrawer(),
    tab: "matched",
    jobs: [], total: 0, counts: {}, unscored: 0, search: "", loading: true,
    get subtitle() {
      if (this.tab === "matched") return this.total ? `${this.total} to decide on, best first` : "";
      return "";
    },
    async init() {
      await this.load();
      bus.on((ev) => {
        if (["scoring_complete", "job_status_changed", "job_matched"].includes(ev.type)) this.loadSoon();
      });
    },
    _t: null,
    loadSoon() { clearTimeout(this._t); this._t = setTimeout(() => this.load(), 1000); },
    async load(offset = 0) {
      this.loading = true;
      const params = new URLSearchParams({ status: this.tab, sort_by: this.tab === "matched" ? "match_score" : "discovered_at",
                                           sort_order: "desc", limit: 30, offset });
      if (this.search) params.set("search", this.search);
      const [r, stats] = await Promise.all([api.get("/api/jobs?" + params), api.get("/api/stats")]);
      this.jobs = offset ? this.jobs.concat(r.jobs) : r.jobs;
      this.total = r.total;
      this.counts = { matched: stats.matched, saved: stats.saved, skipped: stats.skipped };
      this.unscored = stats.discovered;
      this.loading = false;
    },
    more() { return this.load(this.jobs.length); },
    remove(job) { this.jobs = this.jobs.filter((j) => j.id !== job.id); this.total--; },
    async save(job) { await api.post(`/api/jobs/${job.id}/save`); this.remove(job); this.counts.saved++; },
    async skip(job) { await api.post(`/api/jobs/${job.id}/skip`); this.remove(job); this.counts.skipped++; },
    async restore(job) { await api.patch(`/api/jobs/${job.id}`, { status: "matched" }); this.remove(job); this.counts.matched++; },
    async rescore(job) {
      job._busy = true;
      try {
        const r = await api.post(`/api/jobs/${job.id}/score`);
        Object.assign(job, r.job);
        toast(`Scored ${r.job.match_score}`);
        if (r.job.status !== this.tab) this.remove(job);
      } finally { job._busy = false; }
    },
  };
}
