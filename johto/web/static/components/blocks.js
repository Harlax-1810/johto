// Renderers for dossier blocks (docs/dossier-spec.md section 3). Everything here except real code and
// live counts is AI research: it is labelled with the lilac AI label, never offered as text for a field.
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { CodeView } from "./codeview.js";
import { shortSlot } from "../lib/format.js";

// --- pointers and real code ----------------------------------------------------------

export function parsePointer(p) {
  const s = (p || "").trim();
  const m = /^(.+?):(\d+)(?:-(\d+))?$/.exec(s);
  if (!m) return { path: s, start: null, end: null };
  const start = +m[2];
  return { path: m[1], start, end: m[3] ? +m[3] : start };
}

const baseName = (p) => (p || "").replace(/\/$/, "").split("/").pop() || p;
const range = ({ start, end }) => (start ? `:${start}${end !== start ? `-${end}` : ""}` : "");
export const shortPtr = (p) => { const x = parsePointer(p); return baseName(x.path) + range(x); };

let seq = 0;
/** The real code behind a pointer. With `context`, that many lines show around the range and the range
 * is marked in the gutter only. The mark is a scoped style, so opening never scrolls the column. */
export function PointerCode({ ctx, pointer, context = 0 }) {
  const x = parsePointer(pointer);
  const id = useMemo(() => `pc${++seq}`, []);
  const span = x.start ? x.end - x.start + 1 : 0;
  let style = "";
  if (context && span && span <= 40) {
    const sel = Array.from({ length: span }, (_, i) => `#${id} .cl[data-n="${x.start + i}"] .n`);
    style = `${sel.join(",")}{background:var(--select);color:var(--text);font-weight:600;box-shadow:inset 3px 0 var(--select-bar)}`;
  }
  const gh = ctx.repo && ctx.commit
    ? `https://github.com/${ctx.repo}/blob/${ctx.commit}/${x.path}${x.start ? `#L${x.start}${x.end !== x.start ? `-L${x.end}` : ""}` : ""}`
    : null;
  return html`<div class="blk-excerpt" id=${id}>
    ${style ? html`<style>${style}</style>` : null}
    <div class="blk-codehead"><span class="mono">${x.path}${range(x)}</span>
      ${gh ? html`<a href=${gh} target="_blank" rel="noreferrer">GitHub ↗</a>` : null}</div>
    <div class="blk-codebox">
      <${CodeView} target=${ctx.target} path=${x.path} start=${x.start ? Math.max(1, x.start - context) : null}
        end=${x.end ? x.end + context : null} />
    </div>
  </div>`;
}

/** A pointer button; the caller decides where the code opens. */
export function Ptr({ pointer, open, onToggle, full }) {
  return html`<button class=${`ptr blk-ptr ${open ? "on" : ""}`} aria-expanded=${!!open} title=${pointer}
    onClick=${(e) => { e.stopPropagation(); onToggle?.(pointer); }}>${full ? pointer : shortPtr(pointer)} ${open ? "▾" : "▸"}</button>`;
}

function useToggles() {
  const [open, setOpen] = useState(() => new Set());
  const toggle = (k) => setOpen((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n; });
  return [open, toggle];
}

// --- small shared pieces -------------------------------------------------------------

export const ONE_SHOT_TONE = { unlikely: "green", possible: "amber", likely: "red" };
export function OneShot({ o, word }) {
  const l = o?.likelihood;
  if (!l) return null;
  return html`<span class=${`chip ${ONE_SHOT_TONE[l] || ""}`} title=${`One-shot ${l} (estimate)${o.why ? `: ${o.why}` : ""}`}>${l}${word ? " one-shot" : ""}</span>`;
}
const SIZE_WORD = { S: "Small", M: "Medium", L: "Large" };
/** Bounded-task size: the letter in tables (a column legend explains it), the word elsewhere. */
export const Size = ({ s, long }) => s
  ? html`<span class=${`chip ghost blk-size ${long ? "long" : ""}`} title=${`${SIZE_WORD[s] || s} bounded task`}>${long ? SIZE_WORD[s] || s : s}</span>`
  : null;

