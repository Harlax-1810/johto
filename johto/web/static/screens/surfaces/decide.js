import { useEffect, useRef, useState } from "preact/hooks";
import { signal } from "@preact/signals";
import { html } from "../../lib/html.js";
import { Surface } from "../../components/surface.js";
import { PackButton } from "../../components/pack.js";
import { Collapsible, Kbd } from "../../components/ui.js";
import { api } from "../../lib/api.js";
import { toast, toasts, loadTask, patchTask, putJob } from "../../lib/store.js";
import { reportError, report } from "../../lib/actions.js";
import { pushContext } from "../../lib/keys.js";
import { fullSlot, mins, plural } from "../../lib/format.js";

// ---------- Shared by this group's surfaces (details, decide, rate, log_discard) ----------

/**
 * The run an item is about: the item's own `run` (or its primary action's), else a run named in its id
 * ("decide:<short>:<run>"), else for rating the run Studio gets, else the newest run of that kind with facts.
 */
export function runOf(task, item, kind) {
  const runs = task?.runs || [];
  const named = item?.run || item?.primary?.args?.run || (item?.id || "").split(":")[2];
  const hit = runs.find((r) => r.name === named);
  if (hit) return hit;
  if (kind === "validate") {
    const sub = submissionRun(task);
    const r = runs.find((x) => x.name === sub);
    if (r) return r;
  }
  return runs.filter((r) => r.kind === kind && r.facts).slice(-1)[0] || null;
}

export const mmss = (s) => `${Math.floor((s || 0) / 60)}:${String((s || 0) % 60).padStart(2, "0")}`;
export const runName = (run) => run ? `${run.kind === "calib" ? "calibration" : "validation"} ${run.n}` : "";

/** The run Studio gets: the one Austin chose (task.json submission_run), else the newest collected validation run. */
export function submissionRun(task) {
  const c = task?.submission_run ?? task?.summary?.submission_run;
  if (c) return typeof c === "string" ? c : c.run || c.name || null;
  const v = (task?.runs || []).filter((r) => r.kind === "validate" && (r.collected_at || r.state === "collected"));
  return v.length ? v[v.length - 1].name : null;
}
export const chosenRun = (task) => {
  const c = task?.submission_run ?? task?.summary?.submission_run;
  return c ? (typeof c === "string" ? c : c.run || c.name || null) : null;
};
export const collectedRuns = (task) => (task?.runs || []).filter((r) => r.collected_at || r.state === "collected");

/** Choose which run is submitted (null: back to the newest validation run). */
export async function chooseRun(short, run, label) {
  try {
    const r = await api.send("POST", `/api/tasks/${short}/submission-run`, { run });
    patchTask(short, (t) => { t.submission_run = run; if (t.summary) t.summary.submission_run = run; });
    report(r, run ? `Studio gets ${label || run} when you submit` : "Studio gets the validation run when you submit");
    loadTask(short, true).catch(() => {});
    return r;
  } catch (e) { reportError(e); return null; }
}

/**
 * Which run goes to Studio, as one quiet line with a switch. Shown only when there is a choice:
 * a chosen run, or more than one collected run.
 */
export function RunChoice({ task, extra, run: about }) {
  const s = task.summary;
  const runs = collectedRuns(task);
  const chosen = chosenRun(task);
  const sub = submissionRun(task);
  const choice = !!chosen || runs.length >= 2;
  if (!choice && !extra) return null;
  const subRun = runs.find((r) => r.name === sub);
  const others = runs.filter((r) => r.name !== sub);
  const tail = extra ? html`<span class="rc-extra">${extra}</span>` : null;
  if (!choice) return html`<div class="why runchoice">${about ? html`<span>${runName(about)}</span>` : null}${tail}</div>`;
  return html`<div class="why runchoice">
    <span>Studio gets ${subRun ? html`<b>${runName(subRun)}</b>` : "the next validation run"}${subRun?.kind === "calib"
      ? html`<span class="faint">, a run made before you wrote the goal and decision points</span>` : null}</span>
    ${others.map((r) => html`<button class="sb-link" onClick=${() => chooseRun(s.short, r.name, runName(r))}
        title=${`Submit ${runName(r)} instead: its conversation log, final diff and Claude minutes`}>use ${runName(r)}</button>`)}
    ${subRun?.kind === "calib" && !runs.some((r) => r.kind === "validate") ? html`<button class="sb-link"
        onClick=${() => chooseRun(s.short, null)} title="Back to keep or drop; Keep then queues a validation run in a new lane">use a validation run instead</button>` : null}
    ${tail}
  </div>`;
}

