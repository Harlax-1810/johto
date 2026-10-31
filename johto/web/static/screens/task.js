import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { api } from "../lib/api.js";
import {
  app, tasks, loadTask, patchTask, lane, queue, hash, navigate, select, toast, tick, openDrawer, fieldFocused,
  jobs, jobFor, jobLine, waitJob, isLive, putJob,
} from "../lib/store.js";
import { pushContext } from "../lib/keys.js";
import { registerPalette } from "../components/overlays.js";
import { Field } from "../components/field.js";
import { Chip, Dot, Kbd, Segmented, PassGrid } from "../components/ui.js";
import { PackButton } from "../components/pack.js";
import { AppLink } from "../components/surface.js";
import { Reference } from "./reference.js";
import { fullSlot, mins, ago, clock, plural, cutWords } from "../lib/format.js";
import { setPrimaryHandler } from "../app.js";

// Stages as Austin sees them, in order. The derived stage maps onto one of these.
const STAGES = ["Prompt", "Calibration", "Keep or drop", "Details", "Validation", "Rate", "Studio", "Review"];
const STAGE_AT = {
  claiming: 0, prompt: 0, calibration: 1, waiting_calib: 1, calibrating: 1, decide: 2, details: 3,
  validation: 4, waiting_validate: 4, validating: 4, rate: 5,
  studio: 6, ready_to_save: 6, saving: 6, qc_running: 6, fix_qc: 6, ready_to_submit: 6, submitting: 6,
  in_review: 7, sent_back: 7, accepted: 7, rejected: 7, dropped: 2, discarded: 2,
};
const DONE_WORD = { 2: "Kept", 7: "Accepted" };
const CLAUDE_BUDGET_MIN = 90;
const LANE_NEEDS_YOU = { waiting: "waiting", permission: "asks permission", needs_login: "needs login", ready_for_prompt: "paste prompt" };
const LANE_BUSY = { preparing: "setting up", exited: "collecting", collecting: "collecting" };

// Fields in submission.md order. "validation" is the ratings block, not a text field.
const ORDER = ["opening_prompt", "goal", "decision_points", "codebase", "initial_state", "validation", "validation_notes", "reviewer_notes"];
const DETAILS = ["goal", "decision_points", "codebase", "initial_state"];
const ROWS = { opening_prompt: 4, goal: 3, decision_points: 6, codebase: 5, initial_state: 4, validation_notes: 5, reviewer_notes: 3 };
const SHORT_NAME = {
  opening_prompt: "Opening prompt", goal: "Goal", decision_points: "Decision points", codebase: "Codebase understanding",
  initial_state: "Initial state", validation_notes: "Validation notes", reviewer_notes: "Reviewer notes",
};
// Which field a reviewer's sentence is about. Order matters only for ties; the result keeps ORDER.
const FEEDBACK_HINTS = [
  ["opening_prompt", /\bopening prompt\b|\bprompt\b/i],
  ["goal", /\bgoal\b|\bsuccess criteri|\bcriteri(on|a)\b/i],
  ["decision_points", /\bdecision points?\b/i],
  ["codebase", /\bcodebase\b|\bdifficulty\b/i],
  ["initial_state", /\binitial state\b|\bsetup\b/i],
  ["validation_notes", /\bvalidation\b/i],
];
const LANG = { typescript: "TypeScript", javascript: "JavaScript", csharp: "C#", "c#": "C#", cpp: "C++", php: "PHP", golang: "Go" };
const langName = (l) => (l ? LANG[l.toLowerCase()] || l : "");
const pad2 = (n) => String(n).padStart(2, "0");

const sentBack = (t) => t.summary.stage === "sent_back" || (!!t.studio?.feedback && STAGE_AT[t.summary.stage] === 6);

function lastCalib(t) {
  const c = (t.runs || []).filter((r) => r.kind === "calib");
  return c[c.length - 1] || null;
}
const dropped = (t) => !!t.discard || ["dropped", "discarded"].includes(t.summary.stage)
  || (!!lastCalib(t) && t.decisions?.[lastCalib(t).name]?.decision === "drop");
const dropLogged = (t) => !!t.discard?.logged_at || t.summary.stage === "discarded";

function qcFails(t) {
  return (t.studio?.qc?.dims || []).filter((d) => d.status === "fail");
}

/** The fields the reviewer's feedback is about; goal and validation notes when it names none. */
function feedbackKeys(t) {
  const fb = t.studio?.feedback || "";
  const hit = new Set(FEEDBACK_HINTS.filter(([k, re]) => re.test(fb)).map(([k]) => k));
  const keys = ORDER.filter((k) => hit.has(k) && t.sections[k]);
  return keys.length ? keys : ["goal", "validation_notes"].filter((k) => t.sections[k]);
}
const firstEditable = (t, keys) => keys.find((k) => !t.sections[k]?.locked) || keys[0] || null;

/**
 * The lane, when it is running this task. A prework container the app didn't start
 * (named PREWORK_<short>-…) counts too: it is this task's newest open run, or the next one.
 */
function laneHere(t, l) {
  if (!l || ["free", "collected"].includes(l.state)) return null;
  const short = t.summary.short;
  if (l.run) return l.run.short === short ? l : null;
  const c = (l.foreign?.container || "").toLowerCase();
  if (!c.includes(`prework_${short.toLowerCase()}-`)) return null;
  const runs = t.runs || [];
  const open = [...runs].reverse().find((r) => !r.collected_at && r.state !== "torn_down");
  if (open) return { ...l, run: { short, run: open.name, kind: open.kind, n: open.n } };
  const kind = (STAGE_AT[t.summary.stage] ?? 0) >= 3 ? "validate" : "calib";
  const n = runs.filter((r) => r.kind === kind).length + 1;
  return { ...l, run: { short, run: `${kind}-${n}`, kind, n } };
}

/** The task's runs, plus the one in the lane when the files don't list it yet. */
function runsOf(t, here) {
  const runs = t.runs || [];
  if (!here || runs.some((r) => r.name === here.run.run)) return runs;
  const n = here.run.n || +(here.run.run.split("-")[1]) || 1;
  return [...runs, { name: here.run.run, kind: here.run.kind, n, created_at: here.since, collected_at: null,
                     state: "active", facts: null, notes: [], moments: [] }];
}

function stageIndex(t, here) {
  if (sentBack(t)) return 7;
  if (dropped(t)) return 2;
  if (here) return here.run.kind === "calib" ? 1 : 4;
  const s = t.summary.stage;
  if (s === "collecting" || s === "collect_failed") return (t.runs || []).some((r) => r.kind === "validate") ? 4 : 1;
  return STAGE_AT[s] ?? 0;
}

