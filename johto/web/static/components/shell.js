// The app shell (spec.md 2.1): the left rail (Now, Find work, In flight, Submitted, one line per machine) and,
// under 1100 px, the one-line top bar. Built by the lead; owned by the home builder after the foundation.
import { html } from "../lib/html.js";
import { app, route, navigate, queue, lane, toggleDrawer, drawer, palette } from "../lib/store.js";
import { laneWords, taskStage, splitLabel, actionWord } from "../lib/words.js";
import { Dot, narrow } from "./primitives.js";

const link = (path) => (e) => {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
  e.preventDefault(); navigate(path);
};

/** Queue items that wait on Austin with a clock: the lane needs him, or a blocker. */
export function waitingCount(q) {
  return (q || []).filter((i) => i.lane || (i.attention && i.lane_need) || i.tier === 0).length;
}

/** Tasks sorted into the rail's groups. */
export function railTasks(tasks, q, l) {
  const flight = [], submitted = [], accepted = [];
  for (const t of tasks || []) {
    const st = taskStage(t, { queue: q, lane: l });
    if (st.group === "flight") flight.push({ t, st });
    else if (st.group === "submitted") (t.stage === "accepted" ? accepted : submitted).push({ t, st });
  }
  const rank = { wait: 0, stop: 1, work: 2, busy: 3, ink: 4, idle: 5 };
  flight.sort((a, b) => (rank[a.st.tone] ?? 9) - (rank[b.st.tone] ?? 9) || String(b.t.updated_at).localeCompare(String(a.t.updated_at)));
  submitted.sort((a, b) => String(b.t.updated_at).localeCompare(String(a.t.updated_at)));
  return { flight, submitted, accepted };
}

/** Repo name and problem for a rail row; a task with no problem label shows its slot's action word. */
function names(t) {
  const { repo, problem } = splitLabel(t.label);
  const name = repo || (t.repo?.full_name || "").split("/").pop() || "Task";
  return { name, problem: problem || actionWord(t.slot?.action) || "" };
}

/** The PC's line: a live run there, else free. */
function pcLine(tasks) {
  const live = (tasks || []).find((t) => t.monitor?.host === "PC" && t.monitor?.live);
  if (!live) return { text: "free", tone: "idle", short: null };
  const m = live.monitor;
  if (m.state === "working") return { text: "Claude working", tone: "work", short: live.short };
  if (m.state === "waiting") return { text: m.question ? "Claude waiting" : "Claude ended its turn", tone: m.question ? "wait" : "idle", short: live.short };
  return { text: m.state || "running", tone: "busy", short: live.short };
}

function MachineLines({ compact }) {
  const l = lane.value;
  const mac = laneWords(l);
  const pc = pcLine(app.value?.tasks);
  const cls = (tone) => `machine ${tone === "wait" ? "wait" : tone === "stop" ? "stop" : ""}`;
  return html`
    <button type="button" class=${cls(mac.tone)} onClick=${toggleDrawer} title="The Mac lane (⌃\`)" aria-pressed=${drawer.value.open}>
      <${Dot} tone=${mac.tone} /><span class="mt"><b>Mac</b>${compact ? "" : html` · ${mac.text}`}</span>
    </button>
    <button type="button" class=${cls(pc.tone)} onClick=${() => pc.short && navigate(`/task/${pc.short}`)}
      title=${pc.short ? "The task running on the PC" : "No lane is running on the PC"}>
      <${Dot} tone=${pc.tone} /><span class="mt"><b>PC</b>${compact ? "" : html` · ${pc.text}`}</span>
    </button>`;
}

