import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { Surface } from "../../components/surface.js";
import { Field, flushFields } from "../../components/field.js";
import { Chip, Kbd, JobNote } from "../../components/ui.js";
import { act, reportError, report as reportWrite, useJob } from "../../lib/actions.js";
import { RunChoice } from "./decide.js";
import { api } from "../../lib/api.js";
import { fullSlot, hhmm, plural, clock } from "../../lib/format.js";
import { toast, navigate, tick } from "../../lib/store.js";
import { pushContext, zone } from "../../lib/keys.js";

// Studio surface (design 3.6): failures first, then the checklist, then what Studio holds.
// The right pane is Austin's own fields; the app never puts text into them.

const TEXT_KEYS = ["opening_prompt", "goal", "decision_points", "codebase", "initial_state", "validation_notes", "reviewer_notes"];

/** Every Studio field the save writes, in Studio's order. */
const STUDIO_FIELDS = [
  ["field_repo_name", "Repo name", (t) => t.summary.repo?.full_name],
  ["field_repo_url", "Repo URL", (t) => t.summary.repo?.pinned_url, "mono"],
  ["field_primary_language", "Primary language", (t) => t.summary.repo?.language],
  ["field_opening_prompt", "Opening Prompt", (t) => t.sections.opening_prompt?.text, null, "opening_prompt"],
  ["field_high_level_goal", "High-Level Task Goal", (t) => t.sections.goal?.text, null, "goal"],
  ["field_rationale_difficulty", "Decision Points", (t) => t.sections.decision_points?.text, null, "decision_points"],
  ["field_codebase_overview", "Difficulty & Codebase", (t) => t.sections.codebase?.text, null, "codebase"],
  ["field_initial_state", "Initial State", (t) => t.sections.initial_state?.text, null, "initial_state",
    (t) => t.summary.slot?.action === "Operate"],
  ["field_self_check", "Validation Notes", (t) => t.sections.validation_notes?.text, null, "validation_notes"],
  ["field_one_shot", "One-shot", (t) => t.ratings?.one_shot, null, "ratings"],
  ["field_task_success", "Task Success", (t) => t.ratings?.task_success, null, "ratings"],
  ["field_steering", "Steering", (t) => t.ratings?.steering, null, "ratings"],
  ["field_claude_minutes", "Claude minutes", (t) => t.ratings?.claude_minutes ?? t.summary.claude_min, null, "ratings"],
  ["field_conversation_log", "Conversation log", (t) => t.gists?.conversation_log, "mono", "gists"],
  ["field_final_output", "Final diff", (t) => t.gists?.final_output, "mono", "gists"],
  ["field_writer_submission_notes", "Notes for the Reviewer", (t) => t.sections.reviewer_notes?.text, null, "reviewer_notes"],
];

const studioFields = (t) => STUDIO_FIELDS.filter((f) => !f[5] || f[5](t));

/** 14:29, yesterday 16:02, or Oct 28 16:02. */
export function when(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 864e5);
  const t = hhmm(iso).padStart(5, "0");
  if (diff === 0) return t;
  if (diff === 1) return `yesterday ${t}`;
  return `${d.toLocaleDateString("en-US", { month: "short", day: "numeric" })} ${t}`;
}

