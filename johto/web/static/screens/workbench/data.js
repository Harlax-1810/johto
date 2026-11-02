// The task page's data and the writes its buttons make. Every write is one of Austin's presses; the app never
// moves a task on by itself. Reads: GET /api/tasks/{short} (fields, runs) and GET /api/tasks/{short}/workbench
// (everything around the fields, in words).
import { useEffect, useRef, useState } from "preact/hooks";
import { api } from "../../lib/api.js";
import { tasks, loadTask, lane, toast, putJob, waitJob, openDrawer, app, isLive, jobFor, jobs } from "../../lib/store.js";
import { report, reportError } from "../../lib/actions.js";
import { flushFields } from "../../components/field.js";
import { setPrimaryHandler } from "../../app.js";
import { startDrop } from "../surfaces/decide.js";

export const isDemo = () => /(?:^|;\s*)johto_mode=demo\b/.test(document.cookie);

/** A server message for the first layer: no command lines or backticked code. */
export function plainMessage(m) {
  return String(m || "")
    .replace(/[^.!?]*`johto[^`]*`[^.!?]*[.!?]?/g, " ")
    .replace(/`[^`]*`/g, " ")
    .replace(/\s+([.,;:])/g, "$1").replace(/\s{2,}/g, " ").trim() || "That didn't work.";
}

const fail = (e, what) => toast(plainMessage(e?.message || what), { level: "error" });

/** The task detail and its workbench payload, refetched whenever the task changes (SSE refetches the task). */
export function useWorkbench(short) {
  const [wb, setWb] = useState(null);
  const [err, setErr] = useState(null);
  const seq = useRef(0);
  const t = tasks.value.get(short);
  const reload = async () => {
    const n = ++seq.current;
    try {
      const w = await api.get(`/api/tasks/${short}/workbench`);
      if (n === seq.current) { setWb(w); setErr(null); }
    } catch (e) { if (n === seq.current) setErr(e); }
  };
  useEffect(() => {
    setWb(null); setErr(null);
    loadTask(short).catch((e) => setErr(e));
    reload();
  }, [short]);
  useEffect(() => {
    if (!t) return undefined;
    const id = setTimeout(reload, 150);
    return () => clearTimeout(id);
  }, [t]);
  const l = lane.value;
  const laneKey = `${l?.state || ""}:${l?.run?.short || ""}:${l?.run?.run || ""}`;
  useEffect(() => { if (wb) reload(); }, [laneKey]);
  return { wb, task: t, err, reload };
}

/** Unaccepted red overlaps in the opening prompt: the run and the prompt's save to Studio wait on them. */
export const promptRed = (task) => (task?.sections?.opening_prompt?.overlap || [])
  .filter((o) => o.level === "red" && !o.accepted).length;
export const PROMPT_RED_WHY = "The run waits until you reword the words that match AI text, or mark them as your own.";

// ---------- the lane ----------

/** The lane, when it is running this task. */
export function laneHere(short) {
  const l = lane.value;
  if (!l || ["free", "collected"].includes(l.state)) return null;
  if (l.run) return l.run.short === short ? l : null;
  const c = (l.foreign?.container || "").toLowerCase();
  return c.includes(`prework_${String(short).toLowerCase()}-`) ? l : null;
}

/** Which task the lane is busy with, in words, or null when it is free (or busy with this task). */
export function laneBusyWith(short) {
  const l = lane.value;
  if (!l || ["free", "collected", "setup_failed"].includes(l.state) || !l.run || l.run.short === short) return null;
  const s = (app.value?.tasks || []).find((x) => x.short === l.run.short);
  const label = s?.label || l.run.label || "another task";
  return label.split(" · ")[0];
}

async function settle(r, { done } = {}) {
  if (r?.toast) toast(r.toast.text, r.toast);
  if (!r?.job) return r;
  putJob(r.job);
  const j = await waitJob(r.job);
  if (j?.state === "succeeded" || j?.state === "done") { if (done) toast(done, { level: "ok" }); }
  return j;
}

/**
 * Start the validation run (his press): the prompt's Done (no calibration, Austin's order) when the task is still on
 * the prompt, then the lane service starts the run. A busy lane queues it instead. Returns "armed" when the server
 * asks for a second press (lint errors in the prompt).
 */
// The lane refuses a validation run until Studio holds this prompt and Mercor's check passed it. These are the
// refusals in words with the one way out (api_workbench.prompt_check_view says the same for the check's own job).
const GATE_TITLES = {
  differs: "Studio needs this prompt, checked by Mercor, before the run.",
  stale: "Mercor's automatic check hasn't seen this prompt yet.",
  not_started: "Studio didn't start Mercor's check.",
  unfinished: "Mercor's check didn't finish.",
  running: "Mercor's check is running on this prompt.",
};
const GATE_CODES = new Set(["prompt_qc", "sync_conflict", "same_prompt"]);

