import { useRef } from "preact/hooks";
import { html } from "../lib/html.js";
import { fieldFocused, tick, jobs, jobFor, isLive, jobLine, openDrawer, navigate } from "../lib/store.js";
import { est, clock } from "../lib/format.js";
import { Kbd, Dot } from "./ui.js";

function Row({ item, isNext, sel, onSelect, onHide }) {
  tick.value;
  const laneRow = item.lane && item.group !== "running";
  const right = laneRow ? clock(item.since) : est(item.est_s);
  const cls = ["row", sel ? "sel" : "", isNext ? "next" : "", laneRow ? "lane-row" : ""].join(" ");
  return html`<div class=${cls} role="option" aria-selected=${sel} data-id=${item.id} onClick=${() => onSelect(item)}>
    <span class="t">${item.title}</span>
    <span class="e num">${right}</span>
    <span class="l">${item.label}</span>
    <span class="k">${isNext ? html`<${Kbd} k="⌘↵" />` : null}</span>
    <button class="x" title="Hide until it changes (x)" onClick=${(e) => { e.stopPropagation(); onHide?.(item); }}>×</button>
  </div>`;
}

// Machine work the running row can show progress for, newest first.
const RUN_JOBS = ["lane_collect", "pack", "review_pack", "save", "qc", "qc_wait", "submit", "lane_prepare", "claim", "unclaim", "drop", "lane_teardown"];

function RunRow({ item, sel }) {
  tick.value;
  jobs.value;
  const working = /working|Claude/.test(item.title);
  const j = item.short ? jobFor(RUN_JOBS, item.short) : null;
  const line = isLive(j) ? jobLine(j) : "";
  const open = item.lane ? () => openDrawer("user") : undefined;
  return html`<div class=${`row run ${open ? "open" : ""} ${sel ? "sel" : ""}`} title=${line || item.reason || (open ? "Open the lane (⌃`)" : "")} onClick=${open}>
    <span class="t">${item.label}</span><span class="e num">${item.since && /running|Collecting|Saving|Submitting/.test(item.title) ? clock(item.since) : ""}</span>
    <span class="l">${working ? html`<${Dot} kind="work" />` : html`<${Dot} kind="busy" />`}${item.title}${line ? html`<span class="faint"> · ${line}</span>` : null}</span>
  </div>`;
}

/** The queue. Order freezes while a field has focus so nothing moves under his hands. */
export function Rail({ queue, running, selectedId, onSelect, onHide }) {
  const frozen = useRef(null);
  let rows = queue;
  if (fieldFocused.value && frozen.current) {
    const ids = frozen.current;
    rows = [...queue].sort((a, b) => (ids.indexOf(a.id) + 1 || 999) - (ids.indexOf(b.id) + 1 || 999));
  } else {
    frozen.current = queue.map((q) => q.id);
  }
  const next = queue[0];
  const rest = rows.filter((r) => r.id !== next?.id);
  // Submitted tasks wait days for review: one quiet line that links to Finished, no spinner per task.
  const inReview = (running || []).filter((it) => it.kind === "in_review");
  const active = (running || []).filter((it) => it.kind !== "in_review");
  return html`<aside class="rail" role="listbox" aria-label="Queue">
    ${next ? html`
      <div class="grp"><span class="label">Next</span></div>
      <${Row} item=${next} isNext=${true} sel=${selectedId === next.id} onSelect=${onSelect} onHide=${onHide} />
      ${rest.length ? html`<div class="rule"></div>` : null}` : null}
    ${rest.map((it) => html`<${Row} key=${it.id} item=${it} isNext=${false} sel=${selectedId === it.id} onSelect=${onSelect} onHide=${onHide} />`)}
    ${active.length || inReview.length ? html`
      <div class="grp" style="margin-top:12px"><span class="label">Running</span></div>
      ${active.map((it) => html`<${RunRow} key=${it.id} item=${it} sel=${it.id === selectedId} />`)}
      ${inReview.length ? html`<div class="row run open" title="Submitted tasks wait for review on Finished" onClick=${() => navigate("/finished")}>
        <span class="l faint">${inReview.length} in review →</span></div>` : null}` : null}
  </aside>`;
}
