// App state in signals. The server is the source of truth; this holds what the UI shows.
import { signal, computed } from "@preact/signals";
import { api } from "./api.js";

export const app = signal(null);                 // GET /api/state body
export const route = signal(location.pathname);
export const hash = signal(new URLSearchParams(location.hash.slice(1)));
export const drawer = signal({ open: false, full: false, from: null });
export const palette = signal(false);
export const overlay = signal(false);
export const toasts = signal([]);
export const leads = signal(null);
export const tasks = signal(new Map());          // short -> TaskDetail
export const jobs = signal(new Map());           // job id -> Job
export const fieldFocused = signal(false);       // freezes rail order while typing
export const tick = signal(Date.now());          // 1 s clock for timers
export const nextChangedAt = signal(0);
export const connected = signal(false);          // the SSE stream is open

function readSel() { try { return sessionStorage.getItem("johto.sel"); } catch { return null; } }
export const selectedId = signal(readSel());
export function select(id) {
  selectedId.value = id;
  try { sessionStorage.setItem("johto.sel", id); } catch {}
}

export const queue = computed(() => app.value?.queue || []);
export const running = computed(() => app.value?.running || []);
export const lane = computed(() => app.value?.lane || { state: "free" });
export const next = computed(() => queue.value[0] || null);

// Item ids carry a hash of the task's state, so they change as the task moves. The selection follows
// the task: the same id, else the same step of the same task, else the task's new step, else Next.
const kindShort = (id) => (id || "").split(":").slice(0, 2).join(":");
const shortOf = (id) => (id || "").split(":")[1] || null;
// While a task's machine step runs right after his press (saving, AutoQC, submitting, collecting), the
// surface stays on that task and shows the progress, instead of jumping to another task's step: a second
// ⌘⇧↵ must never land on a different task.
const HOLD_KINDS = new Set(["saving", "qc_running", "submitting", "collecting"]);
let shown = null; // the last queue item the surface showed
export function rememberShown(item) { if (item && !item.held) shown = item; }
export const selected = computed(() => {
  const q = queue.value;
  const id = selectedId.value;
  if (!id) return q[0] || null;
  const sh = shortOf(id);
  const hit = q.find((i) => i.id === id)
    || q.find((i) => kindShort(i.id) === kindShort(id))
    || (sh && sh !== "-" ? q.find((i) => i.short === sh) : null);
  if (hit) return hit;
  const run = sh && running.value.find((i) => i.short === sh && HOLD_KINDS.has(i.kind));
  if (run && shown?.short === sh) return { ...shown, held: run };
  return q[0] || null;
});

// ⌘⇧↵ is ignored for a moment after the surface changes under it.
export let surfaceChangedAt = 0;
export function markSurfaceChange() { surfaceChangedAt = Date.now(); }

let lastNext = null;
export function setState(s) {
  const n = s?.queue?.[0]?.id;
  if (n !== lastNext) { nextChangedAt.value = Date.now(); lastNext = n; }
  const prev = app.peek();
  const prevSel = prev ? selected.peek() : null;
  app.value = s;
  if (prev) noticeArrivals(prev, s, prevSel);
  paintTitle();
}

export async function refresh() {
  const s = await api.get("/api/state");
  setState(s);
  if (Array.isArray(s?.jobs)) {
    const m = new Map(jobs.value);
    for (const j of s.jobs) m.set(j.id, j);
    jobs.value = m;
  }
}

// ---------- Tasks ----------

const inflight = new Map();
export async function loadTask(short, force = false) {
  if (!short) return null;
  if (!force && tasks.value.has(short)) return tasks.value.get(short);
  if (inflight.has(short)) return inflight.get(short);
  const p = api.get(`/api/tasks/${short}`).then((t) => {
    const m = new Map(tasks.value); m.set(short, t); tasks.value = m;
    return t;
  }).finally(() => inflight.delete(short));
  inflight.set(short, p);
  return p;
}

// A task event refetches that task once things settle (several events often arrive together).
const refetchTimers = new Map();
function refetchTask(short) {
  if (!tasks.value.has(short)) return;
  clearTimeout(refetchTimers.get(short));
  refetchTimers.set(short, setTimeout(() => {
    refetchTimers.delete(short);
    if (tasks.value.has(short)) loadTask(short, true).catch(() => {});   // gone meanwhile (task.removed): no 404
  }, 150));
}

export function patchTask(short, fn) {
  const t = tasks.value.get(short);
  if (!t) return;
  const copy = structuredClone(t);
  fn(copy);
  const m = new Map(tasks.value); m.set(short, copy); tasks.value = m;
}