/** POST a write. The server answers with a job or a toast; a second press comes back as needsConfirm. */
export async function post(path, body = {}) {
  try {
    const r = await api.send("POST", path, body);
    if (r?.toast) toast(r.toast.text, r.toast);
    if (r?.job) putJob(r.job);
    return r || {};
  } catch (e) {
    if (e.code === "needs_confirm") return { needsConfirm: e.details || {} };
    reportError(e);
    return null;
  }
}

/** Register the surface's ⌘⇧↵ action; always calls the latest closure. */
export function usePrimary(setPrimary, fn) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    setPrimary?.(() => ref.current?.());
    return () => setPrimary?.(null);
  }, []);
}

/** Single-letter keys for this surface (list mode only), popped on unmount. */
export function useKeys(name, bindings) {
  const ref = useRef(bindings);
  ref.current = bindings;
  useEffect(() => {
    const wrapped = {};
    for (const k of Object.keys(ref.current)) wrapped[k] = (e) => ref.current[k]?.(e);
    return pushContext(name, wrapped);
  }, []);
}

/**
 * ⌥1-⌥5 jump between the surface's protected fields (works inside a field too).
 * `extra` maps e.code to a handler and wins over the default jump (e.g. Digit2 opening a collapsed field).
 * ⌥digits are always swallowed so a missing target never types "¡" or "™" into the field.
 */
