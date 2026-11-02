// Shared components for the redesign (spec.md 4.5). Props are the contract; every builder uses these.
// FROZEN (lead). Build screen-specific pieces in your own directory; ask the lead to promote shared ones.
import { useEffect, useRef, useState } from "preact/hooks";
import { signal } from "@preact/signals";
import { html } from "../lib/html.js";
import { palette, overlay } from "../lib/store.js";
import { slotWords, variantWords } from "../lib/words.js";

// ---------- Viewport ----------

const mq = typeof matchMedia === "function" ? matchMedia("(max-width: 1100px)") : null;
/** True under 1100 px: the rail is a top bar and the inspector renders as a sheet. */
export const narrow = signal(!!mq?.matches);
mq?.addEventListener?.("change", (e) => { narrow.value = e.matches; });

const cx = (...a) => a.filter(Boolean).join(" ");

// ---------- Buttons, keys, dots, pills ----------

export const Kbd = ({ k }) => (k ? html`<span class="kbd">${k}</span>` : null);

/**
 * A button. kind: primary | secondary | quiet | danger. size: sm | md | lg. `k` shows a key hint (primary only).
 * `armed` is a two-press button's second state (red, `armedLabel`).
 */
export function Btn({ label, children, kind = "secondary", size = "md", k, onClick, disabled, title, armed, armedLabel, full, type = "button", class: extra }) {
  const cls = cx("btn", kind !== "secondary" && kind, size !== "md" && size, armed && "armed", full && "full", extra);
  return html`<button type=${type} class=${cls} onClick=${onClick} disabled=${disabled} title=${title}>
    ${armed ? armedLabel || "Press again" : label ?? children}${k && kind === "primary" && !armed ? html`<${Kbd} k=${k} />` : null}
  </button>`;
}

/** tone: wait | work | ok | stop | ink | idle | busy | ai */
export const Dot = ({ tone = "idle", title }) => html`<span class=${`dot ${tone}`} title=${title}></span>`;

/** tone: neutral | wait | work | ok | stop | ai | outline | ink. `dot` adds a leading dot of the same meaning. */
export function Pill({ tone = "neutral", dot, title, children }) {
  return html`<span class=${cx("pill", tone !== "neutral" && tone)} title=${title}>
    ${dot ? html`<${Dot} tone=${typeof dot === "string" ? dot : tone} />` : null}${children}
  </span>`;
}

/**
 * A slot group with its open count: "Understand · Backend · Enterprise  65". `variant` shows the prompt style and
 * interaction instead ("Casual · Interactive  19"). The count is a size, in ink. Clickable when onClick is set.
 */
export function SlotPill({ slot, count, selected, onClick, disabled, variant, showScale = true, size, title }) {
  const label = variant ? variantWords(slot) : slotWords(slot, { scale: showScale });
  const cls = cx("slot-pill", selected && "sel", size === "sm" && "sm");
  // The count is a size, not a pass: it stays ink (green means done or passes, and every big group was green).
  const inner = html`<span class="sl">${label}</span>${count != null ? html`<span class="sc">${count}</span>` : null}`;
  if (!onClick) return html`<span class=${cls} title=${title}>${inner}</span>`;
  return html`<button type="button" class=${cls} onClick=${onClick} disabled=${disabled} aria-pressed=${!!selected} title=${title}>${inner}</button>`;
}

// ---------- The AI mark ----------

/** "Claude's research, Oct 30", "Findings from the review", "Claude's message". One per AI block. */
export const AiTag = ({ label = "Claude's research" }) => html`<span class="ai-tag">${label}</span>`;

/** A lilac panel for a whole block of AI text. */
export function AiBlock({ label, right, children, class: extra }) {
  return html`<section class=${cx("ai-block", extra)}>
    ${label || right ? html`<div class="ai-head">${label ? html`<${AiTag} label=${label} />` : html`<span></span>`}${right || null}</div>` : null}
    ${children}
  </section>`;
}

// ---------- Cards and headings ----------

/** A card. `ai` adds the lilac left rule. With onClick it is focusable and opens on Enter or Space. */
export function Card({ ai, selected, quiet, onClick, title, class: extra, children }) {
  const cls = cx("card", ai && "ai", selected && "sel", quiet && "quiet", onClick && "click", extra);
  if (!onClick) return html`<div class=${cls} title=${title}>${children}</div>`;
  return html`<div class=${cls} role="button" tabindex="0" aria-pressed=${selected ? "true" : undefined} title=${title}
    onClick=${onClick} onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(e); } }}>${children}</div>`;
}

