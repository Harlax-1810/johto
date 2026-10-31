import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { palette, overlay, toasts, dismissToast, undoToast, follow, tick } from "../lib/store.js";

const providers = [];
/** Register a provider: () => [{id, group, label, key, enabled, why, run}] */
export function registerPalette(fn) { providers.push(fn); }

function score(label, q) {
  if (!q) return 1;
  const l = label.toLowerCase(); let i = 0, s = 0;
  for (const ch of q.toLowerCase()) {
    const j = l.indexOf(ch, i);
    if (j < 0) return 0;
    s += j === i ? 2 : 1; i = j + 1;
  }
  return s;
}

export function Palette() {
  const open = palette.value;
  const [q, setQ] = useState("");
  const [at, setAt] = useState(0);
  const input = useRef(null);
  useEffect(() => { if (open) { setQ(""); setAt(0); setTimeout(() => input.current?.focus(), 0); } }, [open]);
  const items = useMemo(() => !open ? [] : providers.flatMap((p) => p())
    .map((i) => ({ ...i, s: score(i.label, q) })).filter((i) => i.s > 0).sort((a, b) => b.s - a.s).slice(0, 40), [open, q]);
  if (!open) return null;
  const close = () => (palette.value = false);
  const run = (i) => { if (i?.enabled === false) return; close(); i?.run?.(); };
  return html`<div class="scrim" onClick=${close}></div>
  <div class="palette" role="dialog" aria-label="Commands">
    <input ref=${input} value=${q} aria-label="Search commands" autocomplete="off" spellcheck=${false}
      onInput=${(e) => { setQ(e.currentTarget.value); setAt(0); }}
      onKeyDown=${(e) => {
        if (e.key === "Escape") { e.preventDefault(); close(); }
        if (e.key === "ArrowDown") { e.preventDefault(); setAt(Math.min(at + 1, items.length - 1)); }
        if (e.key === "ArrowUp") { e.preventDefault(); setAt(Math.max(at - 1, 0)); }
        if (e.key === "Enter") { e.preventDefault(); run(items[at]); }
      }} />
    <ul>${items.map((i, n) => html`<li class=${`${n === at ? "on" : ""} ${i.enabled === false ? "off" : ""}`}
        onMouseEnter=${() => setAt(n)} onClick=${() => run(i)}>
        <span class="g">${i.group}</span><span class="lab">${i.label}</span>
        ${i.enabled === false && i.why ? html`<span class="sm faint">${i.why}</span>` : null}
        ${i.key ? html`<span class="kbd">${i.key}</span>` : null}</li>`)}</ul>
  </div>`;
}

const KEYS = [
  ["Everywhere", [["⌘↵", "Do the Next row"], ["⌘⇧↵", "Primary action of this screen"], ["⌃`", "Open or close the lane"],
                  ["⌃⇧`", "Lane full width"], ["⌘K", "Commands"], ["⌥N", "Add a run note"]]],
  ["Lists", [["j / k", "Move in the queue"], ["↵", "Open"], ["x", "Hide until it changes"], ["t", "Task page"],
             ["o", "Open in Studio"], ["/", "Search the code"], ["g h · g p · g s · g f", "Home, Repos, Scout, Finished"],
             ["⌘Z", "Undo a claim or drop"], ["?", "This list"]]],
  ["Writing", [["⌥1 – ⌥5", "Jump between fields"], ["⌥R", "Show or hide the reference pane"], ["Esc", "Leave the field"]]],
  ["Pick", [["c", "Claim"], ["← / →", "Slot variant"], ["r", "Check difficulty"], ["s", "Scout more"]]],
  ["Lane", [["⌘⇧↵", "In the lane: type /login, paste my prompt, or end an async run"], ["⇧E ⇧E", "End the run and collect it (press twice)"],
            ["Esc", "Goes to Claude"]]],
  ["Keep or drop", [["⌘⇧↵", "Keep"], ["s", "Use this run for Studio (no validation run)"], ["e", "Edit prompt"], ["d", "Drop (10 s undo)"], ["a", "Run audit"]]],
  ["Rate", [["y / n", "One-shot"], ["1 – 5", "Task Success, then Steering"], ["⌫", "Step back"]]],
  ["Studio", [["q", "Rerun AutoQC"]]],
];

export function KeyOverlay() {
  if (!overlay.value) return null;
  const close = () => (overlay.value = false);
  return html`<div class="scrim" onClick=${close}></div>
  <div class="overlay" role="dialog" aria-label="Keys" tabIndex="-1" onKeyDown=${(e) => e.key === "Escape" && close()}>
    <div class="cols">${KEYS.map(([g, rows]) => html`<section>
      <div class="label" style="margin-bottom:4px">${g}</div>
      ${rows.map(([k, d]) => html`<div class="kr"><span><span class="kbd">${k}</span></span><span>${d}</span></div>`)}
    </section>`)}</div>
  </div>`;
}

export function Toasts() {
  if (toasts.value.some((t) => t.undo?.expires_at)) tick.value;
  return html`<div class="toasts" aria-live="polite">
    ${toasts.value.map((t) => {
      const left = t.undo?.expires_at ? Math.max(0, Math.ceil((Date.parse(t.undo.expires_at) - Date.now()) / 1000)) : null;
      return html`<div class=${`toast ${t.level}`} key=${t.id}>
        <span style="flex:1">${t.text}</span>
        ${t.action ? html`<button class="tlink" onClick=${() => { dismissToast(t.id); t.action.run(); }}>${t.action.label}</button>` : null}
        ${t.link ? html`<button class="tlink" onClick=${() => { dismissToast(t.id); follow(t.link.href); }}>${t.link.label}</button>` : null}
        ${t.undo ? html`<button class="tlink" onClick=${() => undoToast(t)}
            title="Undo (⌘Z)"><span class="kbd">⌘Z</span> ${t.undo.label || "Undo"}${left != null ? html` <span class="num">${left}s</span>` : null}</button>` : null}
        <button class="tx" title="Dismiss" onClick=${() => dismissToast(t.id)}>×</button>
      </div>`;
    })}
  </div>`;
}
