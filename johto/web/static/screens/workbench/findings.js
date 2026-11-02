// Findings from the review (spec 3.5.3): AI text, shown on a lilac panel with the AI mark. Austin places each
// finding (goal, a decision point, notes) with the hover control or a key; the app never turns a finding into
// wording for his fields. Placing uses the existing grading marks (POST .../grading/mark).
import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { AiTag, Btn, Menu, Seg, Sheet, KeyBar, Empty } from "../../components/primitives.js";
import { pushContext } from "../../lib/keys.js";
import { toast } from "../../lib/store.js";
import { mark, reviewLatest, saveReview, rereadReviews, isDemo } from "./data.js";
import { SCOPE_OPTIONS, clip } from "./words.js";

const FALLBACK = "Open the evidence to read this finding.";
const RESULT_ORDER = ["missed", "partial", "credit", "unknown"];
const RESULT_GROUP = { missed: "Claude missed it", partial: "Partly right", credit: "Got it right", unknown: "Not known yet" };
const KEYS = [["J K", "move"], ["G", "goal"], ["1 2 3", "decision point"], ["N", "notes"], ["↵", "evidence"]];

/** Where a finding is placed, in words: ["Goal", "Point 1"]. */
export function placeWords(m) {
  const out = [];
  if (m?.goal) out.push("Goal");
  if (m?.dp) out.push(`Point ${m.dp}`);
  if (m?.notes) out.push("Notes");
  return out;
}

const norm = (s) => String(s || "").toLowerCase().replace(/\s+/g, " ");

/** The card's sentence with `phrase` marked (lilac) when it is in there; [nodes, found]. */
function withPhrase(text, phrase) {
  if (!phrase) return [text, false];
  const hay = norm(text), needle = norm(phrase).trim();
  if (needle.length < 8) return [text, false];
  // map positions in the normalized string back to the original
  const map = []; let prevSpace = false;
  for (let i = 0; i < text.length; i++) {
    const sp = /\s/.test(text[i]);
    if (sp && prevSpace) continue;
    map.push(i); prevSpace = sp;
  }
  const at = hay.indexOf(needle);
  if (at < 0) return [text, false];
  const a = map[at], b = (map[at + needle.length - 1] ?? text.length - 1) + 1;
  return [[text.slice(0, a), html`<mark class="wb-lit">${text.slice(a, b)}</mark>`, text.slice(b)], true];
}

function Place({ row, m, onMark }) {
  const b = (label, on, change, title) => html`<button type="button" class=${on ? "on" : ""} aria-pressed=${on} title=${title}
      onClick=${(e) => { e.stopPropagation(); onMark(row, change); }}>${label}</button>`;
  return html`<div class="wb-place" role="group" aria-label="Place this finding">
    ${b("Goal", !!m.goal, { goal: !m.goal }, "Goal (G)")}
    ${[1, 2, 3].map((n) => b(String(n), m.dp === n, { dp: m.dp === n ? null : n }, `Decision point ${n} (${n})`))}
    ${b("Notes", !!m.notes, { notes: !m.notes }, "Validation notes (N)")}
  </div>`;
}

