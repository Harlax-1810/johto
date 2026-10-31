import { useEffect, useRef } from "preact/hooks";
import { html } from "../../lib/html.js";
import { Surface } from "../../components/surface.js";
import { Chip } from "../../components/ui.js";
import { act } from "../../lib/actions.js";
import { clock, fullSlot, mins, plural } from "../../lib/format.js";
import { drawer, lane, openDrawer, tick, toast, dismissToast, ENDING_TOAST } from "../../lib/store.js";
import { pushContext } from "../../lib/keys.js";
import { focusTerminal } from "../../components/drawer.js";

// Lane surface: shown behind the drawer when a lane row is selected. With the drawer open it
// shrinks to the strip the drawer leaves visible and keeps only what the drawer does not show:
// his goal and decision points, which he needs while he replies.

const STATE = {
  waiting: ["amber", "Waiting for you", true],
  permission: ["amber", "Asks permission", true],
  needs_login: ["amber", "Needs /login", true],
  ready_for_prompt: ["amber", "Ready for your prompt", true],
  working: ["blue", "Claude working", true],
  preparing: ["neutral", "Setting up", false],
  exited: ["neutral", "Collecting", false],
  collecting: ["neutral", "Collecting", false],
  setup_failed: ["red", "Setup failed", false],
  free: ["neutral", "Lane free", false],
};

/** The one extra action some lane states have, besides opening the lane. Each says what it types. */
function extraAction(l) {
  const post = (path, body, what) => async () => {
    const r = await act(path, body, { quiet: true });
    if (r == null) return;
    if (r.toast) toast(r.toast.text, r.toast); else if (what) toast(what);
  };
  if (l.state === "needs_login") return { key: "l", label: "Type /login", run: post("/api/lane/type", { what: "login" }, "Typed /login. Finish signing in in the browser tab it opens.") };
  if (l.state === "ready_for_prompt" && l.input?.empty !== false) return { key: "p", label: "Paste my prompt", run: post("/api/lane/type", { what: "prompt" }, "Pasted your saved Opening Prompt. Press Enter in the lane to send it.") };
  // The automatic collect starts within seconds of Claude exiting; offer it by hand only if it didn't.
  if (l.run && l.state === "exited" && Date.now() - Date.parse(l.since || 0) > 30000) {
    return { key: "c", label: "Collect now", run: post("/api/lane/collect", {}, "Collecting the run"),
      title: "Copies the diff and transcript out of the lane, then builds the review pack" };
  }
  if (l.run && l.state === "waiting") {
    // Ending Claude's session takes two presses of ⇧E (e alone means Edit prompt elsewhere).
    const name = runName(l.run);
    return { key: "E", label: endArmed() ? `Press again to end ${name}` : "End run and collect", armed: endArmed(),
      run: async () => {
        if (!endArmed()) {
          endArmedAt = Date.now();
          toast(`Press ⇧E again to end ${name}. It types /exit; the run is collected next.`, { id: "end-arm", ms: 3000 });
          return;
        }
        endArmedAt = 0;
        dismissToast("end-arm");
        const r = await act("/api/lane/end", {}, { quiet: true });
        if (r == null) return;
        toast(r.toast?.text || `Ending ${name}. It is collected and its review pack built next.`, { ...(r.toast || {}), id: ENDING_TOAST });
      },
      title: "Types /exit in the lane; the run is then collected and its review pack built (press twice)" };
  }
  if (l.state === "free" && l.queue?.length) {
    const q = l.queue[0];
    return { key: "s", label: `Start ${q.kind === "calib" ? "calibration" : "validation"}`, run: post("/api/lane/start", { short: q.short, kind: q.kind }, "Setting up the lane") };
  }
  if (l.state === "setup_failed" && l.run) {
    return { key: "r", label: "Retry in a new folder", run: post("/api/lane/start", { short: l.run.short, kind: l.run.kind }, "Setting up the lane again") };
  }
  return null;
}

let endArmedAt = 0;
const endArmed = () => Date.now() - endArmedAt < 3000;

function runName(run) {
  if (!run) return "";
  return `${run.kind === "calib" ? "calibration" : "validation"} ${(run.run || "").split("-")[1] || ""}`.trim();
}

