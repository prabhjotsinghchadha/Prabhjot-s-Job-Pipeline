/* Resume: structured builder (saved to profile.resume), uploads, tailoring, ATS. */

function resume() {
  const blank = () => ({ headline: "", summary: "", skills: [], experience: [], education: [], projects: [],
                         certifications: [], awards: [], languages: [], use_tailored_when_applying: true });
  return {
    r: blank(), files: [], tailored: [], dirty: false, saving: false, importing: false, uploading: false,
    atsBusy: false, atsReport: null, uploadName: "", saveAsName: "",
    custom: { title: "", company: "", url: "", description: "", busy: false },
    get hasBuilder() { return !!(this.r.summary || this.r.experience.length); },
    get certText() {
      return (this.r.certifications || []).map((c) => typeof c === "string" ? c : [c.name, c.issuer, c.year].filter(Boolean).join(" — ")).join("\n");
    },
    setCerts(text) {
      this.r.certifications = fmt.lines(text).map((l) => { const [name, issuer, year] = l.split(/\s+—\s+|\s+-\s+/); return { name: name || "", issuer: issuer || "", year: year || "" }; });
      this.dirty = true;
    },
    async init() {
      const p = await api.get("/api/profile");
      this.r = { ...blank(), ...(p.resume || {}) };
      for (const k of ["skills", "experience", "education", "projects", "certifications", "awards", "languages"]) if (!Array.isArray(this.r[k])) this.r[k] = [];
      this.r.experience.forEach((e) => (e.bullets ||= []));
      this.r.skills.forEach((g) => (g.items ||= []));
      await Promise.all([this.loadFiles(), this.loadTailored()]);
    },
    async loadFiles() { this.files = await api.get("/api/resumes"); },
    async loadTailored() { this.tailored = await api.get("/api/resume/tailored"); },
    async save() {
      this.saving = true;
      try { await api.patch("/api/profile", { resume: JSON.parse(JSON.stringify(this.r)) }); this.dirty = false; toast("Saved"); }
      finally { this.saving = false; }
    },
    async ats(jobId = "") {
      this.atsBusy = true;
      try { this.atsReport = await api.get("/api/resume/ats" + (jobId ? "?job_id=" + jobId : "")); window.scrollTo({ top: 0, behavior: "smooth" }); }
      finally { this.atsBusy = false; }
    },
    async importPdf() {
      if (this.hasBuilder && !confirm("Replace the builder content with what Claude reads from your default PDF?")) return;
      this.importing = true;
      try { const r = await api.post("/api/resume/import", { save: true }); this.r = { ...blank(), ...r.resume }; this.dirty = false; toast("Imported"); }
      finally { this.importing = false; }
    },
    async upload() {
      const f = this.$refs.file.files[0];
      if (!f) return toast("Choose a PDF first", "error");
      const fd = new FormData(); fd.append("file", f); fd.append("name", this.uploadName || f.name.replace(/\.pdf$/i, ""));
      this.uploading = true;
      try { await api.post("/api/resumes", fd); this.uploadName = ""; this.$refs.file.value = ""; await this.loadFiles(); toast("Uploaded"); }
      finally { this.uploading = false; }
    },
    async setDefault(f) { await api.patch(`/api/resumes/${encodeURIComponent(f.name)}/default`); await this.loadFiles(); },
    async deleteFile(f) { if (confirm(`Delete ${f.name}?`)) { await api.del(`/api/resumes/${encodeURIComponent(f.name)}`); await this.loadFiles(); } },
    async saveAs() { await api.post("/api/resume/save-as", { name: this.saveAsName }); this.saveAsName = ""; await this.loadFiles(); toast("Saved as PDF"); },
    async customize() {
      this.custom.busy = true;
      try {
        const job = await api.post("/api/jobs/manual", { title: this.custom.title, company: this.custom.company, url: this.custom.url, description: this.custom.description });
        const r = await api.post(`/api/jobs/${job.id}/tailor`);
        if (r.error) toast("Tailoring: " + r.error, "error"); else toast(`Tailored (ATS ${r.ats_score ?? "?"})`);
        this.custom = { title: "", company: "", url: "", description: "", busy: false };
        await this.loadTailored();
      } finally { this.custom.busy = false; }
    },
  };
}
