// The Submit step (spec 3.5.4): one list of what stands between him and submitting (blockers, then calls he
// makes, then what passes, folded), each with the button that fixes it. Save to Studio and Submit are his presses;
// Submit asks twice.
import { useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { AiTag, Btn, Dot, Flag } from "../../components/primitives.js";
import { useConfirm, useJob } from "../../lib/actions.js";
import { jobLine } from "../../lib/store.js";
import { numberWord } from "./words.js";
import { usePrimary, claimSlot, refreshStudio } from "./data.js";

/** "One thing to fix", "Three calls to make". */
const count = (n, one, many) => `${numberWord(n)} ${n === 1 ? one : many}`;

function Item({ it, onAction, readOnly }) {
  // Blockers are red with their buttons; calls are quiet: an ink dot and one quiet button at most.
  const stop = it.tone === "stop";
  const acts = stop ? it.actions || [] : (it.actions || []).slice(0, 1);
  return html`<li class=${`wb-si ${it.tone}`}>
    <${Dot} tone=${stop ? "stop" : "ink"} />
    <div class="wb-si-b">
      <div class="wb-si-t">${it.title}${it.ai ? html` <${AiTag} label=${it.ai} />` : null}</div>
      ${it.why ? html`<div class="wb-si-why">${it.why}</div>` : null}
      ${it.quote ? html`<blockquote class="wb-si-q">${it.quote}</blockquote>` : null}
      ${(it.parts || []).length ? html`<ul class="wb-si-parts">${it.parts.map((x) => html`<li><span>${x.title}</span>${x.why ? html`<span class="wb-si-why"> ${x.why}</span>` : null}</li>`)}</ul>` : null}
    </div>
    ${readOnly || !acts.length ? null : html`<div class="wb-si-a">${acts.map((a, n) => html`<${Btn} size="sm" kind=${stop && n === 0 ? "secondary" : "quiet"}
        label=${a.label} onClick=${() => onAction(a.action, it)} />`)}</div>`}
  </li>`;
}

/** The list: blockers, then calls, then passes folded. */
export function Checklist({ sub, onAction, readOnly }) {
  const [passOpen, setPassOpen] = useState(false);
  const b = sub.blockers || [], c = sub.calls || [], p = sub.passes || [];
  return html`<section class="wb-sl" aria-label="Before you submit">
    ${b.length ? html`<h3 class="wb-sl-h stop">${count(b.length, "thing to fix", "things to fix")}</h3>
      <ul class="wb-sl-list">${b.map((it) => html`<${Item} key=${it.id} it=${it} onAction=${onAction} readOnly=${readOnly} />`)}</ul>` : null}
    ${c.length ? html`${b.length ? html`<h3 class="wb-sl-h calls">${count(c.length, "call to make", "calls to make")}</h3>` : null}
      <p class=${`wb-sl-sub ${b.length ? "" : "first"}`}>${b.length ? "These don't stop you. Look at each and decide." : "None of these stop you. Look at each and decide."}</p>
      <ul class="wb-sl-list">${c.map((it) => html`<${Item} key=${it.id} it=${it} onAction=${onAction} readOnly=${readOnly} />`)}</ul>` : null}
    ${!b.length && !c.length ? html`<p class="wb-sl-clear"><${Dot} tone="ok" /> Nothing stands in the way.</p>` : null}
    ${p.length ? html`<div class="wb-sl-pass">
      <button type="button" class="wb-asks-btn" aria-expanded=${passOpen} onClick=${() => setPassOpen(!passOpen)}>
        ${count(p.length, "check passes", "checks pass")} <span class="wb-tri">${passOpen ? "▾" : "▸"}</span></button>
      ${passOpen ? html`<ul class="wb-sl-ok">${p.map((x) => html`<li><span class="wb-ok">✓</span>${x.title}</li>`)}</ul>` : null}
    </div>` : null}
  </section>`;
}

/** What Studio gets: one line per field, the ratings rule, the minutes. */
export function Sent({ sub, onAction, title = "What gets sent", quiet }) {
  return html`<section class=${`wb-sent ${quiet ? "quiet" : ""}`}>
    <h3 class="wb-h3">${title}</h3>
    <dl>${(sub.sent || []).map((r) => html`<dt>${r.field ? html`<button type="button" class="linkbtn" onClick=${() => onAction(r.field === "ratings" ? "rate" : `focus_field:${r.field}`)}>${r.label}</button>` : r.label}</dt>
      <dd class=${r.tone}>${r.value}${r.pass_rule ? html`<span class="faint"> · ${r.pass_rule}</span>` : null}</dd>`)}</dl>
  </section>`;
}

/** A draft: Claim a slot and fill Studio (one press), then Submit for review once Mercor's checks pass. */
export function DraftSteps({ short, task, sub, studio, onAction, red }) {
  const claiming = useJob(["claim_slot"], short);
  const looking = useJob(["slots_sync"], null);
  const d = studio?.draft || sub.draft || {};
  const open = d.open || 0;
  const blockers = (sub.blockers || []).length;
  const can = open > 0 && !blockers && !red && !claiming;
  const claim = () => { if (can) claimSlot(short, task, { onRed: (o) => onAction(o.key === "opening_prompt" ? "open_prompt" : `focus_field:${o.key}`) }); };
  usePrimary(() => claim());
  const why = red ? "Waits until you reword the words that match AI text, or mark them as your own."
    : blockers ? "Fix the items on the left first." : open ? "" : "No open slot matches right now. Johto looks every ten minutes and says when one opens.";
  return html`<section class="wb-steps">
    <div class="wb-step now">
      <div class="wb-step-n">1</div>
      <div class="wb-step-b">
        <div class="wb-step-h"><h3>Claim a slot and fill Studio</h3><span class="wb-step-s">${sub.save_state}</span></div>
        <p class="wb-step-w">${sub.save_words}</p>
        ${d.slot_words ? html`<p class="wb-step-w wb-slotw">Needs a slot seeded <b>${d.slot_words}</b>.</p>` : null}
        <div class="wb-acts">
          <${Btn} kind="primary" label=${claiming ? "Claiming…" : "Claim a slot and fill Studio"} k=${can ? "⌘⇧↵" : null}
            disabled=${!can} onClick=${claim} title=${why || "Claims one open slot, then saves everything here to Studio"} />
          <${Btn} size="sm" kind="quiet" label=${looking ? "Looking…" : "Look for open slots now"} disabled=${!!looking || !!claiming}
            onClick=${() => refreshStudio(short)} title="Reads Studio's open slots again" />
        </div>
        ${why ? html`<p class=${`wb-why ${red ? "stop" : ""}`}>${why}</p>` : null}
      </div>
    </div>
    <div class="wb-step">
      <div class="wb-step-n">2</div>
      <div class="wb-step-b">
        <div class="wb-step-h"><h3>Submit for review</h3></div>
        <p class="wb-step-w">Sends the task to a Mercor reviewer. It can't be undone.</p>
        <div class="wb-acts"><${Btn} kind="secondary" label="Submit for review" disabled=${true} title="Claim a slot first" /></div>
        <p class="wb-why">Claim a slot first. Once Mercor's checks pass, this step opens.</p>
      </div>
    </div>
    ${claiming ? html`<p class="wb-job"><${Dot} tone="busy" />${jobLine(claiming)}</p>` : null}
  </section>`;
}

/** Save to Studio, then Submit for review (two presses). */
export function SubmitSteps({ short, sub, studio, onSave, onAction, red }) {
  const saving = useJob(["save", "studio_save"], short);
  const qcJob = useJob(["qc", "qc_wait"], short);
  const submitting = useJob(["submit"], short);
  const confirm = useConfirm("POST", `/api/tasks/${short}/studio/submit`);
  const changed = (studio?.changed || []).length > 0;
  const saveFirst = !sub.saved || changed;
  const qcState = sub.qc?.state;
  const job = submitting || saving || qcJob;
  const submit = () => { if (sub.can_submit && !submitting && !saving) confirm.press({}, { what: "Submitting" }); };
  usePrimary(() => (saveFirst ? (!saving && !submitting && onSave()) : submit()));
  return html`<section class="wb-steps">
    <div class=${`wb-step ${saveFirst ? "now" : "done"}`}>
      <div class="wb-step-n">1</div>
      <div class="wb-step-b">
        <div class="wb-step-h"><h3>Save to Studio</h3><span class="wb-step-s">${sub.save_state}</span></div>
        <p class="wb-step-w">${sub.save_words}</p>
        <div class="wb-acts">
          <${Btn} kind=${saveFirst ? "primary" : "secondary"} label=${saving ? "Saving…" : studio?.saved_at ? "Save again" : "Save to Studio"}
            k=${saveFirst && !red ? "⌘⇧↵" : null} disabled=${!!saving || !!submitting || !!red} onClick=${onSave}
            title=${red ? "A field repeats AI text word for word" : "Save to Studio"} />
          ${sub.saved && ["failed", "stale", "error"].includes(qcState) ? html`<${Btn} size="sm" kind="quiet" label="Run Mercor's checks again"
            disabled=${!!qcJob || !!saving} onClick=${() => onAction("rerun_qc")} />` : null}
        </div>
        ${red ? html`<p class="wb-why stop">Waits until you reword the words that match AI text, or mark them as your own.</p>` : null}
        ${sub.qc?.words ? html`<p class=${`wb-qc ${qcState || ""}`}>${sub.qc.words}</p>` : null}
      </div>
    </div>
    <div class=${`wb-step ${!saveFirst ? "now" : ""}`}>
      <div class="wb-step-n">2</div>
      <div class="wb-step-b">
        <div class="wb-step-h"><h3>Submit for review</h3></div>
        <p class="wb-step-w">Sends the task to a Mercor reviewer. It can't be undone.</p>
        <div class="wb-acts">
          <${Btn} kind=${confirm.armed ? "danger" : sub.can_submit ? "primary" : "secondary"}
            label=${confirm.armed ? "Press again to submit. This can't be undone." : submitting ? "Submitting…" : "Submit for review"}
            k=${sub.can_submit && !saveFirst && !confirm.armed ? "⌘⇧↵" : null} disabled=${!sub.can_submit || !!submitting || !!saving}
            title=${sub.can_submit ? "Asks twice" : sub.submit_why} onClick=${submit} />
        </div>
        ${!sub.can_submit && sub.submit_why ? html`<p class="wb-why">${sub.submit_why}</p>` : null}
      </div>
    </div>
    ${job ? html`<p class="wb-job"><${Dot} tone="busy" />${jobLine(job)}</p>` : null}
  </section>`;
}

/** How many red overlaps (his words repeating AI text) block Save to Studio. */
export const redOverlaps = (task) => Object.values(task?.sections || {})
  .reduce((n, s) => n + (s?.overlap || []).filter((o) => o.level === "red" && !o.accepted).length, 0);

export function SubmitStep({ short, task, wb, onAction, onSave, readOnly }) {
  const sub = wb.submit;
  if (!sub) return html`<p class="faint">Finish writing first. The list of what's left shows here.</p>`;
  const changed = wb.studio?.changed || [];
  // Nothing blocks: in one column (narrow window) Save and Submit come before the calls to look at.
  return html`<div class=${`wb-cols wb-submit ${(sub.blockers || []).length ? "" : "clear"}`}>
    <div class="wb-col">
      ${changed.length && sub.saved ? html`<${Flag} tone="wait" title="Changed since you saved.">Studio has the older text. Save again before you submit.<//>` : null}
      <${Checklist} sub=${sub} onAction=${onAction} readOnly=${readOnly} />
    </div>
    <div class="wb-col">
      ${readOnly ? null : wb.studio?.draft
        ? html`<${DraftSteps} short=${short} task=${task} sub=${sub} studio=${wb.studio} onAction=${onAction} red=${redOverlaps(task)} />`
        : html`<${SubmitSteps} short=${short} sub=${sub} studio=${wb.studio} onSave=${onSave} onAction=${onAction} red=${redOverlaps(task)} />`}
      <${Sent} sub=${sub} onAction=${onAction} />
      ${readOnly ? null : html`<div class="wb-deep">
        <${Btn} kind="quiet" size="sm" label="Deep check my writing" onClick=${() => onAction("deep_check")} />
        <span class="faint">Claude reads your fields and lists issues. It never rewrites them.</span>
      </div>`}
    </div>
  </div>`;
}
