// The Write step (spec 3.5.3): one field at a time, the goal above the fields that build on it, and the findings
// panel beside the editor. Only his typing goes into the fields; the panel is AI text with the AI mark.
import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { Tabs, Seg, Btn, Flag } from "../../components/primitives.js";
import { Editor, saveRatings, ratingsSaving, overlapRow } from "../../components/field.js";
import { FindingsPanel } from "./findings.js";
import { ASKS, FIELD_NAME, FIELD_LOWER, clip } from "./words.js";
import { finishWriting, usePrimary, dropTask } from "./data.js";
import { drops, cancelDrop } from "../surfaces/decide.js";
import { FeedbackChecklist, selectInEditor } from "./feedback.js";
import { useUnderstand, codeLink } from "./understand.js";
import { AiTag } from "../../components/primitives.js";
import { navigate } from "../../lib/store.js";

const SCALE = { "Enterprise-grade": "Enterprise", "Personal-scale": "Personal" };
const ROWS = { goal: 5, decision_points: 9, codebase: 7, initial_state: 5, validation_notes: 7, reviewer_notes: 4 };

/** "What Mercor asks ▸": the field's rules, quoted. */
export function Asks({ k, style }) {
  const [open, setOpen] = useState(false);
  const lines = k === "opening_prompt" ? (style?.rules_md ? [style.rules_md] : []) : ASKS[k] || [];
  if (!lines.length) return null;
  return html`<div class="wb-asks">
    <button type="button" class="wb-asks-btn" aria-expanded=${open} onClick=${() => setOpen(!open)}>What Mercor asks <span class="wb-tri">${open ? "▾" : "▸"}</span></button>
    ${open ? html`<ul class="wb-asks-list">${lines.map((l) => html`<li>${l}</li>`)}</ul>` : null}
  </div>`;
}

/** The upstream issues and pull requests near this task (GitHub titles, a link each), for Codebase understanding. */
export function Upstream({ origin }) {
  const up = origin?.upstream || [];
  if (!up.length) return null;
  return html`<div class="wb-upstream">
    <div class="label-sm">Related upstream threads</div>
    <p class="faint">Name these in Codebase understanding.</p>
    <ul class="wb-up">${up.map((u) => html`<li><a href=${u.url} target="_blank" rel="noopener">${u.title}</a>${u.state ? html` <span class="faint">${u.state}</span>` : null}</li>`)}</ul>
  </div>`;
}

const RESEARCH_FIELDS = ["goal", "decision_points", "codebase", "initial_state"];
const SOURCE = { research: "Claude's research", dossier: "The dossier", brief: "The repo brief" };
const OPEN_KEY = "johto.wb.research.open";

function readOpen() { try { return localStorage.getItem(OPEN_KEY) !== "0"; } catch { return true; } }
function writeOpen(v) { try { localStorage.setItem(OPEN_KEY, v ? "1" : "0"); } catch { /* per-viewer nicety only */ } }

/** The research behind the field he is writing: Claude's task understanding, the dossier opportunity and the brief's
 *  seed, grouped by what bears on this field. Read-only facts with the AI mark, so a rewrite starts from what the
 *  task was built on. Nothing here goes into the field: he types. */