/** The stage chip: a word that states the problem, a fill that says who it waits on. */
function stageChip(t, here) {
  const s = t.summary.stage;
  if (sentBack(t)) return { word: "Sent back", tone: "amber" };
  if (dropped(t)) return { word: dropLogged(t) ? "Dropped" : "Dropped · not logged", tone: "ghost" };
  if (here) {
    const w = here.run.kind === "calib" ? "Calibrating" : "Validating";
    if (LANE_NEEDS_YOU[here.state]) return { word: `${w} · ${LANE_NEEDS_YOU[here.state]}`, tone: "amber" };
    if (here.state === "setup_failed") return { word: "Lane setup failed", tone: "red" };
    if (LANE_BUSY[here.state]) return { word: `${w} · ${LANE_BUSY[here.state]}`, tone: "neutral", dot: "busy" };
    return { word: w, tone: "neutral", dot: "work" };
  }
  switch (s) {
    case "accepted": return { word: "Accepted", tone: "green" };
    case "rejected": return { word: "Rejected", tone: "red" };
    case "unclaimed": return { word: "Unclaimed", tone: "ghost" };
    case "calibrating": return { word: "Calibrating", tone: "neutral", dot: "work" };
    case "validating": return { word: "Validating", tone: "neutral", dot: "work" };
    case "collecting": return { word: "Collecting", tone: "neutral", dot: "busy" };
    case "collect_failed": return { word: "Run not collected", tone: "red" };
    case "claiming": return { word: "Claiming", tone: "neutral", dot: "busy" };
    case "waiting_calib": return { word: "Calibration queued", tone: "neutral" };
    case "waiting_validate": return { word: "Validation queued", tone: "neutral" };
    case "decide": return { word: "Keep or drop", tone: "neutral" };
  }
  if (STAGE_AT[s] === 6) {
    const fails = qcFails(t).length;
    if (fails) return { word: plural(fails, "AutoQC fail"), tone: "red" };
    if (s === "qc_running" || t.studio?.qc?.status === "running") return { word: "AutoQC running", tone: "neutral", dot: "busy" };
    if (s === "ready_to_submit") return { word: "Ready to submit", tone: "green" };
    if (s === "saving") return { word: "Saving to Studio", tone: "neutral", dot: "busy" };
    if (s === "submitting") return { word: "Submitting", tone: "neutral", dot: "busy" };
    return { word: "Ready to save", tone: "neutral" };
  }
  return { word: t.summary.stage_label, tone: "neutral" };
}

/**
 * This task's next step. Work that lives on this page (a field, the ratings) is opened here;
 * the lane opens in its drawer; anything else is done in Home and the label says so.
 */
const AWAY = { decide: "Keep or drop", log_discard: "Log the discarded run" };
function stepOf(item, t) {
  if (!item) return null;
  const k = item.kind || "";
  if (k === "collect_failed") {
    const run = item.primary?.args?.run || (item.id || "").split(":")[2];
    if (run && run !== "-") return { label: item.title, collect: run };
  }
  if (k === "lane_start") return { label: item.title, start: /valid/i.test(item.title) || /^details/.test(item.surface) ? "validate" : "calib" };
  if (item.surface === "lane" || k.startsWith("lane_")) return { label: item.title, lane: true };
  if (k === "write_prompt") return { label: item.title, field: "opening_prompt" };
  if (k === "write_details") {
    const keys = DETAILS.filter((x) => t.sections[x]);
    return { label: item.title, field: keys.find((x) => !t.sections[x].text?.trim()) || keys[0] || "goal" };
  }
  if (k === "rate") {
    const r = t.ratings || {};
    const rated = r.one_shot && r.task_success && r.steering;
    return { label: item.title, field: rated && t.sections.validation_notes ? "validation_notes" : "validation" };
  }
  if (k === "fix_qc") {
    const f = qcFails(t).map((d) => d.field_key).find((x) => x && t.sections[x]);
    if (f) return { label: item.title, field: f };
  }
  if (k === "feedback") {
    const f = firstEditable(t, feedbackKeys(t));
    if (f) return { label: "Fix reviewer feedback", field: f };
  }
  return { label: `${AWAY[k] || item.title} in Home →`, away: true };
}

/** The fields a link asks for: one field, or the group it belongs to (details, or the ratings and notes). */
function askedGroup(asked) {
  if (!asked) return [];
  if (DETAILS.includes(asked)) return DETAILS;
  if (["validation", "ratings", "validation_notes"].includes(asked)) return ["validation", "validation_notes"];
  return [asked];
}

function applies(key, idx, t, asked) {
  const s = t.sections[key];
  if (askedGroup(asked).includes(key)) return !!s || key === "validation";
  const has = !!s?.text?.trim();
  if (key === "opening_prompt") return true;
  if (key === "initial_state") return !!s;
  if (key === "goal" || key === "decision_points" || key === "codebase") return idx >= 3 || has;
  if (key === "validation") return idx >= 5 || t.ratings?.task_success != null || !!t.ratings?.one_shot;
  return !!s && (idx >= 5 || has);
}

function currentKeys(idx, t) {
  if (sentBack(t)) return feedbackKeys(t);
  if (dropped(t)) return [];
  switch (idx) {
    case 0: return ["opening_prompt", "initial_state"];
    case 1: case 2: case 4: return ["opening_prompt"];
    case 3: return ["goal", "decision_points", "codebase"];
    case 5: return ["validation", "validation_notes"];
    case 6: return [...new Set([...qcFails(t).map((d) => d.field_key).filter(Boolean), ...(t.studio?.changed_since_save || [])])];
    default: return [];
  }
}

function countLine(key, s) {
  if (!s?.text?.trim()) return null;
  if (key === "opening_prompt") return plural(s.words, "word");
  if (key === "decision_points") return `${s.pairs?.better || 0} better · ${s.pairs?.worse || 0} worse`;
  return `${plural(s.sentences, "sentence")} · ${plural(s.words, "word")}`;
}

// The task's write routes (contract 5.2 and johto/web/api_actions.py). Each job's success toast comes
// from the server; this page adds only failures and the job's progress.
const PATH = {
  unclaim: (s) => `/api/tasks/${s}/studio/unclaim`,
  clean: (s) => `/api/tasks/${s}/clean`,
  skip: (s) => `/api/tasks/${s}/skip-calibration`,
  submissionRun: (s) => `/api/tasks/${s}/submission-run`,
  collect: (s) => `/api/tasks/${s}/collect`,
  checklist: (s) => `/api/tasks/${s}/checklist`,
  laneStart: (s) => `/api/tasks/${s}/lane`,      // setup only; the lane service's /api/lane/start goes first
};
// Job kinds each action starts, so a press made elsewhere (palette, CLI, another tab) shows here too.
const JOB = {
  unclaim: ["unclaim"], clean: ["lane_teardown", "clean"], start: ["lane_prepare", "lane_start"],
  collect: ["lane_collect", "collect"], pack: ["pack"],
};
const runOfJob = (j) => j?.run || j?.params?.run || j?.result?.run || (j?.target || "").split(":")[2] || null;

/**
 * Settle a write's answer: show its toast, and when it started a job, wait for that job and say how it
 * ended. Resolves to the finished job, the plain answer, or null after an error toast.
 */
async function settle(r, { done, failed } = {}) {
  if (r?.toast) toast(r.toast.text, r.toast);
  if (!r?.job) return r;
  putJob(r.job);
  const j = await waitJob(r.job);
  if (j?.state === "succeeded" || j?.state === "done") {
    const text = typeof done === "function" ? done(j) : done;
    if (text) toast(text, { level: "ok" });
  } else if (j && !isLive(j) && !SERVER_SAYS_FAILURE.has(j.kind)) {
    toast(j.error?.message || failed || "That did not finish", { level: "error" });
  }
  return j;
}
// Jobs whose failure the server already announces (johto/web/events.py), so the page stays quiet.
const SERVER_SAYS_FAILURE = new Set(["lane_collect", "pack", "save", "qc", "qc_wait", "submit", "unclaim", "drop"]);

