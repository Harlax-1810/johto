// The Validation run step (spec 3.5.2): Claude's state in words, one primary action, the goal editor and his run
// notes beside it. Run ids, paths, commands and the model check live only in Details.
import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { AiBlock, Btn, Dot, Sheet, Flag } from "../../components/primitives.js";
import { Editor, NO_ASSIST } from "../../components/field.js";
import { PackButton } from "../../components/pack.js";
import { focusTerminal } from "../../components/drawer.js";
import { api } from "../../lib/api.js";
import { openDrawer, toast, putJob, jobLine } from "../../lib/store.js";
import { report } from "../../lib/actions.js";
import { startValidation, takeOffQueue, endRun, collectRun, addNote, laneHere, laneBusyWith, useLiveJob, isDemo, plainMessage, usePrimary, promptRed, PROMPT_RED_WHY } from "./data.js";
import { Asks } from "./write.js";

const pad2 = (n) => String(n).padStart(2, "0");
const clockOf = (iso) => { const d = new Date(iso); return Number.isNaN(+d) ? "" : `${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };

/** The run's facts as one line of words: "Claude 24 of 90 min · tests passed · changed 3 files · 3 turns". */
function factsLine(run) {
  const bits = [];
  // The task's Claude minutes against its budget (the figure Now and the lane drawer show).
  const cm = run.claude_min ?? run.minutes;
  if (cm != null) bits.push(`Claude ${cm} of ${run.budget || 90} min`);
  if (run.turns) bits.push(`${run.turns} turn${run.turns === 1 ? "" : "s"}`);
  if (run.tests_words) bits.push(run.tests_words);   // a result hidden by a pipe says nothing (never "failed")
  if (run.changed_files != null) bits.push(run.changed_files ? `changed ${run.changed_files} file${run.changed_files === 1 ? "" : "s"}` : "no files changed");
  return bits.join(" · ");
}

/** The run's primary action, by its state. Returns {label, run, kind, k, why} or a status line. */
export function runPrimary(short, wb, task, { onWrite, onArm } = {}) {
  const run = wb.run || {};
  const here = laneHere(short);
  // On the Prompt step a run that isn't a validation run (a calibration run) leaves the validation run to start.
  const startable = ["none", "queued"].includes(run.state)
    || (wb.step === "prompt" && run.kind !== "validate" && !["working", "asks", "collecting", "ended"].includes(run.state));
  if (startable) {
    if (wb.stage === "waiting_validate") return { label: "Take it off the queue", run: () => takeOffQueue(short), kind: "secondary" };
    const empty = !(task.sections?.opening_prompt?.text || "").trim();
    const red = !empty && promptRed(task) > 0;
    const busy = laneBusyWith(short);
    return { label: busy ? "Queue the validation run" : "Start the validation run", k: "⌘⇧↵", kind: "primary",
      disabled: empty || red, why: empty ? "Write the opening prompt first." : red ? PROMPT_RED_WHY
        : busy ? `The lane is running ${busy}. It starts when the lane frees.` : "",
      run: () => startValidation(short, wb, { onArm }) };
  }
  if (run.state === "asks") return { label: "Answer in the lane", k: "⌘⇧↵", kind: "primary", run: () => { openDrawer("row"); setTimeout(() => focusTerminal(), 160); } };
  if (run.state === "working") {
    if (run.lane === "elsewhere") return { status: "Claude is working on the PC." };
    return { label: "Open the lane", k: "⌘⇧↵", kind: "primary", run: () => openDrawer("user") };
  }
  if (run.state === "collect_failed") return { label: "Collect the run", k: "⌘⇧↵", kind: "primary", run: () => collectRun(short, run.id) };
  if (run.state === "collecting" || run.state === "ended") return { status: "Collecting the run." };
  if (wb.stage === "decide") return { label: "Keep the task", k: "⌘⇧↵", kind: "primary", run: () => decide(short, run.id, "keep") };
  return { label: "Write the goal →", k: "⌘⇧↵", kind: "primary", run: onWrite };
}

async function decide(short, run, decision) {
  try {
    const r = await api.post(`/api/tasks/${short}/decision`, { run, decision });
    report(r, decision === "keep" ? "Kept. Next: the validation run." : "Dropping the task");
  } catch (e) { toast(plainMessage(e.message), { level: "error" }); }
}

function Notes({ short, run }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const notes = run?.notes || [];
  if (!run?.id) return null;
  async function add(e) {
    e?.preventDefault();
    if (!text.trim() || busy) return;
    setBusy(true);
    const r = await addNote(short, run.id, text.trim());
    setBusy(false);
    if (r) setText("");
  }
  return html`<section class="wb-notes">
    <h3 class="wb-h3">Run notes</h3>
    <p class="faint wb-notes-why">Your own notes while Claude works. They help you write the validation notes later.</p>
    ${notes.length ? html`<ul class="wb-notes-list">${notes.map((n) => html`<li><span class="num faint">${clockOf(n.at)}</span><span>${n.text}</span></li>`)}</ul>` : null}
    <form class="wb-notes-add" onSubmit=${add}>
      <input type="text" value=${text} ...${NO_ASSIST} aria-label="Add a run note" onInput=${(e) => setText(e.currentTarget.value)} />
      <${Btn} size="sm" label="Add" type="submit" disabled=${!text.trim() || busy} />
    </form>
  </section>`;
}

/**
 * The last test run in words, for Details. A pipe hides the exit code (facts.py sets last_ok to null): that reads
 * "unknown (piped)", never failed. Only last_ok === false is a failing run.
 */
function testsLine(task, runId) {
  const t = (task.runs || []).find((r) => r.name === runId)?.facts?.tests;
  if (!t || !t.runs) return "";
  const n = `Claude ran the tests ${t.runs} time${t.runs === 1 ? "" : "s"}.`;
  if (t.last_ok === false) return `${n} The last run was failing.`;
  if (t.last_ok === true) return `${n} The last run passed.`;
  return `${n} The last run's result is unknown (piped).`;
}

