/* Resume: one builder per resume track (main = profile.resume, others = tracks[].resume),
   what each track targets, uploads, tailoring, ATS. */

function resume() {
  const blank = () => ({ headline: "", summary: "", skills: [], experience: [], education: [], projects: [],
                         certifications: [], awards: [], languages: [], use_tailored_when_applying: true });
  const normalize = (raw) => {
    const r = { ...blank(), ...JSON.parse(JSON.stringify(raw || {})) };
    for (const k of ["skills", "experience", "education", "projects", "certifications", "awards", "languages"]) if (!Array.isArray(r[k])) r[k] = [];
    r.experience.forEach((e) => (e.bullets ||= []));
    r.skills.forEach((g) => (g.items ||= []));
    return r;
  };
  const hasContent = (r) => !!(r && (r.summary || (r.experience || []).length));
  return {
    tracks: [], cur: "main", t: {}, newName: "", adding: false,
    r: blank(), files: [], tailored: [], dirty: false, saving: false, importing: false, uploading: false,
    atsBusy: false, atsReport: null, uploadName: "", saveAsName: "",
    custom: { title: "", company: "", url: "", description: "", busy: false },
    get hasBuilder() { return hasContent(this.r); },
    get isMain() { return this.cur === "main"; },
    get mainTrack() { return this.tracks.find((x) => x.is_main) || {}; },
    get inherits() { return !this.isMain && !this.hasBuilder; },
    get q() { return "track=" + encodeURIComponent(this.cur); },
    get pdfName() { return (this.files.find((f) => f.used_by.includes(this.cur)) || {}).name || ""; },
    name: (id) => trackName([], id),
    get certText() {
      return (this.r.certifications || []).map((c) => typeof c === "string" ? c : [c.name, c.issuer, c.year].filter(Boolean).join(" — ")).join("\n");
    },
    setCerts(text) {
      this.r.certifications = fmt.lines(text).map((l) => { const [name, issuer, year] = l.split(/\s+—\s+|\s+-\s+/); return { name: name || "", issuer: issuer || "", year: year || "" }; });
      this.dirty = true;
    },
    async init() {
      this.tracks = await tracksReady;
      this.name = (id) => trackName(this.tracks, id);
      this.select(this.tracks.some((x) => x.id === TRACK) ? TRACK : "main", true);
      await Promise.all([this.loadFiles(), this.loadTailored()]);
    },
    select(id, force = false) {
      if (!force && this.dirty && !confirm("Discard unsaved changes to this resume?")) return;
      const t = this.tracks.find((x) => x.id === id) || this.tracks[0] || { id: "main", name: "Main", is_main: true, roles: [] };
      this.cur = t.id;
      this.t = JSON.parse(JSON.stringify(t));
      this.r = normalize(t.resume);
      this.dirty = false; this.atsReport = null;
    },
    async reloadTracks(select) {
      this.tracks = await api.get("/api/tracks");
      this.select(select || this.cur, true);
    },
    async loadFiles() { this.files = await api.get("/api/resumes"); },
    async loadTailored() { this.tailored = await api.get("/api/resume/tailored"); },
    async save() {
      this.saving = true;
      try {
        const body = { name: this.t.name, roles: this.t.roles, search_queries: this.t.search_queries, keywords: this.t.keywords,
                       ideal_job_description: this.t.ideal_job_description, min_match_score: this.t.min_match_score };
        // A track that still borrows the main resume keeps borrowing it until it gets content of its own.
        if (this.isMain || this.hasBuilder) body.resume = JSON.parse(JSON.stringify(this.r));
        await api.patch(`/api/tracks/${this.cur}`, body);
        await this.reloadTracks();
        toast("Saved");
      } finally { this.saving = false; }
    },
    async addTrack() {
      if (!this.newName.trim()) return;
      const t = await api.post("/api/tracks", { name: this.newName.trim(), copy_from: this.cur });
      this.newName = ""; this.adding = false;
      await Promise.all([this.reloadTracks(t.id), this.loadFiles()]);
      toast(`Created "${t.name}". Set its roles, then edit the copy.`);
    },
    async removeTrack() {
      if (!confirm(`Delete the "${this.t.name}" resume? Its jobs move to ${this.mainTrack.name}.`)) return;
      const r = await api.del(`/api/tracks/${this.cur}`);
      if (TRACK === this.cur) { try { localStorage.setItem("track", ""); } catch (_) {} }
      await this.reloadTracks("main");
      toast(`Deleted. ${r.moved_to_main} job(s) moved to ${this.mainTrack.name}.`);
    },
    copyMain() { this.r = normalize(this.mainTrack.resume); this.r.headline = this.t.name || this.r.headline; this.dirty = true; },
    async usePdf(name) {
      await api.patch(`/api/tracks/${this.cur}`, { resume_file: name });
      await Promise.all([this.loadFiles(), this.reloadTracks()]);
      toast(name ? `${this.t.name} now attaches ${name}` : "No PDF: the builder is attached instead");
    },
    async ats(jobId = "") {
      this.atsBusy = true;
      try { this.atsReport = await api.get("/api/resume/ats?" + (jobId ? "job_id=" + jobId : this.q)); window.scrollTo({ top: 0, behavior: "smooth" }); }
      finally { this.atsBusy = false; }
    },
    async importPdf() {
      const src = this.pdfName || (this.isMain ? "" : null);
      if (src === null) return toast("Pick a PDF for this resume first (Uploaded PDFs → Use for this)", "error");
      if (this.hasBuilder && !confirm(`Replace the builder content with what Claude reads from ${src || "the default PDF"}?`)) return;
      this.importing = true;
      try {
        const r = await api.post("/api/resume/import", { save: true, track: this.cur, name: src });
        await this.reloadTracks(); this.r = normalize(r.resume); toast("Imported");
      } finally { this.importing = false; }
    },
    async upload() {
      const f = this.$refs.file.files[0];
      if (!f) return toast("Choose a PDF first", "error");
      const name = this.uploadName || f.name.replace(/\.pdf$/i, "");
      const fd = new FormData(); fd.append("file", f); fd.append("name", name);
      this.uploading = true;
      try {
        const up = await api.post("/api/resumes", fd);
        this.uploadName = ""; this.$refs.file.value = "";
        if (!this.isMain && !this.pdfName) await this.usePdf(up.name); else await this.loadFiles();
        toast("Uploaded");
      } finally { this.uploading = false; }
    },
    async deleteFile(f) { if (confirm(`Delete ${f.name}?`)) { await api.del(`/api/resumes/${encodeURIComponent(f.name)}`); await Promise.all([this.loadFiles(), this.reloadTracks()]); } },
    async saveAs() { await api.post("/api/resume/save-as", { name: this.saveAsName, track: this.cur }); this.saveAsName = ""; await this.loadFiles(); toast("Saved as PDF"); },
    async customize() {
      this.custom.busy = true;
      try {
        const job = await api.post("/api/jobs/manual", { title: this.custom.title, company: this.custom.company, url: this.custom.url,
                                                          description: this.custom.description, track: this.cur });
        const r = await api.post(`/api/jobs/${job.id}/tailor`);
        if (r.error) toast("Tailoring: " + r.error, "error"); else toast(`Tailored (ATS ${r.ats_score ?? "?"})`);
        this.custom = { title: "", company: "", url: "", description: "", busy: false };
        await this.loadTailored();
      } finally { this.custom.busy = false; }
    },
  };
}
