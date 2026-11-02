// Submitted (spec.md 3.6): what happened to what I sent? One lede with the counts that matter, then one row per
// submitted task, newest first: its state in its colour, the date, and the reviewer's improvement sentence (human
// words from Studio, so no AI mark). The week's figures sit behind "Numbers".
import { useEffect, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { api } from "../lib/api.js";
import { app, navigate } from "../lib/store.js";
import { pushContext } from "../lib/keys.js";
import { Stage, Lede, Pill, Sheet, Empty, Btn } from "../components/primitives.js";

export const LESSON_WORDS = { accepted: "Accepted", sent_back: "Sent back", rejected: "Rejected", in_review: "In review", dropped: "Dropped" };
export const LESSON_TONE = { accepted: "ok", sent_back: "wait", rejected: "stop", in_review: "neutral", dropped: "outline" };

/** The reviewer's own words, kept as written: paragraphs, bullet lines and bold headings. */
export function ReviewText({ text }) {
  const paras = String(text || "").split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
  return html`<div class="su-review">${paras.map((p) => {
    const lines = p.split("\n").map((l) => l.trim()).filter(Boolean);
    if (lines.every((l) => /^[*•-]\s+/.test(l))) {
      return html`<ul>${lines.map((l) => html`<li>${l.replace(/^[*•-]\s+/, "").replace(/\*\*/g, "")}</li>`)}</ul>`;
    }
    const bold = /^\*\*(.+)\*\*:?$/.exec(p);
    if (bold) return html`<h3>${bold[1].replace(/:$/, "")}</h3>`;
    return html`<p>${p.replace(/\*\*/g, "")}</p>`;
  })}</div>`;
}

const pct = (n) => (n == null ? null : `${n}%`);

/** The week's figures and the estimate ledger: one click down, never on the first layer. The counts the lede
 * already says are not repeated. */
function Numbers({ d }) {
  if (!d) return html`<p class="su-faint">Loading…</p>`;
  const w = d.week || {};
  const keep = (rows) => rows.filter((r) => r && r[1] != null && r[1] !== "");
  const groups = [
    ["Time", keep([["Hours per accepted task", w.hours_per_accepted], ["Lane busy", pct(w.lane_busy_pct)],
      ["Claude waited on you", w.claude_waited_min != null ? `${w.claude_waited_min} min` : null], ["Dropped", w.dropped || null]])],
    ["AI spend", keep(d.spend ? [["This week", `$${d.spend.week_usd.toFixed(2)}`], ["Per submitted task", `$${d.spend.per_submitted_usd.toFixed(2)}`]] : [])],
    ["Estimates and outcomes", keep((d.ledger || []).map((l) => [
      `${l.source === "difficulty_check" ? "Difficulty check" : "Repo research"} said ${(l.word || "").replace("_", " ")}`,
      `${l.one_shot} of ${l.n} one-shot`]))],
  ].filter(([, rows]) => rows.length);
  if (!groups.length) return html`<div class="su-nums"><p class="su-faint">No figures yet this week.</p></div>`;
  return html`<div class="su-nums">${groups.map(([title, rows]) => html`<section>
    <h3>${title}</h3>
    <dl class="su-dl">${rows.map(([k, v]) => html`<dt>${k}</dt><dd class="num">${v}</dd>`)}</dl>
  </section>`)}</div>`;
}

export default function Finished() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [nums, setNums] = useState(null);
  const [showNums, setShowNums] = useState(false);
  const [open, setOpen] = useState(null);
  const [at, setAt] = useState(-1);
  const mode = app.value?.server?.mode;
  const stages = (app.value?.tasks || []).map((t) => `${t.short}:${t.stage}:${t.studio?.status || ""}`).join(",");
  useEffect(() => { api.get("/api/now/submitted").then((r) => { setD(r); setErr(null); }).catch((e) => setErr(e.message)); }, [mode, stages]);
  useEffect(() => { if (showNums) api.get("/api/finished").then(setNums).catch(() => setNums({})); }, [showNums, mode]);

  const rows = d?.rows || [];
  const known = new Set((app.value?.tasks || []).map((t) => t.short));
  const openTask = (r) => known.has(r.short) && navigate(`/task/${r.short}`);
  useEffect(() => pushContext("finished", {
    ArrowDown: () => setAt((i) => Math.min(rows.length - 1, i + 1)),
    ArrowUp: () => setAt((i) => Math.max(0, i - 1)),
    j: () => setAt((i) => Math.min(rows.length - 1, i + 1)),
    k: () => setAt((i) => Math.max(0, i - 1)),
    Enter: () => rows[at] && openTask(rows[at]),
  }), [rows.length, at, known.size]);

  if (err && !d) return html`<${Stage} class="su"><${Lede} title="Submitted couldn't load." support=${err} /><//>`;
  if (!d) return html`<${Stage} class="su"><div class="nw-loading"></div><//>`;
  const cur = rows.find((r) => r.short === open);
  return html`<${Stage} class="su">
    <${Lede} title=${d.lede?.title} support=${d.lede?.support}
      right=${html`<button type="button" class=${`su-numbtn ${showNums ? "on" : ""}`} aria-expanded=${showNums}
        onClick=${() => setShowNums(!showNums)}>Numbers <span class="tri">${showNums ? "▾" : "▸"}</span></button>`} />
    ${showNums ? html`<${Numbers} d=${nums} />` : null}
    ${rows.length ? html`<div class="su-rows" role="list">
      ${rows.map((r, i) => html`<div role="listitem" key=${r.short}
          class=${`su-row ${i === at ? "at" : ""} ${known.has(r.short) ? "link" : ""} st-${r.state}`}
          onClick=${() => { setAt(i); openTask(r); }}>
        <div class="su-state"><${Pill} tone=${LESSON_TONE[r.state]}>${LESSON_WORDS[r.state]}<//></div>
        <div class="su-main">
          <div class="su-label">${r.label}${r.times > 1 ? html`<span class="su-times">sent ${r.times} times</span>` : null}</div>
          ${r.sentence ? html`<p class="su-lesson">${r.sentence}</p>` : null}
        </div>
        <div class="su-when">${r.when}</div>
        <div class="su-go">${r.full ? html`<button type="button" class="linkbtn"
            onClick=${(e) => { e.stopPropagation(); setAt(i); setOpen(r.short); }}>Full review</button>` : null}</div>
      </div>`)}
    </div>` : html`<${Empty} title="Nothing submitted yet." body="Tasks show here once you send them to Studio."
        action=${html`<${Btn} label="Now" onClick=${() => navigate("/")} />`} />`}
    <${Sheet} open=${!!cur} onClose=${() => setOpen(null)} title=${cur?.label} width=${640}
      meta=${cur ? html`<${Pill} tone=${LESSON_TONE[cur.state]}>${LESSON_WORDS[cur.state]}<//>` : null}>
      ${cur ? html`<${ReviewText} text=${cur.full} />` : null}
    <//>
  <//>`;
}