export function useFieldJumps(selector, extra = {}) {
  const ref = useRef(extra);
  ref.current = extra;
  useEffect(() => {
    const onKey = (e) => {
      if (!e.altKey || e.metaKey || e.ctrlKey || e.shiftKey) return;
      const h = ref.current[e.code];
      if (h) { e.preventDefault(); h(e); return; }
      const m = /^Digit([1-5])$/.exec(e.code);
      if (m) {
        e.preventDefault();
        focusEnd(document.querySelectorAll(selector)[+m[1] - 1]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selector]);
}

export function focusEnd(el) {
  if (!el) return;
  el.focus();
  const n = el.value?.length ?? 0;
  try { el.setSelectionRange(n, n); } catch {}
  el.scrollIntoView?.({ block: "nearest" });
}

// ---------- Drop with a 10 s undo window ----------
// Kept at module level so the countdown survives moving to another row.

export const drops = signal({}); // short -> {until, actionId, label, after}
let dropTimer = null;

function paintDropToasts() {
  const now = Date.now();
  const mine = Object.entries(drops.value).map(([short, d]) => ({
    id: `drop-${short}`, level: "info", undo: { label: "Cancel" }, onUndo: () => cancelDrop(short),
    text: `Dropping in ${Math.max(0, Math.ceil((d.until - now) / 1000))} s: ${d.label}`,
  }));
  const others = toasts.value.filter((t) => !String(t.id).startsWith("drop-"));
  toasts.value = [...others.slice(-(3 - mine.length)), ...mine];
}

function tickDrops() {
  const now = Date.now();
  const left = { ...drops.value };
  const finished = [];
  for (const [short, d] of Object.entries(left)) if (now >= d.until) { finished.push(d); delete left[short]; }
  if (finished.length) drops.value = left;
  paintDropToasts();
  for (const d of finished) if (d.after) toast(d.after.text, d.after);
  if (!Object.keys(left).length) { clearInterval(dropTimer); dropTimer = null; }
}

/** Start a drop: the server holds it for the undo window; ⌘Z or "Cancel drop" calls it off. */
export async function startDrop({ short, run, label }) {
  if (drops.value[short]) return;
  drops.value = { ...drops.value, [short]: { until: Date.now() + 10000, actionId: null, label, after: null } };
  if (!dropTimer) dropTimer = setInterval(tickDrops, 250);
  paintDropToasts();
  try {
    const r = await api.send("POST", `/api/tasks/${short}/decision`, { run, decision: "drop" });
    const cur = drops.value[short];
    if (!cur) return;
    const next = { ...cur };
    if (r?.undo) {
      next.actionId = r.undo.action_id;
      next.until = Date.parse(r.undo.expires_at) || cur.until;
    } else if (r?.toast) next.after = r.toast;
    drops.value = { ...drops.value, [short]: next };
  } catch (e) {
    const { [short]: _, ...rest } = drops.value;
    drops.value = rest;
    paintDropToasts();
    toast(e.message, { level: "error" });
  }
}

export async function cancelDrop(short) {
  const d = drops.value[short];
  if (!d) return;
  const { [short]: _, ...rest } = drops.value;
  drops.value = rest;
  paintDropToasts();
  if (d.actionId) {
    try { await api.send("POST", `/api/undo/${d.actionId}`, {}); }
    catch (e) { toast(e.message, { level: "error" }); return; }
  }
  toast("Drop cancelled");
}

window.addEventListener("keydown", (e) => {
  if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey || e.code !== "KeyZ") return;
  const open = Object.keys(drops.value);
  if (!open.length) return;
  e.preventDefault();
  cancelDrop(open[open.length - 1]);
}, true);

// ---------- Run evidence blocks ----------

export function Stat({ n, unit }) {
  return html`<span class="sa-stat"><b class="num">${n}</b><span>${unit}</span></span>`;
}

export function Diffstat({ diff, bare }) {
  if (!diff?.length) return null;
  const add = diff.reduce((a, d) => a + (d.add || 0), 0);
  const del = diff.reduce((a, d) => a + (d.del || 0), 0);
  return html`<section class="sa-sec">
    ${bare ? null : html`<div class="sa-h"><span class="label">Changes</span>
      <span class="sm faint num">${plural(diff.length, "file")} · <span class="sa-add">+${add}</span> <span class="sa-del">−${del}</span></span></div>`}
    <div class="sa-diff">
      ${diff.map((d) => html`<div class="sa-drow">
        <span class="p" title=${d.path}>${d.path}</span>
        <span class="tags">${d.new ? html`<span class="sa-tag">new</span>` : null}${d.test ? html`<span class="sa-tag">test</span>` : null}</span>
        <span class="sa-add num">+${d.add}</span><span class="sa-del num">${d.del ? `−${d.del}` : ""}</span>
      </div>`)}
    </div>
  </section>`;
}

export function RunNotes({ notes, max, title = "Your run notes" }) {
  const [all, setAll] = useState(false);
  if (!notes?.length) return null;
  const shown = all || !max || notes.length <= max ? notes : notes.slice(0, max);
  return html`<section class="sa-sec">
    <div class="sa-h"><span class="label">${title}${notes.length > 2 ? ` (${notes.length})` : ""}</span></div>
    <div class="sa-notes">
      ${shown.map((n) => html`<div class="sa-note"><span class="t num">${mmss(n.t_run_s)}</span><span>${n.text}</span></div>`)}
    </div>
    ${shown.length < notes.length ? html`<button class="sa-more" onClick=${() => setAll(true)}>▸ ${notes.length - shown.length} more</button>` : null}
  </section>`;
}

export function Moments({ moments }) {
  if (!moments?.length) return null;
  const sorted = [...moments].sort((a, b) => (a.level === "warn" ? 0 : 1) - (b.level === "warn" ? 0 : 1));
  return html`<ul class="sa-moments">
    ${sorted.map((m) => html`<li class=${m.level}>
      <span class="ic">${m.level === "warn" ? "⚠" : "✓"}</span>
      <span>${m.text}${m.ref?.turn ? html` <span class="faint">· turn ${m.ref.turn}</span>` : null}</span>
    </li>`)}
  </ul>`;
}

/**
 * The diff route (GET /runs/{run}/diff) is not served in v1, so the Diff section renders only once the
 * run says it has one (`has_diff`, or the collected `diff` path). Failures read as one plain line.
 */
export const hasDiff = (run) => !!(run?.has_diff || run?.diff);

export function DiffText({ short, run }) {
  const [st, setSt] = useState({ loading: true });
  useEffect(() => {
    let live = true;
    fetch(`/api/tasks/${short}/runs/${run}/diff`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error())))
      .then((d) => live && setSt({ d }), () => live && setSt({ e: true }));
    return () => { live = false; };
  }, [short, run]);
  if (st.loading) return html`<div class="sm faint">Loading the diff…</div>`;
  if (st.e) return html`<div class="sm faint">Diff not available yet</div>`;
  return html`<pre class="sa-pre">${st.d.split("\n").map((l) => html`<span class=${l.startsWith("+") && !l.startsWith("+++") ? "a" : l.startsWith("-") && !l.startsWith("---") ? "d" : l.startsWith("@@") ? "h" : ""}>${l}\n</span>`)}</pre>`;
}

