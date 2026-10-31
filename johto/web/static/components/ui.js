import { useState } from "preact/hooks";
import { html } from "../lib/html.js";

export const Kbd = ({ k }) => k ? html`<span class="kbd">${k}</span>` : null;

export function Btn({ label, k, kind = "", onClick, disabled, title, sm }) {
  return html`<button class=${`btn ${kind} ${sm ? "sm" : ""}`} onClick=${onClick} disabled=${disabled} title=${title}>
    ${k && kind === "primary" ? html`<${Kbd} k=${k} />` : null}${label}${k && kind !== "primary" ? html` <${Kbd} k=${k} />` : null}
  </button>`;
}

export const Chip = ({ tone = "neutral", children, title }) =>
  html`<span class=${`chip ${tone}`} title=${title}>${children}</span>`;

export function Segmented({ options, value, onChange, keys }) {
  return html`<div class="seg" role="radiogroup">
    ${options.map((o, i) => html`<button role="radio" aria-checked=${value === o.value} class=${value === o.value ? "on" : ""}
        title=${o.hint || (keys ? `key ${keys[i]}` : "")} onClick=${() => onChange(o.value)}>${o.label}</button>`)}
  </div>`;
}

/** grid[ts-1][steering-1] = passes. */
export function PassGrid({ grid, ts, steering }) {
  if (!grid) return null;
  return html`<div class="grid5" aria-label="Which ratings pass">
    <span></span>${[1, 2, 3, 4, 5].map((s) => html`<span class="h">${s}</span>`)}
    ${grid.map((row, i) => html`
      <span class="rl">TS ${i + 1}</span>
      ${row.map((p, j) => html`<span class=${`c ${p ? "p" : "r"} ${ts === i + 1 && steering === j + 1 ? "me" : ""}`}
        title=${`Task Success ${i + 1}, Steering ${j + 1}: ${p ? "passes" : "rejected"}`}></span>`)}`)}
  </div>`;
}

export function AiPanel({ title = "AI research", right, children }) {
  return html`<section class="ai">
    <div class="ah"><span class="label">${title}</span>${right}</div>
    ${children}
  </section>`;
}

export function Collapsible({ title, count, open: initial = false, children, right }) {
  const [open, setOpen] = useState(initial);
  return html`<div class="coll">
    <button onClick=${() => setOpen(!open)} aria-expanded=${open}>
      <span class="tri">${open ? "▾" : "▸"}</span><span>${title}</span>
      ${count != null ? html`<span class="faint">${count}</span>` : null}
      ${right ? html`<span style="margin-left:auto">${right}</span>` : null}
    </button>
    ${open ? html`<div class="cbody">${children}</div>` : null}
  </div>`;
}

export function Checklist({ rows, onFix }) {
  const order = { fail: 0, warn: 1, pending: 2, pass: 3 };
  const sorted = [...(rows || [])].sort((a, b) => order[a.status] - order[b.status]);
  const icon = { pass: "✓", fail: "✗", warn: "!", pending: "◌" };
  return html`<ul class="check">
    ${sorted.map((r) => html`<li class=${r.status}>
      <span class="st">${icon[r.status]}</span>
      <span class="tt">${r.title}</span>
      <span class="dd">${r.detail}</span>
      <span>${r.fix ? html`<button class="btn sm" onClick=${() => onFix?.(r)}>${r.fix.action === "focus_field" ? "Go to the field" : "Fix"}</button>` : null}</span>
    </li>`)}
  </ul>`;
}

/** A file:line pointer that expands to the real code in place. */
export function Pointer({ target, pointer, onOpen }) {
  return html`<button class="ptr" onClick=${() => onOpen?.(pointer)} title="Show the code">${pointer} ▸</button>`;
}

export const Dot = ({ kind }) => html`<span class=${`dot ${kind}`}></span>`;

/** One quiet line for machine work in progress (a job's current step), shown under a surface header. */
export function JobNote({ job, what }) {
  if (!job) return null;
  const p = job.progress || {};
  const steps = p.steps || [];
  const i = p.step ? steps.indexOf(p.step) : -1;
  const step = p.step || (p.log_tail || []).slice(-1)[0] || (job.state === "queued" ? "waiting its turn" : "starting");
  return html`<div class="why jobnote" title=${(p.log_tail || []).join("\n")}>
    <span class="dot busy"></span><span>${what ? `${what} · ` : ""}${step}${i >= 0 && steps.length > 1 ? html` <span class="faint num">${i + 1} of ${steps.length}</span>` : null}</span>
  </div>`;
}
