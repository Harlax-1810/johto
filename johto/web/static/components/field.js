import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { api } from "../lib/api.js";
import { fieldFocused, patchTask, toast } from "../lib/store.js";
import { plural } from "../lib/format.js";

// Attributes that switch off writing assistance in browsers and common extensions.
const NO_ASSIST = {
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
export async function flushFields() {
  const all = [...pendingSaves.values()];
  pendingSaves.clear();
  await Promise.allSettled(all.map((f) => f()));
}

function counter(key, s) {
  if (!s) return "";
  if (key === "opening_prompt") return plural(s.words, "word");
  if (key === "decision_points") return `${s.pairs?.better || 0} better · ${s.pairs?.worse || 0} worse`;
  return `${plural(s.sentences, "sentence")} · ${plural(s.words, "word")}`;
}

function mirrorParts(text, overlaps) {
  const marks = (overlaps || []).filter((o) => !o.accepted).sort((a, b) => a.start - b.start);
  const out = []; let at = 0;
  for (const o of marks) {
    if (o.start < at || o.end > text.length) continue;
    out.push(text.slice(at, o.start));
    out.push(html`<mark class=${o.level === "amber" ? "amber" : ""}>${text.slice(o.start, o.end)}</mark>`);
    at = o.end;
  }
  out.push(text.slice(at) + "\n");
  return out;
}

/** A protected field: only Austin's typing goes in. No placeholder, no suggestions. */
export function Field({ short, sectionKey, section, autofocus, minRows = 4, label, onSaved }) {
  const [text, setText] = useState(section?.text || "");
  const [s, setS] = useState(section);
  const [rules, setRules] = useState(false);
  const [openOverlap, setOpenOverlap] = useState(null);
  const [conflict, setConflict] = useState(null); // {text, rev} the file holds
  const ta = useRef(null);
  const timer = useRef(null);
  const typed = useRef({ typed: 0, pasted: 0, deleted: 0 });
  const myTexts = useRef([]);  // texts this field saved, to tell a stale refetch from an outside edit
  const id = useRef(`f${++fieldN}`);
  const live = useRef({});
  live.current = { text, s };

  useEffect(() => { setText(section?.text || ""); setS(section); setConflict(null); }, [short, sectionKey]);
  useEffect(() => { if (autofocus && ta.current && !s?.locked) ta.current.focus(); }, [short, sectionKey]);
  useEffect(() => () => { const f = pendingSaves.get(id.current); if (f) { pendingSaves.delete(id.current); f(); } clearTimeout(timer.current); }, []);
  useEffect(() => {
    const el = ta.current; if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.max(el.scrollHeight, minRows * 24 + 24) + "px";
  }, [text]);

  // The task was refetched (an SSE task event, or vim changed submission.md). With no unsaved typing,
  // take the new text silently; while he types, his text wins and the next save reports a conflict.
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
    try {
      const r = await api.send("PUT", `/api/tasks/${short}/sections/${sectionKey}`,
        { text: v, base_rev: baseRev ?? live.current.s?.rev, authorship: { ...typed.current } });
      typed.current = { typed: 0, pasted: 0, deleted: 0 };
      if (r?.section) {
        myTexts.current = [...myTexts.current.slice(-6), r.section.text];
        setS(r.section);
        setConflict(null);
        patchTask(short, (t) => { if (t.sections) t.sections[sectionKey] = r.section; if (r.checklist) t.checklist = r.checklist; });
        onSaved?.(r.section);
      }
    } catch (e) {
      if (e.code === "conflict_rev") setConflict({ text: e.details?.text ?? "", rev: e.details?.rev });
      else if (e.code === "locked") toast(e.details?.reason || e.message, { level: "error" });
      else toast(e.message, { level: "error" });
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

  const locked = s?.locked;
  const flags = s?.lint || [];
  const overlaps = (s?.overlap || []).filter((o) => !o.accepted);
  return html`<div class=${`field ${locked ? "locked" : ""}`}>
    <div class="fhead">
      <span class="label">${label || s?.heading}</span>
      <span class="count num">
        <span>${counter(sectionKey, s)}</span>
        ${s?.style ? html`<button onClick=${() => setRules(!rules)} aria-expanded=${rules}>${s.style.label} ${rules ? "▾" : "▸"}</button>` : null}
        ${locked ? html`<span>${locked.reason}</span>` : null}
      </span>
    </div>
    ${conflict ? html`<div class="fconflict">
      <span class="m">submission.md changed outside the app while you typed.</span>
      <button class="btn sm" onClick=${() => { setText(conflict.text); setS({ ...(s || {}), text: conflict.text, rev: conflict.rev }); setConflict(null); }}>Load the file's version</button>
      <button class="btn sm quiet" onClick=${() => send(text, conflict.rev)}>Keep mine</button>
    </div>` : null}
    <div class="box">
      <div class="mirror" aria-hidden="true">${mirrorParts(text, s?.overlap)}</div>
      <textarea ref=${ta} ...${NO_ASSIST} spellcheck=${true} readOnly=${!!locked} value=${text}
        aria-label=${s?.heading}
        onFocus=${() => (fieldFocused.value = true)} onBlur=${() => (fieldFocused.value = false)}
        onPaste=${(e) => { typed.current.pasted += (e.clipboardData?.getData("text") || "").length; }}
        onInput=${(e) => {
          const v = e.currentTarget.value;
          const d = v.length - text.length;
          if (d >= 0) typed.current.typed += d; else typed.current.deleted -= d;
          setText(v); save(v);
        }}></textarea>
    </div>
    ${rules && s?.style ? html`<div class="rules">${s.style.rules_md}</div>` : null}
    ${(flags.length || overlaps.length) ? html`<ul class="flags">
      ${overlaps.map((o) => html`<li class=${`overlap ${o.level}`}>
        <span class="ic">${o.level === "amber" ? "⚑" : "✗"}</span>
        <span class="m">${(o.text || "").split(/\s+/).filter(Boolean).length} words match ${o.source?.label}
          <button class="ptr" style="color:inherit" onClick=${() => setOpenOverlap(openOverlap === o.id ? null : o.id)}>${openOverlap === o.id ? "▾" : "▸"}</button>
          ${openOverlap === o.id ? html`<div class="rules mono" style="margin-top:4px">${o.source?.excerpt}</div>` : null}</span>
        <span class="acts"><button class="btn sm" onClick=${() => { ta.current?.focus(); ta.current?.setSelectionRange(o.start, o.end); }}>Reword</button>
          <button class="btn sm quiet" onClick=${() => acceptOverlap(o)} title="Marks this match as your own words and logs it">My own words</button></span>
      </li>`)}
      ${flags.map((f) => html`<li class=${f.level}><span class="ic">${f.level === "error" ? "✗" : "⚑"}</span><span class="m">${f.message}</span></li>`)}
    </ul>` : null}
  </div>`;
}
