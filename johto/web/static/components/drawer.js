import { useEffect, useRef, useState } from "preact/hooks";
import { signal } from "@preact/signals";
import { html } from "../lib/html.js";
import { app, drawer, lane, closeDrawer, tick, tasks, loadTask, patchTask, toast, jobs, jobFor, isLive, afterRunLine, serverNavigate, ENDING_TOAST } from "../lib/store.js";
import { isGlobalChord, saveFocus, restoreFocus } from "../lib/keys.js";
import { api } from "../lib/api.js";
import { act } from "../lib/actions.js";
import { clock, mins } from "../lib/format.js";
import { Chip, Kbd } from "./ui.js";
import { PackButton } from "./pack.js";
import { MonitorLine, stateWord, turnOf } from "./monitor.js";

let xtermMod = null;
async function loadXterm() {
  if (!xtermMod) {
    const [x, fit, links] = await Promise.all([
      import("/static/vendor/xterm.mjs"), import("/static/vendor/addon-fit.mjs"), import("/static/vendor/addon-web-links.mjs")]);
    xtermMod = { Terminal: x.Terminal, FitAddon: fit.FitAddon, WebLinksAddon: links.WebLinksAddon };
  }
  return xtermMod;
}

const TERMS = new Map(); // path -> {term, ws}
/** Put the caret in a mounted terminal (the lane's Claude by default). */
export function focusTerminal(path = "/ws/lane") {
  const t = TERMS.get(path)?.term;
  if (t) { t.focus(); return true; }
  return false;
}

// What the lane socket last said about the input lock; a dropped Enter flashes the chip.
export const ptyInfo = signal({ locked: false, swallowedAt: 0, run: null, exited: null });

/**
 * One xterm per path, kept mounted for the life of the page. Speaks contract 5.4: a hello text frame,
 * a binary replay, then live output; text frames for lock, swallowed Enter, exit, new run and idle.
 * Reconnects on its own when the socket drops (the PTY lives on in the server).
 */