const HEALTH_TONE = { solid: "green", fragile: "amber", neglected: "red" };
const Health = ({ h }) => html`<span class=${`chip ${HEALTH_TONE[h] || ""}`}>${h}</span>`;

function Section({ b, label, children, right }) {
  return html`<section class=${`blk blk-${b.type}`}>
    <div class="blk-h"><span class="label blk-t">${b.title || label}</span>${right || null}</div>
    ${children}
  </section>`;
}

// --- overview --------------------------------------------------------------------------

function Overview({ b }) {
  const sz = b.size || {};
  const run = b.run || {};
  return html`<${Section} b=${b} label="Overview">
    <p class="blk-what">${b.what}</p>
    <dl class="facts blk-dl">
      ${b.users ? html`<dt>For</dt><dd>${b.users}</dd>` : null}
      ${(b.stack || []).length ? html`<dt>Stack</dt><dd class="blk-stack">${b.stack.map((s) => html`<span class="chip ghost">${s}</span>`)}</dd>` : null}
      ${sz.source_files != null ? html`<dt>Size</dt><dd><span class="num">${sz.source_files}</span> source · <span class="num">${sz.test_files ?? 0}</span> test files${(sz.languages || []).length ? html` <span class="faint">· ${sz.languages.join(", ")}</span>` : null}</dd>` : null}
      ${(run.setup || []).length ? html`<dt>Setup</dt><dd class="blk-cmds">${run.setup.map((c) => html`<code>${c}</code>`)}</dd>` : null}
      ${(run.test || []).length ? html`<dt>Tests</dt><dd class="blk-cmds">${run.test.map((c) => html`<code>${c}</code>`)}</dd>` : null}
      ${run.notes ? html`<dt>Notes</dt><dd class="muted">${run.notes}</dd>` : null}
    </dl>
  <//>`;
}

// --- architecture: a layered box-and-arrow diagram laid out here --------------------
// Rows follow the edges (longest path), so every arrow points down to the next row and no arrow
// runs inside a row. Kinds only break ties and place nodes that have no edges.

const KIND_RANK = { ui: 0, external: 0, service: 1, module: 2, worker: 2, store: 3, config: 3 };

let measureCtx = null;
function textW(text, font) {
  try {
    measureCtx = measureCtx || document.createElement("canvas").getContext("2d");
    measureCtx.font = font;
    return measureCtx.measureText(text || "").width;
  } catch { return (text || "").length * 6.5; }
}
/** Truncate text to a pixel width, measured with the real font. */
function fit(text, px, font) {
  text = text || "";
  if (textW(text, font) <= px) return text;
  let t = text;
  while (t.length > 1 && textW(t + "…", font) > px) t = t.slice(0, -1);
  return t.trimEnd() + "…";
}
const FONT_L = '600 12px "Public Sans", sans-serif';
const FONT_P = '10.5px ui-monospace, "SF Mono", Menlo, monospace';
const FONT_E = '600 10.5px "Public Sans", sans-serif';

/** Insert pass-through slots for edges that skip a row, then order each row by barycenter. */
function order(nodes, edges, nRows) {
  const all = [...nodes];
  for (const e of edges) {
    e.chain = [e.s];
    for (let L = e.s.L + 1; L < e.t.L; L++) {
      const dm = { id: `~${e.s.id}>${e.t.id}@${L}`, L, dummy: true, i: 1000 + all.length };
      all.push(dm); e.chain.push(dm);
    }
    e.chain.push(e.t);
  }
  const rows = Array.from({ length: nRows }, (_, li) => all.filter((n) => n.L === li).sort((a, c) => a.i - c.i));
  const adj = new Map(all.map((n) => [n, []]));
  for (const e of edges) for (let k = 1; k < e.chain.length; k++) {
    const a = e.chain[k - 1], c = e.chain[k];
    adj.get(a).push(c); adj.get(c).push(a);
  }
  const nx = (n) => (rows[n.L].indexOf(n) + 0.5) / rows[n.L].length;
  for (let pass = 0; pass < 10; pass++) {
    const seq = rows.map((_, i) => (pass % 2 ? rows.length - 1 - i : i));
    for (const li of seq) {
      const bc = new Map(rows[li].map((n) => {
        const ns = adj.get(n);
        return [n, ns.length ? ns.reduce((s, m) => s + nx(m), 0) / ns.length : nx(n)];
      }));
      rows[li].sort((a, c) => bc.get(a) - bc.get(c) || a.i - c.i);
    }
  }
  return rows;
}

