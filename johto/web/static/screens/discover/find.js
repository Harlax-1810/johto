// Find work (/repos, spec.md 3.2): which repo is worth a look? One row per researched repo, filtered by each slot
// field on its own (prompt style, mode, codebase, task, domain). Counts follow the filters.
import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { navigate, hash } from "../../lib/store.js";
import { pushContext } from "../../lib/keys.js";
import { slotWords, styleWord, modeWord, scaleWord, actionWord, domainWord } from "../../lib/words.js";
import { Stage, Lede, Btn, Menu, SlotPill, Dot, AiTag, Empty, Sheet } from "../../components/primitives.js";
import { useListing, facets, setFacets, anyFacet, matches, groupCounts, facetOptions, howOnly, keyOf, slotOfKey,
         DECLINE, decline, restore, refill, setPaused, setModel, RESEARCH_MODELS } from "./data.js";
import { yoursWords, openWords } from "./words.js";
import { SlotMapSheet } from "./slotmap.js";

/** "Not for me" with its reasons (existing decline). */
export function NotForMe({ id, name, onDone, kind = "secondary", size = "md" }) {
  return html`<${Menu} label="Not for me" title="Set this repo aside" kind=${kind} size=${size}
    items=${[{ note: "Why set it aside?" }, ...DECLINE.map(([k, l]) => ({ label: l, run: async () => { if (await decline(id, name, k)) onDone?.(); } }))]} />`;
}

/** The Research menu: research more, pause or resume, who writes the dossiers, and the repos set aside. */
export function ResearchMenu({ research, setAside, onSetAside }) {
  const r = research || {};
  const model = r.model || "claude";
  return html`<${Menu} label="Research ▾" title="Repo research" kind="secondary" items=${[
    r.today_line ? { note: r.today_line } : null,
    { label: "Research more repos", run: refill },
    r.paused ? { label: "Resume research", run: () => setPaused(false) }
      : { label: r.generating ? `Stop researching ${r.generating} and pause` : "Pause research", run: () => setPaused(true) },
    "-",
    { note: `Dossiers are written by ${r.model_word || "Claude Code"}. Scouting stays with Claude Code.` },
    ...RESEARCH_MODELS.filter(([k]) => k !== model).map(([k, word]) => ({ label: `Write dossiers with ${word}`, run: () => setModel(k),
      title: k === "codex" ? "The Codex CLI runs GPT inside the clone, read-only; from the next repo on" : "Headless Claude Code reads the clone; from the next repo on" })),
    "-",
    { label: `Repos you set aside${setAside?.length ? ` (${setAside.length})` : ""}`, run: onSetAside, disabled: !setAside?.length,
      why: "Nothing is set aside" },
  ]} />`;
}

function SetAsideSheet({ open, onClose, rows }) {
  return html`<${Sheet} open=${open} onClose=${onClose} title="Repos you set aside" width=${520} label="Repos you set aside">
    ${(rows || []).length ? html`<div class="fw-aside">
      ${rows.map((r) => html`<div class="fw-aside-r">
        <div><b>${r.name}</b> <span class="faint">${r.owner}</span><div class="fw-aside-why">${r.why}</div></div>
        <${Btn} size="sm" label=${r.status === "failed" ? "Research again" : "Bring it back"} onClick=${() => restore(r.id, r.name)} />
      </div>`)}
    </div>` : html`<${Empty} title="Nothing is set aside." />`}
  <//>`;
}

// The filters, in the order he thinks about a task: how it is written, how it runs, the codebase, the kind of task.
const FACET_ROWS = [
  { field: "prompt_style", label: "Prompt", word: styleWord, order: ["Casual prose", "Explicit spec", "Error / artifact brief"] },
  { field: "interaction_mode", label: "Mode", word: modeWord, order: ["Interactive/pairing", "Async/autonomous"] },
  { field: "codebase_type", label: "Codebase", word: scaleWord, order: ["Personal-scale", "Enterprise-grade"] },
  { field: "action", label: "Task", word: actionWord, order: ["Create", "Modify", "Operate", "Understand / Assess"] },
];

function Facet({ spec, cells, f }) {
  const cur = f[spec.field];
  const opts = facetOptions(cells, f, spec.field, spec.order);
  const pick = (v) => setFacets({ [spec.field]: v });
  return html`<div class="fw-facet">
    <span class="fw-facet-l">${spec.label}</span>
    <div class="seg sm" role="radiogroup" aria-label=${spec.label}>
      <button type="button" role="radio" aria-checked=${!cur} class=${cur ? "" : "on"} onClick=${() => pick("")}>Any</button>
      ${opts.map((o) => html`<button type="button" role="radio" aria-checked=${cur === o.value} class=${cur === o.value ? "on" : ""}
          disabled=${!o.open && cur !== o.value} title=${`${o.open} open slots in Studio with the other filters`}
          onClick=${() => pick(cur === o.value ? "" : o.value)}>${spec.word(o.value)}<span class="fw-facet-n">${o.open}</span></button>`)}
    </div>
  </div>`;
}