/**
 * Start a lane for this task: the lane service (contract POST /api/lane/start) sets it up and starts Claude
 * in the drawer's terminal. A server without the lane service mounted falls back to setup only.
 */
async function startLane(short, kind) {
  let r = null;
  try { r = await api.send("POST", "/api/lane/start", { short, kind }); }
  catch (e) { if (e.status !== 404 && e.status !== 405) { toast(e.message, { level: "error" }); return null; } }
  if (!r?.job) {
    try { r = await api.send("POST", PATH.laneStart(short), { kind }); }
    catch (e) { toast(e.message, { level: "error" }); return null; }
  }
  const j = await settle(r);
  if (j?.state === "succeeded") openDrawer("row");
  return j;
}

async function post(path, body = {}, opts = {}) {
  let r;
  try { r = await api.send("POST", path, body); }
  catch (e) { toast(e.message, { level: "error" }); return null; }
  return settle(r, opts);
}

/** A press that may come back asking for a second one (needs_confirm); the second goes out at once. */
async function postConfirmed(path, body = {}, opts = {}) {
  try {
    let r = await api.confirmable("POST", path, body);
    if (r?.armed) r = await api.confirmable("POST", path, body);
    return settle(r, opts);
  } catch (e) { toast(e.message, { level: "error" }); return null; }
}

/** Two-press buttons: the first press arms for a few seconds and says what the second will do. */
function useArm(ms = 4000) {
  const [armed, setArmed] = useState(null);
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);
  const arm = (k, wait = ms) => { setArmed(k); clearTimeout(timer.current); timer.current = setTimeout(() => setArmed(null), wait); };
  return [armed, arm, () => { clearTimeout(timer.current); setArmed(null); }];
}

// The page registers its palette commands through this slot while mounted.
let paletteItems = null;
registerPalette(() => (paletteItems ? paletteItems() : []));

/* ---------------------------------------------------------------- header */

function Label({ t }) {
  const [editing, setEditing] = useState(false);
  const [v, setV] = useState(t.summary.label);
  const save = async () => {
    setEditing(false);
    const label = v.trim();
    if (!label || label === t.summary.label) return;
    patchTask(t.summary.short, (c) => { c.summary.label = label; });
    try { await api.send("PUT", `/api/tasks/${t.summary.short}/label`, { label }); }
    catch (e) { toast(e.message, { level: "error" }); }
  };
  if (editing) {
    return html`<input class="tp-rename" value=${v} autofocus aria-label="Task label" spellcheck=${false}
      ref=${(el) => el && document.activeElement !== el && el.focus()}
      onInput=${(e) => setV(e.currentTarget.value)} onBlur=${save}
      onKeyDown=${(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") { setV(t.summary.label); setEditing(false); } }} />`;
  }
  return html`<h1 title="Rename (local only)" onClick=${() => { setV(t.summary.label); setEditing(true); }}>${t.summary.label}</h1>`;
}

function Header({ t, here, runs, step, reason, onNext }) {
  const s = t.summary;
  const repo = s.repo || {};
  const commit = (repo.commit || "").slice(0, 8);
  const claude = s.claude_min || 0;
  const over = claude > CLAUDE_BUDGET_MIN;
  const chip = stageChip(t, here);
  const showClaude = claude > 0 || runs.some((r) => r.facts);
  const lang = langName(repo.language);
  return html`<header class="tp-head">
    <div class="r1">
      <${Label} t=${t} />
      <${Chip} tone=${chip.tone}>${chip.dot ? html`<${Dot} kind=${chip.dot} />` : null}${chip.word}<//>
      <span class="tp-meta">
        <a href=${s.studio?.url} target="_blank" title="Open in Studio">${s.task_name} in Studio ↗</a><${Kbd} k="o" />
        ${s.dossier ? html`<span class="sep">·</span><${AppLink} href=${s.dossier.repo_href} title="The repo page">Repo page<//>
          ${s.dossier.opportunity_href ? html`<span class="sep">·</span><${AppLink} href=${s.dossier.opportunity_href}
            title="The opportunity you chose">Chosen opportunity<//>` : null}` : null}
        ${repo.full_name ? html`<span class="sep">·</span>
          <a href=${repo.pinned_url} target="_blank" title="The repo at the pinned commit">${repo.full_name} @${commit} ↗</a>` : null}
      </span>
    </div>
    <div class="actions">
      ${step ? html`<button class="btn primary" onClick=${onNext} title=${reason || ""}><${Kbd} k="⌘⇧↵" />${step.label}</button>` : null}
    </div>
    <div class="r2">
      <span>${fullSlot(s.slot)}${lang ? html`<span class="sep">·</span>${lang} code` : null}</span>
      <span class="tp-mins num">
        ${showClaude ? html`<span class=${over ? "over" : ""} title="Claude's active minutes across runs, against the 1.5 h budget">Claude <b>${mins(claude) || 0}</b> of ${CLAUDE_BUDGET_MIN} min</span>` : null}
        ${showClaude && s.my_min ? html`<span class="sep">·</span>` : null}
        ${s.my_min ? html`<span title="Your focused minutes on this task">You <b>${mins(s.my_min)}</b> min</span>` : null}
      </span>
    </div>
  </header>`;
}

/* ---------------------------------------------------------------- left column */

function runTitle(r) { return `${r.kind === "calib" ? "calibration" : "validation"} ${r.n}`; }

/**
 * What happens to a run after Claude exits, in words, or null: a collect (the lane watcher's or a
 * pressed one), then its review pack. From the run's `auto` (the watcher) and the live jobs.
 */
function afterRun(t, run, watch) {
  const short = t.summary.short;
  const w = "auto" in run ? run.auto : watch?.[`${short}/${run.name}`];
  const mine = (j) => isLive(j) && (j.short === short || j.target === `t:${short}`) && (!runOfJob(j) || runOfJob(j) === run.name);
  const live = [...jobs.value.values()].filter(mine);
  const cj = live.find((j) => JOB.collect.includes(j.kind));
  if (cj) return { busy: /pack/i.test(cj.progress?.step || "") ? "building review pack" : "collecting" };
  if (live.some((j) => JOB.pack.includes(j.kind))) return { busy: "building review pack" };
  if (w?.phase === "collecting" || run.collecting) return { busy: "collecting" };
  if (w?.phase === "packing") return { busy: "building review pack" };
  if (w?.phase === "failed") return { failed: w.message || "collect failed" };
  return null;
}