/** Row per node: cycles broken, longest path from the roots, roots pulled down next to their children. */
function rank(nodes, edges) {
  const out = new Map(nodes.map((n) => [n.id, []]));
  edges.forEach((e) => out.get(e.s.id).push(e));
  const state = {};
  const dfs = (n) => {
    state[n.id] = 1;
    for (const e of out.get(n.id)) {
      if (state[e.t.id] === 1) { e.rev = true; [e.s, e.t] = [e.t, e.s]; } else if (!state[e.t.id]) dfs(e.t);
    }
    state[n.id] = 2;
  };
  [...nodes].sort((a, c) => a.K - c.K || a.i - c.i).forEach((n) => { if (!state[n.id]) dfs(n); });
  const preds = new Map(nodes.map((n) => [n.id, []])), succs = new Map(nodes.map((n) => [n.id, []]));
  edges.forEach((e) => { preds.get(e.t.id).push(e.s); succs.get(e.s.id).push(e.t); });
  const memo = new Map();
  const depth = (n) => {
    if (memo.has(n.id)) return memo.get(n.id);
    memo.set(n.id, 0);
    const d = preds.get(n.id).reduce((m, p) => Math.max(m, depth(p) + 1), 0);
    memo.set(n.id, d);
    return d;
  };
  nodes.forEach((n) => (n.L = depth(n)));
  for (let k = 0; k < 3; k++) for (const n of nodes) {
    const ss = succs.get(n.id);
    if (!preds.get(n.id).length && ss.length) n.L = Math.max(0, Math.min(...ss.map((s) => s.L)) - 1);
  }
  const linked = nodes.filter((n) => preds.get(n.id).length || succs.get(n.id).length);
  const nRows = Math.max(1, ...linked.map((n) => n.L + 1));
  for (const n of nodes) if (!linked.includes(n)) {
    const want = Math.round((n.K / 3) * (nRows - 1));
    const count = (L) => nodes.filter((m) => m !== n && m.L === L && (linked.includes(m) || m.placed)).length;
    let best = want;
    for (let L = 0; L < nRows; L++) if (count(L) < count(best) - 1 || (count(L) === count(best) && Math.abs(L - want) < Math.abs(best - want))) best = L;
    n.L = best; n.placed = true;
  }
  return nRows;
}

