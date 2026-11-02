// The protected editor: only Austin's typing goes into his fields (opening prompt, goal, decision points, codebase
// understanding, initial state, validation notes, notes for the reviewer). No placeholder, no suggestions, writing
// assistance switched off. Saves go through PUT /api/tasks/{short}/sections/{key}, one at a time per task, and come
// back with the overlap guard's matches, which the editor marks in place and names in a flag under it.
import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { api } from "../lib/api.js";
import { fieldFocused, patchTask, toast } from "../lib/store.js";
import { plural } from "../lib/format.js";

// Attributes that switch off writing assistance in browsers and common extensions.
export const NO_ASSIST = {
  autocomplete: "off", autocorrect: "off", autocapitalize: "off", "data-gramm": "false",
  "data-gramm_editor": "false", "data-enable-grammarly": "false", writingsuggestions: "false",
  "data-lt-active": "false",
};

// Saves waiting out their 400 ms. A write that reads the file (Done, Save to Studio) flushes them first.
const pendingSaves = new Map(); // id -> () => Promise
let fieldN = 0;
// One save at a time per task: every field of a task writes the same submission.md.
const chains = new Map(); // short -> Promise
function serial(short, fn) {
  const prev = chains.get(short) || Promise.resolve();
  const next = prev.catch(() => {}).then(fn);
  chains.set(short, next.finally(() => { if (chains.get(short) === next) chains.delete(short); }));
  return next;
}
/** The save barrier: send every field save still waiting out its delay, then wait for the rating writes. */
export async function flushFields() {
  const all = [...pendingSaves.values()];
  pendingSaves.clear();
  await Promise.allSettled(all.map((f) => f()));
  await Promise.allSettled([...ratingWrites.values()].map((w) => w.running).filter(Boolean));
  await Promise.allSettled([...chains.values()]);
}

// Ratings: one writer per task. Each write sends all three values, writes go out one at a time on the task's
// chain, and picks made while one is in flight collapse into a single follow-up write of the newest values.
const ratingWrites = new Map(); // short -> {want, dirty, running}

/** Save {one_shot, task_success, steering}. Resolves with the server's ratings after the newest values are written. */
export function saveRatings(short, values) {
  let w = ratingWrites.get(short);
  if (!w) { w = { want: null, dirty: false, running: null }; ratingWrites.set(short, w); }
  w.want = { one_shot: values.one_shot ?? null, task_success: values.task_success ?? null, steering: values.steering ?? null };
  w.dirty = true;
  if (!w.running) w.running = drainRatings(short, w).finally(() => { w.running = null; });
  return w.running;
}

async function drainRatings(short, w) {
  let out = null;
  while (w.dirty) {
    w.dirty = false;
    const v = w.want;
    out = await serial(short, async () => {
      try {
        const r = await api.send("PUT", `/api/tasks/${short}/ratings`, v);
        if (r?.toast) toast(r.toast.text, r.toast);
        return r?.ratings || null;
      } catch (e) { toast(e.message, { level: "error" }); return null; }
    });
    if (out && !w.dirty) patchTask(short, (t) => { t.ratings = out; });
  }
  return out;
}

/** True while a rating write for this task is waiting or in flight. */
export const ratingsSaving = (short) => !!ratingWrites.get(short)?.running;

// ---------- Points (decision points) ----------

/**
 * Where each decision point sits in the text: blank-line paragraphs, else one point per line when the text is a
 * single block of several lines. The server counts points the same way (api_workbench.points).
 */
export function pointSpans(text) {
  const s = text || "";
  const out = [];
  const re = /\S[\s\S]*?(?=\n[ \t]*\n|$)/g;
  let m;
  while ((m = re.exec(s))) out.push({ start: m.index, end: m.index + m[0].length });
  if (out.length === 1) {
    const lines = [];
    const lre = /[^\n]*\S[^\n]*/g;
    const block = s.slice(out[0].start, out[0].end);
    let l;
    while ((l = lre.exec(block))) lines.push({ start: out[0].start + l.index, end: out[0].start + l.index + l[0].length });
    if (lines.length > 1) return lines.map((x, i) => ({ ...x, n: i + 1 }));
  }
  return out.map((x, i) => ({ ...x, n: i + 1 }));
}

/** The point (1-based) that holds character `at`, or null. */
export function pointAt(text, at) {
  const p = pointSpans(text).find((x) => at >= x.start && at <= x.end);
  return p ? p.n : null;
}

// ---------- Words for the guard's sources ----------