function RunRow({ t, run, idx, here, sel, onSel, watch, subRun }) {
  tick.value;
  const live = here && here.run?.run === run.name ? here : null;
  const f = run.facts;
  const after = afterRun(t, run, watch);
  let state = null;
  if (live?.state === "working") state = html`<span class="tp-live"><${Dot} kind="work" />working ${clock(live.since)}</span>`;
  else if (live && LANE_NEEDS_YOU[live.state]) state = html`<span class="tp-live wait"><${Dot} kind="wait" />${LANE_NEEDS_YOU[live.state]} ${clock(live.since)}</span>`;
  else if (live && LANE_BUSY[live.state]) state = html`<span class="tp-live"><${Dot} kind="busy" />${LANE_BUSY[live.state]}</span>`;
  else if (after?.busy) state = html`<span class="tp-live"><${Dot} kind="busy" />${after.busy}</span>`;
  else if (after?.failed) state = html`<span class="tp-bad" title=${after.failed}>collect failed</span>`;
  else if (run.state === "active") state = html`<span class="tp-live"><${Dot} kind="work" />running</span>`;
  else if (run.state === "exited") state = html`<span class="tp-amber">ended · not collected</span>`;
  const bits = [];
  if (f && !live) bits.push(`${mins(f.active_min)} min`, plural(f.turns, "turn"));
  if (!live && run.kind === "calib" && run.state === "collected") {
    const calibs = t.runs.filter((r) => r.kind === "calib");
    const last = calibs[calibs.length - 1] === run;
    const d = t.decisions?.[run.name]?.decision;
    if (d === "drop" || (last && dropped(t))) bits.push("dropped");
    else if (d === "keep" || (last && idx > 2)) bits.push("kept");
    else if (!last) bits.push("prompt edited");
    else if (idx === 2) bits.push("to decide");
  }
  if (!live && run.kind === "validate" && run.state === "collected" && f?.tests?.runs && f.tests.last_ok != null) {
    bits.push(f.tests.last_ok ? "tests passed" : "tests failing");
  }
  const forSubmit = subRun === run.name;
  return html`<button class=${`tp-run ${sel ? "sel" : ""}`} onClick=${() => onSel(sel ? null : run.name)}
      title=${f ? "Show this run on the right, with its actions" : "Show this run's actions"}>
    <span class="t">${runTitle(run)}${forSubmit ? html`<span class="tp-sub" title="Studio gets this run's log, diff and minutes">for submission</span>` : null}</span>
    <span class="l">${state}${bits.length ? html`<span class="bits">${bits.join(" · ")}</span>` : null}</span>
  </button>`;
}

/**
 * Actions for one run, under its row: collect it, make its review pack, use it for the submission.
 * Shown for the selected run, else the newest one.
 */
function RunActions({ t, run, watch, subRun }) {
  jobs.value;
  const s = t.summary;
  const after = afterRun(t, run, watch);
  const collecting = !!after?.busy && after.busy === "collecting";
  const canCollect = run.state !== "torn_down" && run.has_transcript !== false
    && (run.state === "exited" || !!after?.failed || (!run.collected_at && run.state !== "active"));
  const collect = () => post(PATH.collect(s.short), { run: run.name, pack: true });
  const choose = () => {
    api.send("POST", PATH.submissionRun(s.short), { run: run.name })
      .then((r) => {
        if (r?.toast) toast(r.toast.text, r.toast);
        patchTask(s.short, (c) => { c.submission_run = run.name; if (c.summary) c.summary.submission_run = run.name; });
        loadTask(s.short, true).catch(() => {});
      })
      .catch((e) => toast(e.message, { level: "error" }));
  };
  const collected = run.state === "collected" || !!run.collected_at;
  return html`<div class="tp-runacts">
    ${collecting ? html`<span class="tp-live sm"><${Dot} kind="busy" />Collecting</span>`
      : canCollect ? html`<button class="btn sm" onClick=${collect}
          title="Copies the diff and transcript out of the lane, summarizes them, then builds the review pack">Collect</button>` : null}
    ${collected && subRun !== run.name ? html`<button class="btn sm quiet" onClick=${choose}
        title="Studio gets this run's conversation log, final diff and Claude minutes (no validation run)">Use this run for Studio</button>` : null}
    ${!String(s.task_id || "").startsWith("task_demo") ? html`<div class="tp-pack">
      <${PackButton} key=${`${run.name}:${after?.busy || ""}:${(run.packs || []).length}`} short=${s.short} run=${run.name} /></div>` : null}
  </div>`;
}

/** Start a lane for this task (calibration before details, validation after), or open the one running it. */
function LaneStart({ t, idx, here, runs }) {
  jobs.value;
  const s = t.summary;
  const l = lane.value;
  if (here) {
    return html`<div class="tp-runacts"><button class="btn sm" onClick=${() => openDrawer("user")}><${Kbd} k="⌃\`" />Open the lane</button></div>`;
  }
  const job = jobFor(JOB.start, s.short);
  if (isLive(job)) {
    return html`<div class="tp-runacts"><span class="tp-live sm" title=${(job.progress?.log_tail || []).join("\n")}>
      <${Dot} kind="busy" />${jobLine(job)}</span></div>`;
  }
  const kind = runs.some((r) => r.kind === "validate") || idx >= 3 ? "validate" : "calib";
  const n = runs.filter((r) => r.kind === kind).length + 1;
  const word = `${kind === "calib" ? "calibration" : "validation"} ${n}`;
  const other = l.run && l.run.short !== s.short && !["free", "collected", "setup_failed"].includes(l.state) ? l.run : null;
  const why = other ? `The lane is running ${other.label}` : null;
  const start = () => startLane(s.short, kind);
  // With calibration off by default, a first calibration is the optional extra step.
  const optional = kind === "calib" && n === 1 && !app.value?.settings?.calibrate_first;
  return html`<div class="tp-runacts">
    <button class="btn sm" onClick=${start} disabled=${!!why}
      title=${why || (optional ? "Optional: runs your prompt once in a calibration lane before you write the goal"
                               : `Sets up a new lane for ${word} and starts Claude in it (⌃\` opens it)`)}>
      ${optional ? "Calibrate first" : `Start ${word}`}</button>
  </div>`;
}

/** Studio save time: the clock time within the last few hours, otherwise how long ago. */
function savedWhen(iso) {
  if (Date.now() - Date.parse(iso) > 4 * 3600e3) return ago(iso);
  const d = new Date(iso);
  return `at ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function StudioBox({ t }) {
  const st = t.studio || {};
  const fails = qcFails(t).length;
  const qc = st.qc;
  const g = t.gists || {};
  const changed = st.changed_since_save || [];
  // "Writing" is Studio's word for every claimed task, so only the other statuses are worth a line.
  const status = st.status && st.status !== "Writing" ? st.status : null;
  return html`<div class="tp-studio">
    ${status ? html`<div class="tp-ststat">${status}</div>` : null}
    <div>${st.saved_at ? `Saved ${savedWhen(st.saved_at)}` : "Not saved to Studio"}</div>
    ${st.saved_at && changed.length ? html`<div class="tp-amber" title=${changed.map((k) => t.sections?.[k]?.heading || k).join(", ")}>
      ${changed.length === 1 ? `${SHORT_NAME[changed[0]] || changed[0]} changed since save` : `${changed.length} fields changed since save`}</div>` : null}
    ${qc ? html`<div>AutoQC ${qc.status === "running" ? html`<span class="faint">running</span>`
      : html`<span class="num">${qc.counts?.pass || 0}</span> pass${fails ? html` · <span class="tp-bad num">${fails} fail</span>` : null}`}</div>` : null}
    ${g.conversation_log || g.final_output ? html`<div>
      ${g.conversation_log ? html`<a href=${g.conversation_log} target="_blank">log ↗</a>` : null}
      ${g.conversation_log && g.final_output ? html`<span class="sep">·</span>` : null}
      ${g.final_output ? html`<a href=${g.final_output} target="_blank">diff ↗</a>` : null}</div>` : null}
  </div>`;
}