const listJoin = (xs) => xs.length <= 1 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`;
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function counter(key, s) {
  if (!s) return "";
  if (!s.words) return "empty";
  if (key === "opening_prompt") return plural(s.words, "word");
  if (key === "decision_points") return `${s.pairs?.better || 0} better · ${s.pairs?.worse || 0} worse`;
  return `${plural(s.sentences, "sentence")} · ${plural(s.words, "word")}`;
}


function heading(task, key) {
  return task.sections[key]?.heading || ({ ratings: "Ratings", gists: "Gist links" }[key] || key);
}

/** Everything the surface needs, derived from the TaskDetail. */
function derive(task) {
  const st = task.studio || {};
  const qc = st.qc || null;
  const dims = qc?.dims || [];
  const rows = task.checklist || [];
  // With an AutoQC result in hand, its dimensions replace the summary checklist row.
  const listed = rows.filter((r) => !(qc && r.key === "autoqc"));
  const failures = [
    ...dims.filter((d) => d.status === "fail").map((d) => ({
      id: `qc:${d.section}:${d.dimension}`, qc: true, title: d.dimension, section: d.section,
      text: d.justification, field: d.field_key || null, span: null })),
    ...listed.filter((r) => r.status === "fail").map((r) => ({
      id: `row:${r.key}`, qc: r.key === "autoqc", title: r.title, text: r.detail,
      field: r.fix?.action === "focus_field" ? r.fix.field : null, span: r.fix?.span || null })),
  ];
  const warnings = dims.filter((d) => d.status === "neutral");
  const passDims = dims.filter((d) => d.status === "pass");
  const passCount = qc?.counts?.pass ?? passDims.length;
  const total = qc ? (qc.counts ? qc.counts.pass + qc.counts.fail + qc.counts.neutral : dims.length) : 0;
  const others = listed.filter((r) => r.status !== "fail");
  const changed = st.changed_since_save || [];
  const red = TEXT_KEYS.flatMap((k) => (task.sections[k]?.overlap || [])
    .filter((o) => o.level === "red" && !o.accepted).map((o) => ({ ...o, key: k })));
  const running = qc?.status === "running";
  const ready = !!st.saved_at && !changed.length && qc?.status === "completed" && !failures.length && !red.length
    && !rows.some((r) => r.status === "fail" || r.status === "pending");
  const flags = TEXT_KEYS.flatMap((k) => (task.sections[k]?.lint || []).map((f) => ({ ...f, key: k })));
  const lintErrors = flags.filter((f) => f.level === "error").length;
  const qcAt = qc?.status === "completed" ? when(qc.finished_at || qc.started_at) : "";
  return { st, qc, qcAt, failures, warnings, passDims, passCount, total, others, changed, red, running, ready,
    flags, lintErrors, lintWarns: flags.length - lintErrors };
}

/** One faint line under the header saying exactly what ⌘⇧↵ will write. */
function Writes({ task, d, studioUrl }) {
  let text;
  if (d.ready) {
    text = `Moves ${task.summary.label} from ${d.st.status || "Writing"} to Awaiting Review, then removes its lanes and code clone`;
  } else {
    const missing = [["conversation_log", "conversation log"], ["final_output", "final diff"]]
      .filter(([k]) => !task.gists?.[k]).map((m) => m[1]);
    const n = studioFields(task).length;
    const names = d.changed.map((k) => heading(task, k));
    const fields = !d.st.saved_at ? `all ${n} fields for the first time`
      : names.length ? `all ${n} fields (${listJoin(names)} changed)` : `all ${n} fields (nothing changed)`;
    // Name the run the gists come from: the chosen run, else the latest validation run (the checklist's row).
    const vrun = (task.checklist || []).find((r) => r.key === "validation_run")?.run;
    const m = /^(calib|validate)-(\d+)$/.exec(vrun || "");
    const runName = m ? `${m[1] === "calib" ? "calibration" : "validation"} ${m[2]}'s` : "the run's";
    const steps = [missing.length ? `uploads ${runName} ${listJoin(missing)} as secret gists (austeane)` : null, `saves ${fields}`].filter(Boolean);
    text = cap(`${steps.join(", ")}, then ${d.qc ? "reruns" : "runs"} AutoQC`);
  }
  const redIn = listJoin([...new Set(d.red.map((o) => heading(task, o.key)))]);
  return html`<div class="why sb-writes">
    <span class="sb-wtext">${text}${d.red.length ? html`<span class="sb-block"> · Blocked by red overlap in ${redIn}</span>` : null}</span>
    ${studioUrl ? html`<a class="sb-slink" href=${studioUrl} target="_blank" rel="noopener" title="Open this task in Studio"><${Kbd} k="o" />Studio ↗</a>` : null}
  </div>`;
}

