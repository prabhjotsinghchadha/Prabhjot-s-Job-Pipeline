/* Applications: everything you applied to, grouped by stage. */

function applications() {
  const LABELS = { applied: "Applied", interviewing: "Interviewing", offer: "Offer", rejected: "Rejected", archived: "Archived" };
  const NEXT = {
    applied: ["interviewing", "rejected", "archived"],
    interviewing: ["offer", "rejected", "archived"],
    offer: ["archived"],
    rejected: ["archived"],
    archived: ["applied"],
  };
  return {
    stages: Object.keys(LABELS),
    tab: "applied",
    groups: {}, total: 0, loading: true, checking: false,
    followUps: { overdue: [], ghosts: [] },
    label: (s) => LABELS[s] || s,
    nextStages: (s) => NEXT[s] || [],
    async init() {
      await this.load();
      bus.on((ev) => { if (["job_status_changed", "job_applied", "email_check_complete"].includes(ev.type)) this.loadSoon(); });
    },
    _t: null,
    loadSoon() { clearTimeout(this._t); this._t = setTimeout(() => this.load(), 800); },
    async load() {
      const [r, fu] = await Promise.all([
        api.get("/api/jobs?status=applied,interviewing,offer,rejected,archived&sort_by=applied_at&sort_order=desc&limit=300"),
        api.get("/api/follow-ups"),
      ]);
      const groups = {};
      for (const j of r.jobs) (groups[j.status] ||= []).push(j);
      this.groups = groups; this.total = r.total; this.followUps = fu; this.loading = false;
    },
    async setStatus(job, status) {
      await api.patch(`/api/jobs/${job.id}`, { status });
      toast(`Moved to ${this.label(status)}`);
      await this.load();
    },
    async saveNotes(job) { await api.patch(`/api/jobs/${job.id}`, { notes: job.notes || "" }); },
    async followedUp(j) { await api.post(`/api/jobs/${j.id}/follow-up`, { next_days: 7 }); await this.load(); },
    async dismissFollowUp(j) { await api.post(`/api/jobs/${j.id}/dismiss-follow-up`); await this.load(); },
    async checkEmail() {
      this.checking = true;
      try {
        const r = await api.post("/api/check-email");
        if (r.error) toast(r.error, "error");
        else if (r.skipped) toast("Email checking is off. Turn it on in Settings.");
        else toast(r.checked ? `${r.checked} update(s) found` : "No new updates");
        await this.load();
      } finally { this.checking = false; }
    },
  };
}