function Stages({ t, idx }) {
  const runs = t.runs || [];
  if (dropped(t)) {
    const logged = dropLogged(t);
    const rows = [["Prompt", "done"], ["Calibration", "done"], [logged ? "Dropped" : "Dropped · not logged", logged ? "done" : "cur"]];
    return html`<div class="tp-stages" role="list" aria-label="Stages">
      ${rows.map(([w, c]) => html`<div role="listitem" class=${`tp-stage ${c}`}><span class="ic">${c === "done" ? "✓" : "●"}</span><span>${w}</span></div>`)}
    </div>`;
  }
  const finished = t.summary.stage === "accepted";
  const skippedCalib = idx > 2 && !runs.some((r) => r.kind === "calib");
  return html`<div class="tp-stages" role="list" aria-label="Stages">
    ${STAGES.map((w, i) => {
      const skipped = skippedCalib && (i === 1 || i === 2);
      if (skipped && i === 2) return null;
      const done = i < idx || (finished && i === idx);
      const cur = i === idx && !finished;
      return html`<div role="listitem" class=${`tp-stage ${skipped ? "skip" : done ? "done" : ""} ${cur ? "cur" : ""}`}>
        <span class="ic">${skipped ? "–" : done ? "✓" : cur ? "●" : ""}</span>
        <span>${skipped ? "Calibration skipped" : done ? DONE_WORD[i] || w : cur && i === 7 && sentBack(t) ? "Sent back" : w}</span></div>`;
    })}
  </div>`;
}

function Left({ t, idx, here, runs, canSkip, selRun, setSelRun, armed, unclaimArm, acts, watch, subRun }) {
  jobs.value;
  const s = t.summary;
  const laneBusy = !!here;
  const cleaning = isLive(jobFor(JOB.clean, s.short));
  const unclaiming = isLive(jobFor(JOB.unclaim, s.short));
  const actRun = runs.find((r) => r.name === selRun) || runs[runs.length - 1] || null;
  const unclaimed = s.stage === "unclaimed" || s.studio?.status === "Ready to Claim";
  // A lane needs the opening prompt; before it is written there is nothing to start.
  const showStart = !dropped(t) && !unclaimed && idx < 7 && (!!here || !!t.sections.opening_prompt?.text?.trim());
  return html`<aside class="tp-left">
    <${Stages} t=${t} idx=${idx} />
    ${runs.length || showStart ? html`<div class="tp-grp"><span class="label">Runs</span></div>` : null}
    ${runs.map((r) => html`<${RunRow} key=${r.name} t=${t} run=${r} idx=${idx} here=${here} sel=${selRun === r.name}
      onSel=${setSelRun} watch=${watch} subRun=${subRun} />
      ${actRun === r ? html`<${RunActions} t=${t} run=${r} watch=${watch} subRun=${subRun} />` : null}`)}
    ${showStart ? html`<${LaneStart} t=${t} idx=${idx} here=${here} runs=${runs} />` : null}
    <div class="tp-grp"><span class="label">Studio</span></div>
    <${StudioBox} t=${t} />
    <div class="tp-rare">
      ${canSkip ? html`<button class=${`btn sm calm ${armed === "skip" ? "armed" : "quiet"}`} onClick=${acts.skip}
          title="Calibration is optional. Skipping is logged.">${armed === "skip" ? "Press again: go to details without a calibration run" : "Skip calibration"}</button>` : null}
      ${runs.length ? html`<button class=${`btn sm ${armed === "clean" ? "armed" : "quiet"}`} onClick=${acts.clean} disabled=${laneBusy || cleaning}
          title=${laneBusy ? "The lane is running this task" : "Removes this task's lane containers, volumes and login folders. Run files stay."}>
          ${cleaning ? "Removing lanes…" : armed === "clean" ? `Press again: removes ${plural(runs.length, "lane")}` : "Clean lanes"}</button>` : null}
      ${!unclaimed ? html`<button class=${`btn sm ${unclaimArm ? "armed" : "quiet"}`} onClick=${acts.unclaim} disabled=${unclaiming}
          title="Gives the slot back in Studio. Asks twice.">
        ${unclaiming ? "Unclaiming…" : unclaimArm ? (/^press again/i.test(unclaimArm) ? unclaimArm : `Press again: ${unclaimArm}`) : "Unclaim"}</button>` : null}
    </div>
  </aside>`;
}

/* ---------------------------------------------------------------- fields */

function QcNote({ dims }) {
  if (!dims.length) return null;
  return html`<div class="tp-qc">${dims.map((d) => html`<div><b>AutoQC · ${d.dimension}</b> ${d.justification}</div>`)}</div>`;
}