function layout(b, W) {
  const gap = 14, dgap = 6, dw = 8, nh = 40, vgap = 46, pad = 2, minW = 108, maxW = 188;
  const avail = W - 2 * pad;
  const perRow = Math.max(2, Math.floor((avail + gap) / (minW + gap)));
  const nodes = (b.nodes || []).map((n, i) => ({ ...n, i, K: KIND_RANK[n.kind] ?? 2 }));
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const mk = () => (b.edges || []).filter((e) => byId[e.from] && byId[e.to] && e.from !== e.to)
    .map((e) => ({ ...e, s: byId[e.from], t: byId[e.to], rev: false }));
  // Pass 1: rows by rank, ordered; a row wider than the column wraps into balanced rows.
  let edges = mk();
  const nRank = rank(nodes, edges);
  const first = order(nodes, edges, nRank).map((L) => L.filter((n) => !n.dummy));
  let row = 0;
  for (const L of first) {
    const rows = Math.ceil(L.length / perRow) || 1, size = Math.ceil(L.length / rows);
    for (let r = 0; r < rows; r++) {
      L.slice(r * size, (r + 1) * size).forEach((n, k) => { n.L = row; n.i = k; });
      row++;
    }
  }
  // Pass 2: the wrapped rows are the layers. Keep the cycle breaks from pass 1.
  const flips = new Set(edges.filter((e) => e.rev).map((e) => `${e.from}>${e.to}`));
  edges = mk().map((e) => (flips.has(`${e.from}>${e.to}`) ? { ...e, s: e.t, t: e.s, rev: true } : e));
  const layers = order(nodes, edges, row);
  let nw = maxW;
  for (const L of layers) {
    const real = L.filter((n) => !n.dummy).length, dum = L.length - real;
    if (real) nw = Math.min(nw, Math.floor((avail - dum * dw - (L.length - 1) * gap) / real));
  }
  nw = Math.max(84, nw);
  const wOf = (n) => (n.dummy ? dw : nw);
  const sep = (a, c) => (a.dummy && c.dummy ? dgap : gap);
  // x: start packed and centered, then pull each node toward its neighbours, keeping row order.
  layers.forEach((L, li) => {
    const tot = L.reduce((s, n, k) => s + wOf(n) + (k ? sep(L[k - 1], n) : 0), 0);
    let x = pad + Math.max(0, (avail - tot) / 2);
    L.forEach((n, k) => { if (k) x += sep(L[k - 1], n); n.x = x; n.y = 6 + li * (nh + vgap); x += wOf(n); });
  });
  const up = new Map(), down = new Map();
  const push = (m, k, v) => (m.has(k) ? m.get(k).push(v) : m.set(k, [v]));
  for (const e of edges) for (let k = 1; k < e.chain.length; k++) { push(down, e.chain[k - 1], e.chain[k]); push(up, e.chain[k], e.chain[k - 1]); }
  const cx = (n) => n.x + wOf(n) / 2;
  const resolve = (L, want) => {
    const x = want.slice();
    for (let k = 0; k < L.length; k++) {
      x[k] = Math.max(x[k], pad);
      if (k) x[k] = Math.max(x[k], x[k - 1] + wOf(L[k - 1]) + sep(L[k - 1], L[k]));
    }
    const R = W - pad, last = L.length - 1;
    if (last >= 0 && x[last] + wOf(L[last]) > R) {
      x[last] = R - wOf(L[last]);
      for (let k = last - 1; k >= 0; k--) x[k] = Math.min(x[k], x[k + 1] - sep(L[k], L[k + 1]) - wOf(L[k]));
    }
    L.forEach((n, k) => (n.x = x[k]));
  };
  for (let it = 0; it < 8; it++) {
    const goingDown = it % 2 === 0;
    const seqL = goingDown ? layers.slice(1) : layers.slice(0, -1).reverse();
    for (const L of seqL) {
      resolve(L, L.map((n) => {
        const ns = (goingDown ? up : down).get(n) || (goingDown ? down : up).get(n) || [];
        return ns.length ? ns.reduce((s, m) => s + cx(m), 0) / ns.length - wOf(n) / 2 : n.x;
      }));
    }
  }
  // Centre the whole drawing.
  const all = layers.flat();
  const minX = Math.min(...all.map((n) => n.x)), maxX = Math.max(...all.map((n) => n.x + wOf(n)));
  const shift = pad + (avail - (maxX - minX)) / 2 - minX;
  all.forEach((n) => (n.x += shift));
  // Anchors spread along each side of a real node, ordered by where the next hop sits.
  const sides = new Map();
  const side = (n, s) => { const k = `${n.id}:${s}`; if (!sides.has(k)) sides.set(k, { n, s, list: [] }); return sides.get(k).list; };
  for (const e of edges) {
    side(e.s, "b").push({ e, end: "s", other: e.chain[1] });
    side(e.t, "t").push({ e, end: "t", other: e.chain[e.chain.length - 2] });
  }
  for (const { n, s, list } of sides.values()) {
    list.sort((a, c) => cx(a.other) - cx(c.other));
    list.forEach((a, j) => {
      a.e[a.end === "s" ? "p0" : "p3"] = [n.x + (nw * (j + 1)) / (list.length + 1), s === "t" ? n.y : n.y + nh];
    });
  }
  // Paths: a cubic between rows, a straight run through each pass-through slot. Sampled for labels.
  const cubic = (a, c) => {
    const my = (a[1] + c[1]) / 2, c1 = [a[0], my], c2 = [c[0], my];
    const pts = [];
    for (let k = 1; k <= 12; k++) {
      const t = k / 12, u = 1 - t;
      pts.push([u * u * u * a[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t * t * t * c[0],
                u * u * u * a[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t * t * t * c[1]]);
    }
    return { d: `C${c1[0]},${c1[1]} ${c2[0]},${c2[1]} ${c[0]},${c[1]}`, pts };
  };
  for (const e of edges) {
    let p = e.p0, d = `M${p}`, pts = [p];
    for (const dm of e.chain.slice(1, -1)) {
      const x = dm.x + dw / 2, c = cubic(p, [x, dm.y]);
      d += ` ${c.d} L${x},${dm.y + nh}`;
      pts.push(...c.pts, [x, dm.y + nh / 2], [x, dm.y + nh]);
      p = [x, dm.y + nh];
    }
    const c = cubic(p, e.p3);
    e.d = d + ` ${c.d}`;
    e.pts = [...pts, ...c.pts];
  }
  const H = 6 + layers.length * nh + (layers.length - 1) * vgap + 8;
  return { nodes, edges, rows: layers.map((L) => L.filter((n) => !n.dummy)), nw, nh, H, W };
}

/** Labels for the edges of one node: each goes at the first free spot along its edge, or is left out
 * (the connection list under the diagram still names it). */
function placeLabels(g, focus) {
  if (!focus) return [];
  const boxes = g.nodes.map((n) => ({ x: n.x - 3, y: n.y - 3, w: g.nw + 6, h: g.nh + 6 }));
  const hit = (r) => boxes.some((q) => r.x < q.x + q.w && q.x < r.x + r.w && r.y < q.y + q.h && q.y < r.y + r.h);
  const out = [];
  const mine = g.edges.filter((e) => e.label && (e.from === focus || e.to === focus));
  for (const e of mine) {
    const w = textW(e.label, FONT_E) + 10, h = 15, n = e.pts.length;
    for (const f of [0.5, 0.42, 0.58, 0.34, 0.66, 0.26, 0.74, 0.18, 0.82]) {
      const [px, py] = e.pts[Math.min(n - 1, Math.round(f * (n - 1)))];
      const r = { x: Math.min(Math.max(px - w / 2, 0), g.W - w), y: py - h / 2, w, h };
      if (!hit(r)) { out.push({ e, x: r.x + w / 2, y: py }); boxes.push(r); break; }
    }
  }
  return out;
}

function Architecture({ b }) {
  const box = useRef(null);
  const [W, setW] = useState(600);
  const [sel, setSel] = useState(null);
  const [hover, setHover] = useState(null);
  useEffect(() => {
    if (!box.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(360, Math.floor(e.contentRect.width))));
    ro.observe(box.current);
    return () => ro.disconnect();
  }, []);
  const g = useMemo(() => layout(b, W), [b, W]);
  const focus = hover || sel;
  const labels = useMemo(() => placeLabels(g, focus), [g, focus]);
  const touches = (e) => focus && (e.from === focus || e.to === focus);
  const near = (n) => !focus || n.id === focus || g.edges.some((e) => touches(e) && (e.from === n.id || e.to === n.id));
  const selNode = g.nodes.find((n) => n.id === sel);
  const pathOf = (n) => (n.paths || [])[0] ? baseName(n.paths[0]) + (n.paths[0].endsWith("/") ? "/" : "") : n.kind;
  return html`<${Section} b=${b} label="Architecture">
    ${b.summary ? html`<p class="blk-sum">${b.summary}</p>` : null}
    <div class=${`arch ${focus ? "focus" : ""}`} ref=${box}>
      <svg width=${W} height=${g.H} viewBox=${`0 0 ${W} ${g.H}`} role="img" aria-label="Architecture diagram">
        <defs>
          <marker id="arch-a" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0.5 L7.5,4 L0,7.5 z" class="arch-ah" /></marker>
          <marker id="arch-a2" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0.5 L7.5,4 L0,7.5 z" class="arch-ah on" /></marker>
        </defs>
        ${g.edges.map((e) => {
          const m = `url(#${touches(e) ? "arch-a2" : "arch-a"})`;
          return html`<path class=${`arch-e ${touches(e) ? "on" : ""}`} d=${e.d}
            marker-end=${e.rev ? null : m} marker-start=${e.rev ? m : null} />`;
        })}
        ${g.nodes.map((n) => html`<g class=${`arch-n k-${n.kind} ${sel === n.id ? "sel" : ""} ${near(n) ? "" : "dim"}`}
            transform=${`translate(${n.x},${n.y})`} onClick=${() => setSel(sel === n.id ? null : n.id)}
            onMouseEnter=${() => setHover(n.id)} onMouseLeave=${() => setHover(null)}>
          <title>${[n.label, n.note].filter(Boolean).join(": ")}</title>
          <rect width=${g.nw} height=${g.nh} rx="5" />
          ${n.kind === "store" || n.kind === "config" ? html`<line x1="0" x2=${g.nw} y1="5" y2="5" />` : null}
          <text class="arch-l" x="9" y="17">${fit(n.label, g.nw - 16, FONT_L)}</text>
          <text class="arch-p" x="9" y="31">${fit(pathOf(n), g.nw - 16, FONT_P)}</text>
        </g>`)}
        ${labels.map((l) => html`<text class="arch-el" x=${l.x} y=${l.y + 3.5} text-anchor="middle">${l.e.label}</text>`)}
      </svg>
    </div>
    ${selNode ? html`<div class="arch-detail">
      <div class="ad-1"><b>${selNode.label}</b><span class="faint">${selNode.kind}</span>
        ${(selNode.paths || []).map((p) => html`<span class="mono">${p}</span>`)}</div>
      ${selNode.note ? html`<div>${selNode.note}</div>` : null}
      <div class="ad-links">${g.edges.filter((e) => e.from === sel || e.to === sel).map((e) => html`<span>
        ${e.from === sel ? html`<span class="faint">to</span> <b>${byLabel(g, e.to)}</b>` : html`<span class="faint">from</span> <b>${byLabel(g, e.from)}</b>`}${e.label ? html`<span class="muted">: ${e.label}</span>` : null}</span>`)}</div>
    </div>` : null}
  <//>`;
}
const byLabel = (g, id) => g.nodes.find((n) => n.id === id)?.label || id;