export async function loadLeads(force = false) {
  if (leads.value && !force) return leads.value;
  leads.value = await api.get("/api/leads");
  return leads.value;
}

// ---------- Jobs ----------

const LIVE = new Set(["queued", "running"]);
export const isLive = (j) => !!j && LIVE.has(j.state);

/** The newest job matching kind(s) and target ("t:<short>" or the short itself). */
export function jobFor(kinds, short) {
  const ks = Array.isArray(kinds) ? kinds : [kinds];
  let best = null;
  for (const j of jobs.value.values()) {
    if (!ks.includes(j.kind)) continue;
    if (short && j.target !== `t:${short}` && j.target !== short && j.short !== short) continue;
    if (!best || (j.created_at || "") > (best.created_at || "")) best = j;
  }
  return best;
}

// Job step names in plain words (the server's names are for code).
const STEP_WORDS = {
  recheck: "Checking Studio", gh_account: "Checking the gh account", gists: "Uploading gists",
  saving_fields: "Saving fields", starting_qc: "Starting AutoQC", polling: "Waiting for AutoQC", starting: "Starting",
  submitting: "Submitting", tearing_down: "Removing lanes", unclaiming: "Unclaiming", claiming: "Claiming",
  repo: "Reading the repo", saving_repo: "Saving the repo", making_folder: "Making the task folder",
  marking_lead: "Updating the lead", waiting: "Waiting", opening_form: "Opening the form", collecting: "Collecting",
  packing: "Building the review pack", setup: "Setting up", get: "Reading Studio",
};
export function stepWord(step) {
  if (!step) return "";
  const [head, ...rest] = String(step).split(" ");
  const w = STEP_WORDS[head];
  if (!w) return step;
  return rest.length ? `${w} (${rest.join(" ").replace(/^(calib|validate)-(\d+)$/, (_, k, n) => `${k === "calib" ? "calibration" : "validation"} ${n}`)})` : w;
}

/** One line for a job in progress: its current step in words, else its last log line. */
export function jobLine(j) {
  if (!j) return "";
  const p = j.progress || {};
  const i = p.step ? (p.steps || []).indexOf(p.step) : -1;
  const n = (p.steps || []).length;
  const step = p.step ? `${stepWord(p.step)}${i >= 0 && n > 1 ? ` (${i + 1} of ${n})` : ""}` : "";
  return step || (p.log_tail || []).slice(-1)[0] || (j.state === "queued" ? "waiting its turn" : "starting");
}

const runWordOf = (run) => {
  const m = /^(calib|validate)-(\d+)$/.exec(run || "");
  return m ? `${m[1] === "calib" ? "calibration" : "validation"} ${m[2]}` : (run || "");
};
/** A run's collect or review pack, for the lane strip and drawer: "Review pack · validation 1", no log lines. */
export function afterRunLine(j) {
  if (!j) return "";
  const packing = ["pack", "review_pack"].includes(j.kind) || j.phase === "packing" || j.progress?.step === "packing";
  const run = j.run || j.params?.run || "";
  return `${packing ? "Review pack" : "Collecting"}${run ? ` · ${runWordOf(run)}` : ""}`;
}

const waiters = new Map(); // job id -> [resolve]
/** Resolves with the job once it leaves queued/running (from SSE, with a slow poll as a fallback). */
export function waitJob(job) {
  if (!job?.id) return Promise.resolve(job);
  if (!isLive(job)) return Promise.resolve(job);
  return new Promise((resolve) => {
    const list = waiters.get(job.id) || [];
    list.push(resolve);
    waiters.set(job.id, list);
    const poll = async () => {
      if (!waiters.has(job.id)) return;
      try {
        const r = await api.get(`/api/jobs?limit=50`);
        for (const j of r?.jobs || []) putJob(j);
      } catch {}
      if (waiters.has(job.id)) setTimeout(poll, 5000);
    };
    setTimeout(poll, 5000);
  });
}

export function putJob(j) {
  if (!j?.id) return;
  // A job never goes back from finished to running: a 202's "running" copy that arrives after the SSE
  // "succeeded" event must not undo it.
  const cur = jobs.value.get(j.id);
  if (cur && !isLive(cur) && isLive(j)) return;
  const m = new Map(jobs.value); m.set(j.id, j); jobs.value = m;
  if (!isLive(j) && waiters.has(j.id)) {
    const list = waiters.get(j.id); waiters.delete(j.id);
    list.forEach((r) => r(j));
  }
}

// ---------- Toasts ----------

