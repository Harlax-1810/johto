// After submit (spec 3.5.5): in review, accepted, rejected or dropped. The fields read-only, the reviewer's words
// as written, and nothing else to do. Sent back is the Write step with the reviewer's words pinned (write.js).
import { useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { Btn } from "../../components/primitives.js";
import { queue, navigate, select } from "../../lib/store.js";
import { act } from "../../lib/actions.js";
import { FIELD_NAME } from "./words.js";
import { Sent } from "./submit.js";


/** The reviewer's words as written: paragraphs and "* " bullets, nothing reworded. */
export function ReviewerWords({ text }) {
  const blocks = String(text || "").split(/\n\s*\n/).map((b) => b.trim()).filter(Boolean);
  return html`<div class="wb-rw">${blocks.map((b) => {
    const lines = b.split("\n").map((l) => l.trim()).filter(Boolean);
    if (lines.every((l) => /^[*-]\s+/.test(l))) return html`<ul>${lines.map((l) => html`<li>${l.replace(/^[*-]\s+/, "")}</li>`)}</ul>`;
    return html`<p>${lines.join(" ")}</p>`;
  })}</div>`;
}

function Fields({ task }) {
  const keys = Object.keys(FIELD_NAME).filter((k) => (task.sections?.[k]?.text || "").trim());
  if (!keys.length) return html`<p class="faint">The fields were written in Studio. Open it there to read them.</p>`;
  return html`<div class="wb-ro">${keys.map((k) => html`<section class="wb-ro-f">
    <h3 class="wb-h3">${FIELD_NAME[k]}</h3>
    <div class="wb-ro-t">${task.sections[k].text}</div>
  </section>`)}</div>`;
}

export function AfterStep({ short, task, wb, onAction }) {
  const stage = wb.stage;
  const dropped = !!wb.dropped || ["dropped", "discarded"].includes(stage);
  const fb = wb.feedback?.words;
  const [showFields, setShowFields] = useState(stage === "in_review");
  const logItem = queue.value.find((q) => q.short === short && q.kind === "log_discard");
  const logged = !!task.discard?.logged_at || stage === "discarded";
  const hasText = Object.keys(FIELD_NAME).some((k) => k !== "opening_prompt" && (task.sections?.[k]?.text || "").trim());
  return html`<div class="wb-cols">
    <div class="wb-col">
      ${dropped && !logged ? html`<div class="wb-after">
        <p>Log the discarded run in Mercor's form.</p>
        <div class="wb-acts">
          ${logItem ? html`<${Btn} kind="primary" label="Log the discarded run" title="Opens it on Now, with the form's questions"
            onClick=${() => { navigate("/"); select(logItem.id); }} />` : null}
          <${Btn} kind="quiet" label="I logged it" onClick=${() => act(`/api/tasks/${short}/discard-logged`, {}, { what: "Marked as logged" })} />
        </div>
      </div>` : null}
      ${fb ? html`<section class="wb-fbook">
        <div class="label-sm">The reviewer wrote</div>
        <${ReviewerWords} text=${fb} />
      </section>` : null}
      ${dropped ? null : html`<div class="wb-more">
        <button type="button" class="wb-asks-btn" aria-expanded=${showFields} onClick=${() => setShowFields(!showFields)}>What you wrote <span class="wb-tri">${showFields ? "▾" : "▸"}</span></button>
        ${showFields ? html`<${Fields} task=${task} />` : null}
      </div>`}
    </div>
    <div class="wb-col">${wb.submit && !dropped && hasText ? html`<${Sent} sub=${wb.submit} onAction=${onAction} title="What was sent" quiet=${true} />` : null}</div>
  </div>`;
}
