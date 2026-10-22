/* Shared helpers: API calls, WebSocket, toast, formatting, the top bar. */

const api = {
  async call(method, path, body, opts = {}) {
    const init = { method, headers: {} };
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
    const r = await fetch(path, init);
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch (_) {}
      if (!opts.quiet) toast(detail, "error");
      throw new Error(detail);
    }
    const ct = r.headers.get("content-type") || "";
    return ct.includes("json") ? r.json() : r.text();
  },
  get: (p, o) => api.call("GET", p, undefined, o),
  post: (p, b = {}, o) => api.call("POST", p, b, o),
  patch: (p, b, o) => api.call("PATCH", p, b, o),
  del: (p, o) => api.call("DELETE", p, undefined, o),
};

let _toastTimer;
function toast(msg, kind = "") {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.className = "toast " + kind;
  el.style.display = "block";
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => (el.style.display = "none"), kind === "error" ? 6000 : 3000);
}

/* One WebSocket per page; listeners get every event. */
const bus = {
  listeners: [],
  on(fn) { this.listeners.push(fn); },
  connect() {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.onmessage = (m) => {
      if (m.data === "pong") return;
      let ev; try { ev = JSON.parse(m.data); } catch (_) { return; }
      this.listeners.forEach((fn) => { try { fn(ev); } catch (_) {} });
    };
    ws.onclose = () => setTimeout(() => this.connect(), 3000);
    this.ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000);
    ws.onclose = () => { clearInterval(this.ping); setTimeout(() => this.connect(), 3000); };
  },
};
bus.connect();

const fmt = {
  date(s) {
    if (!s) return "";
    const d = new Date(s);
    if (isNaN(d)) return s;
    const diff = (Date.now() - d) / 86400000;
    if (diff < 1) return "today";
    if (diff < 2) return "yesterday";
    if (diff < 14) return Math.floor(diff) + " days ago";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  },
  dateTime(s) { return s ? new Date(s).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : ""; },
  scoreClass(n) { return n == null ? "" : n >= 70 ? "good" : n >= 50 ? "weak" : "bad"; },
  list(v) { return Array.isArray(v) ? v.join(", ") : v || ""; },
  toList(s) { return String(s || "").split(/[\n,]/).map((x) => x.trim()).filter(Boolean); },
  lines(s) { return String(s || "").split("\n").map((x) => x.trim()).filter(Boolean); },
  host(u) { try { return new URL(u).host.replace(/^www\./, ""); } catch (_) { return ""; } },
};

async function copyText(t) {
  try { await navigator.clipboard.writeText(t); toast("Copied"); } catch (_) { toast("Copy failed", "error"); }
}

/* Top bar: scheduler state, pause switch, the two big buttons. */
function shell() {
  return {
    stats: {},
    sched: { running: false, paused: false, jobs: [], busy: {} },
    paused: false,
    busyDiscover: false,
    busyScore: false,
    progress: null,
    theme: "system",
    THEMES: ["system", "light", "dark", "gruvbox-light", "gruvbox-dark"],
    setTheme(t) {
      if (!this.THEMES.includes(t)) t = "system";
      this.theme = t;
      try { localStorage.setItem("theme", t); } catch (_) {}
      if (t === "system") delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = t;
    },
    async init() {
      try { const t = localStorage.getItem("theme"); if (this.THEMES.includes(t)) this.theme = t; } catch (_) {}
      await this.refresh();
      setInterval(() => this.refresh(), 20000);
      bus.on((ev) => this.onEvent(ev));
    },
    async refresh() {
      try {
        const [stats, sched, state] = await Promise.all([
          api.get("/api/stats", { quiet: true }), api.get("/api/scheduler/status", { quiet: true }),
          api.get("/api/system/state", { quiet: true }),
        ]);
        this.stats = stats; this.sched = sched; this.paused = state.paused;
        this.busyDiscover = !!(sched.busy || {}).discover;
        this.busyScore = !!(sched.busy || {}).score;
      } catch (_) {}
    },
    onEvent(ev) {
      const t = ev.type, d = ev.data || {};
      if (t === "discovery_started") { this.busyDiscover = true; this.progress = "Finding jobs…"; }
      if (t === "discovery_progress") this.progress = `Finding jobs… ${d.source}: ${d.new} new`;
      if (t === "discovery_complete") { this.busyDiscover = false; this.progress = null; toast(`Found ${d.new} new jobs (${d.prefiltered} filtered out)`); this.refresh(); }
      if (t === "discovery_error") { this.busyDiscover = false; this.progress = null; toast("Finding jobs failed: " + d.error, "error"); }
      if (t === "scoring_started") { this.busyScore = true; this.progress = `Scoring 0 / ${d.total}`; }
      if (t === "scoring_progress") this.progress = `Scoring ${d.scored} / ${d.total} (${d.matched} matches)`;
      if (t === "scoring_complete") { this.busyScore = false; this.progress = null; if (d.total) toast(`Scored ${d.scored} jobs, ${d.matched} matches`); this.refresh(); }
      if (t === "scoring_paused") { this.busyScore = false; this.progress = null; }
      if (t === "scoring_error") { this.busyScore = false; this.progress = null; toast("Scoring failed: " + d.error, "error"); }
      if (t === "system_state") this.paused = d.paused;
      if (["job_matched", "job_status_changed", "job_applied", "job_discovered"].includes(t)) this.refreshSoon();
    },
    _rt: null,
    refreshSoon() { clearTimeout(this._rt); this._rt = setTimeout(() => this.refresh(), 800); },
    async findJobs() {
      const r = await api.post("/api/discover");
      if (r.status === "already_running") toast("Already finding jobs");
      else { this.busyDiscover = true; this.progress = "Finding jobs…"; }
    },
    async scoreJobs() {
      const r = await api.post("/api/score");
      if (r.status === "already_running") toast("Already scoring");
      else if (!r.count) toast("Nothing to score");
      else { this.busyScore = true; this.progress = `Scoring 0 / ${r.count}`; }
    },
    async togglePause() {
      const s = await api.post(this.paused ? "/api/system/resume" : "/api/system/pause");
      this.paused = s.paused;
      toast(this.paused ? "Paused. Nothing will use Claude until you resume." : "Resumed");
    },
    get unscored() { return this.stats.discovered || 0; },
  };
}
