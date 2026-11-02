import { html } from "../lib/html.js";
import { tick } from "../lib/store.js";
import { clock, mins } from "../lib/format.js";
import { Dot } from "./ui.js";

// The run monitor (johto/web/lane/runmon.py) on the page: which run, Claude's working minutes for the run and
// the task, background work, how long Claude has waited on Austin, what the last turn was, and the outside
// review with the turn it covers. The drawer shows it as one line; the rail's machine line borrows its words.
// Every number is measured from the transcript; nothing here is a guess. No command on the page (rule 5).

// Styles live with the component until they move into the stylesheets (WEB-HOME doesn't own the CSS files).
const CSS = `
.mon-line { display: inline-flex; align-items: center; gap: 10px; min-width: 0; flex-wrap: wrap; color: var(--text-2); font-size: var(--fs-sm); }
.mon-line b { color: var(--text); font-weight: 600; }
.mon-badge { display: inline-flex; align-items: center; gap: 4px; flex: none; height: 16px; padding: 0 5px; border-radius: var(--r-sm);
  border: 1px solid var(--line); color: var(--text-3); font-size: var(--fs-xs); line-height: 14px; white-space: nowrap; background: none; }
.mon-badge.ready { color: var(--ok-fg); border-color: color-mix(in srgb, var(--ok-dot) 35%, var(--line)); }
.mon-badge.reviewing .dot { width: 6px; height: 6px; }
`;
export const MonitorStyle = () => html`<style>${CSS}</style>`;

// What the last turn was, in his words (budget.classify_turn's six kinds).
export const TURN_WORD = {
  question: "Claude asked you something", permission: "Claude asks permission", substantive: "Claude answered",
  progress: "Claude posted progress", background: "Background work still running", problem: "Claude stopped on a problem",
};

// The class that matters to him: a turn that did real work and then asked something is a question
// (the same rule as queue.turn_of on the server).
export function turnOf(m) {
  if (!m) return null;
  if ((m.turn_class === "substantive" || m.turn_class === "progress") && m.question) return "question";
  return m.turn_class || null;
}

/** One phrase for the run's state. */
export function stateWord(m) {
  if (!m) return "";
  const host = m.host || "Mac";
  switch (m.state) {
    case "working": return m.turn?.no_reply ? `No reply from Claude for ${Math.round(m.turn.no_reply)} min` : "Claude working";
    case "waiting": return TURN_WORD[turnOf(m)] || "Claude ended its turn";
    case "background": return "Background work running";
    case "ready": return "Ready for your prompt";
    case "collected": return "Collected";
    case "removed": return "Lane removed, not collected";
    case "stopped": return "Lane stopped, not collected";
    case "not_started": return "Not started";
    case "paused": return `Paused: Docker isn't running on the ${host}`;
    case "unknown": return `Can't check the ${host} now`;
    default: return m.state || "";
  }
}

/** The quiet review badge: queued, reviewing or ready, named by the turn it covers. */
export function ReviewBadge({ rv, short = false }) {
  if (!rv || !["queued", "reviewing", "ready"].includes(rv.state) || !rv.text) return null;
  const tip = [rv.text, rv.covers_latest === false ? "Claude has taken more turns since" : "", rv.unconfirmed ? "not confirmed" : ""]
    .filter(Boolean).join(" · ");
  // `short` (the rail): a word or two; the turn it covers is in the tooltip and the lane surface. A ready review
  // of an earlier turn names that turn and gets no tick: Claude has taken more turns since.
  const behind = rv.state === "ready" && rv.covers_latest === false;
  const text = behind ? (rv.turn != null && rv.of ? `Review of turn ${rv.turn} of ${rv.of}` : "Review of an earlier turn")
    : short ? { queued: "Review queued", reviewing: "Reviewing", ready: "Review ready" }[rv.state] : rv.text;
  return html`<span class=${`mon-badge ${rv.state}`} title=${tip}>
    ${rv.state === "reviewing" ? html`<${Dot} kind="busy" />` : rv.state === "ready" && !behind ? "✓" : null}${text}</span>`;
}

function budgetSpan(m) {
  const tone = m.budget_tone ? `chip ${m.budget_tone}` : "";
  return html`<span class=${tone}><b class="num">${mins(m.task_minutes)}</b> of ${m.budget || 90} min</span>`;
}

/** One line for the drawer header: run, minutes against the budget, the wait, the review. */
export function MonitorLine({ m }) {
  tick.value;
  if (!m) return null;
  const wait = m.human_wait?.since;
  return html`<span class="mon-line" title="Measured turn by turn from the conversation">
    <${MonitorStyle} />
    <span>Claude ${budgetSpan(m)}</span>
    ${m.turns ? html`<span>this run <b class="num">${mins(m.working_minutes)}</b> min</span>` : null}
    ${m.background?.running ? html`<span>${m.background.running} background job${m.background.running === 1 ? "" : "s"}</span>` : null}
    ${wait ? html`<span>waiting on you <b class="num">${clock(wait)}</b></span>` : null}
    <${ReviewBadge} rv=${m.review} />
  </span>`;
}
