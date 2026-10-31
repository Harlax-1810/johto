import { useRef, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { Surface } from "../../components/surface.js";
import { PackButton } from "../../components/pack.js";
import { Field } from "../../components/field.js";
import { Segmented, Collapsible, Kbd } from "../../components/ui.js";
import { api } from "../../lib/api.js";
import { patchTask, toast } from "../../lib/store.js";
import { fullSlot, mins, plural } from "../../lib/format.js";
import {
  runOf, usePrimary, useKeys, useFieldJumps, focusEnd, drops, startDrop, cancelDrop, runName, RunChoice,
  Stat, RunNotes, Moments, Diffstat, DiffText, Transcript, useTranscript, hasDiff,
} from "./decide.js";
import { flushFields } from "../../components/field.js";
import { useConfirm } from "../../lib/actions.js";

// Short meanings beside the controls; the full wording from Studio sits in each button's tooltip.
const TS = {
  1: ["failed completely", "1 - Failed completely"],
  2: ["partial, major issues", "2 - Partial, major issues"],
  3: ["completed, notable problems", "3 - Completed, but highly inefficient or non-optimal, with notable problems"],
  4: ["successful, minor issues", "4 - Successful, minor or style issues"],
  5: ["completely successful", "5 - Completely successful"],
};
const ST = {
  1: ["constant input", "1 - Constant input: corrected or redirected the model at most turns"],
  2: ["frequent input", "2 - Frequent input"],
  3: ["occasional input", "3 - Occasional input at a few key points"],
  4: ["one or two nudges", "4 - One or two light nudges"],
  5: ["no input", "5 - None: the model reached the solution on its own"],
};
const FIVE = [1, 2, 3, 4, 5];

const pick = (r) => ({ one_shot: r?.one_shot ?? null, task_success: r?.task_success ?? null, steering: r?.steering ?? null });
const same = (a, b) => a.one_shot === b.one_shot && a.task_success === b.task_success && a.steering === b.steering;
const firstUnset = (r) => (r.one_shot !== "No" ? 0 : r.task_success == null ? 1 : r.steering == null ? 2 : 3);

function passRule(enterprise) {
  return enterprise ? "Enterprise passes: Task Success 1-2, or 3 with Steering 1-2"
                    : "Personal-scale passes: Task Success 1-3, or 4 with Steering 1-2";
}

/** Which Task Success × Steering pairs pass for this codebase type; the pick and its row are marked. */
function Grid({ grid, ts, st, typeLabel }) {
  if (!grid) return null;
  return html`<div class="rate-grid" aria-label="Which ratings pass">
    <div class="sa-h"><span class="label" title="Task Success and Steering pairs that pass review for this codebase type">Pass grid</span></div>
    <div class="sm faint rate-gtype">${typeLabel}</div>
    <div class="rate-gt">
      <span></span><span class="cap">Steering</span>
      <span></span>${FIVE.map((s) => html`<span class=${`h ${st === s ? "on" : ""}`}>${s}</span>`)}
      ${grid.map((row, i) => html`
        <span class=${`rl ${ts === i + 1 ? "on" : ""}`} title=${TS[i + 1][1]}>TS ${i + 1}</span>
        ${row.map((p, j) => html`<span class=${`c ${p ? "p" : "r"} ${ts === i + 1 ? "cur" : ""} ${ts === i + 1 && st === j + 1 ? "me" : ""}`}
            title=${`Task Success ${i + 1}, Steering ${j + 1}: ${p ? "passes" : "rejected"}`}></span>`)}`)}
    </div>
    <div class="rate-legend sm"><span><i class="c p"></i>passes</span><span><i class="c r"></i>rejected</span></div>
  </div>`;
}

/** One rating row: label, control, its keys (shown on the active row, space kept on the others), meaning. */
function Row({ label, on, hint, keys, children }) {
  return html`<div class=${`rate-row ${on ? "on" : ""}`}>
    <span class="rl">${label}</span>
    ${children}
    <span class="keys" aria-hidden=${!on}>${keys}</span>
    <span class="hint">${hint || ""}</span>
  </div>`;
}

export default function Rate({ item, task, setPrimary }) {
  const s = task.summary;
  const run = runOf(task, item, "validate");
  const f = run?.facts || {};
  const enterprise = (s.slot?.codebase_type || "").startsWith("Enterprise");
  const [r, setR] = useState(() => pick(task.ratings));
  const [res, setRes] = useState(task.ratings);
  const [step, setStep] = useState(() => firstUnset(pick(task.ratings)));
  const seq = useRef(0);
  const notes = task.sections.validation_notes;
  const dropping = !!drops.value[s.short];
  const { armed, press } = useConfirm("POST", `/api/tasks/${s.short}/done`);

  async function save(next) {
    setR(next);
    const n = ++seq.current;
    try {
      const out = await api.send("PUT", `/api/tasks/${s.short}/ratings`, { ...next, base_rev: res?.rev });
      if (n !== seq.current) return;
      setRes(out.ratings);
      patchTask(s.short, (t) => { t.ratings = out.ratings; });
    } catch (e) { toast(e.message, { level: "error" }); }
  }
  const grid = res?.grid || task.ratings?.grid;
  const passes = (ts, st) => !!grid?.[ts - 1]?.[st - 1];
  const focusNotes = () => setTimeout(() => focusEnd(document.querySelector(".rate-notes .field textarea")), 0);
  function set(key, v) {
    const next = { ...r, [key]: v };
    save(next);
    if (key === "one_shot") setStep(v === "No" ? 1 : 0);
    if (key === "task_success") setStep(2);
    if (key === "steering") {
      // Forward into Validation Notes only on a passing pick; on a failing one stay on Steering,
      // so the next digit fixes it instead of landing in his notes.
      if (next.one_shot === "No" && next.task_success && passes(next.task_success, v)) { setStep(3); focusNotes(); }
      else setStep(2);
    }
  }
  const digit = (d) => () => {
    if (step === 1) set("task_success", d);
    else if (step === 2) set("steering", d);
  };

  // What the ratings say, from the server's check (lint.check_ratings) once it matches the pick.
  const settled = res && same(pick(res), r);
  const complete = r.one_shot === "No" && r.task_success && r.steering;
  const passLocal = complete ? passes(r.task_success, r.steering) : null;
  const errs = settled ? (res.check || []).filter((c) => c.level === "error" && !/missing|required|must be Yes or No/.test(c.message)) : [];
  let line;
  if (r.one_shot === "Yes") line = { cls: "bad", ic: "✗", text: "One-shot: this task gets dropped" };
  else if (complete && passLocal) line = { cls: "ok", ic: "✓", text: `Passes for ${enterprise ? "Enterprise" : "Personal-scale"}` };
  else if (complete) line = { cls: "bad", ic: "✗", text: errs[0]?.message || "These ratings are rejected" };
  else if (r.task_success && r.task_success >= 5) line = { cls: "bad", ic: "✗", text: "Task Success 5 is always rejected" };
  else line = { cls: "", ic: "", text: passRule(enterprise) };

  const ready = complete && passLocal && (notes?.text || "").trim();
  const title = run ? `Rate ${runName(run)}` : "Rate the validation run";
  async function done() {
    if (r.one_shot === "Yes") return dropOneShot();
    if (r.one_shot !== "No") { toast("Set One-shot first (n or y)", { level: "error" }); setStep(0); return; }
    if (!r.task_success) { toast("Rate Task Success first", { level: "error" }); setStep(1); return; }
    if (!r.steering) { toast("Rate Steering first", { level: "error" }); setStep(2); return; }
    if (!passLocal) { toast(line.text, { level: "error" }); return; }
    await flushFields();
    const typedNotes = document.querySelector(".rate-notes .field textarea")?.value ?? notes?.text ?? "";
    if (!typedNotes.trim()) { toast("Write Validation Notes first", { level: "error" }); focusNotes(); return; }
    await press({ surface: "rate" }, { what: "Rated. Next: save to Studio" });
  }
  function dropOneShot() {
    if (dropping) return;
    startDrop({ short: s.short, run: run?.name, label: `one-shot in validation; unclaims ${s.task_name} in Studio and removes its lanes` });
  }
  usePrimary(setPrimary, done);
  useKeys("rate", {
    y: () => set("one_shot", "Yes"), n: () => set("one_shot", "No"),
    1: digit(1), 2: digit(2), 3: digit(3), 4: digit(4), 5: digit(5),
    Backspace: () => setStep((x) => Math.max(0, x - 1)),
  });
  const rev = task.sections.reviewer_notes;
  const [revOpen, setRevOpen] = useState(() => !!(rev?.text || "").trim());
  const openRev = () => { setRevOpen(true); setTimeout(() => focusEnd(document.querySelector(".rate-rev .field textarea")), 0); };
  useFieldJumps(".rate-main .rate-notes .field textarea", { Digit2: openRev });
  const tx = useTranscript(s.short, run?.name);
  const replies = (tx.entries || []).filter((e) => e.role === "human");
  const yes = r.one_shot === "Yes";

  const primary = r.one_shot === "Yes"
    ? (dropping ? { label: "Cancel drop", keys: "⌘Z", run: () => cancelDrop(s.short), armed: true, armedLabel: "Cancel drop" }
                : { label: "Drop: one-shot in validation", run: dropOneShot, why: "Unclaims the task after a 10 s undo window" })
    : { label: "Done", run: done, disabled: !ready, armed: !!armed, armedLabel: armed?.summary || "Press again",
        why: ready ? "Moves the task to Save to Studio" : "Needs all three ratings, a passing pick and Validation Notes" };

  const warn = (run?.moments || []).filter((m) => m.level === "warn").length;
  const typeLabel = s.slot?.codebase_type || "";

  return html`<${Surface} title=${title} label=${s.label} dossier=${s.dossier}
      slotLine=${fullSlot(s.slot)} reason=${item.reason} primary=${primary} head=${html`<${RunChoice} task=${task} />`}>
    <div class=${`sbody rate ${yes ? "yes" : ""}`}>
      <div class="scol rate-main">
        <div class="rate-ctl">
          <${Row} label="One-shot" on=${step === 0} keys=${html`<${Kbd} k="n" /><${Kbd} k="y" />`}>
            <div class=${yes ? "rate-yes" : ""}>
              <${Segmented} value=${r.one_shot} onChange=${(v) => set("one_shot", v)} keys=${["n", "y"]}
                options=${[{ value: "No", label: "No", hint: "No: the model did not one-shot it (key n)" },
                           { value: "Yes", label: "Yes", hint: "Yes: the model one-shot it; the task is dropped (key y)" }]} />
            </div>
          <//>
          <${Row} label="Task Success" on=${step === 1} hint=${r.task_success ? TS[r.task_success][0] : ""} keys=${html`<${Kbd} k="1-5" />`}>
            <${Segmented} value=${r.task_success} onChange=${(v) => set("task_success", v)}
              options=${FIVE.map((n) => ({ value: n, label: String(n), hint: TS[n][1] }))} />
          <//>
          <${Row} label="Steering" on=${step === 2} hint=${r.steering ? ST[r.steering][0] : ""} keys=${html`<${Kbd} k="1-5" />`}>
            <${Segmented} value=${r.steering} onChange=${(v) => set("steering", v)}
              options=${FIVE.map((n) => ({ value: n, label: String(n), hint: ST[n][1] }))} />
          <//>
          <div class=${`rate-line ${line.cls}`} role="status">${line.ic ? html`<span class="ic">${line.ic}</span>` : null}<span>${line.text}</span>
            ${step > 0 && step < 3 && !yes ? html`<span class="back"><${Kbd} k="⌫" /> back</span>` : null}</div>
        </div>
        <div class="rate-notes">
          <${Field} short=${s.short} sectionKey="validation_notes" section=${notes} minRows=${8}
            label=${html`${notes?.heading || "Validation Notes"}<${Kbd} k="⌥1" />`} />
        </div>
        ${rev ? html`<div class="coll rate-rev">
          <button onClick=${() => setRevOpen(!revOpen)} aria-expanded=${revOpen}>
            <span class="tri">${revOpen ? "▾" : "▸"}</span><span>Notes for the reviewer</span><${Kbd} k="⌥2" /><span class="faint">optional</span>
          </button>
          ${revOpen ? html`<${Field} short=${s.short} sectionKey="reviewer_notes" section=${rev} minRows=${3} />` : null}
        </div>` : null}
      </div>
      <div class="scol rate-gcol">
        <${Grid} grid=${grid} ts=${yes ? null : r.task_success} st=${yes ? null : r.steering} typeLabel=${typeLabel} />
      </div>
      ${run?.facts ? html`<div class="scol rate-side">
        <div class="sa-h" style="display:flex;justify-content:space-between;align-items:center"><span class="label">${runName(run)}</span><${PackButton} short=${s.short} run=${run.name} /></div>
        <div class="sa-stats sm-stats">
          <${Stat} n=${f.turns} unit=${f.turns === 1 ? "your turn" : "your turns"} />
          <${Stat} n=${mins(f.active_min)} unit="active min" />
          <${Stat} n=${mins(f.wall_min)} unit="wall min" />
        </div>
        <ul class="rate-facts">
          ${f.task_claude_min != null ? html`<li><span class="k">Claude on task</span><span><b class="num">${mins(f.task_claude_min)}</b> of 90 min
            <span class="rate-meter"><i class=${f.task_claude_min >= 90 ? "bad" : f.task_claude_min >= 60 ? "warn" : ""}
              style=${{ width: `${Math.min(100, (f.task_claude_min / 90) * 100)}%` }}></i></span></span></li>` : null}
          ${f.tests?.runs ? html`<li><span class="k">Tests</span><span>${plural(f.tests.runs, "run")}, last${" "}${f.tests.last_ok ? html`<span class="sa-ok">passed</span>` : html`<span class="sa-bad">failed</span>`}${f.tests.after_last_edit ? " after last edit" : ""}</span></li>` : null}
          ${f.files_edited?.length ? html`<li><span class="k">Edited</span><span>${plural(f.files_edited.length, "file")}</span></li>` : null}
          <li><span class="k">Model</span><span>${f.model_ok && f.effort_ok ? "Opus 5.5 · high ✓" : html`<span class="sa-bad">model or effort wrong</span>`}</span></li>
        </ul>
        <${RunNotes} notes=${run.notes} max=${6} />
        <div class="sa-colls">
          ${replies.length ? html`<${Collapsible} title="Your replies" count=${replies.length}><${Transcript} tx=${tx} only="human" /><//>` : null}
          ${run.moments?.length ? html`<${Collapsible} title="Measured" count=${run.moments.length}
              right=${warn ? html`<span class="sa-warnc">⚠ ${warn}</span>` : null}><${Moments} moments=${run.moments} /><//>` : null}
          ${f.diffstat?.length ? html`<${Collapsible} title="Changes" count=${f.diffstat.length}><${Diffstat} diff=${f.diffstat} bare=${true} /><//>` : null}
          ${hasDiff(run) ? html`<${Collapsible} title="Diff"><${DiffText} short=${s.short} run=${run.name} /><//>` : null}
          ${f.transcripts !== 0 && !(tx.entries && !tx.entries.length) ? html`<${Collapsible} title="Transcript" count=${tx.entries?.length}><${Transcript} tx=${tx} /><//>` : null}
        </div>
      </div>` : null}
    </div>
  <//>`;
}