// One load per run, shared by "Your replies" and "Transcript" (and kept while moving between rows).
const txCache = new Map();
export function useTranscript(short, run) {
  const key = `${short}/${run}`;
  const [st, setSt] = useState(() => txCache.get(key)?.st || { loading: true });
  useEffect(() => {
    if (!short || !run) return;
    let live = true;
    let c = txCache.get(key);
    if (!c) {
      c = { st: { loading: true } };
      c.p = api.get(`/api/tasks/${short}/runs/${run}/transcript?after=0&limit=200`)
        .then((d) => (c.st = { entries: d?.entries || [] }), () => (c.st = { e: true }, txCache.delete(key), c.st));
      txCache.set(key, c);
    }
    if (!c.st.loading) setSt(c.st);
    else c.p.then(() => live && setSt(c.st));
    return () => { live = false; };
  }, [key]);
  return st;
}

const ROLE = { human: "You", claude: "Claude", tool: "Tool", system: "System" };

/** Transcript rows; the role and turn print only when they change, tool calls sit indented under them. */
export function Transcript({ tx, only }) {
  if (tx.loading) return html`<div class="sm faint">Loading the transcript…</div>`;
  if (tx.e) return html`<div class="sm faint">Transcript not available yet</div>`;
  const rows = (tx.entries || []).filter((e) => !only || e.role === only);
  let prev = null;
  return html`<div class="sa-tx">
    ${rows.map((e) => {
      const who = e.role === "tool" ? prev : `${e.role}:${e.turn ?? ""}`;
      const show = e.role !== "tool" && who !== prev;
      prev = who;
      return html`<div class=${`sa-txr ${e.role} ${show ? "first" : ""}`}>
        <span class="who">${show ? html`${ROLE[e.role] || e.role}${e.turn ? html` <span class="faint num">${e.turn}</span>` : null}` : null}</span>
        <span class="txt">${e.tool ? html`<span class="tn">${e.tool.name}</span>${e.tool.summary}` : e.text}</span>
      </div>`;
    })}
  </div>`;
}

// ---------- Keep or drop ----------

/** The fixed rule from the design: measured facts in, a recommendation out. */
export function decideRule(f, enterprise) {
  const t = f?.tests || {};
  const drop = [
    { ok: f?.turns === 1, text: "1 turn" },
    { ok: f?.active_min != null && f.active_min < 10, text: "under 10 active min" },
    { ok: !!(t.last_ok && t.after_last_edit), text: "last test passed after the last edit" },
  ];
  const keep = [
    { ok: (f?.turns || 0) >= 3, text: "3 or more turns" },
    ...(enterprise ? [{ ok: (f?.active_min || 0) >= 30, text: "30 or more active min" }] : []),
  ];
  const decision = drop.every((c) => c.ok) ? "drop" : keep.some((c) => c.ok) ? "keep" : null;
  return { drop, keep, decision };
}

/**
 * Active and wall time on one track. Only short numbers sit at the marks so they never run into each
 * other; what the marks mean is in the legend row.
 */