export function Terminal({ path = "/ws/lane", active, idleText }) {
  const host = useRef(null);
  const st = useRef({});
  const [idle, setIdle] = useState(false);
  const [down, setDown] = useState(false);
  useEffect(() => {
    let disposed = false;
    let retry = 0;
    loadXterm().then(({ Terminal: T, FitAddon, WebLinksAddon }) => {
      if (disposed) return;
      const term = new T({
        fontFamily: 'ui-monospace, "SF Mono", Menlo, monospace', fontSize: 13, lineHeight: 1.25,
        cursorBlink: true, allowProposedApi: true, scrollback: 5000,
        theme: { background: "#16191D", foreground: "#D7DAE0", cursor: "#E5E7EB", selectionBackground: "#3B4250",
                 black: "#16191D", brightBlack: "#5C6370", blue: "#6CA8F0", brightBlue: "#8DBBF5" },
      });
      const fit = new FitAddon();
      term.loadAddon(fit); term.loadAddon(new WebLinksAddon((e, uri) => window.open(uri, "_blank", "noopener")));
      term.attachCustomKeyEventHandler((e) => !(e.type === "keydown" && isGlobalChord(e)));
      term.open(host.current);
      try { fit.fit(); } catch {}
      st.current = { term, fit };
      TERMS.set(path, st.current);
      const sendResize = () => {
        const ws = st.current.ws;
        if (ws?.readyState === 1) ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      };
      term.onResize(sendResize);
      term.onData((d) => { const ws = st.current.ws; if (ws?.readyState === 1) ws.send(new TextEncoder().encode(d)); });

      const connect = () => {
        if (disposed) return;
        const proto = location.protocol === "https:" ? "wss" : "ws";
        const ws = new WebSocket(`${proto}://${location.host}${path}?cols=${term.cols || 100}&rows=${term.rows || 32}`);
        ws.binaryType = "arraybuffer";
        st.current.ws = ws;
        let replayed = false;
        ws.onopen = () => {
          retry = 0; setDown(false);
          // The lane socket may move the page when a run is collected (lane service `navigate` frames).
          if (path === "/ws/lane") ws.send(JSON.stringify({ type: "hello", caps: ["navigate"] }));
        };
        ws.onmessage = (m) => {
          if (typeof m.data !== "string") {
            if (!replayed) { replayed = true; term.reset(); }
            term.write(new Uint8Array(m.data));
            return;
          }
          let msg = {};
          try { msg = JSON.parse(m.data); } catch { return; }
          if (msg.type === "hello") {
            setIdle(false); replayed = false;
            if (path === "/ws/lane") ptyInfo.value = { ...ptyInfo.value, locked: !!msg.locked, run: msg.run || null, exited: null };
            sendResize();
          } else if (msg.type === "idle") {
            setIdle(true);
          } else if (msg.type === "run") {
            term.reset(); setIdle(false); replayed = true;
            if (path === "/ws/lane") ptyInfo.value = { ...ptyInfo.value, run: msg.run || null, exited: null };
          } else if (msg.type === "locked") {
            if (path === "/ws/lane") ptyInfo.value = { ...ptyInfo.value, locked: !!msg.locked };
          } else if (msg.type === "swallowed") {
            if (path === "/ws/lane") ptyInfo.value = { ...ptyInfo.value, swallowedAt: Date.now() };
          } else if (msg.type === "navigate") {
            if (path === "/ws/lane") serverNavigate(msg.href, msg.reason);
          } else if (msg.type === "exit") {
            term.write(`\r\n\x1b[2m[the lane session ended]\x1b[0m\r\n`);
            if (path === "/ws/lane") ptyInfo.value = { ...ptyInfo.value, exited: msg.code ?? 0 };
          }
        };
        ws.onclose = (ev) => {
          if (disposed) return;
          if (ev.code === 4401) { setDown(true); return; } // signed out: the page link fixes it
          setDown(true);
          const wait = Math.min(10000, 500 * 2 ** retry++);
          st.current.timer = setTimeout(connect, wait);
        };
      };
      connect();
      const ro = new ResizeObserver(() => { try { fit.fit(); } catch {} });
      ro.observe(host.current);
      st.current.ro = ro;
    });
    return () => {
      disposed = true;
      clearTimeout(st.current.timer); st.current.ro?.disconnect(); st.current.ws?.close(); st.current.term?.dispose();
      if (TERMS.get(path) === st.current) TERMS.delete(path);
    };
  }, [path]);
  useEffect(() => { if (active) setTimeout(() => { try { st.current.fit?.fit(); st.current.term?.focus(); } catch {} }, 140); }, [active]);
  return html`<div class="term">
    <div ref=${host} style=${`height:100%;${idle ? "display:none" : ""}`}></div>
    ${idle ? html`<div class="term-idle">${idleText || (path === "/ws/shell" ? "No lane container is running." : "No lane is running.")}</div>` : null}
    ${down && !idle ? html`<div class="term-down">Reconnecting to the lane…</div>` : null}
  </div>`;
}

const STATE_CHIP = {
  waiting: ["amber", "Waiting for you"], permission: ["amber", "Asks permission"], needs_login: ["amber", "Log in"],
  ready_for_prompt: ["amber", "Paste your prompt"], working: ["blue", "Working"], preparing: ["neutral", "Setting up"],
  free: ["neutral", "Lane free"], setup_failed: ["red", "Setup failed"], exited: ["neutral", "Collecting"],
  collecting: ["neutral", "Collecting"], collected: ["neutral", "Collected"],
};
const kindWord = (k) => (k === "calib" ? "calibration" : "validation");
/** "validation run", "validation run 2": a run's words, never its id. */
const runLabel = (run) => {
  if (!run) return "";
  const n = (run.run || "").split("-")[1];
  return `${kindWord(run.kind)} run${n && n !== "1" ? ` ${n}` : ""}`;
};
const mmss = (s) => `${Math.floor((s || 0) / 60)}:${String((s || 0) % 60).padStart(2, "0")}`;

/** "Changed 3 files · Ran the tests: all passed". A result the pipe hid (last_ok null) says nothing, never failed. */
function footFacts(f) {
  const n = f.files_edited?.length || 0;
  const out = [n ? `Changed ${n} file${n === 1 ? "" : "s"}` : "No edits yet"];
  const t = f.tests || {};
  if (t.runs && t.last_ok === true) out.push("Ran the tests: all passed");
  else if (t.runs && t.last_ok === false) out.push("Ran the tests: failing");
  return out.join(" · ");
}

// The drawer's state action, run by ⌘⇧↵ while focus is in the drawer (the terminal included).
let drawerPrimary = null;
export function runDrawerPrimary() { if (drawerPrimary) { drawerPrimary(); return true; } return false; }