let toastN = 0;
const toastTimers = new Map();
/** Show a toast. Server toasts carry an id (dedupe), `undo` {action_id, expires_at, label} and `link` {label, href}. */
export function toast(text, opts = {}) {
  const id = opts.id ?? `c${++toastN}`;
  const undo = opts.undo && (opts.undo.action_id || opts.onUndo) ? opts.undo : null;
  const t = { id, text, level: opts.level || "info", undo, link: opts.link || null, onUndo: opts.onUndo || null, action: opts.action || null };
  toasts.value = [...toasts.value.filter((x) => x.id !== id), t].slice(-3);
  clearTimeout(toastTimers.get(id));
  const until = undo?.expires_at ? Date.parse(undo.expires_at) - Date.now() : null;
  const ms = opts.sticky ? null : Math.max(opts.ms || 6000, until || 0);
  if (ms) toastTimers.set(id, setTimeout(() => dismissToast(id), ms));
  return id;
}
export function dismissToast(id) {
  clearTimeout(toastTimers.get(id)); toastTimers.delete(id);
  toasts.value = toasts.value.filter((x) => x.id !== id);
}

/** Undo what a toast offers: its own handler, else the server's undo window. */
export async function undoToast(t) {
  if (!t?.undo) return false;
  dismissToast(t.id);
  if (t.onUndo) { await t.onUndo(); return true; }
  if (!t.undo.action_id) return false;
  try {
    const r = await api.post(`/api/undo/${t.undo.action_id}`, {});
    if (!r?.job) toast("Undone", { level: "ok" });   // an undo that runs a job (unclaim) says when it is done
  } catch (e) { toast(e.status === 410 ? "Too late to undo" : e.message, { level: "error" }); }
  return true;
}

/** ⌘Z: undo the newest toast that offers it (claims, drops and other undo windows). */
export function undoLast() {
  const t = [...toasts.value].reverse().find((x) => x.undo);
  return t ? undoToast(t) : Promise.resolve(false);
}

/** Follow a toast or notification link: app paths in place, anything else in a new tab. */
export function follow(href) {
  if (!href) return;
  const u = new URL(href, location.origin);
  if (u.origin !== location.origin) { window.open(href, "_blank", "noopener"); return; }
  const h = new URLSearchParams(u.hash.slice(1));
  navigate(u.pathname, u.hash);
  if (h.get("item")) select(h.get("item"));
  if (h.get("drawer") === "open") openDrawer("row");
}

// ---------- Navigation and drawer ----------