function TimeBar({ active, wall, enterprise }) {
  const max = Math.max(35, Math.ceil(((wall || 0) + 5) / 5) * 5);
  const pct = (m) => `${Math.min(100, (m / max) * 100)}%`;
  return html`<div class="dec-bar" aria-label=${`${mins(active)} active minutes of ${mins(wall)} wall minutes`}>
    <div class="track">
      <span class="wall" style=${{ width: pct(wall || 0) }}></span>
      <span class="act" style=${{ width: pct(active || 0) }}></span>
      <span class="mark drop" style=${{ left: pct(5) }}></span>
      <span class=${`mark ent ${enterprise ? "" : "dim"}`} style=${{ left: pct(30) }}></span>
    </div>
    <div class="ticks num">
      <span class="drop" style=${{ left: pct(5) }}>5</span>
      <span class=${`ent ${enterprise ? "" : "dim"}`} style=${{ left: pct(30) }}>30</span>
      <span class="end">${max} min</span>
    </div>
    <div class="legend sm">
      <span><i class="act"></i>active</span><span><i class="wall"></i>wall</span>
      <span class="gap"></span>
      <span><i class="ln drop"></i>reviewers drop under 5 min</span>
      <span class=${enterprise ? "" : "faint"}><i class=${`ln ent ${enterprise ? "" : "dim"}`}></i>Enterprise bar, 30 min</span>
    </div>
  </div>`;
}

/** The fixed rule in two plain rows; the row that decides is in full text, unmet conditions are grey. */
function RuleRows({ rule, rec }) {
  const cond = (c) => html`<span class=${`dec-c ${c.ok ? "ok" : ""}`}>${c.ok ? html`<span class="ic">✓</span>` : null}${c.text}</span>`;
  return html`<section class="dec-rules">
    <div class=${`dec-rule ${rec === "drop" ? "on" : ""}`}><span class="k">Drop if all</span><span class="cs">${rule.drop.map(cond)}</span></div>
    <div class=${`dec-rule ${rec === "keep" ? "on" : ""}`}><span class="k">Keep if any</span><span class="cs">${rule.keep.map(cond)}</span></div>
  </section>`;
}

/** The paid audit starts only from its button or `a`; the header toggles findings once they exist. */
function Audit({ run, onRun }) {
  const a = run.audit;
  const [open, setOpen] = useState(false);
  const findings = a?.moments || [];
  return html`<section class="ai dec-audit">
    <div class="ah">
      ${a ? html`<button class="dec-audit-t" onClick=${() => setOpen(!open)} aria-expanded=${open}>
            <span class="tri">${open ? "▾" : "▸"}</span><span class="label">Run audit · AI</span>
            <span class="sm">${plural(findings.length, "finding")}</span>
          </button>`
        : html`<span class="label dec-audit-l">Run audit · AI</span>
          <button class="btn sm quiet dec-audit-go" onClick=${onRun} title="Reads the transcript and lists misses from a fixed list, each with a quote"><${Kbd} k="a" />Run · about $0.30-0.60</button>`}
    </div>
    ${a && open ? html`<ul>${findings.map((m) => html`<li><span><b>${m.category}</b> “${m.quote}”</span><span class="sm">turn ${m.turn}</span></li>`)}</ul>` : null}
  </section>`;
}