export default function LaneSurface({ item, task, setPrimary }) {
  tick.value;
  const l = lane.value;
  const d = drawer.value;
  const run = l.run;
  const f = l.facts;
  const short = task?.summary?.short || item.short;
  const [tone, stateLabel, timed] = STATE[l.state] || ["neutral", l.state, false];
  const extra = extraAction(l);
  const open = () => {
    const was = drawer.value.open;
    if (!was) openDrawer("row");
    setTimeout(() => focusTerminal(), was ? 0 : 160);
  };

  const live = useRef({});
  live.current = { open, extra };
  useEffect(() => {
    setPrimary?.(() => live.current.open());
    const pop = pushContext(`lane:${item.id}`, {
      Enter: () => live.current.open(),
      ...(extra ? { [extra.key]: () => live.current.extra?.run() } : {}),
    });
    return () => { pop(); setPrimary?.(null); };
  }, [item.id, extra?.key]);

  const budget = f?.task_claude_min ?? null;
  const budgetTone = budget >= 90 ? "red" : budget >= 60 ? "amber" : "";
  const runNotes = (task?.runs || []).find((r) => r.name === run?.run)?.notes || [];
  const prompt = task?.sections?.opening_prompt;
  const goal = task?.sections?.goal;
  const dps = task?.sections?.decision_points;

  // What Claude put in front of him, verbatim.
  let ask = null;
  if (l.state === "waiting" && l.question) ask = { label: "Claude asks", text: l.question };
  else if (l.state === "permission") ask = { label: "Claude wants to run", text: l.evidence || "", mono: true };
  else if (l.state === "ready_for_prompt" && prompt?.text) ask = { label: "Your opening prompt, as saved", text: prompt.text, mine: true };

  const narrow = d.open && !d.full;
  // The state chip carries a live clock, so the queue's fixed "waited 1:12" would only disagree with it.
  const reason = narrow || (timed && l.since) ? "" : item.reason;
  return html`<div class=${`sb-lane ${narrow ? "sb-narrow" : ""}`}>
    <${Surface} title=${item.title} label=${task?.summary?.label || item.label} dossier=${task?.summary?.dossier}
        slotLine=${narrow ? "" : task ? fullSlot(task.summary.slot) : ""} reason=${reason}
        primary=${{ label: "Open the lane", run: open, why: "Opens the lane with the terminal focused (also ⌃`)" }}
        secondary=${extra ? [{ label: extra.label, key: extra.key === "E" ? "⇧E" : extra.key, run: extra.run, title: extra.title,
                               kind: extra.armed ? "armed" : undefined }] : []}>
      <div class="sbody">
        <div class="scol sb-lbody">
          <div class="sb-lmain">
            <div class="sb-lstate">
              <${Chip} tone=${tone}>${stateLabel}${timed && l.since ? html` <span class="num">${clock(l.since)}</span>` : null}<//>
              <span>${[runName(run), f ? `turn ${f.turns}` : ""].filter(Boolean).join(" · ")}</span>
              ${f ? (f.model_ok !== false && f.effort_ok !== false
                ? html`<span class="muted">Opus 5.5 · high ✓</span>`
                : html`<${Chip} tone="red">model or effort wrong<//>`) : null}
              ${l.input_locked ? html`<${Chip} tone="neutral">Input locked (async)<//>` : null}
            </div>

            ${ask ? html`<section class=${`sb-ask ${ask.mine ? "mine" : ""}`}>
              <div class="label">${ask.label}</div>
              <blockquote class=${ask.mono ? "mono" : ""}>${ask.text}</blockquote>
            </section>` : null}

            ${f ? html`<dl class="sb-lfacts">
              ${budget != null ? html`<dt>Claude on task</dt>
                <dd><span class=${budgetTone ? `chip ${budgetTone}` : ""}><b class="num">${mins(budget)}</b> of 90 min</span>
                  <span class="faint">${" · "}${mins(f.active_min)} min this run</span></dd>` : null}
              ${f.tests?.runs ? html`<dt>Tests</dt><dd>${plural(f.tests.runs, "run")}, last${" "}
                ${f.tests.last_ok ? html`<span class="sb-ok">passed</span>` : html`<span class="sb-bad">failed</span>`}
                ${f.tests.after_last_edit === false ? html`<span class="faint"> · before the last edit</span>` : null}</dd>` : null}
              ${(f.files_edited || []).length ? html`<dt>Files edited</dt>
                <dd class="sb-files">${f.files_edited.map((p) => html`<code>${p}</code>`)}</dd>` : null}
              ${runNotes.length ? html`<dt>Your notes</dt><dd>${runNotes.map((n) => html`<div class="sb-note">
                <span class="faint num">${Math.floor(n.t_run_s / 60)}:${String(n.t_run_s % 60).padStart(2, "0")}</span><span>${n.text}</span></div>`)}</dd>` : null}
            </dl>` : null}
          </div>

          ${goal?.text || dps?.text ? html`<div class="sb-mine">
            ${goal?.text ? html`<section><div class="label">Your goal</div><p>${goal.text}</p></section>` : null}
            ${dps?.text ? html`<section><div class="label">Your decision points</div><p>${dps.text}</p></section>` : null}
          </div>` : null}
        </div>
      </div>
    <//>
  </div>`;
}
