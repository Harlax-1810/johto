import { useEffect, useRef } from "preact/hooks";
import { html } from "../../lib/html.js";
import { Surface } from "../../components/surface.js";
import { Field, flushFields } from "../../components/field.js";
import { JobNote } from "../../components/ui.js";
import { Reference } from "../reference.js";
import { fullSlot } from "../../lib/format.js";
import { app, lane, toast } from "../../lib/store.js";
import { useConfirm, useJob } from "../../lib/actions.js";

export default function WritePrompt({ item, task, setPrimary }) {
  const s = task.sections.opening_prompt;
  const short = task.summary.short;
  const spec = task.summary.slot.prompt_style === "Explicit spec";
  const laneFree = lane.value.state === "free" && !(lane.value.queue || []).length;
  // Calibration is optional; by default Done goes to the goal, whose Done queues the validation run.
  const calibFirst = !!app.value?.settings?.calibrate_first;
  const calibLabel = laneFree ? "Done, start calibration" : "Done, queue calibration";
  const label = calibFirst ? calibLabel : "Done, write the goal";
  const errors = (s.lint || []).filter((f) => f.level === "error").length;
  const { armed, press } = useConfirm("POST", `/api/tasks/${short}/done`);
  const prep = useJob(["lane_prepare"], short);

  // The field saves 400 ms after typing stops; Done sends any waiting save first, so the queued
  // prompt is exactly what he typed.
  async function done(kind) {
    const text = document.querySelector(".surface .field textarea")?.value ?? s.text;
    if (!(text || "").trim()) { toast("Write the prompt first", { level: "error" }); return; }
    await flushFields();
    const calib = kind === "calib" || (!kind && calibFirst);
    await press({ surface: "prompt", kind: kind || (calibFirst ? "calib" : "skip") },
      { what: calib ? (laneFree ? "Starting calibration" : "Queued calibration") : "Prompt done; the goal is next" });
  }
  const ref = useRef(done);
  ref.current = done;
  useEffect(() => { setPrimary?.(() => ref.current()); return () => setPrimary?.(null); }, [short]);

  const primary = {
    label: errors ? `${label} · ${errors} lint error${errors > 1 ? "s" : ""}` : label,
    run: () => done(), disabled: !s.words, armed: !!armed,
    armedLabel: armed?.summary || `Press again: ${label.replace(/^Done, /, "")} anyway`,
    why: !s.words ? "Write the prompt first" : calibFirst ? "Saves the prompt as written and puts calibration in the lane queue"
      : "Saves the prompt as written. Next: goal, decision points and codebase notes, then the validation run",
  };
  const secondary = calibFirst
    ? [{ label: "Skip calibration", run: () => done("skip"), disabled: !s.words, title: "Go to the goal; validation runs after it" }]
    : [{ label: "Calibrate first", run: () => done("calib"), disabled: !s.words,
         title: "An optional difficulty check before you write the goal" }];
  return html`<${Surface} title="Write the opening prompt" label=${task.summary.label} dossier=${task.summary.dossier} slotLine=${fullSlot(task.summary.slot)}
      reason=${item.reason} primary=${primary} secondary=${secondary}
      head=${prep ? html`<${JobNote} job=${prep} what="Setting up the lane" />` : null}>
    <div class="sbody">
      <div class="scol" style=${spec ? "flex:0 0 680px" : "flex:0 0 46%"}>
        <${Field} short=${short} sectionKey="opening_prompt" section=${s} autofocus=${true} minRows=${spec ? 16 : 6} />
      </div>
      <div class="scol" style="padding:0;display:flex">
        <${Reference} target=${`t:${short}`} lead=${task.lead} opportunity=${task.opportunity} runs=${task.runs} defaultTab="code" />
      </div>
    </div>
  <//>`;
}