const runWordsOf = (s) => {
  const m = /(calib|validate)-(\d+)/.exec(s || "");
  return m ? `${m[1] === "calib" ? "calibration" : "validation"} ${m[2]}` : "";
};

/** The overlap's source in words ("a finding", "something Claude said in validation 1"). */
export function sourceWords(src) {
  const k = src?.kind;
  if (k === "grading") return "a finding";
  if (k === "transcript_claude") {
    const r = runWordsOf(`${src.label || ""} ${src.ref || ""}`);
    return r ? `something Claude said in ${r}` : "something Claude said";
  }
  if (k === "lead") return "a line from Claude's research";
  if (k === "review") return "the review";
  if (k === "autoqc") return "Mercor's automatic check";
  if (k === "audit") return "the outside review";
  if (k === "kit") return "a finding";
  if (k === "check") return "the deep check";
  // Facts Claude wrote in a helper chat (tasks/<t>/notes/ai-*.md): AI text, never his own run notes.
  if (k === "notes") return "something Claude told you in a helper chat";
  return "text the app showed you";
}

/** Matches in the same place from the same source, as one flag: "Point 4 repeats ... “a” and “b”". */
export function groupOverlaps(overlaps, where) {
  const groups = [];
  for (const o of overlaps) {
    const red = o.level !== "amber";
    const key = `${where(o)}|${red}|${o.source?.kind || ""}|${o.source?.ref || ""}`;
    const g = groups.find((x) => x.key === key);
    if (g) g.items.push(o); else groups.push({ key, red, items: [o] });
  }
  return groups;
}

/** The grading row an overlap came from ("plain.json#R4", "grading.json#R4"), or null. */
export function overlapRow(o) {
  if (o?.source?.kind !== "grading") return null;
  const m = /#(R\d+)\b/.exec(o.source.ref || "");
  return m ? m[1] : null;
}

const clip = (s, n = 90) => { const t = (s || "").replace(/\s+/g, " ").trim(); return t.length > n ? `${t.slice(0, n - 1).trimEnd()}…` : t; };

// ---------- The editor's state ----------

function useProtected({ short, sectionKey, section, onSaved }) {
  const [text, setText] = useState(section?.text || "");
  const [s, setS] = useState(section);
  const [conflict, setConflict] = useState(null); // {text, rev} the file holds
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  const timer = useRef(null);
  const typed = useRef({ typed: 0, pasted: 0, deleted: 0 });
  const myTexts = useRef([]);  // texts this editor saved, to tell a stale refetch from an outside edit
  const id = useRef(`f${++fieldN}`);
  const live = useRef({});
  live.current = { text, s };

  useEffect(() => { setText(section?.text || ""); setS(section); setConflict(null); }, [short, sectionKey]);
  useEffect(() => () => { const f = pendingSaves.get(id.current); if (f) { pendingSaves.delete(id.current); f(); } clearTimeout(timer.current); }, []);

  // The task was refetched (an SSE task event, or vim changed submission.md). With no unsaved typing, take the new
  // text silently; while he types, his text wins and the next save reports a conflict.
  useEffect(() => {
    if (!section) return;
    const cur = live.current.s;
    const changed = section.rev !== cur?.rev || section.text !== cur?.text || section.locked?.reason !== cur?.locked?.reason
      || JSON.stringify(section.lint) !== JSON.stringify(cur?.lint) || JSON.stringify(section.overlap) !== JSON.stringify(cur?.overlap);
    if (!changed) return;
    const dirty = pendingSaves.has(id.current) || live.current.text !== (cur?.text || "");
    const stale = section.text !== cur?.text && myTexts.current.includes(section.text);
    if (stale) return;
    if (!dirty || section.text === live.current.text) { setS(section); if (!dirty) setText(section.text || ""); }
  }, [section?.rev, section?.text, section?.locked?.reason, section?.lint, section?.overlap]);

  function send(v, baseRev) { return serial(short, () => sendNow(v, baseRev)); }
  async function sendNow(v, baseRev) {
    setSaving(true);
    try {
      const r = await api.send("PUT", `/api/tasks/${short}/sections/${sectionKey}`,
        { text: v, base_rev: baseRev ?? live.current.s?.rev, authorship: { ...typed.current } });
      typed.current = { typed: 0, pasted: 0, deleted: 0 };
      if (r?.section) {
        myTexts.current = [...myTexts.current.slice(-6), r.section.text];
        setS(r.section);
        setConflict(null);
        setSavedAt(new Date());
        patchTask(short, (t) => { if (t.sections) t.sections[sectionKey] = r.section; if (r.checklist) t.checklist = r.checklist; });
        onSaved?.(r.section);
      } else if (r?.toast) {
        setSavedAt(new Date());
        toast(r.toast.text, r.toast);
      }
    } catch (e) {
      if (e.code === "conflict_rev") setConflict({ text: e.details?.text ?? "", rev: e.details?.rev });
      else if (e.code === "locked") toast(e.details?.reason || e.message, { level: "error" });
      else toast(e.message, { level: "error" });
    } finally {
      setSaving(false);
    }
  }
  function save(v) {
    clearTimeout(timer.current);
    const run = () => { clearTimeout(timer.current); pendingSaves.delete(id.current); return send(v); };
    pendingSaves.set(id.current, run);
    timer.current = setTimeout(run, 400);
  }

  async function acceptOverlap(o) {
    try {
      const r = await api.post(`/api/tasks/${short}/overlaps/${encodeURIComponent(o.id)}/accept`, {});
      if (r?.section) { setS(r.section); patchTask(short, (t) => { if (t.sections) t.sections[sectionKey] = r.section; }); }
      else if (r?.toast) toast(r.toast.text, r.toast);
    } catch (e) { toast(e.message, { level: "error" }); }
  }

  const onPaste = (e) => { typed.current.pasted += (e.clipboardData?.getData("text") || "").length; };
  const onInput = (e) => {
    const v = e.currentTarget.value;
    const d = v.length - live.current.text.length;
    if (d >= 0) typed.current.typed += d; else typed.current.deleted -= d;
    setText(v); save(v);
  };
  const keepMine = () => send(live.current.text, conflict?.rev);
  const loadTheirs = () => { setText(conflict.text); setS({ ...(s || {}), text: conflict.text, rev: conflict.rev }); setConflict(null); };
  return { text, s, conflict, saving: saving || pendingSaves.has(id.current), savedAt, onInput, onPaste, acceptOverlap, keepMine, loadTheirs };
}