/** A lane-gate error (from the lane start or the prompt check's job) as {reason, tone, title, why, action, failed}. */
export function gateView(err) {
  const det = err?.details || {};
  if (det.view) return det.view;
  const code = err?.code || { conflict: "sync_conflict", same_prompt: "same_prompt" }[det.reason];
  if (code === "sync_conflict") return { reason: "conflict", tone: "wait", title: "The prompt differs here and in Studio.",
    why: "It isn't clear which is newer. Make both the same, then start again.", action: "open_studio", failed: [] };
  if (code === "same_prompt") return { reason: "same_prompt", tone: "wait", title: "An earlier validation run used this prompt.",
    why: "Change the prompt for a new run, or continue the earlier one in its lane.", action: null, failed: [] };
  const reason = det.reason || err?.reason || "differs";
  if (reason === "failed") return { reason, tone: "stop", title: "Mercor's check failed the prompt.",
    why: "Change the prompt, then check it again.", action: "check", failed: det.failed || [] };
  return { reason, tone: "wait", title: GATE_TITLES[reason] || GATE_TITLES.differs,
    why: "This saves only the opening prompt to Studio and runs Mercor's check, a few minutes. The run starts if it passes.",
    action: "check", failed: [] };
}

/**
 * Save the prompt to Studio and run Mercor's check (his press of "Save to Studio, check, then start"). Resolves to
 * {ok: true} or {gate} with the refusal in words.
 */
export async function checkPrompt(short) {
  await flushFields();
  if (promptRed(tasks.peek().get(short))) { toast(PROMPT_RED_WHY, { level: "error" }); return null; }
  try {
    const r = await api.post(`/api/tasks/${short}/prompt-check`, {});
    putJob(r.job);
    const j = await waitJob(r.job);
    if (j?.state === "succeeded") { toast("The prompt passed Mercor's check.", { level: "ok" }); return { ok: true }; }
    if (j?.error?.code === "blocked_overlap") { toast(PROMPT_RED_WHY, { level: "error" }); return null; }
    return { gate: gateView(j?.error) };
  } catch (e) {
    if (GATE_CODES.has(e.code)) return { gate: gateView(e) };
    fail(e); return null;
  }
}

export async function startValidation(short, wb, { onArm } = {}) {
  await flushFields();
  const done = `/api/tasks/${short}/done`;
  try {
    if (["prompt", "claiming"].includes(wb.stage)) {
      const r = await api.confirmable("POST", done, { surface: "prompt", kind: "skip" }, { onArm });
      if (r?.armed) return "armed";
    }
  } catch (e) { fail(e); return null; }
  let r = null;
  try {
    r = await api.send("POST", "/api/lane/start", { short, kind: "validate" });
  } catch (e) {
    if (e.code === "lane_busy" || e.code === "lane_state") {
      try {
        const q = await api.confirmable("POST", done, { surface: "details" }, { onArm });
        if (q?.armed) return "armed";
        toast(`Queued. It starts when the lane frees.`, { level: "info" });
        return "queued";
      } catch (e2) { fail(e2); return null; }
    }
    if (GATE_CODES.has(e.code)) return { gate: gateView(e) };
    if (e.code === "blocked_overlap") { toast(PROMPT_RED_WHY, { level: "error" }); return null; }
    if (e.status === 404 || e.status === 405) {
      try { r = await api.send("POST", `/api/tasks/${short}/lane`, { kind: "validate" }); }
      catch (e2) { if (GATE_CODES.has(e2.code)) return { gate: gateView(e2) }; fail(e2); return null; }
    } else { fail(e); return null; }
  }
  toast("Starting the validation run", { level: "info" });
  const j = await settle(r);
  if (j?.state === "succeeded") openDrawer("row");
  else if (j && !isLive(j) && j.state === "failed") toast(plainMessage(j.error?.message || "The run didn't start"), { level: "error" });
  return j;
}

export async function takeOffQueue(short) {
  try { await api.send("DELETE", `/api/tasks/${short}/ready`); toast("Taken off the lane queue", { level: "ok" }); }
  catch (e) { fail(e); }
}

export async function endRun() {
  try { await api.post("/api/lane/end", {}); toast("Ending the run. It is collected next.", { level: "info" }); }
  catch (e) { fail(e); }
}

export async function collectRun(short, run) {
  try {
    const r = await api.post(`/api/tasks/${short}/collect`, { run, pack: true });
    toast("Collecting the run", { level: "info" });
    return settle(r, { done: "The run is collected." });
  } catch (e) { fail(e); return null; }
}

export async function addNote(short, run, text) {
  try { const r = await api.post(`/api/tasks/${short}/runs/${run}/notes`, { text }); await loadTask(short, true); return r; }
  catch (e) { fail(e); return null; }
}