// --- flow --------------------------------------------------------------------------------

function Flow({ b, ctx }) {
  const [open, toggle] = useToggles();
  return html`<${Section} b=${b} label="How it flows">
    ${b.summary ? html`<p class="blk-sum">${b.summary}</p>` : null}
    <ol class="blk-flow">
      ${(b.steps || []).map((s, i) => html`<li class=${open.has(i) ? "open" : ""}>
        <div class="fl-row" onClick=${() => toggle(i)}>
          <span class="fl-n num">${i + 1}</span>
          <div class="fl-b"><div class="fl-l">${s.label}</div>${s.note ? html`<div class="fl-note">${s.note}</div>` : null}</div>
          <${Ptr} pointer=${s.pointer} open=${open.has(i)} onToggle=${() => toggle(i)} />
        </div>
        ${open.has(i) ? html`<div class="fl-code"><${PointerCode} ctx=${ctx} pointer=${s.pointer} /></div>` : null}
      </li>`)}
    </ol>
  <//>`;
}

// --- subsystems ---------------------------------------------------------------------------

function Subsystems({ b, ctx }) {
  const [open, toggle] = useToggles();
  return html`<${Section} b=${b} label="Subsystems">
    <div class="blk-subs">
      ${(b.items || []).map((s, i) => html`<div class="sub-r">
        <div class="sub-name"><b>${s.name}</b><span class="mono faint" title=${(s.paths || []).join("\n")}>${(s.paths || []).map(baseName).join(", ")}</span></div>
        <div><${Health} h=${s.health} /></div>
        <div class="sub-what">${s.what}
          <span class="sub-ev">${(s.evidence || []).map((p) => html`<${Ptr} pointer=${p} open=${open.has(`${i}|${p}`)} onToggle=${() => toggle(`${i}|${p}`)} />`)}</span>
        </div>
        ${(s.evidence || []).filter((p) => open.has(`${i}|${p}`)).map((p) => html`<div class="sub-code"><${PointerCode} ctx=${ctx} pointer=${p} context=${2} /></div>`)}
      </div>`)}
    </div>
  <//>`;
}

