// Writes from surfaces: one way to call a route, show its result, follow the job it starts, and
// run the two-press confirm (contract 5.1).
import { useEffect, useRef, useState } from "preact/hooks";
import { api } from "./api.js";
import { toast, putJob, waitJob, jobFor, isLive, jobs, refresh, loadTask } from "./store.js";

/** Show what a write answered: the server's toast, else `what` (a job's progress shows where it was started). */
export function report(r, what) {
  if (r?.job) putJob(r.job);
  if (r?.toast) { toast(r.toast.text, r.toast); return; }
  if (r?.undo) { toast(what || "Done", { undo: r.undo }); return; }
  if (what) toast(what, { level: r?.job ? "info" : "ok" });
}

/** A failed write, in Austin's words where the server gave them. Returns the error code. */
export function reportError(e, what) {
  const d = e?.details || {};
  let text = e?.message || `${what} failed`;
  if (e?.code === "studio_error" && d.status) text = `Studio answered ${d.status}: ${e.message}`;
  toast(text, { level: "error" });
  return e?.code || "error";
}

/**
 * POST (or another method) and report. Resolves to the answer, or null on an error.
 * With `wait: true` a 202 job is followed to its end and the finished job is returned.
 */
export async function act(path, body = {}, { method = "POST", what = "", wait = false, quiet = false } = {}) {
  try {
    const r = await api.send(method, path, body);
    if (!quiet) report(r, what);
    if (wait && r?.job) return { ...r, job: await waitJob(r.job) };
    return r ?? {};
  } catch (e) {
    reportError(e, what);
    return null;
  }
}

/**
 * Two-press button state for one route. `press()` sends the first call; when the server arms it,
 * `armed` holds the server's summary until the window closes; the next `press()` sends the nonce.
 */
export function useConfirm(method, path) {
  const [armed, setArmed] = useState(null); // {summary, until, details}
  const live = useRef(true);
  useEffect(() => () => { live.current = false; api.disarm(method, path); }, [method, path]);
  async function press(body = {}, { what = "", onDone, onError } = {}) {
    try {
      const r = await api.confirmable(method, path, body, {
        onArm: (summary, secs, details) => live.current && setArmed({ summary, until: Date.now() + secs * 1000, details }),
        onDisarm: () => live.current && setArmed(null),
      });
      if (r?.armed) return r;
      report(r, what);
      onDone?.(r);
      return r;
    } catch (e) {
      if (live.current) setArmed(null);
      reportError(e, what);
      onError?.(e);
      return null;
    }
  }
  return { armed, press, cancel: () => { api.disarm(method, path); setArmed(null); } };
}

/** The live (queued or running) job of these kinds for a task, or null. Re-renders on job events. */
export function useJob(kinds, short) {
  jobs.value;
  const j = jobFor(kinds, short);
  return isLive(j) ? j : null;
}

/** The newest job of these kinds for a task whether or not it finished. */
export function useLastJob(kinds, short) {
  jobs.value;
  return jobFor(kinds, short);
}

/** After a write that changes the task, refetch it and the state (SSE normally does this first). */
export async function resync(short) {
  await Promise.allSettled([refresh(), short ? loadTask(short, true) : null]);
}