/**
 * A row's suggestions recounted under the filters: each seed's largest matching group, in the brief's own order
 * (Claude ranked them; a big group is no better). Rows with no seeds show the groups their research fits.
 */
function recount(r, counts) {
  const best = (keys) => {
    let k = null, n = 0;
    for (const x of keys || []) { const c = counts.get(x) || 0; if (c > n) { n = c; k = x; } }
    return k ? { ...slotOfKey(k), open: n } : null;
  };
  const seeds = (r.seeds || []).map((x) => { const b = best(x.keys); return b ? { ...x, ...b } : null; }).filter(Boolean);
  const fromKeys = (keys) => [...new Set(keys || [])].map((k) => ({ ...slotOfKey(k), open: counts.get(k) || 0 }))
    .filter((x) => x.open).sort((a, b) => b.open - a.open);
  let fits = seeds.length ? [] : fromKeys(r.fit_keys);
  if (!seeds.length && !fits.length) fits = fromKeys(r.fits_any);
  const all = seeds.length ? seeds : fits;
  return { seeds: seeds.slice(0, 2), fits: fits.slice(0, 2), fit: all.length > 0, matching: all.length,
           scarce: all.some((x) => openWords(x.open).scarce) };
}

function SeedLine({ x, f }) {
  const o = openWords(x.open);
  // The group's fields the filters already fix are left out.
  const where = [!f.action && actionWord(x.action), !f.domain && domainWord(x.domain), !f.codebase_type && scaleWord(x.codebase_type)]
    .filter(Boolean).join(" · ");
  return html`<div class="fw-seed" title=${`${slotWords(x)}: ${o.text}`}>
    <span class="fw-seed-t">${x.title}</span>
    <span class="fw-seed-m">${where ? `${where} · ` : ""}<span class=${o.scarce ? "fw-left" : ""}>${o.text}</span></span>
  </div>`;
}

function Row({ r, v, f, sel, onOpen, onSel }) {
  const yours = yoursWords(r.yours);
  return html`<div class=${`fw-row ${sel ? "sel" : ""} ${r.state}`} data-id=${r.id} role="link" tabindex="0"
      aria-label=${`Open ${r.name}`} onClick=${onOpen} onFocus=${onSel}
      onKeyDown=${(e) => { if (e.key === "Enter" && e.target === e.currentTarget) { e.preventDefault(); onOpen(); } }}>
    <div class="fw-c1">
      <div class="fw-name"><span class="fw-n">${r.name}</span><span class="fw-owner">${r.owner}</span></div>
      ${r.state === "researching" ? null : html`<p class="fw-one">${r.one_liner}</p>`}
      ${yours ? html`<span class="fw-yours">${yours}</span>` : null}
    </div>
    <div class="fw-c2">
      ${r.state === "ready" ? html`
        ${r.list_line ? html`<p class="fw-why">${r.list_line}</p>` : null}
        ${(r.tags || []).length ? html`<div class="fw-tags">${r.tags.map((t) => html`<span class="fw-tag">${t}</span>`)}</div>` : null}`
      : r.state === "researching" ? html`<span class="fw-wait"><${Dot} tone="work" />Researching</span>`
      : html`${(r.problems || []).length ? html`<ul class="fw-probs">${r.problems.map((t) => html`<li>${t}</li>`)}</ul>` : null}
        <span class="fw-wait">${r.generating ? html`<${Dot} tone="work" />` : null}Brief coming</span>`}
    </div>
    <div class="fw-c3">
      ${v.seeds.length ? v.seeds.map((x) => html`<${SeedLine} x=${x} f=${f} />`)
        : v.fits.map((x) => html`<${SlotPill} slot=${x} count=${x.open} size="sm" />`)}
    </div>
    ${r.state !== "researching" ? html`<div class="fw-hov" onClick=${(e) => e.stopPropagation()}>
      <${NotForMe} id=${r.id} name=${r.name} kind="quiet" size="sm" /></div>` : null}
  </div>`;
}

/** "15 open slots in Studio match. Repos that fit the same group share its slots." */
function matchWords(cells, f, scarce) {
  const n = (cells || []).reduce((t, c) => t + (matches(c, f) ? c.open : 0), 0);
  const head = n === 1 ? "1 open slot in Studio matches." : `${n} open slots in Studio match.`;
  return scarce ? `${head} Repos that fit the same group share its slots, so claiming one takes it from the others.` : head;
}