// --- small blocks -------------------------------------------------------------------------

function CodeBlock({ b, ctx }) {
  const [open, setOpen] = useState(true);
  return html`<${Section} b=${b} label="Code" right=${html`<${Ptr} pointer=${b.pointer} open=${open} onToggle=${() => setOpen(!open)} />`}>
    ${b.caption ? html`<p class="blk-sum">${b.caption}</p>` : null}
    ${open ? html`<${PointerCode} ctx=${ctx} pointer=${b.pointer} />` : null}
  <//>`;
}

function Facts({ b }) {
  return html`<${Section} b=${b} label="Facts">
    <dl class="facts blk-dl">${(b.items || []).map((f) => html`<dt>${f.label}</dt><dd>${f.value}</dd>`)}</dl>
  <//>`;
}

function Callout({ b }) {
  return html`<section class=${`blk blk-callout ${b.tone === "warn" ? "warn" : "info"}`}>
    ${b.title ? html`<div class="blk-h"><span class="label blk-t">${b.title}</span></div>` : null}
    <p>${b.text}</p>
  </section>`;
}

function Questions({ b, ctx }) {
  const key = `johto.dq.${ctx.target}.${(b.items || []).length}`;
  const [done, setDone] = useState(() => { try { return new Set(JSON.parse(localStorage.getItem(key) || "[]")); } catch { return new Set(); } });
  const flip = (i) => {
    const n = new Set(done); n.has(i) ? n.delete(i) : n.add(i); setDone(n);
    try { localStorage.setItem(key, JSON.stringify([...n])); } catch {}
  };
  return html`<${Section} b=${b} label="Check by hand">
    <ul class="blk-q">${(b.items || []).map((q, i) => html`<li class=${done.has(i) ? "done" : ""} onClick=${() => flip(i)}>
      <span class="box" role="checkbox" aria-checked=${done.has(i)}>${done.has(i) ? "✓" : ""}</span><span>${q}</span></li>`)}</ul>
  <//>`;
}

