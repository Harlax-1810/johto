import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { Surface } from "../../components/surface.js";
import { Field, flushFields } from "../../components/field.js";
import { Kbd, JobNote } from "../../components/ui.js";
import { Reference } from "../reference.js";
import { PackButton } from "../../components/pack.js";
import { fullSlot } from "../../lib/format.js";
import { lane, toast } from "../../lib/store.js";
import { usePrimary, useFieldJumps, focusEnd, RunChoice, submissionRun, runName } from "./decide.js";
import { useConfirm, useJob } from "../../lib/actions.js";

const FIELDS = ["goal", "decision_points", "codebase"];
const ROWS = { goal: 3, decision_points: 5, codebase: 5, initial_state: 4 };

const numberOf = (url) => (/\/(?:issues|pull)\/(\d+)/.exec(url || "") || [])[1];

/** True once the issue/PR number or its URL appears in Difficulty or the reviewer notes. */
function mentionedIn(text, u) {
  if (!text) return false;
  if (u.url && text.includes(u.url)) return true;
  const n = numberOf(u.url);
  return !!n && new RegExp(`#${n}(?!\\d)`).test(text);
}

function Upstream({ task }) {
  const ups = task.upstream_mentions || [];
  if (!ups.length) return null;
  const hay = `${task.sections.codebase?.text || ""}\n${task.sections.reviewer_notes?.text || ""}`;
  const marks = ups.map((u) => ({ ...u, ok: u.mentioned || mentionedIn(hay, u) }));
  const all = marks.every((m) => m.ok);
  return html`<div class=${`det-up ${all ? "ok" : ""}`}>
    <span class="ic">${all ? "✓" : "⚑"}</span>
    <span class="m">${all ? "Upstream mentioned:" : "Mention in Difficulty or reviewer notes:"}</span>
    <span class="refs">${marks.map((m) => html`<a href=${m.url} target="_blank" rel="noopener" class=${m.ok && !all ? "on" : ""}
        title=${m.title || m.url}>${m.ok && !all ? "✓ " : ""}${numberOf(m.url) ? `#${numberOf(m.url)}` : m.title}<span class="st">${m.state || ""}</span></a>`)}</span>
  </div>`;
}

export default function Details({ item, task, setPrimary }) {
  const s = task.summary;
  const keys = [...FIELDS, ...(s.slot?.action === "Operate" && task.sections.initial_state ? ["initial_state"] : [])];
  const secs = keys.map((k) => task.sections[k]).filter(Boolean);
  const missing = secs.filter((x) => !(x.text || "").trim());
  const errors = secs.reduce((n, x) => n + (x.lint || []).filter((f) => f.level === "error").length, 0);
  const [showRef, setShowRef] = useState(true);
  const firstEmpty = missing[0]?.key;

  // A chosen calibration run (Austin's case) is rated next; otherwise Done queues the validation run.
  const sub = submissionRun(task);
  const subRun = (task.runs || []).find((r) => r.name === sub);
  const chosenCalib = subRun?.kind === "calib";
  const lv = lane.value;
  const laneFree = lv.state === "free" && !(lv.queue || []).length;
  const label = chosenCalib ? `Done, rate ${runName(subRun)}` : laneFree ? "Done, start validation" : "Done, queue validation";
  const focusField = (k) => focusEnd(document.querySelectorAll(".det-fields .field textarea")[keys.indexOf(k)]);
  const { armed, press } = useConfirm("POST", `/api/tasks/${s.short}/done`);
  const prep = useJob(["lane_prepare"], s.short);

  async function done() {
    await flushFields();
    const now = keys.filter((k) => task.sections[k]).find((k) => !(document.querySelectorAll(".det-fields .field textarea")[keys.indexOf(k)]?.value ?? task.sections[k].text ?? "").trim());
    if (now) {
      toast(`Write ${task.sections[now].heading} first`, { level: "error" });
      focusField(now);
      return;
    }
    await press({ surface: "details" }, {
      what: chosenCalib ? `Details saved. Next: rate ${runName(subRun)}` : laneFree ? "Starting validation" : "Queued validation",
      onError: (e) => { const k = e.details?.fields?.[0]; if (k && keys.includes(k)) focusField(k); },
    });
  }
  usePrimary(setPrimary, done);
  useFieldJumps(".det-fields .field textarea", { KeyR: () => setShowRef((v) => !v) });

  const why = missing.length ? `Empty: ${missing.map((m) => m.heading).join(", ")}` : "";
  const primary = {
    label: errors ? `${label} · ${errors} lint error${errors > 1 ? "s" : ""}` : label,
    run: done, disabled: !!missing.length, why, armed: !!armed,
    armedLabel: armed?.summary || `Press again: ${errors} lint error${errors > 1 ? "s" : ""}`,
  };
  // The run these details are about (the chosen one, else the kept calibration run) and its review pack.
  const aboutRun = subRun || (task.runs || []).find((r) => r.name === item.run) || null;
  const pack = aboutRun && (aboutRun.collected_at || aboutRun.state === "collected") ? html`<${PackButton} short=${s.short} run=${aboutRun.name} />` : null;
  const head = html`<${RunChoice} task=${task} run=${aboutRun} extra=${pack} />${prep ? html`<${JobNote} job=${prep} what="Setting up the lane" />` : null}`;

  return html`<${Surface} title="Write goal, decision points and codebase notes" label=${s.label} dossier=${s.dossier} slotLine=${fullSlot(s.slot)}
      reason=${item.reason} primary=${primary} head=${head}>
    <div class=${`sbody det ${showRef ? "" : "noref"}`}>
      <div class="scol det-fields">
        ${keys.map((k, i) => task.sections[k] ? html`<${Field} key=${k} short=${s.short} sectionKey=${k} section=${task.sections[k]}
            autofocus=${k === firstEmpty} minRows=${ROWS[k] || 4}
            label=${html`${task.sections[k].heading}<${Kbd} k=${`⌥${i + 1}`} />`} />` : null)}
        <${Upstream} task=${task} />
      </div>
      ${showRef ? html`<div class="scol det-ref">
        <${Reference} target=${`t:${s.short}`} lead=${task.lead} opportunity=${task.opportunity} runs=${task.runs} defaultTab="run"
          pinned=${{ label: "Your opening prompt", text: task.sections.opening_prompt?.text }} />
      </div>` : html`<div class="det-reopen">
        <button class="btn sm quiet" onClick=${() => setShowRef(true)} title="Show the reference pane"><${Kbd} k="⌥R" />Reference</button>
      </div>`}
    </div>
  <//>`;
}