export function Research({ short, k }) {
  const { u, err } = useUnderstand(short);
  const repo = u?.repo || {};
  const [open, setOpen] = useState(readOpen);
  if (!RESEARCH_FIELDS.includes(k)) return null;
  const groups = u?.fields?.[k] || [];
  const toggle = () => { setOpen(!open); writeOpen(!open); };
  const busy = !!u && (u.state === "queued" || u.state === "generating");
  const n = groups.reduce((a, g) => a + g.items.length, 0);
  const head = html`<div class="ai-head wb-rs-head">
    <button type="button" class="wb-rs-btn" aria-expanded=${open} onClick=${toggle}>
      <${AiTag} label=${`What the task was built on, for ${FIELD_LOWER[k] || FIELD_NAME[k]}`} />
      <span class="faint">${n ? `${n} facts` : ""}</span><span class="wb-tri">${open ? "▾" : "▸"}</span>
    </button>
    <button type="button" class="linkbtn" onClick=${() => navigate(`/task/${short}/understand`)}>Full research →</button>
  </div>`;
  if (!open) return html`<section class="ai-block wb-rs closed">${head}</section>`;
  let body;
  if (err && !u) body = html`<p class="faint">The research didn't load: ${err.message}</p>`;
  else if (!u) body = html`<p class="faint">Loading the research…</p>`;
  else if (!groups.length) body = html`<p class="faint">${busy ? "Claude is still researching the task. The dossier's notes show here once it's done."
    : u.state === "failed" ? "The task research failed, and the dossier has nothing for this field."
    : "Nothing in the research bears on this field."}</p>`;
  else body = html`<div class="wb-rs-body">
    ${busy ? html`<p class="faint">Claude is researching the task again. This is the older research.</p>` : null}
    ${groups.map((g) => html`<div class=${`wb-rs-g src-${g.source}`}>
      <div class="wb-rs-gh"><span class="label-sm">${g.head}</span><span class="wb-rs-src">${SOURCE[g.source] || g.source}</span></div>
      ${g.note ? html`<p class="wb-rs-note">${g.note}</p>` : null}
      <ul class="wb-rs-l">${g.items.map((it) => {
        const href = it.url || (it.path ? codeLink(repo.full_name, repo.commit, it.path) : null);
        return html`<li>${it.path ? (href ? html`<a href=${href} target="_blank" rel="noopener"><code>${it.path}</code></a> `
          : html`<code>${it.path}</code> `) : null}${it.text}${it.url && !it.path ? html` <a href=${it.url} target="_blank" rel="noopener">↗</a>` : null}</li>`;
      })}</ul>
    </div>`)}
  </div>`;
  return html`<section class="ai-block wb-rs" aria-label="The research behind this field">${head}${body}</section>`;
}

/** One line of his goal above the fields that build on it, with Show to read it all in place. */
function GoalPreview({ text, onEdit }) {
  const [open, setOpen] = useState(false);
  if (!(text || "").trim()) {
    return html`<div class="wb-goalpv empty"><span class="label-sm">Your goal</span><span class="faint">Not written yet</span>
      <button type="button" class="btn sm quiet" onClick=${onEdit}>Write it</button></div>`;
  }
  return html`<div class=${`wb-goalpv ${open ? "open" : ""}`}>
    <span class="label-sm">Your goal</span>
    <span class="wb-goalpv-t">${open ? text : clip(text, 150)}</span>
    <button type="button" class="btn sm quiet" aria-expanded=${open} onClick=${() => setOpen(!open)}>${open ? "Hide" : "Show"}</button>
  </div>`;
}

function Grid({ grid, ts, st }) {
  if (!grid) return null;
  return html`<table class="wb-grid" aria-label="Which ratings pass">
    <thead><tr><th>Task success</th>${[1, 2, 3, 4, 5].map((s) => html`<th>Steering ${s}</th>`)}</tr></thead>
    <tbody>${grid.map((row, i) => html`<tr><th>${i + 1}</th>${row.map((ok, j) => html`<td class=${`${ok ? "ok" : "no"} ${ts === i + 1 && st === j + 1 ? "cur" : ""}`}>${ok ? "Passes" : "Rejected"}</td>`)}</tr>`)}</tbody>
  </table>`;
}