function Evidence({ row, scope, onScope }) {
  const d = row.drill || {};
  return html`<div class="wb-ev" onClick=${(e) => e.stopPropagation()}>
    <section><h4>As the review wrote it</h4><p>${row.as_written || FALLBACK}</p></section>
    ${row.limits_words ? html`<section><h4>What the check didn't cover</h4><p>${row.limits_words}</p></section>` : null}
    <section><h4>Was the model asked to find this?</h4>
      <${Seg} size="sm" label="Scope" options=${SCOPE_OPTIONS} value=${scope} onChange=${onScope} /></section>
    <p class="wb-ev-src">${row.source_words}${row.refuted ? " · A later check refuted it" : ""}${row.changed_since_mark ? " · Its facts changed since you placed it" : ""}</p>
    ${(d.refs || []).length || d.finding ? html`<details class="wb-drill">
      <summary>See it in the code</summary>
      ${d.finding ? html`<p class="wb-drill-raw">${d.finding}</p>` : null}
      ${(d.refs || []).length ? html`<ul class="wb-refs">${d.refs.map((r) => html`<li class="mono">${r}</li>`)}</ul>` : null}
      ${d.limits ? html`<p class="wb-drill-raw faint">${d.limits}</p>` : null}
    </details>` : null}
  </div>`;
}

function FindingCard({ row, m, sel, open, lit, compact, onMark, onOpen, onFocus, onScope, scope, also }) {
  const text = row.plain || row.finding || FALLBACK;
  const [nodes, found] = withPhrase(text, lit);
  return html`<div class=${`wb-fc ${sel ? "sel" : ""} ${open ? "open" : ""} ${compact && !open ? "compact" : ""} ${lit ? "lit" : ""}`}
      data-row=${row.id} tabindex="0" aria-expanded=${open}
      onClick=${() => onOpen(row.id)} onFocus=${() => onFocus(row.id)}
      onKeyDown=${(e) => { if (e.key === "Enter" && e.target === e.currentTarget) { e.preventDefault(); onOpen(row.id); } }}>
    <p class="wb-fc-text">${compact && !open ? clip(text, 120) : nodes}</p>
    ${lit && !found ? html`<p class="wb-fc-match">Your words match this finding: <mark class="wb-lit">${clip(lit, 90)}</mark></p>` : null}
    ${compact && !open ? null : html`<div class="wb-fc-meta">
      <b>${row.result_words}</b><span>${row.basis_words}</span>
      ${row.serious ? html`<span class="wb-serious">Serious</span>` : null}
      ${also ? html`<span class=${`wb-also ${also === "Not placed" ? "none" : ""}`}>${also}</span>` : null}
    </div>`}
    <${Place} row=${row} m=${m} onMark=${onMark} />
    ${open ? html`<${Evidence} row=${row} scope=${scope} onScope=${(v) => onScope(row, v)} />` : null}
  </div>`;
}

function alsoWords(m, here) {
  const w = placeWords(m).filter((x) => x !== here);
  if (!w.length) return "";
  return `Also in ${w.map((x) => (x === "Goal" ? "the goal" : x === "Notes" ? "the notes" : x.toLowerCase())).join(" and ")}`;
}

/** Groups for the field being written; null for a field findings aren't placed in. */
function fieldGroups(field) {
  if (field === "goal") return [{ id: "goal", title: "Goal", has: (m) => !!m.goal }];
  if (field === "decision_points") return [1, 2, 3].map((n) => ({ id: `p${n}`, title: `Point ${n}`, has: (m) => m.dp === n }));
  if (field === "validation_notes") return [{ id: "notes", title: "Notes", has: (m) => !!m.notes }];
  return null;
}

function Folded({ title, count, children, open: start = false }) {
  const [open, setOpen] = useState(start);
  if (!count) return null;
  return html`<div class="wb-fold">
    <button type="button" class="wb-fold-h" aria-expanded=${open} onClick=${() => setOpen(!open)}>
      <span>${title}</span><span class="num faint">${count}</span><span class="wb-tri">${open ? "▾" : "▸"}</span></button>
    ${open ? html`<div class="wb-fold-b">${children}</div>` : null}
  </div>`;
}

/** The coverage flag and, after the pack is copied, the paste box for GPT Pro's reply (his press saves it). */
function Coverage({ short, cov, onSaved }) {
  const [busy, setBusy] = useState(false);
  const [paste, setPaste] = useState(false);
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  if (!cov?.flag && !paste) return null;
  async function pack() {
    setBusy(true);
    const j = await reviewLatest(short);
    setBusy(false);
    if (j?.state === "succeeded" || isDemo()) setPaste(true);
  }
  async function save() {
    if (!text.trim()) return;
    setSaving(true);
    const r = await saveReview(short, text, cov?.turns || null);
    setSaving(false);
    if (r) { setText(""); setPaste(false); onSaved?.(); }
  }
  return html`<div class="wb-cov">
    ${cov?.flag ? html`<div class="flag wb-cov-flag" role="status">
      <div class="flag-text">${cov.flag}</div>
      <div class="flag-acts">
        <button type="button" class="btn sm" disabled=${busy} onClick=${pack}>${busy ? "Building the pack…" : cov.action}</button>
        ${paste ? null : html`<button type="button" class="btn sm quiet" onClick=${() => setPaste(true)}>Paste a reply</button>`}
      </div>
    </div>` : null}
    ${paste ? html`<div class="wb-paste">
      <label class="label-sm" for="wb-paste">Paste GPT Pro's reply</label>
      <textarea id="wb-paste" rows="5" value=${text} onInput=${(e) => setText(e.currentTarget.value)} spellcheck=${false}></textarea>
      <div class="hstack"><${Btn} size="sm" kind="primary" label=${saving ? "Saving…" : "Save the review"} disabled=${!text.trim() || saving} onClick=${save} />
        <${Btn} size="sm" kind="quiet" label="Not now" onClick=${() => setPaste(false)} /></div>
    </div>` : null}
  </div>`;
}

/** Every finding grouped by result, each row saying where it is placed. */
function AllView({ rows, marksOf, focusRow, openRow, lit, onMark, onOpen, onFocus, onScope, scopeOf }) {
  return html`<div class="wb-all">
    ${RESULT_ORDER.map((res) => {
      const list = rows.filter((r) => r.result === res);
      if (!list.length) return null;
      return html`<section class="wb-grp">
        <h3 class="wb-grp-h">${RESULT_GROUP[res]} <span class="num faint">${list.length}</span></h3>
        ${list.map((r) => {
          const m = marksOf(r);
          const where = placeWords(m);
          return html`<${FindingCard} key=${r.id} row=${r} m=${m} sel=${focusRow === r.id} open=${openRow === r.id}
            lit=${lit?.row === r.id ? lit.phrase : null} onMark=${onMark} onOpen=${onOpen} onFocus=${onFocus}
            onScope=${onScope} scope=${scopeOf(r)} also=${where.length ? `Placed: ${where.join(", ")}` : "Not placed"} />`;
        })}
      </section>`;
    })}
  </div>`;
}

/**
 * The findings panel. `field` is the field being written; `focus` asks to show one finding (a submit item's Show it,
 * an overlap's Show it) and `lit` marks the phrase his words share with it.
 */
export function FindingsPanel({ short, wb, field, focusReq, lit, onChanged, readOnly }) {
  const rows = wb.findings?.rows || [];
  const cov = wb.findings?.coverage;
  const groups = fieldGroups(field);
  const [view, setView] = useState(groups ? "field" : "all");
  const [over, setOver] = useState({});      // optimistic marks and scope, by row id
  const [focusRow, setFocusRow] = useState(null);
  const [openRow, setOpenRow] = useState(null);
  const [sorting, setSorting] = useState(false);
  const ref = useRef(null);

  useEffect(() => { setView(fieldGroups(field) ? "field" : "all"); }, [field]);
  // A refetch that agrees with what we set clears the optimistic copy.
  useEffect(() => {
    setOver((o) => {
      const next = { ...o };
      for (const r of rows) {
        const v = next[r.id];
        if (v && JSON.stringify({ ...r.marks, scope: r.scope }) === JSON.stringify({ ...r.marks, ...v.marks, scope: v.scope ?? r.scope })) delete next[r.id];
      }
      return next;
    });
  }, [rows]);
  useEffect(() => {
    if (!focusReq?.row) return;
    const r = rows.find((x) => x.id === focusReq.row);
    if (!r) return;
    const m = marksOf(r);
    if (groups && !groups.some((g) => g.has(m))) setView("all");
    setOpenRow(focusReq.open ? r.id : openRow);
    setFocusRow(r.id);
    setTimeout(() => {
      const el = ref.current?.querySelector(`[data-row="${r.id}"]`);
      el?.scrollIntoView({ block: "center", behavior: "smooth" });
    }, 60);
  }, [focusReq?.n]);

  const marksOf = (r) => ({ ...r.marks, ...(over[r.id]?.marks || {}) });
  const scopeOf = (r) => (over[r.id] && "scope" in over[r.id] ? over[r.id].scope : r.scope);

  async function onMark(row, change) {
    if (readOnly) return;
    const before = marksOf(row);
    setOver((o) => ({ ...o, [row.id]: { ...(o[row.id] || {}), marks: { ...before, ...change } } }));
    const r = await mark(short, [row.id], change);
    if (r == null) { setOver((o) => { const n = { ...o }; delete n[row.id]; return n; }); return; }
    if (r.demo) { toast("Demo data: placed here only, nothing was written.", { level: "info", id: "demo-mark" }); return; }
    onChanged?.();
  }
  async function onScope(row, scope) {
    if (readOnly) return;
    setOver((o) => ({ ...o, [row.id]: { ...(o[row.id] || {}), scope } }));
    const r = await mark(short, [row.id], { scope });
    if (r == null) { setOver((o) => { const n = { ...o }; delete n[row.id]; return n; }); return; }
    if (!r.demo) onChanged?.();
  }
  const onOpen = (id) => setOpenRow((cur) => (cur === id ? null : id));

  // Keys on the panel: J K move, G goal, 1 2 3 decision point, N notes, Enter opens the evidence.
  useEffect(() => {
    const move = (d) => {
      const root = sorting ? document.querySelector(".sheet .wb-sort") : ref.current;
      const els = [...(root?.querySelectorAll("[data-row]") || [])];
      if (!els.length) return;
      const i = els.findIndex((el) => el.dataset.row === focusRow);
      const el = els[Math.max(0, Math.min(els.length - 1, i < 0 ? 0 : i + d))];
      el.focus(); el.scrollIntoView({ block: "nearest" });
    };
    const cur = () => rows.find((r) => r.id === focusRow);
    const place = (fn) => () => { const r = cur(); if (r) onMark(r, fn(marksOf(r))); };
    return pushContext("findings", {
      j: () => move(1), k: () => move(-1),
      G: place((m) => ({ goal: !m.goal })),
      1: place((m) => ({ dp: m.dp === 1 ? null : 1 })), 2: place((m) => ({ dp: m.dp === 2 ? null : 2 })),
      3: place((m) => ({ dp: m.dp === 3 ? null : 3 })),
      n: place((m) => ({ notes: !m.notes })),
      Enter: () => { if (focusRow) onOpen(focusRow); },
    });
  }, [focusRow, rows, over, readOnly, sorting]);

  const card = (r, extra = {}) => {
    const m = marksOf(r);
    return html`<${FindingCard} key=${r.id} row=${r} m=${m} sel=${focusRow === r.id} open=${openRow === r.id}
      lit=${lit?.row === r.id ? lit.phrase : null} onMark=${onMark} onOpen=${onOpen} onFocus=${setFocusRow}
      onScope=${onScope} scope=${scopeOf(r)} ...${extra} />`;
  };

  let body;
  if (!rows.length) {
    body = html`<div class="wb-fp-empty">${cov?.flag ? "No findings yet. They come from the review of the run." : "No findings for this task."}</div>`;
  } else if (view === "all" || !groups) {
    body = html`<${AllView} rows=${rows} marksOf=${marksOf} focusRow=${focusRow} openRow=${openRow} lit=${lit}
      onMark=${onMark} onOpen=${onOpen} onFocus=${setFocusRow} onScope=${onScope} scopeOf=${scopeOf} />`;
  } else {
    const placed = new Set();
    const grp = groups.map((g) => {
      const list = rows.filter((r) => g.has(marksOf(r)));
      list.forEach((r) => placed.add(r.id));
      return html`<section class="wb-grp">
        <h3 class="wb-grp-h">${g.title}</h3>
        ${list.length ? list.map((r) => card(r, { also: alsoWords(marksOf(r), g.title) }))
          : html`<div class="wb-none">Nothing placed yet</div>`}
      </section>`;
    });
    const rest = rows.filter((r) => !placed.has(r.id));
    const missed = rest.filter((r) => r.result === "missed" || r.result === "partial");
    const credit = rest.filter((r) => r.result === "credit");
    const unknown = rest.filter((r) => r.result === "unknown");
    const missedTitle = missed.some((r) => r.result === "partial") ? "Missed or partly right, not placed" : "Claude missed, not placed";
    body = html`${grp}
      <${Folded} title=${missedTitle} count=${missed.length} open=${true}>${missed.map((r) => card(r, { compact: true, also: alsoWords(marksOf(r), "") }))}<//>
      <${Folded} title="Got right" count=${credit.length}>${credit.map((r) => card(r, { compact: true }))}<//>
      <${Folded} title="Not known yet" count=${unknown.length}>${unknown.map((r) => card(r, { compact: true }))}<//>`;
  }

  const title = wb.findings?.has_review ? "Findings from the review" : "Findings";
  return html`<aside class="wb-fp ai-block" ref=${ref} aria-label=${title}>
    <div class="wb-fp-head">
      <${AiTag} label=${title} />
      <div class="hstack">
        ${groups && rows.length ? html`<${Seg} size="sm" label="Show" value=${view} onChange=${setView}
          options=${[{ value: "field", label: "For this field" }, { value: "all", label: `All ${rows.length}` }]} />` : null}
        <${Menu} size="sm" title="More" items=${[
          rows.length ? { label: "Sort findings", run: () => setSorting(true) } : null,
          { label: "Read the reviews again", run: async () => { await rereadReviews(short); onChanged?.(); } },
        ]} />
      </div>
    </div>
    ${readOnly ? null : html`<${Coverage} short=${short} cov=${cov} onSaved=${onChanged} />`}
    ${wb.findings?.plain_state === "pending" || wb.findings?.fallback ? html`<p class="wb-fp-note">${wb.findings?.plain_state === "pending"
      ? "Writing the findings in plain words. Until then, open a card to read the review's own words."
      : "Some findings aren't in plain words yet. Open a card to read the review's own words."}</p>` : null}
    ${body}
    ${rows.length && !sorting ? html`<div class="wb-fp-keys"><${KeyBar} keys=${KEYS} /></div>` : null}
    <${Sheet} open=${sorting} onClose=${() => setSorting(false)} tag=${title} title="Sort findings"
      foot=${html`<${KeyBar} keys=${KEYS} />`} width=${560}>
      <div class="wb-sort">
        <${AllView} rows=${rows} marksOf=${marksOf} focusRow=${focusRow} openRow=${openRow} lit=${lit}
          onMark=${onMark} onOpen=${onOpen} onFocus=${setFocusRow} onScope=${onScope} scopeOf=${scopeOf} />
      </div>
    <//>
  </aside>`;
}
