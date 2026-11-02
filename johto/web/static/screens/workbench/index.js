// The task page (spec.md 3.5). One task, four steps: Prompt, Validation run, Write, Submit, then Review. The header
// names the task; the path shows where it is; the step's body has one primary action (⌘⇧↵). Every write to Studio
// is his press. Owned by the workspace builder; app.js renders <Workbench short=... /> inside the Shell.
import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { hash, toast, patchTask, navigate, route } from "../../lib/store.js";
import { Btn, Menu, PathStrip, Stage, Empty } from "../../components/primitives.js";
import { registerPalette } from "../../components/overlays.js";
import { pushContext } from "../../lib/keys.js";
import { flushFields } from "../../components/field.js";
import { useWorkbench, saveToStudio, rerunQc, refreshStudio, deepCheck, revealLog, collectRun, laneHere,
  plainMessage, useLiveJob, finishWriting, dropTask } from "./data.js";
import { slotLine, FIELD_NAME, FIELD_LOWER } from "./words.js";
import { sourceWords } from "../../components/field.js";
import { drops } from "../surfaces/decide.js";
import { PromptStep } from "./prompt.js";
import { RunStep } from "./run.js";
import { WriteStep } from "./write.js";
import { SubmitStep } from "./submit.js";
import { AfterStep } from "./after.js";
import { UnderstandPage } from "./understand.js";

const STEPS = ["prompt", "run", "write", "submit", "review"];

// The page's palette commands, while it is on screen (the Header fills this in).
let paletteItems = null;
registerPalette(() => (paletteItems ? paletteItems() : []));
const AFTER = new Set(["in_review", "accepted", "rejected", "dropped", "discarded", "unclaimed"]);

/** The title: click to rename (local only). */
function Title({ short, title }) {
  const [editing, setEditing] = useState(false);
  const [v, setV] = useState(title);
  async function save() {
    setEditing(false);
    const label = v.trim();
    if (!label || label === title) return;
    patchTask(short, (c) => { c.summary.label = label; });
    try { await api.send("PUT", `/api/tasks/${short}/label`, { label }); }
    catch (e) { toast(plainMessage(e.message), { level: "error" }); }
  }
  if (editing) {
    return html`<input class="wb-rename" value=${v} autofocus aria-label="Task name" spellcheck=${false}
      onInput=${(e) => setV(e.currentTarget.value)} onBlur=${save}
      onKeyDown=${(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") { setV(title); setEditing(false); } }} />`;
  }
  return html`<h1 class="wb-title" title="Rename (only in this app)" tabindex="0"
    onClick=${() => { setV(title); setEditing(true); }}
    onKeyDown=${(e) => { if (e.key === "Enter") { setV(title); setEditing(true); } }}>${title}</h1>`;
}

/** A two-press action started from the ⋯ menu: the second press is a red button in the header. */
function useHeaderArm() {
  const [armed, setArmed] = useState(null);   // {label, run, title}
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);
  const arm = (label, run, secs = 6, title = "") => {
    clearTimeout(timer.current);
    setArmed({ label, run, title });
    timer.current = setTimeout(() => setArmed(null), secs * 1000);
  };
  const clear = () => { clearTimeout(timer.current); setArmed(null); };
  return [armed, arm, clear];
}

/** The first unaccepted red overlap (his words repeating AI text), as {key, o}, or null. */
export function firstRed(task) {
  for (const [key, sec] of Object.entries(task?.sections || {})) {
    const o = (sec?.overlap || []).find((x) => x.level === "red" && !x.accepted);
    if (o) return { key, o };
  }
  return null;
}

/** "Save waits: your decision points repeat a line from Claude's research" */
function redWhy(red) {
  const name = FIELD_LOWER[red.key] || "a field";
  const plural = ["decision_points", "validation_notes", "reviewer_notes"].includes(red.key);
  return `Save waits: your ${name} ${plural ? "repeat" : "repeats"} ${sourceWords(red.o.source)} word for word`;
}