function FieldLine({ k, t, label, onOpen, qc, hint }) {
  const s = t.sections[k];
  const cnt = countLine(k, s);
  const lint = (s?.lint || []).length;
  const ov = (s?.overlap || []).filter((o) => !o.accepted).length;
  const first = (s?.text || "").split("\n").find((x) => x.trim()) || "";
  return html`<button class="tp-fline" data-field=${k} onClick=${onOpen} aria-expanded="false">
    <span class="label">${label}</span>
    <span class="cnt num" title=${s?.locked?.reason || ""}>${cnt || (k === "reviewer_notes" ? "optional" : "not written")}${s?.locked ? " · locked" : ""}</span>
    <span class="ex">${cutWords(first, 160)}</span>
    ${qc ? html`<${Chip} tone="red">AutoQC fail<//>` : null}
    ${ov ? html`<${Chip} tone="red">${plural(ov, "match", "matches")}<//>` : null}
    ${lint ? html`<span class="flag" title=${(s.lint || []).map((f) => f.message).join("\n")}>⚑ ${plural(lint, "flag")}</span>` : null}
    ${hint ? html`<span class="kh"><${Kbd} k=${hint} /></span>` : null}
    <span class="tri">▸</span>
  </button>`;
}

function Ratings({ t, open, onToggle, hint }) {
  const r = t.ratings || {};
  const s = t.summary;
  const [cur, setCur] = useState(r);
  // What he picked last. Each save sends all three and saves go out one at a time, so quick clicks
  // never lose a rating to an older answer.
  const want = useRef(r);
  const seq = useRef(0);
  const chain = useRef(Promise.resolve());
  const saving = useRef(0);
  // Ratings changed outside this editor (vim, the CLI, the Rate surface) show up here, unless a save is in flight.
  useEffect(() => { if (!saving.current) { want.current = t.ratings || {}; setCur(t.ratings || {}); } },
    [s.short, r.rev, r.one_shot, r.task_success, r.steering, r.passes]);
  const set = (patch) => {
    const next = { ...want.current, ...patch };
    want.current = next;
    setCur(next);
    const my = ++seq.current;
    saving.current++;
    chain.current = chain.current.then(async () => {
      try {
        const res = await api.send("PUT", `/api/tasks/${s.short}/ratings`,
          { one_shot: next.one_shot, task_success: next.task_success, steering: next.steering, base_rev: next.rev });
        if (res?.ratings && my === seq.current) {
          want.current = res.ratings;
          setCur(res.ratings);
          patchTask(s.short, (c) => { c.ratings = res.ratings; });
        } else if (res?.toast) toast(res.toast.text, res.toast);
      } catch (e) {
        toast(e.message, { level: "error" });
        if (e.code === "conflict_rev") loadTask(s.short, true).then((x) => { if (x?.ratings) { want.current = x.ratings; setCur(x.ratings); } }).catch(() => {});
      } finally { saving.current--; }
    });
  };
  const verdict = cur.passes === true ? html`<${Chip} tone="green">passes<//>` : cur.passes === false ? html`<${Chip} tone="red">fails the grid<//>` : null;
  if (!open) {
    const bits = [cur.one_shot ? `One-shot ${cur.one_shot}` : null, cur.task_success ? `Task Success ${cur.task_success}` : null,
                  cur.steering ? `Steering ${cur.steering}` : null].filter(Boolean);
    return html`<button class="tp-fline" data-field="validation" onClick=${onToggle} aria-expanded="false">
      <span class="label">Validation</span>
      <span class="cnt num">${bits.length ? bits.join(" · ") : "not rated"}</span>
      <span class="ex"></span>${verdict}
      ${hint ? html`<span class="kh"><${Kbd} k=${hint} /></span>` : null}<span class="tri">▸</span>
    </button>`;
  }
  const scale = [1, 2, 3, 4, 5].map((n) => ({ value: n, label: String(n) }));
  return html`<div class="tp-rate" data-field="validation">
    <div class="fhead" onClick=${onToggle}>
      <span class="label">Validation</span>
      <span class="count">${verdict}<span class="tri">▾</span></span>
    </div>
    <div class="tp-rate-body">
      <div class="tp-rate-rows">
        <span class="k">One-shot</span>
        <${Segmented} value=${cur.one_shot} onChange=${(v) => set({ one_shot: v })}
          options=${[{ value: "Yes", label: "Yes" }, { value: "No", label: "No" }]} />
        <span class="k">Task Success</span><${Segmented} value=${cur.task_success} onChange=${(v) => set({ task_success: v })} options=${scale} />
        <span class="k">Steering</span><${Segmented} value=${cur.steering} onChange=${(v) => set({ steering: v })} options=${scale} />
        <span class="k">Claude minutes</span><span class="num">${mins(s.claude_min) || 0} <span class="faint">from the runs</span></span>
      </div>
      <div class="tp-grid">
        <div class="label" style="margin-bottom:6px">${s.slot?.codebase_type} passes</div>
        <${PassGrid} grid=${cur.grid} ts=${cur.task_success} steering=${cur.steering} />
      </div>
    </div>
    ${cur.one_shot === "Yes" ? html`<div class="tp-qc">One-shot yes means the task is dropped.</div>` : null}
    ${(cur.check || []).length ? html`<ul class="flags">${cur.check.map((f) => html`<li class=${f.level}><span class="ic">${f.level === "error" ? "✗" : "⚑"}</span><span class="m">${f.message}</span></li>`)}</ul>` : null}
  </div>`;
}

function PromptUnlock({ t, armed, arm, disarm }) {
  const s = t.sections.opening_prompt;
  if (!s?.locked) return null;
  const validated = t.runs.some((r) => r.kind === "validate");
  const cost = validated ? "editing means a new validation run" : "editing means a new calibration run";
  const go = () => {
    if (armed !== "unlock") return arm("unlock");
    disarm();
    postConfirmed(`/api/tasks/${t.summary.short}/sections/opening_prompt/unlock`)
      .then((r) => { if (r?.section) patchTask(t.summary.short, (c) => { c.sections.opening_prompt = r.section; }); });
  };
  return html`<div class="tp-unlock">
    <button class=${`btn sm calm ${armed === "unlock" ? "armed" : "quiet"}`} onClick=${go}>${armed === "unlock" ? `Press again: ${cost}` : "Unlock to edit"}</button>
  </div>`;
}