function FailCard({ f, at, sel, locked, onGo, onJustify, onPick }) {
  const goLabel = f.field === "opening_prompt" && locked ? "Edit prompt (needs a new validation run)" : "Go to the field";
  return html`<div class=${`sb-fail ${sel ? "sel" : ""}`} onClick=${onPick}>
    <span class="sb-ic">✗</span>
    <div class="sb-fbody">
      <div class="sb-ftitle">${f.qc && !/^AutoQC/.test(f.title) ? html`<span class="sb-src">AutoQC${at ? ` ${at}` : ""}</span>` : null}${f.title}</div>
      ${f.text ? html`<blockquote class=${f.qc ? "sb-q" : "sb-plain"}>${f.text}</blockquote>` : null}
      <div class="sb-facts">
        ${f.field ? html`<button class="btn sm" onClick=${(e) => { e.stopPropagation(); onPick(); onGo(f); }}>${sel ? html`<${Kbd} k="↵" />` : null}${goLabel}</button>` : null}
        ${f.qc ? html`<button class="btn sm quiet" onClick=${(e) => { e.stopPropagation(); onJustify(); }}><${Kbd} k="n" />Justify in reviewer notes</button>` : null}
      </div>
    </div>
  </div>`;
}

/** A one-line checklist row. Rows with an action show "↵ <what it does>" when selected or hovered. */
function Row({ status, title, detail, sel, act, open, children }) {
  const icon = { pass: "✓", fail: "✗", warn: "!", pending: "◌" }[status] || "·";
  const toggles = act && open != null;
  return html`<div class=${`sb-row ${status} ${sel ? "sel" : ""} ${act ? "act" : ""}`}
      onClick=${act ? act.run : undefined} aria-expanded=${toggles ? open : undefined}>
    <span class="sb-ic">${icon}</span>
    <span class="sb-tt">${title}</span>
    <span class="sb-dd">${detail}${toggles ? html` <span class="sb-tri">${open ? "▾" : "▸"}</span>` : null}</span>
    <span class="sb-rt">${act?.hint ? html`<span class="sb-hint"><${Kbd} k="↵" />${act.hint}</span>` : null}</span>
    ${open && children ? html`<div class="sb-sub" onClick=${(e) => e.stopPropagation()}>${children}</div>` : null}
  </div>`;
}

function Coll({ title, count, sel, open, onToggle, children }) {
  return html`<div class=${`sb-coll ${sel ? "sel" : ""}`}>
    <button onClick=${onToggle} aria-expanded=${open}>
      <span class="sb-tri">${open ? "▾" : "▸"}</span><span>${title}</span>${count != null ? html`<span class="faint num">${count}</span>` : null}
      <span class="sb-hint"><${Kbd} k="↵" />${open ? "Hide" : "Show"}</span>
    </button>
    ${open ? html`<div class="sb-cbody">${children}</div>` : null}
  </div>`;
}

/**
 * What Studio holds, field by field. Uses Studio's own copy when the task carries one
 * (task.studio.fields); otherwise a changed field shows the local text with "changed".
 */
function AsSaved({ task, d, onOpenField }) {
  const [openRow, setOpenRow] = useState(null);
  const remote = task.studio?.fields || null;
  const changed = new Set(d.changed);
  return html`<div class="sb-saved">
    ${studioFields(task).map(([id, name, get, mono, key]) => {
      const local = get(task);
      const shown = remote && id in remote ? remote[id] : local;
      const diff = remote ? String(remote[id] ?? "") !== String(local ?? "") : changed.has(key);
      const empty = shown == null || shown === "";
      const isText = key && TEXT_KEYS.includes(key);
      const open = openRow === id;
      return html`<div class=${`sb-srow ${open ? "open" : ""}`} onClick=${() => !empty && setOpenRow(open ? null : id)}>
        <span class="sb-sname">${name}</span>
        <span class=${`sb-sval ${mono ? "mono" : ""} ${empty ? "faint" : ""} ${open ? "full" : ""}`}>${empty ? "not set" : String(shown)}</span>
        <span class="sb-sst">${!d.st.saved_at ? html`<span class="faint">not saved</span>`
          : diff ? html`<${Chip} tone="amber" title="Changed here since the last save. Studio still holds the earlier text.">changed<//>` : null}
          ${isText ? html`<button class="sb-link" title="Open this field" onClick=${(e) => { e.stopPropagation(); onOpenField(key); }}>edit</button>` : null}</span>
      </div>`;
    })}
  </div>`;
}

