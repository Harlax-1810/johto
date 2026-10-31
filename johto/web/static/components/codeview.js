import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { api } from "../lib/api.js";

const cache = new Map();

/** Real code at the pinned commit, read-only, with line numbers. */
export function CodeView({ target, path, start, end, highlight, compact }) {
  const [file, setFile] = useState(null);
  const [error, setError] = useState(null);
  const box = useRef(null);
  useEffect(() => {
    if (!target || !path) return;
    const key = `${target}|${path}`;
    setError(null);
    if (cache.has(key)) { setFile(cache.get(key)); return; }
    setFile(null);
    api.get(`/api/code/${encodeURIComponent(target)}/file?path=${encodeURIComponent(path)}`)
      .then((f) => { cache.set(key, f); setFile(f); })
      .catch((e) => setError(e.message));
  }, [target, path]);
  useEffect(() => {
    if (!file || !highlight || !box.current) return;
    const el = box.current.querySelector(`[data-n="${highlight[0]}"]`);
    el?.scrollIntoView({ block: "center" });
  }, [file, highlight?.[0]]);
  if (error) return html`<div class="sm faint" style="padding:12px 16px">${error}</div>`;
  if (!file) return html`<div class="sm faint" style="padding:12px 16px">Loading ${path}…</div>`;
  let lines = file.lines;
  if (start || end) lines = lines.filter(([n]) => n >= (start || 1) && n <= (end || 1e9));
  const hl = (n) => highlight && n >= highlight[0] && n <= (highlight[1] || highlight[0]);
  return html`<div class="code" ref=${box} style=${compact ? "border:1px solid var(--line);border-radius:6px;overflow:auto;max-height:260px" : ""}>
    ${lines.map(([n, s]) => html`<div class=${`cl ${hl(n) ? "hl" : ""}`} data-n=${n}><span class="n">${n}</span><span class="s">${s || " "}</span></div>`)}
  </div>`;
}
