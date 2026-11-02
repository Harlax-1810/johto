// The Prompt step (spec 3.5.1): his opening prompt on the left, the task understanding on the right (Claude's
// research, with the AI mark, nothing that reads like a prompt; understand.js), with the full page one click away.
// Only the prompt is needed before the run.
import { useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { AiBlock, AiTag, Btn, Dot, Flag } from "../../components/primitives.js";
import { Editor } from "../../components/field.js";
import { api } from "../../lib/api.js";
import { toast, hash } from "../../lib/store.js";
import { report } from "../../lib/actions.js";
import { Asks } from "./write.js";
import { runPrimary } from "./run.js";
import { plainMessage, usePrimary, gateView, checkPrompt, useLiveJob, promptRed, PROMPT_RED_WHY } from "./data.js";
import { Glance } from "./understand.js";

const STYLE_BAND = { "Casual prose": "Casual: 20 to 60 words", "Explicit spec": "Spec: 450 to 1,200 words",
  "Error / artifact brief": "Error report: starts from the pasted error" };

function Origin({ origin }) {
  if (!origin || origin.kind === "none") {
    return html`<div class="wb-origin none"><h3 class="wb-h3">Where this task came from</h3>
      <p class="faint">No research is attached to this task.</p></div>`;
  }
  // Only the problem and how it works today sit beside his prompt. Where Claude goes wrong and the upstream
  // threads stay out of this panel: a fix idea next to the prompt bleeds into it, and the threads belong in
  // Codebase understanding (Mercor's rule), where the Write step lists them.
  return html`<${AiBlock} label=${origin.tag || "Claude's research"} class="wb-origin">
    <h3 class="wb-h3">Where this task came from</h3>
    ${origin.title ? html`<p class="wb-origin-t">${origin.title}</p>` : null}
    ${origin.problem ? html`<p class="wb-origin-p">${origin.problem}</p>` : null}
    ${(origin.today || []).length ? html`<div class="wb-origin-s"><div class="label-sm">How it works today</div>
      <ul>${origin.today.map((x) => html`<li>${x}</li>`)}</ul></div>` : null}
  <//>`;
}


/** Unlocking the sent prompt: two presses, and it means a new validation run. */
function Unlock({ short }) {
  const [armed, setArmed] = useState(null);
  const path = `/api/tasks/${short}/sections/opening_prompt/unlock`;
  async function press() {
    try {
      const r = await api.confirmable("POST", path, {}, { onArm: (s) => setArmed(s || "Press again to unlock"), onDisarm: () => setArmed(null) });
      if (r?.armed) return;
      setArmed(null);
      report(r, "Unlocked. A new validation run is needed after you change it.");
    } catch (e) { setArmed(null); toast(plainMessage(e.message), { level: "error" }); }
  }
  return html`<${Btn} size="sm" kind=${armed ? "danger" : "quiet"} label=${armed ? "Press again to unlock" : "Unlock to edit"}
    title="The prompt was sent to Claude. Changing it means a new validation run." onClick=${press} />`;
}

/** Why the lane won't start yet, with the one way out: Save to Studio, check, then start (his press). */
function Gate({ gate, studioUrl, onCheck, checking, red }) {
  const acts = checking ? null
    : gate.action === "check" ? html`<${Btn} kind="primary" label=${gate.reason === "failed" ? "Check it again" : "Save to Studio, check, then start"}
        disabled=${red} title=${red ? PROMPT_RED_WHY : ""} onClick=${onCheck} />`
    : gate.action === "open_studio" && studioUrl ? html`<${Btn} label="Open in Studio ↗" onClick=${() => window.open(studioUrl, "_blank", "noopener")} />`
    : null;
  return html`<div class="wb-gate">
    <${Flag} tone=${gate.tone} title=${gate.title} actions=${acts}>${checking
      ? html`<span class="wb-gate-busy"><${Dot} tone="work" />Saving the prompt to Studio and running Mercor's check. A few minutes.</span>`
      : gate.why}<//>
    ${red && !checking ? html`<p class="wb-why stop">${PROMPT_RED_WHY}</p>` : null}
    ${(gate.failed || []).length ? html`<div class="wb-gate-why"><${AiTag} label="Mercor's automatic check" />
      <ul>${gate.failed.map((f) => html`<li><b>${f.dimension}</b>${f.justification ? html`: ${f.justification}` : null}</li>`)}</ul></div>` : null}
  </div>`;
}

export function PromptStep({ short, task, wb, readOnly, onWrite }) {
  const s = task.sections?.opening_prompt;
  const style = task.summary?.slot?.prompt_style;
  const spec = style === "Explicit spec";
  const [goalOpen, setGoalOpen] = useState(!!(task.sections?.goal?.text || "").trim());
  const [armed, setArmed] = useState(null);
  // A start the lane refused (Now sends #gate=<reason> here when its Start was refused).
  const [gate, setGate] = useState(() => (hash.peek().get("gate") ? gateView({ details: { reason: hash.peek().get("gate") } }) : null));
  const checking = !!useLiveJob(["prompt_check"], short);
  const p = runPrimary(short, wb, task, { onWrite, onArm: (x) => setArmed(x || "Press again to start anyway") });
  const current = wb.step === "prompt";
  const start = async () => {
    const r = await p.run();
    if (r !== "armed") setArmed(null);
    setGate(r?.gate || null);
    return r;
  };
  const press = async () => {
    if (!current || readOnly || p.status || p.disabled || checking) return;
    if (gate?.action === "check") return promptRed(task) ? null : check();
    return start();
  };
  async function check() {
    const r = await checkPrompt(short);
    if (r?.ok) { setGate(null); await start(); } else if (r?.gate) setGate(r.gate);
  }
  usePrimary(press);
  return html`<div class="wb-cols">
    <div class="wb-col">
      <div class="wb-fhead">
        <h2>Opening prompt</h2>
        ${STYLE_BAND[style] ? html`<span class="wb-band">${STYLE_BAND[style]}</span>` : null}
        <${Asks} k="opening_prompt" style=${s?.style} />
      </div>
      ${s ? html`<${Editor} key="opening_prompt" short=${short} sectionKey="opening_prompt" section=${s} label="Opening prompt"
          autofocus=${current && !readOnly} minRows=${spec ? 16 : 6} readOnly=${readOnly}
          extra=${s.locked && !readOnly ? html`<${Unlock} short=${short} />` : null} />` : null}
      ${current && !readOnly && !(checking || gate?.action === "check") ? html`<div class="wb-acts wb-start">
        ${p.status ? html`<span class="wb-status">${p.status}</span>` : html`<${Btn} kind=${armed ? "danger" : p.kind} size="lg" label=${armed || p.label} k=${p.k}
          disabled=${p.disabled} onClick=${press} />`}
        ${gate ? null : p.why ? html`<span class=${`wb-why ${promptRed(task) ? "stop" : ""}`}>${p.why}</span>`
          : /Start the validation run/.test(p.label || "") ? html`<span class="wb-why">Only the prompt is needed now. The goal can wait until the run is going.</span>` : null}
      </div>` : null}
      ${current && !readOnly && (gate || checking) ? html`<${Gate} gate=${gate || gateView({})} checking=${checking} studioUrl=${wb.studio?.url} onCheck=${check} red=${promptRed(task) > 0} />` : null}
      ${task.sections?.goal ? html`<div class="wb-more">
        <button type="button" class="wb-asks-btn" aria-expanded=${goalOpen} onClick=${() => setGoalOpen(!goalOpen)}>${current ? "High-level goal, optional now" : "High-level goal"} <span class="wb-tri">${goalOpen ? "▾" : "▸"}</span></button>
        ${goalOpen ? html`<div class="wb-more-b"><${Asks} k="goal" />
          <${Editor} key="goal" short=${short} sectionKey="goal" section=${task.sections.goal} minRows=${4} label="High-level goal" readOnly=${readOnly} compact=${true} noLint=${true} /></div>` : null}
      </div>` : null}
    </div>
    <div class="wb-col">${readOnly ? html`<${Origin} origin=${wb.origin} />` : html`<${Glance} short=${short} origin=${wb.origin} />`}</div>
  </div>`;
}