/** Diff, Claude's last message, Transcript: collapsed, each only when it has something to show. */
function RunColls({ short, run, f }) {
  const tx = useTranscript(short, run.name);
  const hasTx = f.transcripts !== 0 && !(tx.entries && !tx.entries.length);
  return html`<div class="sa-colls">
    ${hasDiff(run) ? html`<${Collapsible} title="Diff"><${DiffText} short=${short} run=${run.name} /><//>` : null}
    ${f.final_message_excerpt ? html`<${Collapsible} title="Claude's last message"><div class="sa-quote">${f.final_message_excerpt}</div><//>` : null}
    ${hasTx ? html`<${Collapsible} title="Transcript" count=${tx.entries?.length}><${Transcript} tx=${tx} /><//>` : null}
  </div>`;
}

export default function Decide({ item, task, setPrimary }) {
  const s = task.summary;
  const run = runOf(task, item, "calib");
  const f = run?.facts || {};
  const enterprise = (s.slot?.codebase_type || "").startsWith("Enterprise");
  const rule = decideRule(f, enterprise);
  const rec = run?.recommend ? run.recommend.decision : rule.decision;
  const dropping = !!drops.value[s.short];
  const base = `/api/tasks/${s.short}`;
  const rn = run?.name;
  const taskMin = f.task_claude_min ?? s.claude_min ?? null;
  const overBudget = taskMin != null && taskMin >= 60;

  const keep = () => post(`${base}/decision`, { run: rn, decision: "keep" });
  const rework = () => post(`${base}/decision`, { run: rn, decision: "rework" });
  const drop = () => startDrop({ short: s.short, run: rn, label: `unclaims ${s.task_name} in Studio and removes its lanes` });
  const audit = () => post(`${base}/runs/${rn}/audit`, {});
  // Austin's case: a calibration run long and steered enough to be the one Studio gets. Choosing it
  // skips the validation run; the details come next, then the ratings for this run.
  const submitThis = () => run && chooseRun(s.short, rn, runName(run));
  usePrimary(setPrimary, keep);
  useKeys("decide", { e: rework, d: drop, a: audit, s: submitThis });

  // Slate always means ⌘⇧↵ (Keep). The recommendation is the selection tint plus bar, on Drop or on Keep.
  const why = (x) => (rec === x && !(x === "keep" && overBudget) ? "Recommended. " : "");
  // Past 60 min of Claude time a validation run would push the task over its 90 min budget, so submitting
  // this run takes the recommendation tint from Keep (the facts sit under Measured).
  const subRec = overBudget && rec !== "drop";
  const secondary = [
    ...(run?.facts && rec !== "drop" ? [{ label: "Use this run for Studio", key: "s", kind: subRec ? "dec-recbtn" : "quiet", run: submitThis,
      title: `${subRec ? `Recommended: Claude has spent ${mins(taskMin)} of 90 min on this task. ` : ""}Keeps the problem and gives Studio ${runName(run)}'s log and diff instead of a new validation run: you write the details, then rate this run. It ran before you wrote the goal.` }] : []),
    { label: "Edit prompt", key: "e", kind: "quiet", run: rework, title: "Unlocks the opening prompt; Done queues calibration " + ((run?.n || 1) + 1) },
    dropping
      ? { label: "Cancel drop", key: "⌘Z", kind: "armed", run: () => cancelDrop(s.short) }
      : { label: "Drop", key: "d", kind: rec === "drop" ? "dec-recbtn" : "outline", run: drop,
          title: `${why("drop")}Unclaims ${s.task_name} in Studio after 10 s, removes its lanes and opens the Discarded Run Log` },
  ];
  const primary = { label: "Keep", run: keep, kind: rec === "keep" && !subRec ? "primary dec-recbtn" : "primary",
    why: `${why("keep")}Tears down the calibration lane (files and transcript stay) and moves on to details, then a validation run` };
  const head = { title: `Keep or drop after calibration ${run?.n || ""}`.trim(), label: s.label, dossier: s.dossier, slotLine: fullSlot(s.slot),
    reason: item.reason, secondary, primary };

  if (!run?.facts) {
    return html`<${Surface} ...${head}>
      <div class="sbody dec"><div class="scol dec-main"><${RunNotes} notes=${run?.notes} /></div></div>
    <//>`;
  }

  return html`<${Surface} ...${head}>
    <div class="sbody dec">
      <div class="scol dec-main">
        <div class="sa-stats">
          <${Stat} n=${f.turns} unit=${f.turns === 1 ? "your turn" : "your turns"} />
          <${Stat} n=${mins(f.active_min)} unit="active min" />
          <${Stat} n=${mins(f.wall_min)} unit="wall min" />
        </div>
        <${TimeBar} active=${f.active_min} wall=${f.wall_min} enterprise=${enterprise} />
        <${RuleRows} rule=${rule} rec=${rec} />
        <${Diffstat} diff=${f.diffstat} />
        <${RunNotes} notes=${run.notes} />
        <${RunColls} short=${s.short} run=${run} f=${f} />
      </div>
      <div class="scol dec-side">
        ${run.moments?.length ? html`<section class="sa-sec">
          <div class="sa-h"><span class="label">Measured</span></div>
          <${Moments} moments=${run.moments} />
        </section>` : null}
        <${Audit} run=${run} onRun=${audit} />
        <section class="sa-sec" style="margin-top:12px"><${PackButton} short=${s.short} run=${run.name} /></section>
      </div>
    </div>
  <//>`;
}