/** The submit checklist (failures first). Expanded in the Studio stage, one line before it. */
function CheckBlock({ rows, open, onToggle, onFix }) {
  if (!rows?.length) return null;
  const n = (st) => rows.filter((r) => r.status === st).length;
  const fail = n("fail"), warn = n("warn"), pend = n("pending");
  const sum = [fail ? `${fail} failing` : null, warn ? plural(warn, "warning") : null, pend ? `${pend} pending` : null]
    .filter(Boolean).join(" · ") || "all pass";
  if (!open) {
    const first = rows.find((r) => r.status === "fail") || rows.find((r) => r.status === "warn");
    return html`<button class="tp-fline" data-field="checklist" onClick=${onToggle} aria-expanded="false">
      <span class="label">Submit checklist</span>
      <span class="cnt num">${sum}</span>
      <span class="ex">${first ? `${first.title}${first.detail ? `: ${first.detail}` : ""}` : ""}</span>
      ${fail ? html`<${Chip} tone="red">${fail} failing<//>` : !warn && !pend ? html`<${Chip} tone="green">ready<//>` : null}
      <span class="tri">▸</span>
    </button>`;
  }
  const order = { fail: 0, pending: 1, warn: 2, pass: 3 };
  const icon = { pass: "✓", fail: "✗", warn: "!", pending: "◌" };
  const sorted = [...rows].sort((a, b) => (order[a.status] ?? 4) - (order[b.status] ?? 4));
  return html`<div class="tp-check" data-field="checklist">
    <div class="fhead" onClick=${onToggle}>
      <span class="label">Submit checklist</span>
      <span class="count num">${sum}<span class="tri">▾</span></span>
    </div>
    <ul class="check">${sorted.map((r, i) => {
      // Rows fixed by the same Studio step share one button, on the first of them.
      const studioFix = /^studio_/.test(r.fix?.action || "");
      const label = studioFix && sorted.slice(0, i).some((x) => x.status !== "pass" && /^studio_/.test(x.fix?.action || "")) ? null : fixLabel(r);
      return html`<li class=${r.status} key=${r.key}>
        <span class="st">${icon[r.status] || "·"}</span>
        <span class="tt">${r.title}</span>
        <span class="dd">${r.detail}</span>
        <span>${label && r.status !== "pass" ? html`<button class="btn sm" onClick=${() => onFix(r)}>${label}</button>` : null}</span>
      </li>`;
    })}</ul>
  </div>`;
}

/** What a checklist row's button does, in words. Studio writes are left to the Studio step in Home, where the key says what it writes. */
function fixLabel(r) {
  const a = r.fix?.action;
  if (a === "focus_field") return r.fix.field === "ratings" ? "Go to the ratings" : "Go to the field";
  if (a === "collect") return `Collect ${r.fix.args?.run || "the run"}`;
  if (a === "studio_save" || a === "studio_qc") return "Studio step in Home";
  return a ? "Open" : null;
}

function Fields({ t, idx, keys, open, toggle, autoKey, armed, arm, disarm, checklist, onFix }) {
  const s = t.summary;
  const fails = qcFails(t);
  const fb = t.studio?.feedback;
  const showCheck = idx >= 5 || sentBack(t);
  return html`<div class="tp-mid">
    ${fb ? html`<div class="tp-feedback">
      <div class="label">Reviewer feedback</div><div>${fb}</div></div>` : null}
    ${showCheck ? html`<${CheckBlock} rows=${checklist} open=${open.checklist} onToggle=${() => toggle("checklist")} onFix=${onFix} />` : null}
    ${keys.map((k, i) => {
      const dims = fails.filter((d) => d.field_key === k);
      const hint = i < 9 ? `⌥${i + 1}` : null;
      if (k === "validation") return html`<${Ratings} key=${k} t=${t} open=${open[k]} hint=${hint} onToggle=${() => toggle(k)} />`;
      const sec = t.sections[k];
      if (!open[k]) return html`<${FieldLine} key=${k} k=${k} t=${t} label=${sec.heading} qc=${dims.length > 0}
        hint=${hint} onOpen=${() => toggle(k)} />`;
      return html`<div key=${k} class="tp-fopen" data-field=${k} onClick=${(e) => { if (e.target.closest(".fhead") && !e.target.closest("button")) toggle(k); }}>
        <${Field} key=${`${k}:${sec.locked ? "locked" : "open"}`} short=${s.short} sectionKey=${k} section=${sec}
          minRows=${sec.locked ? 1 : k === "opening_prompt" && s.slot?.prompt_style === "Explicit spec" ? 14 : ROWS[k]}
          autofocus=${autoKey === k} />
        ${k === "opening_prompt" ? html`<${PromptUnlock} t=${t} armed=${armed} arm=${arm} disarm=${disarm} />` : null}
        <${QcNote} dims=${dims} />
      </div>`;
    })}
  </div>`;
}

/* ---------------------------------------------------------------- live data */

/** The run Studio gets: the one he chose, else the newest collected validation run. */
function submissionRun(t) {
  const chosen = t.submission_run || t.summary?.submission_run;
  if (chosen) return typeof chosen === "string" ? chosen : chosen.run || chosen.name || null;
  const v = (t.runs || []).filter((r) => r.kind === "validate" && (r.collected_at || r.state === "collected"));
  return v.length ? v[v.length - 1].name : null;
}

/**
 * The lane watcher's view of this task's runs ({"<short>/<run>": {phase, message}}): collecting, building
 * the review pack, done or failed. Read again while a run is open or the watcher is busy with one.
 */
function useWatch(t) {
  const short = t.summary.short;
  const [w, setW] = useState({});
  // Task details that carry the watcher's state per run (`auto`) make this read unnecessary.
  const needed = (t.runs || []).some((r) => !("auto" in r));
  const busy = needed && ((t.runs || []).some((r) => r.state === "active" || r.state === "exited")
    || Object.values(w).some((x) => x.phase === "collecting" || x.phase === "packing"));
  const runsKey = (t.runs || []).map((r) => `${r.name}:${r.state}:${r.collected_at || ""}:${(r.packs || []).length}`).join(",");
  useEffect(() => {
    if (!needed) return;
    let alive = true, timer;
    const read = async () => {
      try {
        const r = await api.get("/api/watch");
        if (!alive) return;
        const mine = {};
        for (const [k, v] of Object.entries(r?.runs || {})) if (k.startsWith(`${short}/`)) mine[k] = v;
        setW(mine);
      } catch { /* the watcher is optional */ }
      if (alive && busy) timer = setTimeout(read, 4000);
    };
    read();
    return () => { alive = false; clearTimeout(timer); };
  }, [short, busy, needed, runsKey]);
  return w;
}

/** GET /api/tasks/{short}/checklist, read again shortly after the task changes. Null until the server has one. */
function useChecklist(t) {
  const short = t.summary.short;
  const [rows, setRows] = useState(() => (t.checklist?.length ? t.checklist : null));
  const first = useRef(true);
  useEffect(() => {
    let alive = true;
    const timer = setTimeout(async () => {
      try {
        const r = await api.get(PATH.checklist(short));
        const list = Array.isArray(r) ? r : r?.checklist || r?.rows || [];
        if (alive) setRows(list);
      } catch { /* keep the last rows */ }
    }, first.current ? 0 : 700);
    first.current = false;
    return () => { alive = false; clearTimeout(timer); };
  }, [short, t]);
  return rows;
}

/* ---------------------------------------------------------------- page */

export default function TaskPage({ short }) {
  const [err, setErr] = useState(null);
  const t = tasks.value.get(short);
  useEffect(() => {
    setErr(null);
    loadTask(short, true).catch((e) => setErr(e));
  }, [short]);
  if (err) {
    const demo = app.value?.server?.mode === "demo";
    return html`<section class="tp"><div class="tp-err">
      <h1>${err.status === 404 ? `No task ${short} in ${demo ? "the demo data" : "your tasks"}` : err.message}</h1>
      ${err.status === 404 ? html`<a href=${`/task/${short}?demo=${demo ? 0 : 1}`}>${demo ? "Open it in your real tasks →" : "Open it in the demo data →"}</a>` : null}
    </div></section>`;
  }
  if (!t || !t.sections) return html`<section class="tp"></section>`;
  return html`<${Loaded} key=${short} t=${t} />`;
}

function Loaded({ t }) {
  const s = t.summary;
  const here = laneHere(t, lane.value);
  const runs = runsOf(t, here);
  const idx = stageIndex(t, here);
  const next = queue.value.find((q) => q.short === s.short) || null;
  const step = stepOf(next, t);
  // The lane watcher's tab asks for the details; once they are written and the run to rate is in, the
  // ratings are what is left to fill.
  const linkKey = hash.value.get("field");
  const detailsDone = DETAILS.every((k) => !t.sections[k] || t.sections[k].text?.trim());
  const focusKey = DETAILS.includes(linkKey) && detailsDone && idx >= 5 ? "validation" : linkKey;
  // Sent back opens on the field the feedback is about, with the caret in it.
  const [autoKey] = useState(() => focusKey || (sentBack(t) ? firstEditable(t, feedbackKeys(t)) : null));
  const [open, setOpen] = useState(() => {
    const o = {};
    for (const k of currentKeys(idx, t)) o[k] = true;
    for (const k of askedGroup(focusKey)) o[k] = true;
    return o;
  });
  const [selRun, setSelRun] = useState(null);
  const [refShown, setRefShown] = useState(true);
  const [armed, arm, disarm] = useArm();
  const [unclaimArm, setUnclaimArm] = useState(null);   // the server's summary while the second press is due
  const toggle = (k) => setOpen((o) => ({ ...o, [k]: !o[k] }));
  // A link that names a field (the lane watcher's "write the fields" tab) shows it even before its stage.
  const keys = ORDER.filter((k) => applies(k, idx, t, focusKey));
  // Skipping calibration is the default (settings.calibrate_first off): then the prompt's Done already skips it
  // and this page offers "Calibrate first" instead; "Skip calibration" only when calibration is the default.
  const calibFirst = !!app.value?.settings?.calibrate_first;
  const canSkip = calibFirst && idx === 0 && !here && !runs.some((r) => r.kind === "calib");
  const watch = useWatch(t);
  const checklist = useChecklist(t);
  const subRun = submissionRun(t);

  // When the stage moves on (a run was collected, ratings done), open the fields it asks for, unless he is typing.
  const lastIdx = useRef(idx);
  useEffect(() => {
    if (lastIdx.current === idx) return;
    lastIdx.current = idx;
    if (fieldFocused.value) return;
    setOpen((o) => {
      const n = { ...o };
      for (const k of currentKeys(idx, t)) n[k] = true;
      if (idx === 6) n.checklist = true;
      return n;
    });
  }, [idx]);
  useEffect(() => { if (idx === 6 || sentBack(t)) setOpen((o) => ("checklist" in o ? o : { ...o, checklist: true })); }, []);

  const acts = {
    skip: () => {
      if (armed !== "skip") return arm("skip");
      disarm();
      post(PATH.skip(s.short), {}).then((r) => {
        if (r?.summary) patchTask(s.short, (c) => { c.summary = r.summary; });
        loadTask(s.short, true).catch(() => {});
      });
    },
    clean: () => {
      if (armed !== "clean") return arm("clean");
      disarm();
      postConfirmed(PATH.clean(s.short), {});
    },
    // Studio write: the server holds the nonce, so the first press only asks it what the second will do.
    unclaim: async () => {
      try {
        const r = await api.confirmable("POST", PATH.unclaim(s.short), {}, {
          onArm: (summary) => setUnclaimArm(summary || `unclaims ${s.task_name} in Studio`),
          onDisarm: () => setUnclaimArm(null),
        });
        if (r?.armed) return;
        setUnclaimArm(null);
        await settle(r);
      } catch (e) { setUnclaimArm(null); toast(e.message, { level: "error" }); }
    },
  };

  /** Open a field (or the ratings) and put the caret in it. */
  const focusField = (k) => {
    setOpen((o) => ({ ...o, [k]: true }));
    setTimeout(() => {
      const box = document.querySelector(`.tp-mid [data-field="${k}"]`);
      const el = box?.querySelector("textarea:not([readonly])") || box?.querySelector(".seg button") || box?.querySelector("textarea");
      el?.focus();
      box?.scrollIntoView({ block: "nearest" });
    }, 0);
  };

  // ⌥1-⌥9 open and focus a field, ⌥R shows or hides the reference pane. Bound only while this page is up.
  useEffect(() => {
    const onKey = (e) => {
      if (!e.altKey || e.metaKey || e.ctrlKey) return;
      if (e.code === "KeyR") { e.preventDefault(); setRefShown((v) => !v); return; }
      const m = /^Digit([1-9])$/.exec(e.code);
      const k = m && keys[+m[1] - 1];
      if (!k) return;
      e.preventDefault();
      focusField(k);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [keys.join(",")]);

  const goNext = () => {
    if (!step) return;
    if (step.field) return focusField(step.field);
    if (step.start) return startLane(s.short, step.start);
    if (step.collect) return post(PATH.collect(s.short), { run: step.collect, pack: true });
    if (step.lane) return openDrawer("row");
    navigate("/"); select(next.id);
  };
  const studio = () => s.studio?.url && window.open(s.studio.url, "_blank", "noopener");
  const fix = (row) => {
    const f = row.fix;
    if (f?.action === "focus_field" && f.field) return focusField(f.field === "ratings" ? "validation" : f.field);
    if (f?.action === "collect" && f.args?.run) return post(PATH.collect(s.short), { run: f.args.run, pack: true });
    const item = queue.value.find((q) => q.short === s.short && q.surface === "studio") || queue.value.find((q) => q.short === s.short);
    if (item) { navigate("/"); select(item.id); }
    else toast("Nothing for this task in the queue right now");
  };

  useEffect(() => { setPrimaryHandler(step ? goNext : null); return () => setPrimaryHandler(null); }, [next?.id, step?.field, step?.label, step?.start, step?.collect]);
  useEffect(() => pushContext("task", { o: studio, Escape: () => { disarm(); api.disarm("POST", PATH.unclaim(s.short)); } }), [s.short]);
  useEffect(() => { if (focusKey) setOpen((o) => ({ ...o, ...Object.fromEntries(askedGroup(focusKey).map((k) => [k, true])) })); }, [focusKey]);
  useEffect(() => { if (focusKey === "validation" || focusKey === "ratings") focusField("validation"); }, []);

  const unclaimed = s.stage === "unclaimed" || s.studio?.status === "Ready to Claim";
  paletteItems = () => [
    step ? { id: "t-next", group: "Task", label: step.label.replace(" →", ""), key: "⌘⇧↵", run: goNext } : null,
    { id: "t-studio", group: "Task", label: `Open ${s.task_name} in Studio`, key: "o", run: studio },
    { id: "t-ref", group: "Task", label: refShown ? "Hide the reference pane" : "Show the reference pane", key: "⌥R", run: () => setRefShown((v) => !v) },
    canSkip ? { id: "t-skip", group: "Task", label: "Skip calibration (asks twice)", run: () => arm("skip", 8000) } : null,
    runs.length ? { id: "t-clean", group: "Task", label: `Clean lanes for ${s.label} (asks twice)`, run: () => arm("clean", 8000),
                    enabled: !here, why: here ? "the lane is running this task" : undefined } : null,
    ...runs.filter((r) => r.state === "exited").map((r) => ({ id: `t-collect-${r.name}`, group: "Task", label: `Collect ${runTitle(r)}`,
      run: () => post(PATH.collect(s.short), { run: r.name, pack: true }) })),
    !unclaimed ? { id: "t-unclaim", group: "Studio", label: `Unclaim ${s.task_name} in Studio (asks twice)`, run: acts.unclaim } : null,
  ].filter(Boolean);
  useEffect(() => () => { paletteItems = null; }, []);

  const run = selRun ? runs.find((r) => r.name === selRun) : null;
  const hasRun = runs.some((r) => r.facts);
  const runFirst = hasRun && (idx === 2 || idx === 5);
  const refTab = run || runFirst ? "run" : (t.lead?.files || []).length ? "code" : hasRun ? "run" : t.lead ? "research" : "code";
  return html`<section class="tp">
    <${Header} t=${t} here=${here} runs=${runs} step=${step} reason=${next?.reason} onNext=${goNext} />
    <div class="tp-body">
      <${Left} t=${t} idx=${idx} here=${here} runs=${runs} canSkip=${canSkip} selRun=${selRun} setSelRun=${setSelRun}
        armed=${armed} unclaimArm=${unclaimArm} acts=${acts} watch=${watch} subRun=${subRun} />
      <${Fields} t=${t} idx=${idx} keys=${keys} open=${open} toggle=${toggle} autoKey=${autoKey} armed=${armed} arm=${arm} disarm=${disarm}
        checklist=${checklist} onFix=${fix} />
      ${refShown ? html`<div class="tp-right">
        <${Reference} key=${selRun || "all"} target=${`t:${s.short}`} lead=${t.lead} opportunity=${t.opportunity} runs=${run ? [run] : runs}
          defaultTab=${refTab} />
        <button class="tp-refx" onClick=${() => setRefShown(false)} title="Hide the reference pane"><${Kbd} k="⌥R" /></button>
      </div>` : html`<button class="tp-refshow" onClick=${() => setRefShown(true)} title="Show the reference pane">
        <span>Reference</span><${Kbd} k="⌥R" /></button>`}
    </div>
  </section>`;
}