/** Details: the run record, one click away (transcript as prose, files changed, the model check, packs). */
function Details({ short, wb, task, open, onClose }) {
  const run = wb.run || {};
  const d = run.drill || {};
  const [entries, setEntries] = useState(null);
  useEffect(() => {
    if (!open || !run.id) return;
    setEntries(null);
    api.get(`/api/tasks/${short}/runs/${run.id}/transcript?limit=600`).then((r) => setEntries(r.entries || [])).catch(() => setEntries([]));
  }, [open, run.id]);
  const prose = [];
  let tools = 0;
  for (const e of entries || []) {
    if (e.role === "tool") { tools++; continue; }
    if (!(e.text || "").trim()) continue;
    if (tools) { prose.push({ role: "tools", n: tools }); tools = 0; }
    prose.push(e);
  }
  if (tools) prose.push({ role: "tools", n: tools });
  const others = (run.runs || []).filter((r) => r.collected && !r.chosen);
  async function useRun(id) {
    try { report(await api.post(`/api/tasks/${short}/submission-run`, { run: id }), "Studio will use that run"); }
    catch (e) { toast(plainMessage(e.message), { level: "error" }); }
  }
  return html`<${Sheet} open=${open} onClose=${onClose} title=${`Details of ${run.name_words || "the run"}`} width=${720}
      meta=${html`<span>${run.state_words}</span>${d.model_words ? html`<span>${d.model_words}</span>` : null}`}>
    <section class="wb-det">
      <h3 class="wb-h3">What changed</h3>
      ${(d.files || []).length ? html`<ul class="wb-files">${d.files.map((f) => html`<li><span class="mono">${f.path}</span>
          <span class="num ok">+${f.add ?? "?"}</span><span class="num stop">−${f.del ?? "?"}</span>${f.new ? html`<span class="faint">new</span>` : null}${f.test ? html`<span class="faint">test</span>` : null}</li>`)}</ul>`
        : html`<p class="faint">No files changed.</p>`}
      ${testsLine(task, run.id) ? html`<p class="faint">${testsLine(task, run.id)}</p>` : null}
    </section>
    ${(run.runs || []).length > 1 ? html`<section class="wb-det">
      <h3 class="wb-h3">Runs</h3>
      <ul class="wb-runs">${run.runs.map((r) => html`<li><span>${r.name_words}</span><span class="faint">${r.state_words}</span>
        ${r.chosen ? html`<span class="pill ok">Studio uses this run</span>` : r.collected ? html`<button type="button" class="btn sm" onClick=${() => useRun(r.id)}>Use this run for Studio</button>` : null}</li>`)}</ul>
    </section>` : null}
    ${run.id && !isDemo() ? html`<section class="wb-det"><h3 class="wb-h3">Review pack</h3><${PackButton} short=${short} run=${run.id} /></section>` : null}
    <section class="wb-det">
      <h3 class="wb-h3">The conversation</h3>
      ${entries == null ? html`<p class="faint">Reading the transcript…</p>` : !prose.length ? html`<p class="faint">No transcript yet.</p>`
        : html`<div class="wb-tr">${prose.map((e) => e.role === "tools" ? html`<p class="wb-tr-tools">Claude ran ${e.n} command${e.n === 1 ? "" : "s"}</p>`
          : html`<div class=${`wb-tr-e ${e.role}`}><div class="label-sm">${e.role === "human" ? "You" : "Claude"}${e.turn ? ` · turn ${e.turn}` : ""}</div><p>${e.text}</p></div>`)}</div>`}
    </section>
  <//>`;
}