const CSP = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:">`;
function HtmlBlock({ b }) {
  const box = useRef(null);
  const [w, setW] = useState(560);
  useEffect(() => {
    if (!box.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.floor(e.contentRect.width)));
    ro.observe(box.current);
    return () => ro.disconnect();
  }, []);
  const vb = /viewBox\s*=\s*["']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(b.html || "");
  const h = vb ? Math.ceil((w * +vb[2]) / +vb[1]) + 4 : 220;
  const doc = `<!doctype html><html><head><meta charset="utf-8">${CSP}<style>html,body{margin:0;background:#fff}</style></head><body>${b.html || ""}</body></html>`;
  return html`<${Section} b=${b} label="Diagram">
    <div ref=${box} class="blk-frame"><iframe sandbox="" srcdoc=${doc} title=${b.caption || b.title || "Diagram"}
      style=${`width:100%;height:${Math.min(h, 900)}px`} loading="lazy"></iframe></div>
    ${b.caption ? html`<p class="blk-cap">${b.caption}</p>` : null}
  <//>`;
}

const RENDER = { overview: Overview, architecture: Architecture, flow: Flow, subsystems: Subsystems, code: CodeBlock,
                 facts: Facts, callout: Callout, questions: Questions, html: HtmlBlock };

/** Any block except an opportunity. Unknown types render nothing. */
export function Block({ b, ctx }) {
  const C = RENDER[b?.type];
  return C ? html`<${C} b=${b} ctx=${ctx} />` : null;
}

// --- opportunities -------------------------------------------------------------------

/** One-line fit: "Modify · Backend · Personal" and its live open count. */
export function Fit({ f, open }) {
  return html`<span class="blk-fit" title=${[f.action, f.domain, f.codebase_type].join(" · ") + (f.why ? `: ${f.why}` : "")}>
    <span>${shortSlot(f)}</span><b class="num">${open ?? "–"}</b></span>`;
}