/** The screen's first line: one plain sentence (serif 28), an optional grey support line, header actions at right. */
export function Lede({ title, support, tag, right, children }) {
  return html`<header class="lede">
    <div class="lede-text">
      ${tag ? html`<div class="lede-tag"><${AiTag} label=${tag} /></div>` : null}
      <h1>${title}</h1>
      ${support ? html`<p class="lede-support">${support}</p>` : null}
      ${children || null}
    </div>
    ${right ? html`<div class="lede-right">${right}</div>` : null}
  </header>`;
}

/** A section heading (serif 21 on reading screens; serif=false for sans 20). */
export function SectionHead({ title, right, serif = true }) {
  return html`<div class="section-head"><h2 class=${serif ? "" : "sans"}>${title}</h2>${right || null}</div>`;
}

export const Band = ({ children, class: extra }) => html`<section class=${cx("band", extra)}>${children}</section>`;

/** A strip that needs him: tone wait (amber) | stop (red) | ok. `actions` are buttons at the right. */
export function Flag({ tone = "wait", title, children, actions }) {
  return html`<div class=${cx("flag", tone !== "wait" && tone)} role=${tone === "stop" ? "alert" : "status"}>
    <div class="flag-text">${title ? html`<b>${title}</b> ` : null}${children}</div>
    ${actions ? html`<div class="flag-acts">${actions}</div>` : null}
  </div>`;
}

export function Empty({ title, body, action }) {
  return html`<div class="empty">${title ? html`<h3>${title}</h3>` : null}${body ? html`<p>${body}</p>` : null}${action || null}</div>`;
}

export const Measure = ({ children }) => html`<div class="measure">${children}</div>`;

export function Facts({ items }) {
  return html`<dl class="facts">${(items || []).filter(Boolean).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>`;
}

export const Rows = ({ children }) => html`<div class="rows">${children}</div>`;

/** keys: [["J K", "move"], ["G", "goal"], ...] */
export function KeyBar({ keys }) {
  return html`<div class="keybar">${(keys || []).map(([k, label]) => html`<span class="kb">${String(k).split(" ").map((x) => html`<${Kbd} k=${x} />`)}${label}</span>`)}</div>`;
}

// ---------- Tabs, segmented, path ----------

/** tabs: [{id, label, done, count, title}] */
export function Tabs({ tabs, value, onChange, label = "Sections" }) {
  return html`<div class="tabs" role="tablist" aria-label=${label}>
    ${(tabs || []).map((t) => html`<button type="button" role="tab" aria-selected=${value === t.id} class=${cx("tab", value === t.id && "on")}
        title=${t.title} onClick=${() => onChange?.(t.id)}>
      ${t.short ? html`<span class="tl-l">${t.label}</span><span class="tl-s">${t.short}</span>` : t.label}${t.done ? html`<span class="done" aria-label="done">✓</span>`
        : t.todo ? html`<span class="todo" aria-label="not done yet"></span>` : null}${t.count != null ? html`<span class="tc">${t.count}</span>` : null}
    </button>`)}
  </div>`;
}

/** options: [{value, label, hint}] */
export function Seg({ options, value, onChange, size, label }) {
  return html`<div class=${cx("seg", size === "sm" && "sm")} role="radiogroup" aria-label=${label}>
    ${(options || []).map((o) => html`<button type="button" role="radio" aria-checked=${value === o.value} class=${value === o.value ? "on" : ""}
        title=${o.hint} onClick=${() => onChange?.(o.value)}>${o.label}</button>`)}
  </div>`;
}

/**
 * The task's path: steps [{id, label, state: done|current|later|blocked|wait, summary}]. `value` is the step being
 * viewed (underlined); onPick(id) shows that step's body.
 */
export function PathStrip({ steps, value, onPick }) {
  return html`<nav class="path" aria-label="Steps">
    ${(steps || []).map((s) => html`<button type="button" class=${cx("step", s.state, value === s.id && "viewing")}
        aria-current=${s.state === "current" ? "step" : undefined} onClick=${() => onPick?.(s.id)}>
      <span class="sl">${s.label}${s.flag ? html`<span class=${cx("pf", s.flag)} title=${s.flag_why || ""} aria-label=${s.flag_why || "Needs you"}></span>` : null}</span>${s.summary ? html`<span class="ss" title=${s.summary}>${s.summary}</span>` : null}
    </button>`)}
  </nav>`;
}

// ---------- Stage, inspector, sheet ----------

/** A screen's scrolling stage (centred, capped at --stage-max, stage padding). */
export function Stage({ children, class: extra }) {
  return html`<div class="page"><div class=${cx("stage", extra)}><div class="stage-in">${children}</div></div></div>`;
}