/** Ratings: one-shot, task success and steering, with the pass rule in words under them. */
export function Ratings({ short, task, readOnly, runMinutes }) {
  const r = task.ratings || {};
  const [v, setV] = useState({ one_shot: r.one_shot, task_success: r.task_success, steering: r.steering });
  const [rule, setRule] = useState(false);
  useEffect(() => {
    if (ratingsSaving(short)) return;
    setV({ one_shot: r.one_shot, task_success: r.task_success, steering: r.steering });
  }, [r.one_shot, r.task_success, r.steering, short]);
  const pick = (k) => (val) => {
    if (readOnly) return;
    const nv = { ...v, [k]: v[k] === val ? null : val };
    setV(nv);
    saveRatings(short, nv);
  };
  const scale = SCALE[task.summary?.slot?.codebase_type] || "";
  const errors = (r.check || []).filter((c) => c.level === "error").map((c) => c.message);
  const nums = (lo, hi) => Array.from({ length: hi - lo + 1 }, (_, i) => ({ value: lo + i, label: String(lo + i) }));
  const mins = r.claude_minutes ?? runMinutes;   // the run's measured minutes, which Save to Studio sends
  drops.value;
  const dropBtn = (why) => (drops.value[short]
    ? html`<${Btn} kind="secondary" size="sm" label="Cancel the drop" onClick=${() => cancelDrop(short)} />`
    : html`<${Btn} kind="danger" size="sm" label="Drop the task" title="Unclaims the task in Studio after a 10 second undo"
        onClick=${() => dropTask(short, task, why)} />`);
  return html`<div class="wb-rate">
    <div class="wb-rate-row"><span class="wb-rate-l">One-shot</span>
      <${Seg} label="One-shot" value=${v.one_shot} onChange=${pick("one_shot")} options=${[{ value: "No", label: "No" }, { value: "Yes", label: "Yes" }]} /></div>
    <div class="wb-rate-row"><span class="wb-rate-l">Task success</span>
      <${Seg} label="Task success" value=${v.task_success} onChange=${pick("task_success")} options=${nums(1, 5)} /></div>
    <div class="wb-rate-row"><span class="wb-rate-l">Steering</span>
      <${Seg} label="Steering" value=${v.steering} onChange=${pick("steering")} options=${nums(1, 5)} /></div>
    ${v.one_shot === "Yes" ? html`<${Flag} tone="wait" title="Claude did it in one go." actions=${readOnly ? null : dropBtn("one-shot in validation")}>
        Mercor drops a task Claude finishes in one go. Dropping unclaims it in Studio, then you log it in the Discarded Run Log.<//>` : null}
    <div class="wb-rate-rule">
      ${r.passes === true ? html`<span class="pill ok">Passes the ${scale} rule</span>`
        : r.passes === false ? html`<span class="pill stop">Rejected by the ${scale} rule</span><span class="wb-rate-why">${errors[0] || ""}</span>`
        : html`<span class="faint">Pick all three to see the ${scale} rule.</span>`}
      <button type="button" class="wb-asks-btn" aria-expanded=${rule} onClick=${() => setRule(!rule)}>Show the rule <span class="wb-tri">${rule ? "▾" : "▸"}</span></button>
    </div>
    ${r.passes === false && v.one_shot !== "Yes" ? html`<div class="wb-rate-honest"><p>Mercor asks for honest ratings. A task that passes only when the ratings are stretched is too easy, and is dropped.</p>
      ${readOnly ? null : dropBtn("ratings fail the pass rule")}</div>` : null}
    ${rule ? html`<${Grid} grid=${r.grid} ts=${v.task_success} st=${v.steering} />` : null}
    <p class="wb-rate-min">Claude minutes · ${mins != null ? `${Math.round(mins)}, measured from the run` : "not measured yet"}</p>
  </div>`;
}

/**
 * The Write step. `tab` is the field shown; `setTab` changes it. `focus` asks the findings panel to show a row.
 */
export function WriteStep({ short, task, wb, tab, setTab, readOnly, findingReq, showFinding, onDone, reload, goStep }) {
  const keys = wb.write_fields || ["goal", "decision_points", "codebase", "ratings", "validation_notes"];
  const k = keys.includes(tab) ? tab : keys[0];
  const s = task.sections?.[k];
  const fb = wb.feedback?.state === "sent_back" ? wb.feedback : null;
  const [lit, setLit] = useState(null);       // {row, phrase}: his words that match a finding
  const [notesOpen, setNotesOpen] = useState(!!task.sections?.reviewer_notes?.text);
  const [armed, setArmed] = useState(null);
  const inputRef = useRef(null);
  const i = keys.indexOf(k);
  const next = keys[i + 1];

  useEffect(() => { setLit(null); }, [k]);
  // ⌥1 to ⌥5 pick a field (Option-digit, by key code: the character differs by layout).
  useEffect(() => {
    const onKey = (e) => {
      if (!e.altKey || e.metaKey || e.ctrlKey) return;
      const m = /^Digit([1-9])$/.exec(e.code);
      const want = m && keys[+m[1] - 1];
      if (!want) return;
      e.preventDefault();
      setTab(want);
      setTimeout(() => inputRef.current?.focus(), 30);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [keys.join(",")]);

  const SHORT = { codebase: "Codebase", validation_notes: "Notes" };
  const tabs = keys.map((key, n) => ({
    id: key, label: FIELD_NAME[key], short: SHORT[key], done: wb.fields?.[key]?.state === "done",
    todo: !readOnly && !["done", "locked"].includes(wb.fields?.[key]?.state),
    title: `${FIELD_NAME[key]} (⌥${n + 1})${fb?.fields?.includes(key) ? ". The reviewer named this field." : ""}`,
    count: fb?.fields?.includes(key) ? "•" : null,
  }));

  async function done() {
    const r = await finishWriting(short, wb, {
      onMissing: (f) => { const want = f.find((x) => keys.includes(x)) || (f.some((x) => ["one_shot", "task_success", "steering"].includes(x)) ? "ratings" : null); if (want) setTab(want); },
      onArm: (summary) => setArmed(summary || "Press again to go on anyway"),
    });
    if (r === "armed") return;
    setArmed(null);
    if (r === "done") { reload?.(); onDone?.(); }
  }
  const goNext = () => { setTab(next); setTimeout(() => inputRef.current?.focus(), 30); };
  usePrimary(readOnly ? null : next ? goNext : fb ? onDone : done);
  const nextBtn = readOnly ? null : next
    ? html`<${Btn} label=${`Next: ${FIELD_NAME[next]} →`} onClick=${goNext} />`
    : fb ? html`<${Btn} label="Go to submit →" onClick=${onDone} />`
    : html`<${Btn} kind=${armed ? "danger" : "secondary"} label=${armed || "Done writing →"} onClick=${done} title="Checks every field and the ratings, then the next step is Save to Studio" />`;

  // The finding an overlap came from: a grading row by its ref, else the row whose review text holds the phrase.
  const norm = (x) => String(x || "").toLowerCase().replace(/\s+/g, " ");
  const rowFor = (o) => overlapRow(o)
    || (wb.findings?.rows || []).find((r) => norm(r.as_written || r.finding).includes(norm(o.text)))?.id || null;
  const showSource = (o, row) => {
    const phrase = (o.source?.excerpt || "").slice(o.source?.match_start ?? 0, o.source?.match_end ?? undefined) || o.text;
    setLit({ row, phrase: phrase || o.text, ov: o.id });
    showFinding(row, false);
  };

  return html`<div class="wb-write">
    <div class="wb-main">
      ${fb ? html`<${FeedbackChecklist} short=${short} wb=${wb} reload=${reload}
          onStep=${(st) => (goStep ? goStep(st) : st === "submit" && onDone?.())}
          onField=${(f, text) => {
            if (!keys.includes(f)) { if (f === "opening_prompt") goStep?.("prompt"); return; }
            setTab(f);
            setTimeout(() => { if (!(text && selectInEditor(text))) inputRef.current?.focus(); }, 80);
          }} />` : null}
      <${Tabs} label="Fields" tabs=${tabs} value=${k} onChange=${setTab} />
      <div class="wb-fhead">
        <h2>${FIELD_NAME[k]}</h2>
        <${Asks} k=${k} />
      </div>
      ${["decision_points", "codebase"].includes(k) ? html`<${GoalPreview} text=${task.sections?.goal?.text}
          onEdit=${() => setTab("goal")} />` : null}
      ${k === "codebase" ? html`<${Upstream} origin=${wb.origin} />` : null}
      <${Research} short=${short} k=${k} />
      ${k === "ratings" ? html`<${Ratings} short=${short} task=${task} readOnly=${readOnly} runMinutes=${wb.run?.minutes} />
          <div class="wb-ed-meta"><span></span><span class="wb-ed-extra">${nextBtn}</span></div>`
        : s ? html`<${Editor} key=${k} short=${short} sectionKey=${k} section=${s} autofocus=${!readOnly && !fb}
            minRows=${ROWS[k] || 6} label=${FIELD_NAME[k]} inputRef=${inputRef} readOnly=${readOnly}
            lit=${lit?.ov} onShowSource=${showSource} rowFor=${rowFor} extra=${nextBtn} />`
        : html`<p class="faint">This field isn't part of this task.</p>`}
      ${k === "validation_notes" && task.sections?.reviewer_notes ? html`<div class="wb-more">
        <button type="button" class="wb-asks-btn" aria-expanded=${notesOpen} onClick=${() => setNotesOpen(!notesOpen)}>Notes for the reviewer, optional <span class="wb-tri">${notesOpen ? "▾" : "▸"}</span></button>
        ${notesOpen ? html`<div class="wb-more-b"><${Asks} k="reviewer_notes" />
          <${Editor} key="reviewer_notes" short=${short} sectionKey="reviewer_notes" section=${task.sections.reviewer_notes}
            minRows=${3} label="Notes for the reviewer" readOnly=${readOnly} compact=${true} /></div>` : null}
      </div>` : null}
    </div>
    ${(wb.findings?.rows || []).length || (wb.findings?.coverage?.flag && !readOnly)
      ? html`<${FindingsPanel} short=${short} wb=${wb} field=${k} focusReq=${findingReq} lit=${lit} onChanged=${reload} readOnly=${readOnly} />`
      : html`<aside class="wb-side-ref" aria-label="Your opening prompt">
          <div class="label-sm">Your opening prompt, as Claude got it</div>
          <div class="wb-ro-t">${task.sections?.opening_prompt?.text || ""}</div>
          <p class="faint">No findings from a review for this task.</p>
        </aside>`}
  </div>`;
}