/** One opportunity as a single compact row: number, title and area, one-shot, size, best fit. */
export function OppRow({ o, n, openFor, selected, onClick, mark }) {
  const fit = (o.slot_fit || [])[0];
  return html`<div class=${`opp-row ${selected ? "sel" : ""}`} onClick=${onClick} data-opp=${o.id}>
    <span class="opp-n num">${n}</span>
    <span class="opp-tt"><span class="opp-title">${o.title}</span>${mark || null}<span class="opp-area">${o.area}</span></span>
    <span class="opp-os"><${OneShot} o=${o.one_shot} /></span>
    <span class="opp-sz"><${Size} s=${o.bounded_task?.size} /></span>
    ${fit ? html`<${Fit} f=${fit} open=${openFor?.(fit)} />` : html`<span></span>`}
  </div>`;
}

/** The selected opportunity: every field, evidence pointers open to the real code in place. */
export function OppDetail({ o, ctx, openFor, fits = true }) {
  const [open, toggle] = useToggles();
  const bt = o.bounded_task || {};
  const up = o.upstream || {};
  const sc = o.self_contained || {};
  return html`<div class="opp-detail">
    <div class="opp-dh"><b>${o.title}</b><span class="faint">${o.area}</span></div>
    <dl class="opp-dl">
      <dt>Ambition</dt><dd>${o.ambition}</dd>
      <dt>Bounded task</dt><dd>${bt.exists === false ? html`<span class="muted">None found inside this area</span>` : html`
        <div class="opp-shape"><${Size} s=${bt.size} long=${true} /><span>${bt.shape}</span></div>`}</dd>
      ${bt.exists !== false && bt.check_in_session ? html`<dt>Check</dt><dd>${bt.check_in_session}</dd>` : null}
      <dt>One-shot</dt><dd class="opp-os1"><${OneShot} o=${o.one_shot} /> <span class="faint">estimate</span>${o.one_shot?.why ? html`<span class="sep">·</span>${o.one_shot.why}` : null}</dd>
      ${(o.where_models_fail || []).length ? html`<dt>Where models fail</dt><dd><ul class="opp-ul">${o.where_models_fail.map((t) => html`<li>${t}</li>`)}</ul></dd>` : null}
      ${(o.evidence || []).length ? html`<dt>Evidence</dt><dd><div class="opp-ev">
        ${o.evidence.map((e, i) => html`<div class=${`ev-r ${open.has(i) ? "open" : ""}`}>
          <div class="ev-line" onClick=${() => toggle(i)}><${Ptr} pointer=${e.pointer} open=${open.has(i)} onToggle=${() => toggle(i)} /><span>${e.fact}</span></div>
          ${open.has(i) ? html`<${PointerCode} ctx=${ctx} pointer=${e.pointer} context=${2} />` : null}
        </div>`)}
      </div></dd>` : null}
      ${o.why_not_done ? html`<dt>Why not done</dt><dd>${o.why_not_done}</dd>` : null}
      ${(up.related || []).length || up.looks_solved ? html`<dt>Upstream</dt><dd class="opp-up">
        ${up.looks_solved ? html`<span class="chip red">looks solved upstream</span>` : null}
        ${(up.related || []).map((r) => html`<a href=${r.url} target="_blank" rel="noreferrer">
          <span class="num">${/\/(\d+)$/.exec(r.url)?.[1] ? `#${/\/(\d+)$/.exec(r.url)[1]}` : ""}</span> ${r.title}
          <span class=${`chip ${r.state === "open" ? "ghost" : ""}`}>${r.state}</span></a>`)}
      </dd>` : null}
      ${sc.ok === false || (sc.needs || []).length ? html`<dt>Needs</dt><dd class="opp-needs">
        ${sc.ok === false ? html`<span class="chip red">not self-contained</span>` : null}
        ${(sc.needs || []).map((x) => html`<span class="chip ghost">${x}</span>`)}</dd>` : null}
      ${fits && (o.slot_fit || []).length ? html`<dt>Fits</dt><dd class="opp-fits">${o.slot_fit.map((f) => html`<${Fit} f=${f} open=${openFor?.(f)} />`)}</dd>` : null}
    </dl>
  </div>`;
}