const STATE_RANK = { ready: 0, brief_coming: 1, researching: 2 };
const VERDICT_RANK = { dive: 0, maybe: 1, skip: 3 };

export default function FindWork() {
  const d = useListing();
  const [selId, setSelId] = useState(null);
  const [aside, setAside] = useState(false);
  const listRef = useRef(null);
  const f = facets.value;
  const filtered = anyFacet(f);
  const cells = d?.slots || [];
  const counts = groupCounts(cells, f);
  const views = new Map((d?.repos || []).map((r) => [r.id, recount(r, counts)]));
  // Order: ready first, then Claude's verdict, then a suggestion with only a few slots left (take it before
  // someone else does), then how many suggestions fit, then the name. The size of a slot group never ranks.
  const rows = (d?.repos || []).filter((r) => !filtered || views.get(r.id).fit).sort((a, b) => {
    const va = views.get(a.id), vb = views.get(b.id);
    return (STATE_RANK[a.state] ?? 3) - (STATE_RANK[b.state] ?? 3) || (VERDICT_RANK[a.verdict] ?? 2) - (VERDICT_RANK[b.verdict] ?? 2)
      || (vb.scarce - va.scarce) || (vb.matching - va.matching) || a.name.localeCompare(b.name);
  });
  const sel = rows.find((r) => r.id === selId) || null;
  const slotsOpen = hash.value.has("slots");
  const how = howOnly(f);
  const mapCount = (c) => groupCounts(cells, how).get(keyOf(c)) || 0;

  const st = useRef({});
  st.current = { rows, sel };
  const open = (r) => r && navigate(`/repos/${r.id}`);
  const move = (k) => {
    const { rows: rs, sel: s } = st.current;
    if (!rs.length) return;
    const i = s ? rs.indexOf(s) : -1;
    const n = rs[Math.min(rs.length - 1, Math.max(0, i + k))];
    setSelId(n.id);
    listRef.current?.querySelector(`[data-id="${n.id}"]`)?.scrollIntoView({ block: "nearest" });
  };
  useEffect(() => pushContext("findwork", { j: () => move(1), k: () => move(-1), Enter: () => open(st.current.sel) }), []);

  const openSlots = () => navigate("/repos", "#slots");
  const closeSlots = () => { history.replaceState({}, "", "/repos"); hash.value = new URLSearchParams(""); };

  return html`<${Stage} class="fw">
    <${Lede} title=${d?.lede?.title || "Find work"} support=${d?.lede?.support}
      right=${html`<${Btn} kind="secondary" label="Every problem" title="Every researched problem, filtered your way" onClick=${() => navigate("/research")} />
        <${Btn} kind="secondary" label="All open slots" onClick=${openSlots} />
        <${ResearchMenu} research=${d?.research} setAside=${d?.set_aside} onSetAside=${() => setAside(true)} />`} />
    ${d ? html`<div class="fw-filters" role="group" aria-label="Show repos with open slots for">
      ${FACET_ROWS.map((spec) => html`<${Facet} spec=${spec} cells=${cells} f=${f} />`)}
      ${f.domain ? html`<button type="button" class="fw-dom" title="Show every domain" onClick=${() => setFacets({ domain: "" })}>
        ${domainWord(f.domain)} <span aria-hidden="true">×</span></button>` : null}
      <span class="fw-ai"><${AiTag} label="Claude's research" /></span>
    </div>` : null}
    ${d && filtered ? html`<p class="fw-note">${matchWords(cells, f, rows.some((r) => views.get(r.id).scarce))}</p>` : null}
    ${d?.error ? html`<${Empty} title="Find work could not load." body=${d.error} />` : null}
    ${d && !d.error && !rows.length ? html`<${Empty}
        title=${filtered ? "No researched repo has an open slot with these filters." : "Nothing is researched yet."}
        body=${filtered ? null : "Research finds and reads repos that fit the open slots."}
        action=${filtered ? html`<${Btn} label="Show every repo" onClick=${() => setFacets(null)} />` : html`<${Btn} kind="primary" label="Research more repos" onClick=${refill} />`} />` : null}
    <div class="fw-list" ref=${listRef}>
      ${rows.map((r) => html`<${Row} key=${r.id} r=${r} v=${views.get(r.id)} f=${f} sel=${sel?.id === r.id} onOpen=${() => open(r)} onSel=${() => setSelId(r.id)} />`)}
    </div>
    <${SlotMapSheet} open=${slotsOpen} onClose=${closeSlots} count=${cells.length ? mapCount : null} how=${how}
      scale=${f.codebase_type} onPick=${(k) => { const g = slotOfKey(k); if (g) setFacets(g); closeSlots(); }} />
    <${SetAsideSheet} open=${aside} onClose=${() => setAside(false)} rows=${d?.set_aside} />
  <//>`;
}