/** The right pane: Austin's own fields, one open at a time. */
function Fields({ task, d, open, setOpen, focusReq }) {
  const keys = TEXT_KEYS.filter((k) => task.sections[k]);
  const failing = new Set(d.failures.map((f) => f.field).filter(Boolean));
  const changed = new Set(d.changed);
  const redKeys = new Set(d.red.map((o) => o.key));
  const pane = useRef(null);
  useEffect(() => {
    if (!focusReq) return;
    const ta = pane.current?.querySelector(".sb-fopen textarea");
    if (!ta) return;
    ta.focus();
    if (focusReq.span) ta.setSelectionRange(focusReq.span[0], focusReq.span[1]);
    pane.current.querySelector(".sb-fopen")?.scrollIntoView({ block: "nearest" });
  }, [focusReq]);
  return html`<div class="sb-fields" ref=${pane}>
    ${keys.map((k, i) => {
      const s = task.sections[k];
      const isOpen = open === k;
      return html`<div class=${`sb-fitem ${isOpen ? "sb-fopen" : ""}`} key=${k}>
        <button class="sb-fhead" onClick=${() => setOpen(isOpen ? null : k)} aria-expanded=${isOpen}>
          <span class="sb-tri">${isOpen ? "▾" : "▸"}</span>
          <span class="sb-fname">${s.heading}</span>
          <${Kbd} k=${`⌥${i + 1}`} />
          ${failing.has(k) ? html`<${Chip} tone="red">AutoQC failed<//>` : null}
          ${redKeys.has(k) ? html`<${Chip} tone="red">red overlap<//>` : null}
          ${changed.has(k) ? html`<${Chip} tone="amber" title="Changed since the last save to Studio">changed<//>` : null}
          <span class="sb-fcount num">${s.locked ? html`<span title=${s.locked.reason}>locked · </span>` : null}${counter(k, s)}</span>
        </button>
        ${isOpen ? html`<div class="sb-fbox"><${Field} key=${k} short=${task.summary.short} sectionKey=${k} section=${s} minRows=${5} /></div>` : null}
      </div>`;
    })}
  </div>`;
}

function Feedback({ text, since, onAnswer }) {
  return html`<section class="sb-feedback" aria-label="Reviewer feedback">
    <div class="sb-fbhead">
      <span class="label">Reviewer feedback</span>
      ${since ? html`<span class="sb-fbwhen">${since}</span>` : null}
      <span style="flex:1"></span>
      <button class="btn sm quiet" onClick=${onAnswer}><${Kbd} k="n" />Answer in reviewer notes</button>
    </div>
    <blockquote>${text}</blockquote>
  </section>`;
}

/** The submit button's label. Both states are stacked so the button keeps its width when armed. */
function SubmitLabel({ armed, label }) {
  return html`<span class="sb-sz">
    <span class=${armed ? "hide" : ""} aria-hidden=${armed}><${Kbd} k="⌘⇧↵ ⌘⇧↵" />Submit (press twice)</span>
    <span class=${armed ? "" : "hide"} aria-hidden=${!armed}><${Kbd} k="⌘⇧↵" />Press again to submit</span>
  </span>`;
}

/**
 * The studio surface. Reviewer feedback, when Studio has any, is pinned above everything else.
 */