function Header({ short, task, wb, view, onSave, onAction, onDetails, readOnly }) {
  const [armed, arm, clear] = useHeaderArm();
  const saving = useLiveJob(["save", "studio_save"], short);
  const st = wb.studio || {};
  const fromWrite = ["write", "submit"].includes(wb.step) && !readOnly;
  const saveHere = fromWrite && view !== "submit";   // the Submit step has its own Save to Studio
  const runs = (wb.run?.runs || []).filter((r) => r.collected);
  const here = !!laneHere(short);
  const mine = !AFTER.has(wb.stage) && st.status !== "Ready to Claim";
  const discardOpen = !!task.discard && !task.discard.logged_at && wb.stage !== "discarded";
  const red = firstRed(task);
  const collected = (task.runs || []).some((r) => r.kind === "validate" && r.collected_at);
  drops.value;

  async function unclaim() {
    const path = `/api/tasks/${short}/studio/unclaim`;
    try {
      const r = await api.confirmable("POST", path, {}, {
        onArm: (summary, secs) => arm("Press again to unclaim in Studio", unclaim, secs || 6, summary || ""),
        onDisarm: clear,
      });
      if (r?.armed) return;
      clear();
      if (r?.toast) toast(r.toast.text, r.toast); else toast("Unclaiming in Studio", { level: "info" });
    } catch (e) { clear(); toast(plainMessage(e.message), { level: "error" }); }
  }
  async function clean(confirmed) {
    if (!confirmed) { arm("Press again to clean this task's lanes", () => clean(true)); return; }
    clear();
    const path = `/api/tasks/${short}/clean`;
    try {
      let r = await api.confirmable("POST", path, {});
      if (r?.armed) r = await api.confirmable("POST", path, {});
      if (r?.toast) toast(r.toast.text, r.toast); else toast("Cleaning the lanes", { level: "info" });
    } catch (e) { toast(plainMessage(e.message), { level: "error" }); }
  }
  async function logged() {
    try { const r = await api.post(`/api/tasks/${short}/discard-logged`, {}); toast(r?.toast?.text || "Marked as logged", { level: "ok" }); }
    catch (e) { toast(plainMessage(e.message), { level: "error" }); }
  }

  const openStudio = () => st.url && window.open(st.url, "_blank", "noopener");
  useEffect(() => pushContext("task", { o: openStudio }), [st.url]);
  // The frame's palette already has Save, Refresh, Unclaim, Clean and Open in Studio for the selected task; the
  // page adds what only it can do.
  paletteItems = () => [
    !readOnly ? { id: "wb-deep", group: "Task", label: "Deep check my writing", run: () => onAction("deep_check") } : null,
    ...["prompt", "run", "write", "submit"].map((id) => ({ id: `wb-step-${id}`, group: "Task",
      label: `Show the ${{ prompt: "Prompt", run: "Validation run", write: "Write", submit: "Submit" }[id]} step`,
      run: () => setView(short, id === wb.step ? null : id) })),
  ].filter(Boolean);
  useEffect(() => () => { paletteItems = null; }, []);

  const items = [
    runs.length > 1 ? { label: "Use another run for Studio", run: onDetails } : null,
    { label: "Refresh from Studio", run: () => refreshStudio(short) },
    { label: "Understand this task", run: () => navigate(`/task/${short}/understand`) },
    wb.origin?.dossier_id ? { label: "Repo research", run: () => navigate(`/repos/${wb.origin.dossier_id}`) } : null,
    "-",
    (task.runs || []).length ? { label: "Clean lanes", run: () => clean(false), disabled: here, why: "The lane is running this task." } : null,
    discardOpen ? { label: "I logged the discarded run", run: logged } : null,
    mine && collected && !drops.value[short] ? { label: "Drop the task", danger: true, run: () => dropTask(short, task) } : null,
    mine ? { label: "Unclaim in Studio", danger: true, run: unclaim } : null,
  ].filter(Boolean);
  while (items.length && items[items.length - 1] === "-") items.pop();

  return html`<header class="wb-head">
    <div class="wb-head-l">
      <${Title} key=${wb.title} short=${short} title=${wb.title} />
      <div class="wb-slot">${slotLine(wb.slot)}</div>
    </div>
    <div class="wb-head-r">
      ${armed ? html`<${Btn} kind="danger" size="sm" label=${armed.label} title=${armed.title || ""} onClick=${() => armed.run()} />` : null}
      ${fromWrite ? html`${red && saveHere && !saving
          ? html`<button type="button" class="linkbtn wb-st stop" title="Reword it, or mark it as your own"
              onClick=${() => onAction(red.key === "opening_prompt" ? "open_prompt" : `focus_field:${red.key}`)}>${redWhy(red)}</button>`
          : html`<span class="wb-st">${saving ? "Saving to Studio…" : st.words}</span>`}
        ${saveHere ? html`<${Btn} kind="secondary" size="sm" label="Save to Studio" disabled=${!!saving || !!red}
          title=${red ? "Waits until you reword the words that match AI text, or mark them as your own" : "Save to Studio"}
          onClick=${onSave} />` : null}` : null}
      ${st.url ? html`<a class="btn sm quiet" href=${st.url} target="_blank" rel="noopener" title="Open in Studio (O)">Open in Studio ↗</a>` : null}
      <${Menu} size="sm" title="More for this task" items=${items} />
    </div>
  </header>`;
}