// ---------- Marks ----------

function mirrorParts(text, overlaps, lit) {
  const marks = (overlaps || []).filter((o) => !o.accepted).sort((a, b) => a.start - b.start);
  const out = []; let at = 0;
  for (const o of marks) {
    if (o.start < at || o.end > text.length) continue;
    out.push(text.slice(at, o.start));
    out.push(html`<mark class=${`${o.level === "amber" ? "amber" : "red"}${lit === o.id ? " lit" : ""}`}>${text.slice(o.start, o.end)}</mark>`);
    at = o.end;
  }
  out.push(text.slice(at) + "\n");
  return out;
}

const pad2 = (n) => String(n).padStart(2, "0");
const clockOf = (d) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;

/** "3 points · 104 words", "3 sentences · 138 words", "32 words". */
export function countWords(key, s, text) {
  const words = s?.words ?? (text || "").split(/\s+/).filter(Boolean).length;
  if (!(text || "").trim()) return "Empty";
  if (key === "opening_prompt") return plural(words, "word");
  if (key === "decision_points") return `${plural(pointSpans(text).length, "point")} · ${plural(words, "word")}`;
  return `${plural(s?.sentences || 0, "sentence")} · ${plural(words, "word")}`;
}

/**
 * The editor. `label` names the field for screen readers. `onShowSource(o)` shows a matched finding beside it.
 * `extra` renders at the right of the meta line (the Next button). `inputRef` receives the textarea. `rowFor(o)` names
 * the finding an overlap came from when the page can show it (default: grading rows only).
 */