/** The next run the free lane can start. A calibration run only when settings.calibrate_first is on. */
const nextRun = (l) => (l.queue || []).find((q) => q.kind !== "calib" || app.value?.settings?.calibrate_first) || null;

/** Lane actions that type into the lane or start it. Each says plainly what it types. */
function laneActions(l, ptyLocked, collecting) {
  const run = l.run;
  const s = l.state;
  const out = { main: null, end: null };
  const typing = (what, okText) => async () => {
    const r = await act("/api/lane/type", { what }, { quiet: true });
    if (r == null) return;
    if (r.toast) toast(r.toast.text, r.toast); else if (okText) toast(okText);
    focusTerminal();
  };
  if (s === "needs_login") out.main = { label: "Type /login", run: typing("login", "Typed /login. Finish signing in in the browser tab it opens."), title: "Types /login and Enter in the lane" };
  // Once text sits in Claude's input box, a paste or /exit would land on top of it (the server refuses both).
  else if (s === "ready_for_prompt" && l.input?.empty !== false) out.main = { label: "Paste my prompt", run: typing("prompt", "Pasted your saved Opening Prompt. Press Enter in the lane to send it."), title: "Pastes your saved Opening Prompt without pressing Enter" };
  else if (s === "free" && nextRun(l)) {
    const q = nextRun(l);
    out.main = { label: `Start the ${kindWord(q.kind)} run`, run: () => act("/api/lane/start", { short: q.short, kind: q.kind }, { what: `Setting up the ${kindWord(q.kind)} run` }),
      title: `Sets up a new lane for ${q.label}` };
  } else if (s === "setup_failed" && run) {
    out.main = { label: "Retry in a new folder", run: () => act("/api/lane/start", { short: run.short, kind: run.kind }, { what: "Setting up again" }),
      title: "Sets up a fresh lane folder for this run" };
  }
  // Only once a prompt was sent (Claude waits for a reply): before that there is no run to end.
  if (run && s === "waiting" && l.input?.empty !== false) {
    out.end = { label: "End run and collect", run: async () => {
      const r = await act("/api/lane/end", {}, { quiet: true });
      if (r == null) return;
      if (r.toast) toast(r.toast.text, { ...r.toast, id: ENDING_TOAST });
      else toast(`Ending ${runLabel(run)}. It is collected and its review pack built next.`, { id: ENDING_TOAST });
    }, title: "Types /exit in the lane. The run is then collected and its review pack built." };
  }
  if (run?.mode === "async" && s === "waiting" && !out.main) out.main = out.end;
  // Claude has exited and the run is not collected yet: the watcher does it within about 20 s; this does it now.
  if (run && s === "exited" && !collecting) {
    out.main = { label: "Collect now", run: () => act("/api/lane/collect", {}, { what: "Collecting the run" }),
      title: "Copies the diff and transcript out of the lane, then builds the review pack" };
  }
  return out;
}

function SetupLog({ l }) {
  const j = l.run ? jobFor("lane_prepare", l.run.short) : null;
  const lines = (j?.error?.details?.errors || j?.progress?.log_tail || []).filter((x) => /ERROR|error|failed/i.test(x));
  const tail = lines.length ? lines : (j?.progress?.log_tail || []).slice(-6);
  return html`<div class="term-msg">
    <div class="tm-h">The lane couldn't be set up. A fresh folder usually fixes it.</div>
    ${tail.length || l.evidence || j?.error?.message ? html`<details class="nv-details"><summary>Details</summary>
      ${j?.error?.message ? html`<p>${j.error.message}</p>` : null}
      ${tail.length ? html`<pre>${tail.join("\n")}</pre>` : null}
      ${l.evidence ? html`<pre>${l.evidence}</pre>` : null}
    </details>` : null}
  </div>`;
}