export function RunStep({ short, task, wb, onWrite, readOnly, detailsReq }) {
  const run = wb.run || {};
  const [details, setDetails] = useState(false);
  const [armed, setArmed] = useState(null);
  const [ending, setEnding] = useState(false);
  const endTimer = useRef(null);
  const prep = useLiveJob(["lane_prepare", "lane_start"], short);
  const collecting = useLiveJob(["lane_collect", "collect"], short);
  const p = runPrimary(short, wb, task, { onWrite, onArm: (s) => setArmed(s || "Press again to start anyway") });
  const here = laneHere(short);
  const goal = task.sections?.goal;
  useEffect(() => () => clearTimeout(endTimer.current), []);
  const pressEnd = () => {
    if (!ending) { setEnding(true); endTimer.current = setTimeout(() => setEnding(false), 4000); return; }
    clearTimeout(endTimer.current); setEnding(false); endRun();
  };
  const asks = run.state === "asks";
  const live = ["working", "asks"].includes(run.state);
  const tone = asks ? "wait" : live ? "work" : run.state === "collect_failed" ? "stop" : run.state === "collected" ? "ok" : "idle";
  const job = prep || collecting;
  const past = wb.step !== "run" && run.state === "collected";   // looking back at a finished run: nothing to press
  const press = async () => {
    if (readOnly || past || p.status || p.disabled || job) return;
    const r = await p.run();
    if (r !== "armed") setArmed(null);
  };
  usePrimary(press);
  useEffect(() => { if (detailsReq) setDetails(true); }, [detailsReq]);
  return html`<div class="wb-cols">
    <div class="wb-col">
      <div class=${`wb-runcard ${asks ? "asks" : ""}`}>
        <div class="wb-runhead"><${Dot} tone=${tone} /><span class="wb-runname">${run.name_words ? run.name_words.charAt(0).toUpperCase() + run.name_words.slice(1) : "The validation run"}</span>
          <span class="faint">${run.state_words}</span></div>
        ${asks ? html`<${AiBlock} label="Claude asked">
            ${run.claude_message && run.claude_message !== run.question ? html`<p class="wb-claude">${run.claude_message}</p>` : null}
            ${run.question ? html`<blockquote class="nw-ask">${run.question}</blockquote>` : null}
            ${!run.question && !run.claude_message ? html`<p class="wb-claude faint">Open the lane to read what Claude asked.</p>` : null}<//>`
          : run.claude_message ? html`<${AiBlock} label="Claude's last message">
            <p class="wb-claude">${run.claude_message}</p><//>` : null}
        ${factsLine(run) ? html`<p class="wb-facts">${factsLine(run)}</p>` : null}
        ${job ? html`<p class="wb-job"><${Dot} tone="busy" />${jobLine(job)}</p>` : null}
        ${readOnly ? null : html`<div class="wb-acts">
          ${past ? null : p.status ? html`<span class="wb-status">${p.status}</span>` : html`<${Btn} kind=${armed ? "danger" : p.kind} label=${armed || p.label} k=${p.k}
              disabled=${p.disabled || !!job} title=${p.why} onClick=${press} />`}
          ${live && here ? html`<${Btn} kind=${ending ? "danger" : "secondary"} label=${ending ? "Press again to end the run" : "End the run"} onClick=${pressEnd}
              title="Ends Claude's session in the lane. The run is collected next." />` : null}
          ${wb.stage === "decide" ? html`<${Btn} kind="quiet" label="Drop the task" onClick=${() => decide(short, run.id, "drop")} />` : null}
          ${run.id ? html`<${Btn} kind="quiet" label="Details" onClick=${() => setDetails(true)} />` : null}
        </div>`}
        ${p.why && !p.disabled ? html`<p class="wb-why">${p.why}</p>` : p.disabled && p.why ? html`<p class="wb-why">${p.why}</p>` : null}
      </div>
    </div>
    <div class="wb-col">
      ${goal ? html`<section class="wb-side-ed">
        <div class="wb-fhead"><h3 class="wb-h3">High-level goal</h3><${Asks} k="goal" /></div>
        ${live || ["none", "queued"].includes(run.state) ? html`<p class="faint wb-side-why">You may write it while the run works.</p>` : null}
        <${Editor} key="goal" short=${short} sectionKey="goal" section=${goal} minRows=${4} label="High-level goal" readOnly=${readOnly} compact=${true} noLint=${true} />
      </section>` : null}
      <${Notes} short=${short} run=${run} />
    </div>
    <${Details} short=${short} wb=${wb} task=${task} open=${details} onClose=${() => setDetails(false)} />
  </div>`;
}