export function Editor({ short, sectionKey, section, autofocus, minRows = 6, label, onSaved, onShowSource, extra, inputRef,
  readOnly, lit, compact, rowFor, noLint }) {
  const ed = useProtected({ short, sectionKey, section, onSaved });
  const ta = useRef(null);
  const [open, setOpen] = useState(null); // overlap id whose source text is shown
  const { text, s } = ed;
  const locked = s?.locked;
  const ro = !!locked || !!readOnly;

  useEffect(() => { if (inputRef) inputRef.current = ta.current; });
  useEffect(() => { if (autofocus && ta.current && !ro) { ta.current.focus(); const n = ta.current.value.length; ta.current.setSelectionRange(n, n); } }, [short, sectionKey]);
  useEffect(() => {
    const el = ta.current; if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.max(el.scrollHeight, minRows * 28 + 28) + "px";
  }, [text, minRows]);

  const overlaps = (s?.overlap || []).filter((o) => !o.accepted);
  const flags = s?.lint || [];
  const where = (o) => {
    if (sectionKey === "decision_points") { const n = pointAt(text, o.start); if (n) return `Point ${n}`; }
    return { opening_prompt: "Your prompt", goal: "Your goal", codebase: "Your codebase understanding",
      validation_notes: "Your validation notes", reviewer_notes: "Your notes for the reviewer", initial_state: "Your initial state" }[sectionKey] || "Your text";
  };
  const select = (o) => { const el = ta.current; if (!el) return; el.focus(); el.setSelectionRange(o.start, o.end); };
  const saved = ed.saving ? "Saving…" : ed.savedAt ? `Saved ${clockOf(ed.savedAt)}` : "";

  return html`<div class=${`wb-ed ${ro ? "ro" : ""} ${compact ? "compact" : ""} ${overlaps.some((o) => o.level !== "amber") ? "has-red" : ""}`}>
    ${ed.conflict ? html`<div class="wb-ed-conflict" role="alert">
      <span>This field changed outside the app while you typed.</span>
      <button type="button" class="btn sm" onClick=${ed.loadTheirs}>Load the saved version</button>
      <button type="button" class="btn sm quiet" onClick=${ed.keepMine}>Keep mine</button>
    </div>` : null}
    <div class="wb-ed-box">
      <div class="wb-ed-mirror" aria-hidden="true">${mirrorParts(text, s?.overlap, lit)}</div>
      <textarea ref=${ta} ...${NO_ASSIST} spellcheck=${true} readOnly=${ro} value=${text} aria-label=${label || s?.heading}
        onFocus=${() => (fieldFocused.value = true)} onBlur=${() => (fieldFocused.value = false)}
        onPaste=${ed.onPaste} onInput=${ed.onInput}></textarea>
    </div>
    ${overlaps.length ? html`<div class="wb-ed-flags">
      ${groupOverlaps(overlaps, where).map(({ red, items }) => {
        const o = items[0];
        const row = rowFor ? rowFor(o) : overlapRow(o);
        const many = items.length > 1;
        const quotes = items.map((x, n) => html`${n ? (n === items.length - 1 ? " and " : ", ") : ""}<q>${clip(x.text)}</q>`);
        return html`<div class=${`flag ${red ? "stop" : ""} wb-ov`} role=${red ? "alert" : "status"}>
          <div class="flag-text">
            ${red ? html`${where(o)} repeats ${sourceWords(o.source)} word for word: ${quotes}. Save to Studio waits until you reword ${many ? "them" : "it"} or mark ${many ? "them" : "it"} as your own.`
              : html`${where(o)} shares ${many ? "phrases" : "a phrase"} with ${sourceWords(o.source)}: ${quotes}.`}
            ${open === o.id ? html`<div class="wb-ov-src"><span class="ai-tag">${o.source?.kind === "grading" ? "Findings from the review" : "AI text"}</span>
              <p>${o.source?.excerpt || ""}</p></div>` : null}
          </div>
          <div class="flag-acts">
            ${row && onShowSource ? html`<button type="button" class="btn sm" onClick=${() => { select(o); onShowSource(o, row); }}>Show it</button>`
              : html`<button type="button" class="btn sm" onClick=${() => { select(o); setOpen(open === o.id ? null : o.id); }}>${open === o.id ? "Hide it" : "Show both"}</button>`}
            ${ro ? null : html`<button type="button" class="btn sm" onClick=${async () => { for (const x of items) await ed.acceptOverlap(x); }}
              title="Marks these words as your own and logs it">${many ? "They're my own" : "It's my own"}</button>`}
          </div>
        </div>`;
      })}
    </div>` : null}
    ${flags.length && !ro && !noLint ? html`<ul class="wb-ed-lint">
      ${flags.map((f) => html`<li class=${f.level}><span class="ic" aria-hidden="true">${f.level === "error" ? "✗" : "!"}</span>${(f.message || "").charAt(0).toUpperCase() + (f.message || "").slice(1)}</li>`)}
    </ul>` : null}
    <div class="wb-ed-meta">
      <span class="num">${countWords(sectionKey, s, text)}</span>
      ${locked ? html`<span>${locked.reason ? `${locked.reason.charAt(0).toUpperCase()}${locked.reason.slice(1)}, locked` : "Locked"}</span>` : null}
      ${saved ? html`<span class="num">${saved}</span>` : null}
      ${extra ? html`<span class="wb-ed-extra">${extra}</span>` : null}
    </div>
  </div>`;
}

/** The old name, kept for any screen that still imports it. */
export const Field = Editor;