// ---------- writing ----------

/**
 * Done writing (his press): the details' Done when a chosen run is waiting for them, then the ratings' Done. The
 * server checks the fields and the pass grid and answers in words.
 */
export async function finishWriting(short, wb, { onMissing, onArm } = {}) {
  await flushFields();
  const done = `/api/tasks/${short}/done`;
  try {
    if (wb.stage === "details") {
      const r = await api.confirmable("POST", done, { surface: "details" }, { onArm });
      if (r?.armed) return "armed";
    }
    await api.send("POST", done, { surface: "rate" });
    toast("Written. Next: save to Studio.", { level: "ok" });
    return "done";
  } catch (e) {
    if (e.code === "missing_fields") { onMissing?.(e.details?.fields || []); toast(e.message, { level: "error" }); return null; }
    fail(e);
    return null;
  }
}

export async function mark(short, ids, marks) {
  if (isDemo()) return { demo: true };
  try { return await api.post(`/api/tasks/${short}/grading/mark`, { ids, ...marks }); }
  catch (e) { fail(e); return null; }
}

export async function rereadReviews(short) {
  try { await api.post(`/api/tasks/${short}/grading/refresh`, {}); toast("Read the reviews again", { level: "ok" }); }
  catch (e) { fail(e); }
}

/** Pack the newest unreviewed turns and copy the pack (his press). Resolves to the finished job. */
export async function reviewLatest(short) {
  try {
    const r = await api.post(`/api/tasks/${short}/review-latest`, {});
    if (r?.toast) { toast(r.toast.text, r.toast); return null; }
    putJob(r.job);
    const j = await waitJob(r.job);
    if (j?.state === "succeeded") toast(j.result?.text || "Copied. Paste it into GPT Pro.", { level: "ok" });
    else if (j?.state === "failed") toast(plainMessage(j.error?.message || "The review pack wasn't built"), { level: "error" });
    return j;
  } catch (e) { fail(e); return null; }
}

export async function saveReview(short, text, turn) {
  try {
    const r = await api.post(`/api/tasks/${short}/reviews`, { text, source: "gptpro", ...(turn ? { turn } : {}) });
    report(r);
    return r;
  } catch (e) { fail(e); return null; }
}

/** Drop the task (his press, with the 10 s undo): unclaims it in Studio, removes its lanes, opens the form. */
export function dropTask(short, task, why) {
  const run = task?.submission_run || (task?.runs || []).filter((r) => r.kind === "validate").slice(-1)[0]?.name;
  const name = task?.summary?.label || "the task";
  return startDrop({ short, run, label: `${why ? `${why}; ` : ""}unclaims ${name} in Studio and removes its lanes` });
}

// ---------- Studio ----------

/** Save to Studio (his press). Red overlap blocks it here, as the server does. */
export async function saveToStudio(short, task, { onRed } = {}) {
  const red = Object.entries(task?.sections || {}).flatMap(([k, s]) => (s?.overlap || [])
    .filter((o) => o.level === "red" && !o.accepted).map((o) => ({ ...o, key: k })));
  if (red.length) {
    toast("A field repeats AI text word for word. Reword it or mark it as your own, then save.", { level: "error" });
    onRed?.(red[0]);
    return null;
  }
  await flushFields();
  try {
    const r = await api.post(`/api/tasks/${short}/studio/save`, {});
    report(r, "Saving to Studio");
    return r;
  } catch (e) { reportError({ ...e, message: plainMessage(e.message) }, "Save to Studio"); return null; }
}

export async function rerunQc(short) {
  try { report(await api.post(`/api/tasks/${short}/studio/qc`, { spec: "AutoQC" }), "Mercor's checks started"); }
  catch (e) { fail(e); }
}

export async function refreshStudio(short) {
  try { report(await api.post(`/api/tasks/${short}/studio/refresh`, {}), "Reading Studio"); }
  catch (e) { fail(e); }
}

export async function deepCheck(short) {
  try {
    const r = await api.post(`/api/tasks/${short}/check`, { deep: true });
    if (r?.job) { putJob(r.job); toast("Deep check started. It reads your fields and lists issues, never rewrites.", { level: "info" }); }
    else report(r);
    return r;
  } catch (e) { fail(e); return null; }
}

export async function revealLog(short) {
  try { report(await api.post(`/api/tasks/${short}/reveal-log`, {})); }
  catch (e) { fail(e); }
}

/** Register this step's primary action for ⌘⇧↵ while the step is on screen. */
export function usePrimary(fn) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => { setPrimaryHandler(() => ref.current?.()); return () => setPrimaryHandler(null); }, []);
}

export function useLiveJob(kinds, short) {
  jobs.value;
  const j = jobFor(kinds, short);
  return isLive(j) ? j : null;
}
