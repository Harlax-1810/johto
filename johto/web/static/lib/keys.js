// Keyboard routing. Chords work everywhere; single letters only outside fields and the terminal.
const chords = [];           // {spec, handler}
const sequences = new Map(); // "g h" -> handler
const contexts = [];         // stack of {name, bindings: {key: handler}}
let pendingG = 0;
let saved = null;

const isMac = navigator.platform.includes("Mac");

export function parse(spec) {
  const parts = spec.split("+");
  const key = parts.pop();
  const m = new Set(parts.map((p) => p.toLowerCase()));
  return { key: key.toLowerCase(), meta: m.has("mod") ? isMac : m.has("meta"), ctrl: m.has("ctrl") || (m.has("mod") && !isMac),
           shift: m.has("shift"), alt: m.has("alt") };
}

function matches(p, e) {
  const k = e.key === "Enter" ? "enter" : e.code === "Backquote" ? "`" : e.code.startsWith("Key") ? e.code.slice(3).toLowerCase() : e.key.toLowerCase();
  return k === p.key && !!e.metaKey === !!p.meta && !!e.ctrlKey === !!p.ctrl && !!e.shiftKey === !!p.shift && !!e.altKey === !!p.alt;
}

export function zone(el = document.activeElement) {
  if (!el || el === document.body) return "list";
  if (el.closest?.(".xterm")) return "terminal";
  if (el.closest?.(".palette")) return "palette";
  if (el.matches?.("textarea, input, [contenteditable=true]")) return "field";
  return "list";
}

export function registerChord(spec, handler) { chords.push({ p: parse(spec), spec, handler }); }
export function registerSequence(seq, handler) { sequences.set(seq, handler); }
export function pushContext(name, bindings) { contexts.push({ name, bindings }); return () => popContext(name); }
export function popContext(name) {
  const i = contexts.map((c) => c.name).lastIndexOf(name);
  if (i >= 0) contexts.splice(i, 1);
}

/** True when a chord should be kept from the terminal (xterm custom key handler). */
export function isGlobalChord(e) { return chords.some((c) => matches(c.p, e)); }

export function saveFocus() {
  const el = document.activeElement;
  if (el && el !== document.body) saved = { el, start: el.selectionStart, end: el.selectionEnd, scroll: el.scrollTop };
}
export function restoreFocus() {
  if (!saved?.el?.isConnected) return;
  saved.el.focus();
  if (saved.start != null) saved.el.setSelectionRange(saved.start, saved.end);
  saved.el.scrollTop = saved.scroll;
  saved = null;
}

function onKey(e) {
  for (const c of chords) {
    if (matches(c.p, e)) { e.preventDefault(); c.handler(e); return; }
  }
  const z = zone();
  if (z !== "list") return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key;
  if (pendingG && Date.now() - pendingG < 900) {
    pendingG = 0;
    const h = sequences.get(`g ${k}`);
    if (h) { e.preventDefault(); h(e); }
    return;
  }
  if (k === "g") { pendingG = Date.now(); return; }
  for (let i = contexts.length - 1; i >= 0; i--) {
    const h = contexts[i].bindings[k];
    if (h) { e.preventDefault(); h(e); return; }
  }
}

window.addEventListener("keydown", onKey, true);

export const KEY_LABEL = { "mod+enter": "⌘↵", "mod+shift+enter": "⌘⇧↵", "ctrl+`": "⌃`", "mod+k": "⌘K" };