export function navigate(path, h = "") {
  if (location.pathname + location.hash !== path + h) history.pushState({}, "", path + h);
  route.value = path;
  hash.value = new URLSearchParams(h.replace(/^#/, ""));
}
window.addEventListener("popstate", () => {
  route.value = location.pathname;
  hash.value = new URLSearchParams(location.hash.slice(1));
});

export function openDrawer(from = "user") { drawer.value = { ...drawer.value, open: true, from }; presence(); }
export function closeDrawer() { drawer.value = { ...drawer.value, open: false, from: null }; presence(); }
export function toggleDrawer() { drawer.value.open ? closeDrawer() : openDrawer("user"); }

setInterval(() => { tick.value = Date.now(); }, 1000);

// ---------- A collected run's next step ----------
// When a run leaves the lane (collected, review pack built), that task's next step is a decision or
// its fields. Say so once; if Austin was looking at the lane for that task, take him there.

const RUN_KINDS = new Set(["calibrating", "validating", "collecting", "packing"]);
const STEP_KINDS = new Set(["decide", "rate", "write_details", "write_prompt", "save"]);
const laneShorts = (s) => new Set([
  ...(s?.running || []).filter((r) => RUN_KINDS.has(r.kind)).map((r) => r.short),
  ...(s?.queue || []).filter((r) => r.lane).map((r) => r.short),
  ...(s?.lane?.run?.short && !["free", "collected"].includes(s.lane.state) ? [s.lane.run.short] : []),
].filter(Boolean));

// One toast per collected run: the server's "Collected …" line and the next step are merged into it,
// whichever arrives first.
const arrivals = new Map(); // short -> {id, label, next, at}
const collectedToast = (t) => /^Collected\b/.test(t?.text || "");

function arrivalText(a, base) {
  const head = (base || `${a.label} · run collected`).replace(/[.\s]+$/, "");
  // Leave out "Next" when that step is already the one on screen; the server's line may name it already.
  if (a.shown || /\bNext:/.test(head)) return head;
  return `${head}. Next: ${a.next}`;
}
const ENDING_TOAST = "lane-ending";   // "Ending validation 1…", replaced once the run is collected

function noticeArrivals(prev, s, was) {
  const before = laneShorts(prev);
  if (!before.size) return;
  const now = laneShorts(s);
  for (const short of before) {
    if (now.has(short)) continue;
    const step = (s.queue || []).find((i) => i.short === short && STEP_KINDS.has(i.kind));
    if (!step) continue;
    const isNext = s.queue[0]?.id === step.id;
    const watching = drawer.peek().open || (was && was.short === short && (was.lane || was.surface === "lane"));
    const { a, base, again } = arrival(short, step, isNext);
    if (watching && !fieldFocused.peek() && !readingElsewhere()) {
      select(step.id);
      if (route.peek() !== "/") navigate("/");
      if (drawer.peek().open) closeDrawer();
    }
    if (!again) showArrival(a, base);
  }
}

/**
 * The arrival record for a run's next step. The tab may hear of one arrival twice (its queue, and the lane
 * service's navigate frame): the second keeps the first one's toast and text instead of replacing them.
 */
function arrival(short, step, isNext) {
  const prev = arrivals.get(short);
  const showing = prev && toasts.peek().some((t) => t.id === prev.id);
  if (prev && prev.item === step.id && showing && Date.now() - prev.at < 15000) return { a: prev, base: prev.base, again: true };
  const server = toasts.peek().find((t) => collectedToast(t) && t.text.includes(step.label));
  if (server) dismissToast(server.id);
  const a = { id: `arrive:${short}`, label: step.label, next: step.title, at: Date.now(), isNext, item: step.id,
              base: server?.text || (prev?.item === step.id ? prev.base : undefined) };
  arrivals.set(short, a);
  return { a, base: a.base, again: false };
}

// He scrolled or clicked on a page other than Home in the last 10 s: an arrival shows a toast with Go
// instead of navigating.
let lastHandsAt = 0;
for (const ev of ["wheel", "pointerdown", "keydown"]) {
  window.addEventListener(ev, (e) => { if (!e.target?.closest?.(".drawer")) lastHandsAt = Date.now(); }, { capture: true, passive: true });
}
function readingElsewhere() { return route.peek() !== "/" && Date.now() - lastHandsAt < 10000; }

/**
 * The server asks this tab to open a task (a run was collected). The page to fill in is that task's next
 * step on Home, so select it there once the queue shows it; without one, open the href. While he types in
 * a field nothing moves: the toast carries the link instead.
 */
export function serverNavigate(href, reason) {
  const m = /^\/task\/([^/#?]+)/.exec(href || "");
  const short = m?.[1];
  const t0 = Date.now();
  const tryIt = () => {
    const step = short ? (app.peek()?.queue || []).find((i) => i.short === short && STEP_KINDS.has(i.kind)) : null;
    if (!step && Date.now() - t0 < 4000) { setTimeout(tryIt, 250); return; }
    if (!step) {
      if (fieldFocused.peek()) toast(`${short || "A task"} is ready for you`, { link: { label: "Open", href } });
      else follow(href);
      return;
    }
    const { a, base, again } = arrival(short, step, app.peek()?.queue?.[0]?.id === step.id);
    // Nothing moves under his hands: not while he types, and not while he reads another page.
    if (!fieldFocused.peek() && !readingElsewhere()) {
      select(step.id);
      if (route.peek() !== "/") navigate("/");
      if (drawer.peek().open && (reason === "collected" || drawer.peek().from !== "user")) closeDrawer();
    }
    if (!again) showArrival(a, base);
  };
  tryIt();
}

function showArrival(a, base) {
  dismissToast(ENDING_TOAST);
  a.shown = route.peek() === "/" && selected.peek()?.id === a.item;
  toast(arrivalText(a, base), {
    id: a.id, level: "ok", ms: 12000,
    link: a.shown ? null : { label: a.isNext ? "⌘↵ Go" : "Go", href: `/#item=${encodeURIComponent(a.item)}` },
  });
}
export { ENDING_TOAST };

/** A server toast; a "Collected …" line for a run whose next step already showed is merged into it. */
function serverToast(t) {
  if (collectedToast(t)) dismissToast(ENDING_TOAST);
  // "Collected …" and "Review pack ready …" for a run whose next step already showed update that toast.
  if (collectedToast(t) || /^Review pack ready\b/.test(t.text || "")) {
    for (const a of arrivals.values()) {
      const showing = toasts.peek().some((x) => x.id === a.id);
      if (t.text.includes(a.label) && (Date.now() - a.at < 15000 || showing)) { a.base = t.text; showArrival(a, t.text); return; }
    }
  }
  toast(t.text, t);
}

// ---------- Title, favicon, notifications, presence ----------

const baseIcon = document.querySelector("link[rel=icon]")?.href;
let lastCount = -1;
function paintTitle() {
  const n = (app.value?.queue || []).filter((i) => i.tier <= 1).length;
  if (n === lastCount) return;
  lastCount = n;
  document.title = n ? `(${n}) Johto` : "Johto";
  const link = document.querySelector("link[rel=icon]");
  if (!link || !baseIcon) return;
  if (!n) { link.href = baseIcon; return; }
  const svg = decodeURIComponent(baseIcon.replace(/^data:image\/svg\+xml,/, ""))
    .replace("</svg>", "<circle cx='13' cy='3' r='3' fill='#D97706'/></svg>");
  link.href = "data:image/svg+xml," + encodeURIComponent(svg);
}

let askedPermission = false;
window.addEventListener("pointerdown", () => {
  if (askedPermission || !("Notification" in window) || Notification.permission !== "default") return;
  askedPermission = true;
  try { Notification.requestPermission(); } catch {}
}, { capture: true });

function notify(n) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  if (document.visibilityState === "visible" && document.hasFocus()) return;
  try {
    const w = new Notification(n.title || "Johto", { body: n.body || "", tag: n.tag || n.id });
    w.onclick = () => { window.focus(); follow(n.href); w.close(); };
  } catch {}
}

let presenceTimer = null;
export function presence() {
  clearTimeout(presenceTimer);
  presenceTimer = setTimeout(() => {
    const el = document.activeElement;
    const body = {
      visible: document.visibilityState === "visible", focused: document.hasFocus(),
      drawer_open: !!drawer.value.open, terminal_focused: !!el?.closest?.(".xterm"),
      surface_item: route.value === "/" ? selected.value?.id || null : null,
    };
    fetch("/api/presence", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) }).catch(() => {});
  }, 250);
}
document.addEventListener("visibilitychange", presence);
window.addEventListener("focus", presence);
window.addEventListener("blur", presence);
document.addEventListener("focusin", (e) => { if (e.target?.closest?.(".xterm")) presence(); });

// ---------- Events ----------

function merge(part) { if (app.value) setState({ ...app.value, ...part }); }

let es = null;
let retry = 0;
export function connectEvents() {
  es?.close();
  es = new EventSource("/api/events");
  const on = (name, fn) => es.addEventListener(name, (e) => {
    let d = {};
    try { d = e.data ? JSON.parse(e.data) : {}; } catch { return; }
    try { fn(d); } catch (err) { console.error(`event ${name}`, err); }
  });
  es.onopen = () => { connected.value = true; retry = 0; };
  es.onerror = () => {
    connected.value = false;
    // EventSource retries by itself (sending Last-Event-ID); if the browser gave up, start over.
    if (es.readyState === EventSource.CLOSED) {
      const wait = Math.min(15000, 1000 * 2 ** retry++);
      setTimeout(() => { refresh().catch(() => {}); connectEvents(); }, wait);
    }
  };
  on("state", (d) => {
    setState(d);
    if (Array.isArray(d.jobs)) for (const j of d.jobs) putJob(j);
    for (const short of tasks.value.keys()) refetchTask(short);
  });
  on("queue", (d) => merge({ queue: d.queue ?? app.value?.queue, running: d.running ?? app.value?.running }));
  on("lane", (d) => merge({ lane: d }));
  on("task", (d) => {
    const s = d.summary;
    if (!s?.short) return;
    const list = app.value?.tasks || [];
    const i = list.findIndex((t) => t.short === s.short);
    merge({ tasks: i >= 0 ? list.map((t, k) => (k === i ? s : t)) : [...list, s] });
    refetchTask(s.short);
  });
  on("task.removed", (d) => {
    clearTimeout(refetchTimers.get(d.short)); refetchTimers.delete(d.short);
    const m = new Map(tasks.value); m.delete(d.short); tasks.value = m;
    merge({ tasks: (app.value?.tasks || []).filter((t) => t.short !== d.short) });
  });
  on("job", (j) => {
    putJob(j);
    const short = (j.target || "").startsWith("t:") ? j.target.slice(2) : j.short;
    if (short && !isLive(j)) refetchTask(short);
  });
  on("toast", serverToast);
  on("notify", notify);
  on("studio", (d) => merge({ studio: { ...(app.value?.studio || {}), ...d } }));
  on("spend", (d) => merge({ spend: { ...(app.value?.spend || {}), ...d } }));
  on("settings", (d) => merge({ settings: d }));
  on("leads", () => { if (leads.value) loadLeads(true).catch(() => {}); });
  return es;
}