/** The left rail. */
export function RailNav() {
  const r = route.value;
  const q = queue.value;
  const tasks = app.value?.tasks || [];
  const { flight, submitted, accepted } = railTasks(tasks, q, lane.value);
  const waiting = waitingCount(q);
  const onTask = (short) => r === `/task/${short}`;
  const Task = ({ t, st, quiet }) => {
    const n = names(t);
    return html`<a class=${`task ${onTask(t.short) ? "on" : ""} ${quiet ? "quiet" : ""}`} href=${`/task/${t.short}`} onClick=${link(`/task/${t.short}`)}
        aria-current=${onTask(t.short) ? "page" : undefined}>
      <${Dot} tone=${st.tone} /><span class="nv-name">${n.name}</span><span class=${`nv-word ${st.tone === "wait" ? "wait" : st.tone === "stop" ? "stop" : ""}`}>${st.word}</span>
      <span class="nv-prob" title=${n.problem}>${n.problem}</span>
    </a>`;
  };
  return html`<nav class="nav-rail" aria-label="Johto">
    <div class="nv-top">
      <a class="brand" href="/" onClick=${link("/")}>Johto</a>
      <button type="button" class="nv-find" title="Go anywhere or do anything" onClick=${() => (palette.value = true)}>⌘K</button>
    </div>
    <a class=${`navlink ${r === "/" ? "on" : ""}`} href="/" onClick=${link("/")}>Now${waiting ? html`<span class="count" title="Waiting on you">${waiting}</span>` : null}</a>
    <a class=${`navlink ${r.startsWith("/repos") ? "on" : ""}`} href="/repos" onClick=${link("/repos")}>Find work</a>
    <a class=${`navlink nv-sub ${r.startsWith("/research") ? "on" : ""}`} href="/research" onClick=${link("/research")}>Research</a>
    ${flight.length ? html`<div class="grp">In flight</div>${flight.map(({ t, st }) => html`<${Task} key=${t.short} t=${t} st=${st} />`)}` : null}
    <a class=${`navlink nv-sub ${r.startsWith("/finished") ? "on" : ""}`} href="/finished" onClick=${link("/finished")}>Submitted</a>
    ${submitted.length || accepted.length ? html`
      ${submitted.filter(({ t }) => t.stage !== "in_review").map(({ t, st }) => html`<${Task} key=${t.short} t=${t} st=${st} quiet=${true} />`)}
      ${(() => { const n = submitted.filter(({ t }) => t.stage === "in_review").length;   // nothing to do: one quiet line
        return n ? html`<a class="task quiet" href="/finished" onClick=${link("/finished")}>
          <${Dot} tone="idle" /><span class="nv-name">${n} in review</span><span class="nv-word"></span></a>` : null; })()}
      ${accepted.length ? html`<a class="task quiet" href="/finished" onClick=${link("/finished")}>
        <${Dot} tone="ok" /><span class="nv-name">${accepted.length} accepted</span><span class="nv-word"></span></a>` : null}` : null}
    <div class="spacer"></div>
    <div class="machines"><${MachineLines} /></div>
    ${app.value?.server?.mode === "demo" ? html`<div class="demo" title="Example data. /?demo=0 shows your real tasks.">Demo data</div>` : null}
  </nav>`;
}

/** Task chips the top bar shows; the rest sit behind "+N", which opens the command list. */
const chipCount = () => (typeof window !== "undefined" && window.innerWidth < 1000 ? 2 : 3);

/** Under 1100 px: one line with the same places. */
export function TopNav() {
  const r = route.value;
  const q = queue.value;
  const { flight: all } = railTasks(app.value?.tasks || [], q, lane.value);
  const CHIPS = chipCount();
  // The open task always keeps its chip.
  const at = all.findIndex(({ t }) => r === `/task/${t.short}`);
  const flight = at >= CHIPS ? [...all.slice(0, CHIPS - 1), all[at], ...all.slice(CHIPS - 1).filter((_, n) => n + CHIPS - 1 !== at)] : all;
  const waiting = waitingCount(q);
  const seen = {};
  for (const { t } of flight) seen[names(t).name] = (seen[names(t).name] || 0) + 1;
  const chip = (t) => {
    const n = names(t);
    return seen[n.name] > 1 && n.problem ? `${n.name} · ${n.problem.split(/\s+/).slice(0, 2).join(" ")}` : n.name;
  };
  return html`<nav class="topnav" aria-label="Johto">
    <a class="brand" href="/" onClick=${link("/")}>Johto</a>
    <a class=${`navlink ${r === "/" ? "on" : ""}`} href="/" onClick=${link("/")}>Now${waiting ? html`<span class="count">${waiting}</span>` : null}</a>
    <a class=${`navlink ${r.startsWith("/repos") ? "on" : ""}`} href="/repos" onClick=${link("/repos")}>Find work</a>
    <a class=${`navlink ${r.startsWith("/research") ? "on" : ""}`} href="/research" onClick=${link("/research")}>Research</a>
    <a class=${`navlink ${r.startsWith("/finished") ? "on" : ""}`} href="/finished" onClick=${link("/finished")}>Submitted</a>
    <div class="nv-chips">${flight.slice(0, CHIPS).map(({ t, st }) => html`<a class=${`tchip ${r === `/task/${t.short}` ? "on" : ""}`} href=${`/task/${t.short}`} onClick=${link(`/task/${t.short}`)} title=${`${names(t).name} · ${names(t).problem}: ${st.word}`}>
      <${Dot} tone=${st.tone} /><span class="tchip-t">${chip(t)}</span></a>`)}
      ${flight.length > CHIPS ? html`<button type="button" class="tchip more" title=${flight.slice(CHIPS).map(({ t }) => names(t).name).join(", ")}
        onClick=${() => (palette.value = true)}>+${flight.length - CHIPS}</button>` : null}</div>
    <${MachineLines} compact=${false} />
    <button type="button" class="nv-find" title="Go anywhere or do anything" onClick=${() => (palette.value = true)}>⌘K</button>
  </nav>`;
}

/** The frame: rail (or top bar) + main. `children` is the screen; overlays mount inside main. */
export function Shell({ children }) {
  return html`<div class="shell">
    ${narrow.value ? html`<${TopNav} />` : html`<${RailNav} />`}
    <main class="main">${children}</main>
  </div>`;
}