export function LaneDrawer() {
  tick.value;
  jobs.value;
  const d = drawer.value;
  const l = lane.value;
  const run = l.run;
  const pty = ptyInfo.value;
  const [tab, setTab] = useState("claude");
  const [shellOn, setShellOn] = useState(false);
  const [note, setNote] = useState("");
  const [pending, setPending] = useState([]); // notes sent but not yet in the task
  const was = useRef(false);
  const prevState = useRef(l.state);
  const closeTimer = useRef(null);

  useEffect(() => {
    if (d.open && !was.current) saveFocus();
    if (!d.open && was.current) restoreFocus();
    was.current = d.open;
  }, [d.open]);
  useEffect(() => { if (run?.short) loadTask(run.short).catch(() => {}); setPending([]); }, [run?.short, run?.run]);

  // Opened from a lane row or a notification: close 2 s after Claude is back at work.
  useEffect(() => {
    const before = prevState.current;
    prevState.current = l.state;
    clearTimeout(closeTimer.current);
    const handedBack = ["waiting", "permission", "ready_for_prompt", "needs_login"].includes(before) && l.state === "working";
    if (d.open && d.from && d.from !== "user" && handedBack) {
      closeTimer.current = setTimeout(() => {
        if (drawer.value.open && drawer.value.from !== "user" && lane.value.state === "working") closeDrawer();
      }, 2000);
    }
    return () => clearTimeout(closeTimer.current);
  }, [l.state]);

  let [tone, label] = l.state === "ready_for_prompt" && l.input?.empty === false ? ["amber", "Press Enter to send"]
    : STATE_CHIP[l.state] || ["neutral", l.state];
  // The run monitor (runmon) for this run: measured minutes, background work, the wait, the review.
  const mon = l.monitor && run && l.monitor.run === run.run ? l.monitor : null;
  if (l.state === "waiting" && mon?.turn_class) {
    // An ended turn alone isn't a question: say what the turn was.
    const c = turnOf(mon);
    label = stateWord(mon).replace(/^Claude /, "").replace(/^./, (x) => x.toUpperCase());
    tone = c === "question" || c === "permission" ? "amber" : c === "problem" ? "red" : "neutral";
  }
  const f = l.facts;
  const budget = f?.task_claude_min ?? 0;
  const budgetTone = budget >= 90 ? "red" : budget >= 60 ? "amber" : "";
  const t = run?.short ? tasks.value.get(run.short) : null;
  const saved = (t?.runs || []).find((x) => x.name === run?.run)?.notes || [];
  const notes = [...saved, ...pending.filter((p) => !saved.some((s) => s.id === p.id || s.text === p.text))];
  const collect = run ? jobFor(["lane_collect", "collect"], run.short) : null;
  const packing = run ? jobFor(["pack", "review_pack"], run.short) : null;
  const busy = [isLive(packing) ? packing : null, isLive(collect) ? collect : null].filter(Boolean)[0];
  // The lane service collects a run as soon as Claude exits, so for the first 30 s "exited" means a collect
  // is about to start: no "Collect now" then, only after that (the automatic collect didn't happen).
  const autoCollecting = l.state === "collecting" || (l.state === "exited" && Date.now() - Date.parse(l.since || 0) < 30000);
  const acts = laneActions(l, pty.locked, !!busy || autoCollecting);
  drawerPrimary = acts.main?.run || null;
  const locked = l.input_locked || pty.locked;
  const flash = Date.now() - pty.swallowedAt < 1500;
  const queued = l.state === "free" ? nextRun(l) : null;

  async function addNote() {
    const text = note.trim();
    if (!text || !run) return;
    setNote("");
    const tmp = { id: `tmp-${Date.now()}`, t_run_s: Math.round((f?.wall_min ?? f?.active_min ?? 0) * 60), text };
    setPending((p) => [...p, tmp]);
    try {
      const n = await api.post(`/api/tasks/${run.short}/runs/${run.run}/notes`, { text });
      if (n?.id && n.text != null) {
        setPending((p) => p.filter((x) => x.id !== tmp.id));
        patchTask(run.short, (task) => {
          const r = (task.runs || []).find((x) => x.name === run.run);
          if (r) r.notes = [...(r.notes || []).filter((x) => x.id !== n.id), n];
        });
      } else if (n?.toast) toast(n.toast.text, n.toast);
    } catch (e) {
      setPending((p) => p.filter((x) => x.id !== tmp.id));
      setNote(text);
      toast(e.message, { level: "error" });
    }
  }
  const pickTab = (x) => { setTab(x); if (x === "shell") setShellOn(true); };

  return html`<aside class=${`drawer ${d.open ? "open" : ""} ${d.full ? "full" : ""}`} aria-label="Lane" aria-hidden=${!d.open}>
    <div class="dhead">
      <div class="r1">
        <span class="label">Lane</span>
        <span class="dtitle">${run ? `${run.label} · ${runLabel(run)}` : queued ? `Next: ${queued.label} · ${kindWord(queued.kind)} run` : "No run"}</span>
        <span style="flex:1"></span>
        <div class="tabs">
          <button class=${`tab ${tab === "claude" ? "on" : ""}`} onClick=${() => pickTab("claude")}>Claude</button>
          <button class=${`tab ${tab === "shell" ? "on" : ""}`} onClick=${() => pickTab("shell")}>Shell</button>
        </div>
        <button class="iconbtn" title="Close (⌃\`)" onClick=${closeDrawer}>✕</button>
      </div>
      <div class="r2">
        <${Chip} tone=${tone}>${label}${["waiting", "working", "permission"].includes(l.state) ? html` <span class="num">${clock(l.since)}</span>` : null}<//>
        ${l.rate_limited ? html`<${Chip} tone="red">Rate limited${l.rate_limited.until ? ` · resets ${l.rate_limited.until}` : ""}<//>` : null}
        ${f ? html`${f.model_ok === false || f.effort_ok === false ? html`<span class="chip red">Claude ran on the wrong model</span>` : null}
          ${mon ? null : html`<span class=${budgetTone ? `chip ${budgetTone}` : ""}>Claude <b class="num">${mins(budget)}</b> of 90 min</span>`}
          ${f.follow_ups ? html`<span><b class="num">${f.follow_ups}</b> follow-up${f.follow_ups === 1 ? "" : "s"}</span>` : null}` : null}
        ${mon ? html`<${MonitorLine} m=${mon} />` : null}
        ${locked ? html`<span class=${`chip neutral ${flash ? "flash" : ""}`} title="After the opening prompt on a one-message task, Enter is held back so nothing else is sent. ⌘K: Unlock lane input">Input locked</span>` : null}
        ${busy ? html`<span class="djob"><span class="dot busy"></span>${afterRunLine(busy)} <span class="num">${clock(busy.started_at || busy.created_at)}</span></span>` : null}
        <span style="flex:1"></span>
        ${acts.main ? html`<button class="btn sm primary" onClick=${acts.main.run} title=${acts.main.title}><${Kbd} k="⌘⇧↵" />${acts.main.label}</button>` : null}
      </div>
    </div>
    ${l.state === "setup_failed" && tab === "claude" ? html`<${SetupLog} l=${l} />` : null}
    <div class="dterms" style=${l.state === "setup_failed" && tab === "claude" ? "display:none" : ""}>
      <div class="dterm" style=${tab === "claude" ? "" : "display:none"}><${Terminal} path="/ws/lane" active=${d.open && tab === "claude"}
        idleText=${run && busy ? `${afterRunLine(busy)}…` : run && autoCollecting ? `Collecting ${runLabel(run)}…` : null} /></div>
      ${shellOn ? html`<div class="dterm" style=${tab === "shell" ? "" : "display:none"}><${Terminal} path="/ws/shell" active=${d.open && tab === "shell"} /></div>` : null}
    </div>
    ${run ? html`<div class="notes">
      <div style="display:flex;align-items:center;justify-content:space-between">
        <span class="label">Run notes</span><span class="kbd">⌥N</span>
      </div>
      ${notes.map((n) => html`<div class=${`nrow ${String(n.id).startsWith("tmp-") ? "faint" : ""}`}><span class="t">${mmss(n.t_run_s)}</span><span>${n.text}</span></div>`)}
      <input value=${note} aria-label="Add a run note" spellcheck=${true} autocomplete="off" disabled=${!run}
        onInput=${(e) => setNote(e.currentTarget.value)}
        onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); addNote(); } }} />
    </div>` : null}
    <div class="dfoot">
      <span>${f ? footFacts(f) : ""}</span>
      <span style="display:flex;gap:8px">
        ${run && !["preparing", "needs_login", "ready_for_prompt"].includes(l.state) ? html`<${PackButton} short=${run.short} run=${run.run} />` : null}
        <button class="btn sm quiet" title="Full width (⌃⇧\`)" onClick=${() => (drawer.value = { ...d, full: !d.full })}>${d.full ? "Narrow" : "Full width"}</button>
        ${acts.end && acts.end !== acts.main ? html`<button class="btn sm" onClick=${acts.end.run} title=${acts.end.title}>End run and collect</button>` : null}
      </span>
    </div>
  </aside>`;
}