export function StudioSurface({ item, task, setPrimary }) {
  if (task.studio?.qc?.status === "running") tick.value; // the AutoQC clock is the only timer here
  const short = task.summary.short;
  const d = derive(task);
  // The queue decides between saving and submitting (the server re-checks every row); the local rows
  // only decide when the surface is open without a queue row.
  if (item?.kind === "submit") d.ready = true;
  else if (["save", "fix_qc", "feedback"].includes(item?.kind)) d.ready = false;
  const saving = useJob(["save", "studio_save"], short);
  const qcJob = useJob(["qc", "qc_wait"], short);
  const submitting = useJob(["submit"], short);
  const busyJob = submitting || saving || qcJob;
  const feedbackText = task.studio?.feedback;
  const showFeedback = !!feedbackText;
  const fieldKeys = TEXT_KEYS.filter((k) => task.sections[k]);

  const defaultOpen = () => {
    const f = d.failures.find((x) => x.field && task.sections[x.field]);
    if (f) return f.field;
    if (d.red[0]) return d.red[0].key;
    const c = d.changed.find((k) => task.sections[k]);
    if (c) return c;
    return task.sections.goal ? "goal" : fieldKeys[0];
  };
  const [open, setOpen] = useState(defaultOpen);
  const [focusReq, setFocusReq] = useState(null);
  const [expanded, setExpanded] = useState({});
  const [armed, setArmed] = useState(null);
  // A row is selected from the start only when something failed; otherwise ↑/↓ starts the selection.
  const [sel, setSel] = useState(() => (derive(task).failures.length ? 0 : -1));
  const armTimer = useRef(null);

  const openField = (key, opts = {}) => {
    if (!key || !task.sections[key]) return;
    setOpen(key);
    if (opts.focus) setFocusReq({ key, span: opts.span || null, at: Date.now() });
  };
  const go = (f) => {
    if (f.field === "opening_prompt" && task.sections.opening_prompt?.locked) {
      navigate(`/task/${short}`, "#field=opening_prompt");
      return;
    }
    openField(f.field, { focus: true, span: f.span });
  };
  const justify = () => openField("reviewer_notes", { focus: true });
  const toggle = (k) => setExpanded((x) => ({ ...x, [k]: !x[k] }));

  async function save() {
    if (d.red.length) {
      toast(`Red overlap in ${heading(task, d.red[0].key)} blocks saving. Reword it or mark it as your own words.`, { level: "error" });
      openField(d.red[0].key, { focus: true, span: [d.red[0].start, d.red[0].end] });
      return;
    }
    if (saving) return;
    await flushFields();
    await act(`/api/tasks/${short}/studio/save`, {});
  }
  async function rerun() {
    if (qcJob) return;
    await act(`/api/tasks/${short}/studio/qc`, { spec: "AutoQC" });
  }
  function arm(nonce, secs = 4, summary = "") {
    clearTimeout(armTimer.current);
    setArmed({ nonce, until: Date.now() + secs * 1000, summary });
    armTimer.current = setTimeout(() => setArmed(null), secs * 1000);
  }
  // Two presses (contract 5.1): the first asks the server, which re-checks and holds a nonce for 4 s;
  // the second sends it back. The server re-reads the Studio status before the transition.
  async function submit() {
    if (submitting) return;
    if (armed && Date.now() < armed.until && armed.nonce) {
      const nonce = armed.nonce;
      clearTimeout(armTimer.current); setArmed(null);
      try { reportWrite(await api.send("POST", `/api/tasks/${short}/studio/submit`, { confirm: nonce })); }
      catch (e) { reportError(e); }
      return;
    }
    try {
      const r = await api.send("POST", `/api/tasks/${short}/studio/submit`, {});
      // A server without the two-step answers the first press; show what it said.
      reportWrite(r);
    } catch (e) {
      if (e.code === "needs_confirm" && e.details?.nonce) arm(e.details.nonce, e.details.expires_in || 4, e.details.summary);
      else reportError(e);
    }
  }
  useEffect(() => () => clearTimeout(armTimer.current), []);

  // What ↵ does on each row. Rows without an action are skipped by ↑/↓.
  const failAct = (f) => f.field ? { hint: "Go to the field", run: () => go(f) } : f.qc ? { hint: "Justify in reviewer notes", run: justify } : null;
  const warnAct = (w) => w.field_key && task.sections[w.field_key]
    ? { hint: "Go to the field", run: () => go({ field: w.field_key }) }
    : { hint: "Justify in reviewer notes", run: justify };
  const rowAct = (r) => {
    if (r.fix?.action === "focus_field" && r.fix.field) return { hint: "Go to the field", run: () => go({ field: r.fix.field, span: r.fix.span }) };
    if (r.key === "lint" && d.flags.length) return { hint: expanded.lint ? "Hide the flags" : "Show the flags", run: () => toggle("lint") };
    if (r.key === "validation_run") return { hint: "Open the run", run: () => navigate(`/task/${short}`, "#field=validation") };
    if (r.key === "gists" && task.gists?.conversation_log) return { hint: "Open the log ↗", run: () => window.open(task.gists.conversation_log, "_blank", "noopener") };
    return null;
  };
  const entries = [
    ...d.failures.map((f) => ({ id: f.id, act: failAct(f) })),
    ...d.warnings.map((w) => ({ id: `w:${w.dimension}`, act: warnAct(w) })),
    ...d.others.map((r) => ({ id: `r:${r.key}`, act: rowAct(r) })),
    ...(d.passDims.length ? [{ id: "qcpass", act: { run: () => toggle("qcpass") } }] : []),
    { id: "saved", act: { run: () => toggle("saved") } },
  ].filter((e) => e.act);
  const selIdx = sel < 0 ? -1 : Math.min(sel, entries.length - 1);
  const isSel = (id) => entries[selIdx]?.id === id;
  const pick = (id) => { const i = entries.findIndex((e) => e.id === id); if (i >= 0) setSel(i); };
  const actOf = (id) => {
    const e = entries.find((x) => x.id === id);
    return e ? { ...e.act, run: () => { pick(id); e.act.run(); } } : null;
  };

  const live = useRef({});
  live.current = {
    primary: d.ready ? submit : save, rerun, justify,
    studio: () => task.studio?.url && window.open(task.studio.url, "_blank", "noopener"),
    move: (n) => setSel((s) => (s < 0 ? 0 : Math.max(0, Math.min(entries.length - 1, Math.min(s, entries.length - 1) + n)))),
    enter: () => {
      const a = document.activeElement;
      if (a && a !== document.body && a.matches?.("button, a")) { a.click(); return; }
      entries[selIdx]?.act.run();
    },
    field: (i) => fieldKeys[i] && openField(fieldKeys[i], { focus: true }),
  };
  useEffect(() => {
    setPrimary?.(() => live.current.primary());
    const pop = pushContext(`studio:${short}`, {
      q: () => live.current.rerun(), n: () => live.current.justify(), o: () => live.current.studio(),
      Enter: () => live.current.enter(), ArrowDown: () => live.current.move(1), ArrowUp: () => live.current.move(-1),
    });
    const onAlt = (e) => {
      if (!e.altKey || e.metaKey || e.ctrlKey || !/^Digit[1-9]$/.test(e.code) || zone() === "terminal") return;
      e.preventDefault();
      live.current.field(+e.code.slice(5) - 1);
    };
    window.addEventListener("keydown", onAlt);
    return () => { pop(); window.removeEventListener("keydown", onAlt); setPrimary?.(null); };
  }, [short]);

  const title = d.ready ? "Submit for review" : item.title;
  const primary = d.ready
    ? { label: html`<${SubmitLabel} label=${task.summary.label} />`, run: submit, armed: !!armed,
        armedLabel: html`<${SubmitLabel} armed label=${task.summary.label} />` }
    : { label: saving ? "Saving to Studio…" : d.qc ? "Save to Studio and rerun AutoQC" : "Save to Studio and run AutoQC", run: save,
        disabled: !!d.red.length || !!saving || !!submitting || !!d.running,
        why: d.red.length ? "Unaccepted red overlap blocks saving" : d.running ? "AutoQC is running on what Studio holds" : "" };
  // AutoQC alone only makes sense once something is in Studio.
  const secondary = d.st.saved_at ? [{ label: d.ready ? "Rerun AutoQC" : d.qc ? "Rerun only" : "Run AutoQC only", key: "q", run: rerun,
    disabled: !!qcJob || !!saving, title: "Run AutoQC again without saving" }] : [];
  // The AutoQC time sits on its card or count and the feedback time on the reviewer band,
  // so the header carries the queue's reason only when neither is on screen.
  const reason = d.qc || showFeedback ? "" : item.reason;
  const passTitle = `${plural(d.passCount, "AutoQC check")} passed${d.qcAt && !d.failures.some((f) => f.qc) ? ` · ${d.qcAt}` : ""}`;

  const statusRow = (r) => {
    const id = `r:${r.key}`;
    if (r.key === "studio_sync" && d.st.saved_at) {
      const names = d.changed.map((k) => heading(task, k));
      const detail = `${when(d.st.saved_at)} · ${names.length ? `changed since: ${names.join(", ")}` : "nothing changed since"}`;
      return html`<${Row} key=${r.key} status=${r.status} title="Saved to Studio" detail=${detail} />`;
    }
    if (r.key === "lint" && d.flags.length) {
      return html`<${Row} key=${r.key} status=${r.status} title=${r.title} sel=${isSel(id)} act=${actOf(id)} open=${!!expanded.lint}
          detail=${`${plural(d.lintErrors, "error")} · ${plural(d.lintWarns, "warning")}`}>
        <ul class="sb-flags">${d.flags.map((f, i) => html`<li class=${f.level} onClick=${() => openField(f.key, { focus: true })}>
          <span class="sb-fl-f">${i && d.flags[i - 1].key === f.key ? "" : heading(task, f.key)}</span><span>${f.level === "error" ? "✗ " : ""}${f.message}</span></li>`)}</ul>
      <//>`;
    }
    return html`<${Row} key=${r.key} status=${r.status} title=${r.title} detail=${r.detail} sel=${isSel(id)} act=${actOf(id)} />`;
  };

  return html`<${Surface} title=${title} label=${task.summary.label} dossier=${task.summary.dossier} slotLine=${fullSlot(task.summary.slot)}
      reason=${reason} primary=${primary} secondary=${secondary}
      head=${html`<${Writes} task=${task} d=${d} studioUrl=${task.studio?.url} />
        <${RunChoice} task=${task} />
        ${busyJob && !(busyJob === qcJob && d.running) ? html`<${JobNote} job=${busyJob} what=${busyJob === submitting ? "Submitting" : busyJob === saving ? "Saving to Studio" : "AutoQC"} />` : null}`}>
    ${showFeedback ? html`<${Feedback} text=${feedbackText} since=${item.surface === "feedback" ? item.reason : ""} onAnswer=${justify} />` : null}
    <div class=${`sbody sb-body ${showFeedback ? "sb-wide-fields" : ""}`}>
      <div class="scol sb-main">
        ${d.running ? html`<${Row} status="pending" title="AutoQC running" detail=${html`<span class="num">${clock(d.qc.started_at)}</span> · checks every 15 s`} />` : null}
        ${d.failures.map((f) => html`<${FailCard} key=${f.id} f=${f} at=${d.qcAt} sel=${isSel(f.id)} locked=${!!task.sections.opening_prompt?.locked}
            onGo=${go} onJustify=${justify} onPick=${() => pick(f.id)} />`)}
        <div class="sb-list">
          ${d.warnings.map((w) => html`<${Row} key=${w.dimension} status="warn"
              title=${html`<span class="sb-src">AutoQC</span>${w.dimension}`} detail=${w.justification}
              sel=${isSel(`w:${w.dimension}`)} act=${actOf(`w:${w.dimension}`)} />`)}
          ${d.others.map(statusRow)}
          ${d.qc && d.passCount ? (d.passDims.length
            ? html`<${Coll} title=${passTitle} sel=${isSel("qcpass")} open=${!!expanded.qcpass} onToggle=${actOf("qcpass").run}>
                ${d.passDims.map((p) => html`<div class="sb-pass"><span class="sb-ic">✓</span><span class="sb-tt">${p.dimension}</span><span class="faint">${p.section}</span></div>`)}
              <//>`
            : html`<div class="sb-flat">${passTitle}</div>`) : null}
          <${Coll} title="Studio fields as saved" count=${studioFields(task).length} sel=${isSel("saved")} open=${!!expanded.saved}
              onToggle=${actOf("saved").run}>
            <${AsSaved} task=${task} d=${d} onOpenField=${(k) => openField(k, { focus: true })} />
          <//>
        </div>
      </div>
      <div class="scol sb-side">
        <${Fields} task=${task} d=${d} open=${open} setOpen=${setOpen} focusReq=${focusReq} />
      </div>
    </div>
  <//>`;
}

export default function Studio(props) {
  return html`<${StudioSurface} ...${props} />`;
}
