import { useEffect, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { api } from "../lib/api.js";
import { app, queue, navigate, select } from "../lib/store.js";
import { mins, plural, cutWords } from "../lib/format.js";
import { pushContext } from "../lib/keys.js";
import { Kbd, Chip } from "../components/ui.js";

const STATUS_TONE = {
  Accepted: "green", "In review": "neutral", "Awaiting Review": "neutral", "Sent back": "amber",
  Rejected: "red", Dropped: "ghost", Discarded: "ghost", Unclaimed: "ghost",
};
const WORDS = ["likely", "possible", "unlikely", "not_checked"];
const WORD = { likely: "likely", possible: "possible", unlikely: "unlikely", not_checked: "not checked" };
const SOURCE = { lead_estimate: "Lead scout said", difficulty_check: "Difficulty check said" };
const SOURCE_SHORT = { lead_estimate: "the lead scout", difficulty_check: "the difficulty check" };

function runWords(runs) {
  const c = (runs || []).filter((r) => r.startsWith("c")).length;
  const v = (runs || []).filter((r) => r.startsWith("v")).length;
  return [c ? plural(c, "calibration") : "", v ? plural(v, "validation") : ""].filter(Boolean).join(" · ");
}

function droppedWhy(w) {
  const r = Object.entries(w.dropped_reasons || {});
  if (!w.dropped || !r.length) return "";
  if (r.length === 1 && r[0][1] === w.dropped) {
    return ` (${w.dropped === 1 ? "" : w.dropped === 2 ? "both " : "all "}${r[0][0]})`;
  }
  return ` (${r.map(([k, n]) => `${n} ${k}`).join(", ")})`;
}

/** Week figures from the server; without them, the counts from the rows. */
function weekOf(d) {
  if (d.week) return d.week;
  const n = (s) => (d.rows || []).filter((r) => r.status === s).length;
  return { accepted: n("Accepted"), in_review: n("In review"), sent_back: n("Sent back"), dropped: n("Dropped"),
    dropped_reasons: {}, hours_per_accepted: null, lane_busy_pct: null, claude_waited_min: null };
}

// A count with something in it takes its status fill, so the week line matches the table below.
function Count({ n, tone, children }) {
  return n ? html`<${Chip} tone=${tone}><b>${n}</b> ${children}<//>` : html`<span class="faint">0 ${children}</span>`;
}

function Week({ w }) {
  const bits = [
    html`<${Count} n=${w.sent_back} tone="amber">sent back<//>`,
    html`<${Count} n=${w.in_review} tone="neutral">in review<//>`,
    html`<${Count} n=${w.accepted} tone="green">accepted<//>`,
    html`<${Count} n=${w.dropped} tone="ghost">dropped${droppedWhy(w)}<//>`,
  ];
  if (w.hours_per_accepted != null) bits.push(html`<span><b>${w.hours_per_accepted} h</b> per accepted task</span>`);
  if (w.lane_busy_pct != null) bits.push(html`<span>lane busy <b>${w.lane_busy_pct}%</b> of the time</span>`);
  if (w.claude_waited_min != null) bits.push(html`<span>Claude waited <b>${w.claude_waited_min} min</b> on you</span>`);
  return html`<div class="fn-kv fn-week">
    <span class="label">This week</span>
    <span class="fn-figs num">${bits.map((b, i) => html`${i ? html`<span class="sep">·</span>` : null}${b}`)}</span>
  </div>`;
}

function Ledger({ ledger }) {
  const by = {};
  for (const l of ledger || []) (by[l.source] ||= {})[l.word] = l;
  const sources = Object.keys(by);
  if (!sources.length) return null;
  return html`<div class="fn-kv">
    <span class="label" title="How often calibration was one-shot for each estimate">Estimates vs outcomes</span>
    <div class="fn-ledger">
      ${sources.map((s) => html`<div class="fn-lrow">
        <span class="fn-src">${SOURCE[s] || s}</span>
        ${WORDS.filter((w) => by[s][w]).map((w) => {
          const l = by[s][w];
          const pct = l.n ? Math.round((l.one_shot / l.n) * 100) : 0;
          return html`<span class="fn-est" title=${`${SOURCE_SHORT[s] || s} said ${WORD[w]}: ${l.one_shot} of ${l.n} calibrations were one-shot`}>
            <span class="fn-w">${WORD[w]}</span><span class="faint">→</span>
            <span class="num"><b>${l.one_shot}</b> of ${l.n} one-shot</span>
            <span class="fn-bar" aria-hidden="true"><i style=${`width:${pct}%`}></i></span>
          </span>`;
        })}
      </div>`)}
    </div>
  </div>`;
}

export default function Finished() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [open, setOpen] = useState(null);     // short whose reviewer feedback is open
  const [at, setAt] = useState(-1);           // keyboard row
  const mode = app.value?.server?.mode;
  useEffect(() => { setD(null); }, [mode]);
  // Read again when any task changes stage (a submit, a review result, a drop arrives over SSE).
  const stages = (app.value?.tasks || []).map((t) => `${t.short}:${t.stage}:${t.studio?.status || ""}`).join(",");
  useEffect(() => { api.get("/api/finished").then(setD).catch((e) => setErr(e.message)); }, [mode, stages]);

  // Sent-back tasks need you, so they come first; the rest keep the server's order.
  const rows = [...(d?.rows || [])].sort((a, b) => (b.status === "Sent back") - (a.status === "Sent back"));
  const known = new Set((app.value?.tasks || []).map((t) => t.short));
  const openTask = (r) => known.has(r.short) && navigate(`/task/${r.short}`);
  useEffect(() => pushContext("finished", {
    ArrowDown: () => setAt((i) => Math.min(rows.length - 1, i + 1)),
    ArrowUp: () => setAt((i) => Math.max(0, i - 1)),
    Enter: () => rows[at] && openTask(rows[at]),
    ArrowRight: () => rows[at]?.feedback && setOpen(rows[at].short),
    ArrowLeft: () => setOpen(null),
  }), [rows.length, at, known.size]);

  if (err) return html`<div class="pg"><div class="pg-err">${err}</div></div>`;
  if (!d) return html`<div class="pg"></div>`;
  const fixRow = (short) => queue.value.find((i) => i.short === short && i.kind === "feedback");

  const hasLedger = (d.ledger || []).length > 0;
  return html`<div class="pg fn">
    <section class="pg-panel fn-sum"><${Week} w=${weekOf(d)} /></section>

    ${rows.length ? html`<section class="pg-panel fn-tasks">
      <header class="pg-ph">
        <span class="label">Tasks</span>
        <span class="pg-sub num">${rows.length}</span>
        <span class="pg-right faint sm"><${Kbd} k="↑↓" /> select <${Kbd} k="↵" /> open <${Kbd} k="→" /> feedback</span>
      </header>
      <div class="fn-thead">
        <span>Task</span><span>Slot</span><span>Status</span><span class="r" title="Your minutes on the task">You</span>
        <span class="r" title="Claude's active minutes across runs">Claude</span><span>Runs</span><span>Last</span>
      </div>
      <div class="fn-tbody">
      ${rows.map((r, i) => {
        const fb = open === r.short && r.feedback;
        const fix = fixRow(r.short);
        return html`<div class=${`fn-row ${i === at ? "at" : ""} ${known.has(r.short) ? "link" : ""}`}
            onClick=${() => { setAt(i); openTask(r); }}>
          <div class="fn-cells">
            <span class="fn-label">${r.label}</span>
            <span class="muted">${r.slot}</span>
            <span><${Chip} tone=${STATUS_TONE[r.status] ?? "neutral"}>${r.status}<//>${r.times_submitted > 1 ? html`<span class="faint sm"> ×${r.times_submitted}</span>` : null}</span>
            <span class="num r" title="Your minutes">${r.my_min != null ? mins(r.my_min) : ""}</span>
            <span class="num r" title="Claude's active minutes">${r.claude_min != null ? mins(r.claude_min) : ""}</span>
            <span class="muted sm">${runWords(r.runs)}</span>
            <span class="fn-last">${r.feedback
              ? html`<button class=${`fn-fb ${fb ? "on" : ""}`} aria-expanded=${!!fb} title="Reviewer feedback"
                  onClick=${(e) => { e.stopPropagation(); setAt(i); setOpen(fb ? null : r.short); }}>
                  <span class="fn-q">${fb ? r.last : `“${cutWords(r.feedback, 70)}”`}</span><span class="tri">${fb ? "▾" : "▸"}</span></button>`
              : html`<span class="muted">${r.last}</span>`}</span>
          </div>
          ${fb ? html`<div class="fn-fbbox" onClick=${(e) => e.stopPropagation()}>
            <div class="fn-fbh"><span class="label">Reviewer feedback</span>
              ${fix ? html`<button class="btn sm" onClick=${() => { navigate("/"); select(fix.id); }}>Work on it in Home</button>` : null}</div>
            <div class="fn-fbtext">${r.feedback}</div>
          </div>` : null}
        </div>`;
      })}
      </div>
    </section>` : null}

    ${hasLedger || d.spend ? html`<section class="pg-panel fn-sum">
      <${Ledger} ledger=${d.ledger} />
      ${d.spend ? html`<div class="fn-kv">
        <span class="label">AI spend</span>
        <span class="num"><b>$${d.spend.week_usd.toFixed(2)}</b> this week<span class="sep">·</span><b>$${d.spend.per_submitted_usd.toFixed(2)}</b> per submitted task</span>
      </div>` : null}
    </section>` : null}
  </div>`;
}