/**
 * The stage with an inspector beside it. Pass an <Inspector> vnode (or null). At ≥ 1100 px the inspector pushes
 * the stage narrower; below that it renders as a sheet over the stage.
 */
export function StageWithInspector({ inspector, children, class: extra }) {
  const open = !!inspector && inspector.props?.open !== false;
  return html`<div class=${cx("page", open && !narrow.value && "with-insp")}>
    <div class=${cx("stage", extra)}><div class="stage-in">${children}</div></div>
    ${open ? inspector : null}
  </div>`;
}

/** Esc closes (not while the palette or key overlay is open); focus goes back where it was when it opened. */
function useDismiss(open, onClose) {
  const back = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    back.current = document.activeElement;
    const onKey = (e) => {
      if (e.key !== "Escape" || palette.value || overlay.value) return;
      if (e.target?.closest?.(".xterm")) return;
      e.preventDefault(); e.stopPropagation(); onClose?.();
    };
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      const el = back.current;
      if (el?.isConnected && typeof el.focus === "function") setTimeout(() => el.focus(), 0);
    };
  }, [open]);
}

function PanelHead({ tag, title, meta, onClose }) {
  return html`<header class="insp-head">
    ${tag ? html`<div class="insp-tag"><${AiTag} label=${tag} /></div>` : null}
    ${title ? html`<h2>${title}</h2>` : null}
    <button type="button" class="insp-x" title="Close (Esc)" aria-label="Close" onClick=${onClose}>✕</button>
    ${meta ? html`<div class="insp-meta">${meta}</div>` : null}
  </header>`;
}

/**
 * The inspector: detail for what is selected. `tag` is the AI tag when the content is AI-written. `foot` is
 * pinned at the bottom (the claim). Under 1100 px it renders as a Sheet.
 */
export function Inspector({ open = true, onClose, tag, title, meta, children, foot, label }) {
  useDismiss(open, onClose);
  if (!open) return null;
  if (narrow.value) return html`<${SheetFrame} onClose=${onClose} tag=${tag} title=${title} meta=${meta} foot=${foot} label=${label}>${children}<//>`;
  return html`<aside class="inspector" aria-label=${label || title || "Details"}>
    <${PanelHead} tag=${tag} title=${title} meta=${meta} onClose=${onClose} />
    <div class="insp-body">${children}</div>
    ${foot ? html`<footer class="insp-foot">${foot}</footer>` : null}
  </aside>`;
}

function SheetFrame({ onClose, tag, title, meta, foot, children, width, label }) {
  return html`<div class="scrim" onClick=${onClose}></div>
    <aside class="sheet" role="dialog" aria-modal="true" aria-label=${label || title || "Details"} style=${width ? `--sheet-w:${width}px` : ""}>
      <${PanelHead} tag=${tag} title=${title} meta=${meta} onClose=${onClose} />
      <div class="sheet-body">${children}</div>
      ${foot ? html`<footer class="insp-foot">${foot}</footer>` : null}
    </aside>`;
}

/** An overlay panel from the right with a scrim (slot map, sorting all findings at narrow widths). */
export function Sheet({ open, onClose, tag, title, meta, children, foot, width, label }) {
  useDismiss(open, onClose);
  if (!open) return null;
  return html`<${SheetFrame} onClose=${onClose} tag=${tag} title=${title} meta=${meta} foot=${foot} width=${width} label=${label}>${children}<//>`;
}

// ---------- Menu ----------

/**
 * A button that opens a small menu. items: [{label, run, danger, disabled, why}] or "-" for a rule, or
 * {note: "text"} for a quiet line.
 */
export function Menu({ label = "⋯", title = "More", items, align = "right", kind = "quiet", size = "md" }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    document.addEventListener("pointerdown", away, true);
    window.addEventListener("keydown", esc, true);
    return () => { document.removeEventListener("pointerdown", away, true); window.removeEventListener("keydown", esc, true); };
  }, [open]);
  return html`<span class="menu-wrap" ref=${ref}>
    <${Btn} kind=${kind} size=${size} label=${label} title=${title} onClick=${() => setOpen(!open)} />
    ${open ? html`<div class=${cx("menu", align === "left" && "left")} role="menu">
      ${(items || []).filter(Boolean).map((it) => (it === "-" ? html`<hr />` : it.note ? html`<div class="mnote">${it.note}</div>`
        : html`<button type="button" role="menuitem" class=${it.danger ? "danger" : ""} disabled=${it.disabled} title=${it.disabled ? it.why : it.title}
            onClick=${() => { setOpen(false); it.run?.(); }}>${it.label}</button>`))}
    </div>` : null}
  </span>`;
}