function setView(short, step, field) {
  const p = new URLSearchParams();
  if (step) p.set("step", step);
  if (field) p.set("field", field);
  const h = p.toString() ? `#${p}` : "";
  history.replaceState({}, "", `/task/${short}${h}`);
  hash.value = p;
}

export default function Workbench({ short }) {
  const { wb, task, err, reload } = useWorkbench(short);
  const [findingReq, setFindingReq] = useState(null);
  const [detailsReq, setDetailsReq] = useState(0);
  const n = useRef(0);
  const h = hash.value;
  const want = h.get("step");
  // A step the path doesn't list (Submit once the task is in review) opens the current step instead.
  const listed = (wb?.path || []).some((p) => p.id === want);
  const view = wb ? (STEPS.includes(want) && listed && (want !== "review" || wb.step === "review") ? want : wb.step) : null;
  const readOnly = wb?.step === "review";
  const tab = h.get("field") || wb?.current_field || (wb?.write_fields || [])[0];

  const goStep = (s, f) => setView(short, s === wb?.step && !f ? null : s, f);
  const goWrite = (f) => {
    setView(short, "write", f);
    setTimeout(() => document.querySelector(".wb-main textarea:not([readonly])")?.focus(), 60);
  };
  const save = () => saveToStudio(short, task, {
    onRed: (o) => (o.key === "opening_prompt" ? goStep("prompt") : goWrite(o.key)),
  });

  function onAction(a, it) {
    const [verb, arg] = String(a || "").split(":");
    switch (verb) {
      case "focus_field": return arg === "opening_prompt" ? goStep("prompt") : goWrite(wb.write_fields.includes(arg) ? arg : undefined);
      case "open_prompt": return goStep("prompt");
      case "rate": return goWrite("ratings");
      case "open_studio": return wb.studio?.url && window.open(wb.studio.url, "_blank", "noopener");
      case "reveal_log": return revealLog(short);
      case "show_finding": {
        const f = it?.field && wb.write_fields.includes(it.field) ? it.field : tab;
        setView(short, "write", f);
        setFindingReq({ row: arg, open: true, n: ++n.current });
        return null;
      }
      case "save": return save();
      case "deep_check": return deepCheck(short);
      case "rerun_qc": return rerunQc(short);
      case "collect": return collectRun(short, wb.run?.id);
      case "open_run": return goStep("run");
      case "refresh": return refreshStudio(short);
      default: return null;
    }
  }

  // ⌘S only makes sure his text is saved here, on every step. Save to Studio is its own button (and the palette):
  // a reflex key never writes to Studio.
  useEffect(() => {
    const onKey = (e) => {
      if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey || e.key.toLowerCase() !== "s") return;
      e.preventDefault();
      if (!wb || readOnly) return;
      flushFields().then(() => toast("Saved", { level: "ok", id: "wb-save" }));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [wb, task, readOnly]);

  if (err && !wb) {
    return html`<${Stage}><${Empty} title="This task didn't load" body=${plainMessage(err.message)}
      action=${html`<${Btn} label="Try again" onClick=${reload} />`} /><//>`;
  }
  if (!wb || !task) return html`<${Stage}><p class="faint wb-loading">Loading the task…</p><//>`;

  const red = readOnly ? null : firstRed(task);
  const steps = wb.path.map((p) => ({ ...p, label: p.id === "review" ? "Review" : p.label,
    ...(red && p.id === (red.key === "opening_prompt" ? "prompt" : "write") ? { flag: "stop", flag_why: "Words here repeat AI text" } : {}) }));
  // The Write step's lede names the open tab's job (the server's lede names the first field left).
  const phrase = (k) => (k === "ratings" ? "rate the run" : `write the ${FIELD_LOWER[k] || "field"}`);
  let lede = wb.lede;
  if (view === "write" && wb.step === "write" && wb.lede_field && tab && tab !== wb.lede_field && FIELD_NAME[tab]) {
    const done = ["done", "locked"].includes(wb.fields?.[tab]?.state);
    lede = done ? `${FIELD_NAME[tab]} is written. Next, ${phrase(wb.lede_field)}.`
      : `${phrase(tab).charAt(0).toUpperCase()}${phrase(tab).slice(1)}.`;
  }
  const allWritten = wb.step === "write" && !wb.current_field && wb.feedback?.state !== "sent_back" && !readOnly;
  async function doneWriting() {
    const r = await finishWriting(short, wb, { onMissing: (f) => goWrite(f.find((x) => wb.write_fields.includes(x))) });
    if (r === "done") { reload(); setView(short, "submit"); }
  }
  const cur = steps.find((s) => s.id === wb.step);
  const viewing = steps.find((s) => s.id === view);
  let body;
  if (view === "prompt") body = html`<${PromptStep} short=${short} task=${task} wb=${wb} readOnly=${readOnly} onWrite=${() => goWrite()} />`;
  else if (view === "run") body = html`<${RunStep} short=${short} task=${task} wb=${wb} readOnly=${readOnly} detailsReq=${detailsReq} onWrite=${() => goWrite("goal")} />`;
  else if (view === "write") body = html`<${WriteStep} short=${short} task=${task} wb=${wb} tab=${tab} readOnly=${readOnly}
      setTab=${(f) => setView(short, "write", f)} findingReq=${findingReq} reload=${reload}
      showFinding=${(row, open) => setFindingReq({ row, open, n: ++n.current })} onDone=${() => goStep("submit")}
      goStep=${goStep} />`;
  else if (view === "submit") body = html`<${SubmitStep} short=${short} task=${task} wb=${wb} readOnly=${readOnly} onAction=${onAction} onSave=${save} />`;
  else body = html`<${AfterStep} short=${short} task=${task} wb=${wb} onAction=${onAction} />`;

  const header = html`<${Header} short=${short} task=${task} wb=${wb} view=${view} onSave=${save} onAction=${onAction} readOnly=${readOnly}
      onDetails=${() => { setView(short, "run"); setDetailsReq((x) => x + 1); }} />`;
  // /task/<short>/understand: the page behind the Prompt step's research panel (understand.js).
  if (route.value.split("/")[3] === "understand") {
    return html`<${Stage} class="wb">${header}<${UnderstandPage} short=${short} wb=${wb} /><//>`;
  }
  return html`<${Stage} class="wb">
    ${header}
    <${PathStrip} steps=${steps} value=${view} onPick=${(id) => goStep(id)} />
    <div class="wb-lede">
      ${view === wb.step || !viewing ? html`<p class="wb-lede-t">${lede}</p>`
        : view === "submit" && allWritten ? html`<p class="wb-lede-t quiet">Every field is written. Done writing brings the task here.
            <${Btn} size="sm" kind="primary" label="Done writing →" onClick=${doneWriting} /></p>`
        : html`<p class="wb-lede-t quiet">${viewing.state === "done" ? `${viewing.label} is done.` : `${viewing.label} comes later.`}
            <button type="button" class="linkbtn" onClick=${() => goStep(wb.step)}>Back to ${cur?.label || "the current step"}</button></p>`}
    </div>
    ${body}
  <//>`;
}
