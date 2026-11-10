// Research (/research): every problem Claude's research found, across every researched repo, as one table he can
// filter by how likely Claude is to get it in one go, the size of the bounded job, the slot group it fits, the repo's
// verdict, its language, and whether a slot is open now. A row opens the repo's brief on that problem, where
// "Claim in Studio" and "Start without a slot" live. The research is Claude's, so the table is labelled as such.
import { useEffect, useRef, useState } from "preact/hooks";
import { signal } from "@preact/signals";
import { html } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { navigate } from "../../lib/store.js";
import { pushContext } from "../../lib/keys.js";
import { actionWord, domainWord, scaleWord, styleWord, modeWord, sizeWords, slotWords,
         ONE_SHOT_WORDS, VERDICT_WORDS, VERDICT_TONE } from "../../lib/words.js";
import { Stage, Lede, Btn, Pill, Empty } from "../../components/primitives.js";
import { groupCounts } from "./data.js";
import { openWords } from "./words.js";

// ---------- data ----------

export const research = signal(null);   // GET /api/research

async function load() {
  try { research.value = await api.get("/api/research"); }
  catch (e) { if (!research.value) research.value = { error: e.message, problems: [], slots: [], lede: { title: "Research" } }; }
  return research.value;
}

function useResearch() {
  useEffect(() => {
    let t, alive = true;
    const run = async () => { await load(); if (alive) t = setTimeout(run, 30000); };
    run();
    return () => { alive = false; clearTimeout(t); };
  }, []);
  return research.value;
}

// ---------- filters ----------

// Each filter is one choice ("" for any). Slot fields match the problem's own fits; prompt style and mode only
// change which open slots count. Kept across visits, like Find work's.
const KEYS = ["action", "domain", "codebase_type", "prompt_style", "interaction_mode", "one_shot", "size", "verdict",
              "language", "slot", "yours", "researcher", "q"];
const NONE = Object.fromEntries(KEYS.map((k) => [k, ""]));
function readFilters() {
  try {
    const f = JSON.parse(localStorage.getItem("johto.rs.filters") || "null");
    return f ? Object.fromEntries(KEYS.map((k) => [k, typeof f[k] === "string" ? f[k] : ""])) : { ...NONE };
  } catch { return { ...NONE }; }
}
const filters = signal(readFilters());
function setFilters(patch) {
  filters.value = patch ? { ...filters.value, ...patch } : { ...NONE };
  try { localStorage.setItem("johto.rs.filters", JSON.stringify(filters.value)); } catch {}
}
const anyFilter = (f) => KEYS.some((k) => f[k]);

/** Open slots this problem could take under the slot filters: its largest matching group. */
function openOf(r, counts) {
  let n = 0;
  for (const k of r.fit_keys || []) n = Math.max(n, counts.get(k) || 0);
  return n;
}

/** Does a problem pass the filters (`skip` leaves one out, for that filter's own counts)? */
function passes(r, f, counts, skip) {
  const on = (k) => k !== skip && f[k];
  if (on("action") && !(r.actions || []).includes(f.action)) return false;
  if (on("domain") && !(r.domains || []).includes(f.domain)) return false;
  if (on("codebase_type") && !(r.scales || []).includes(f.codebase_type)) return false;
  if (on("one_shot") && r.one_shot !== f.one_shot) return false;
  if (on("size") && r.size !== f.size) return false;
  if (on("verdict") && (r.verdict || "none") !== f.verdict) return false;
  if (on("language") && !(r.languages || []).includes(f.language)) return false;
  if (on("slot")) { const n = openOf(r, counts); if (f.slot === "open" ? !n : n) return false; }
  if (on("yours") && r.yours) return false;
  if (on("researcher") && (r.researcher || "Claude") !== f.researcher) return false;
  if (on("q")) {
    const q = f.q.trim().toLowerCase();
    if (q && ![r.title, r.problem, r.trap, r.name, r.repo, r.area].some((t) => (t || "").toLowerCase().includes(q))) return false;
  }
  return true;
}

/** One filter's choices with how many problems each would leave, under the other filters. */
function options(rows, f, counts, field, values, valueOf) {
  const n = new Map();
  for (const r of rows) {
    if (!passes(r, f, counts, field)) continue;
    for (const v of valueOf(r)) if (v) n.set(v, (n.get(v) || 0) + 1);
  }
  const seen = new Set(rows.flatMap((r) => valueOf(r)).filter(Boolean));
  const vals = values ? [...values.filter((v) => seen.has(v)), ...[...seen].filter((v) => !values.includes(v)).sort()] : [...seen].sort();
  return vals.map((v) => ({ value: v, n: n.get(v) || 0 }));
}

function Facet({ label, field, opts, word, f, title }) {
  const cur = f[field];
  return html`<div class="rs-facet" title=${title || ""}>
    <span class="rs-facet-l">${label}</span>
    <div class="seg sm" role="radiogroup" aria-label=${label}>
      <button type="button" role="radio" aria-checked=${!cur} class=${cur ? "" : "on"} onClick=${() => setFilters({ [field]: "" })}>Any</button>
      ${opts.map((o) => html`<button type="button" role="radio" aria-checked=${cur === o.value} class=${cur === o.value ? "on" : ""}
          disabled=${!o.n && cur !== o.value} title=${`${o.n} problem${o.n === 1 ? "" : "s"} with the other filters`}
          onClick=${() => setFilters({ [field]: cur === o.value ? "" : o.value })}>${word(o.value)}<span class="rs-n">${o.n}</span></button>`)}
    </div>
  </div>`;
}

/** Languages are many, so they are a list, not a row of buttons. */
function LanguageFacet({ f, opts }) {
  const cur = f.language;
  if (!opts.length && !cur) return null;
  return html`<div class="rs-facet">
    <span class="rs-facet-l">Language</span>
    <select class=${`rs-select ${cur ? "on" : ""}`} aria-label="Language" value=${cur}
        onChange=${(e) => setFilters({ language: e.currentTarget.value })}>
      <option value="">Any</option>
      ${opts.map((o) => html`<option value=${o.value} disabled=${!o.n && cur !== o.value}>${o.value} (${o.n})</option>`)}
    </select>
  </div>`;
}

/** Prompt style and mode, counted in open slots: they decide which slots count, not which problems show. */
function HowFacet({ label, field, cells, f, word, order }) {
  const cur = f[field];
  const n = new Map();
  for (const c of cells || []) {
    const ok = ["action", "domain", "codebase_type", "prompt_style", "interaction_mode"].every((k) => k === field || !f[k] || c[k] === f[k]);
    if (ok && c[field]) n.set(c[field], (n.get(c[field]) || 0) + c.open);
  }
  const vals = [...order.filter((v) => n.has(v)), ...[...n.keys()].filter((v) => !order.includes(v)).sort()];
  if (!vals.length && !cur) return null;
  return html`<div class="rs-facet" title="Which open slots count">
    <span class="rs-facet-l">${label}</span>
    <div class="seg sm" role="radiogroup" aria-label=${label}>
      <button type="button" role="radio" aria-checked=${!cur} class=${cur ? "" : "on"} onClick=${() => setFilters({ [field]: "" })}>Any</button>
      ${vals.map((v) => html`<button type="button" role="radio" aria-checked=${cur === v} class=${cur === v ? "on" : ""}
          title=${`${n.get(v) || 0} open slots in Studio`} onClick=${() => setFilters({ [field]: cur === v ? "" : v })}>${word(v)}<span class="rs-n">${n.get(v) || 0}</span></button>`)}
    </div>
  </div>`;
}

// ---------- the table ----------

const ONE_SHOT_RANK = { unlikely: 0, possible: 1, likely: 2, not_checked: 3 };
const SIZE_RANK = { L: 0, M: 1, S: 2 };
const VERDICT_RANK = { dive: 0, maybe: 1, none: 2, skip: 3 };
const SIZE_ORDER = ["L", "M", "S"];
const ONE_SHOT_ORDER = ["unlikely", "possible", "likely"];
const ACTION_ORDER = ["Create", "Modify", "Operate", "Understand / Assess"];
const DOMAIN_ORDER = ["Backend / API", "Data / DB", "Web frontend", "DevOps / cloud", "Dev tooling", "AI agents", "Other software"];
const STYLE_ORDER = ["Casual prose", "Explicit spec", "Error / artifact brief"];
const MODE_ORDER = ["Interactive/pairing", "Async/autonomous"];

const COLUMNS = [
  { key: "title", label: "Problem", cls: "rs-c-main" },
  { key: "fits", label: "Fits", cls: "rs-c-fits", sortless: true },
  { key: "one_shot", label: "Claude in one go", cls: "rs-c-one" },
  { key: "size", label: "Size", cls: "rs-c-size" },
  { key: "open", label: "Open slots", cls: "rs-c-open" },
  { key: "verdict", label: "Repo", cls: "rs-c-repo" },
  { key: "yours", label: "Yours", cls: "rs-c-yours" },
];

function compare(key, a, b, openA, openB) {
  switch (key) {
    case "title": return a.title.localeCompare(b.title);
    case "one_shot": return (ONE_SHOT_RANK[a.one_shot] ?? 3) - (ONE_SHOT_RANK[b.one_shot] ?? 3);
    case "size": return (SIZE_RANK[a.size] ?? 3) - (SIZE_RANK[b.size] ?? 3);
    case "open": return openB - openA;
    case "verdict": return (VERDICT_RANK[a.verdict || "none"] ?? 2) - (VERDICT_RANK[b.verdict || "none"] ?? 2) || a.name.localeCompare(b.name);
    case "yours": return (b.yours ? 1 : 0) - (a.yours ? 1 : 0);
    default: return 0;
  }
}

const verdictWord = (v) => (v === "none" ? "Brief coming" : VERDICT_WORDS[v] || v);
const oneGoWord = (v) => ONE_SHOT_WORDS[v] || "not checked";
const slotWord = (v) => (v === "open" ? "Open now" : "None open");

function Row({ r, open, sel, onOpen, onSel, f }) {
  const o = openWords(open);
  const fits = (r.fits || []).slice(0, 2);
  return html`<div class=${`rs-row ${sel ? "sel" : ""}`} data-id=${r.id} role="link" tabindex="0" aria-label=${`Open ${r.title} in ${r.name}`}
      onClick=${onOpen} onFocus=${onSel}
      onKeyDown=${(e) => { if (e.key === "Enter" && e.target === e.currentTarget) { e.preventDefault(); onOpen(); } }}>
    <div class="rs-c-main">
      <div class="rs-t">${r.title}</div>
      <div class="rs-repo">${r.name} <span class="faint">${r.owner}</span>${r.seed ? "" : " · also researched"}</div>
      ${r.problem ? html`<p class="rs-p">${r.problem}</p>` : null}
      ${r.trap ? html`<p class="rs-trap">${r.trap}</p>` : null}
    </div>
    <div class="rs-c-fits rs-fits">
      ${fits.map((x) => html`<span class="rs-fit" title=${slotWords(x)}>${[!f.action && actionWord(x.action), !f.domain && domainWord(x.domain), !f.codebase_type && scaleWord(x.codebase_type)].filter(Boolean).join(" · ") || slotWords(x)}</span>`)}
      ${(r.fits || []).length > 2 ? html`<span class="rs-more">and ${r.fits.length - 2} more</span>` : null}
    </div>
    <div class=${`rs-c-one rs-cell ${r.one_shot === "unlikely" ? "hard" : ""}`}>${oneGoWord(r.one_shot)}</div>
    <div class="rs-c-size rs-cell">${sizeWords(r.size) || ""}</div>
    <div class=${`rs-c-open rs-cell rs-open ${open ? (o.scarce ? "left" : "") : "none"}`}>${open ? o.text : "none"}</div>
    <div class="rs-c-repo rs-cell">
      ${r.verdict ? html`<${Pill} tone=${VERDICT_TONE[r.verdict] || "neutral"}>${VERDICT_WORDS[r.verdict]}<//>` : html`<span class="faint">Brief coming</span>`}
      ${(r.languages || []).length ? html`<div class="rs-langs">${r.languages.join(", ")}</div>` : null}
    </div>
    <div class="rs-c-yours rs-cell rs-yours">${r.yours ? r.yours.stage_word : ""}</div>
  </div>`;
}

function summaryWords(shown, all, counts, repos) {
  const hard = shown.filter((r) => r.one_shot === "unlikely").length;
  const withslot = shown.filter((r) => openOf(r, counts)).length;
  const parts = [shown.length === all.length ? `${all.length} problems` : `${shown.length} of ${all.length} problems match`];
  if (shown.length) {
    parts.push(`${hard} unlikely in one go`);
    parts.push(withslot ? `${withslot} with an open slot now` : "none with an open slot now");
    parts.push(`${repos} repo${repos === 1 ? "" : "s"}`);
  }
  return parts;
}

export default function Research() {
  const d = useResearch();
  const f = filters.value;
  const [sort, setSort] = useState({ key: "", dir: 1 });
  const [selId, setSelId] = useState(null);
  const listRef = useRef(null);
  const all = d?.problems || [];
  const cells = d?.slots || [];
  // Open slots per group under the slot filters (prompt style and mode narrow the count, never the problems).
  const counts = groupCounts(cells, { action: f.action, domain: f.domain, codebase_type: f.codebase_type,
                                      prompt_style: f.prompt_style, interaction_mode: f.interaction_mode });
  const opens = new Map(all.map((r) => [r.id, openOf(r, counts)]));
  let rows = all.filter((r) => passes(r, f, counts));
  if (sort.key) rows = [...rows].sort((a, b) => sort.dir * compare(sort.key, a, b, opens.get(a.id), opens.get(b.id)));
  const repos = new Set(rows.map((r) => r.dossier_id)).size;
  const sel = rows.find((r) => r.id === selId) || null;
  const researchers = [...new Set(all.map((r) => r.researcher || "Claude"))];
  const tag = researchers.length > 1 ? "Claude's and GPT's research" : researchers[0] === "GPT" ? "GPT's research" : "Claude's research";

  const st = useRef({});
  st.current = { rows, sel };
  const open = (r) => r && navigate(`/repos/${r.dossier_id}`, `#seed=${r.opportunity_id}`);
  const move = (k) => {
    const { rows: rs, sel: s } = st.current;
    if (!rs.length) return;
    const i = s ? rs.indexOf(s) : -1;
    const n = rs[Math.min(rs.length - 1, Math.max(0, i + k))];
    setSelId(n.id);
    listRef.current?.querySelector(`[data-id="${n.id}"]`)?.scrollIntoView({ block: "nearest" });
  };
  useEffect(() => pushContext("research", { j: () => move(1), k: () => move(-1), Enter: () => open(st.current.sel) }), []);

  const head = (c) => {
    if (c.sortless) return html`<span class=${c.cls}>${c.label}</span>`;
    const on = sort.key === c.key;
    return html`<button type="button" class=${`${c.cls} ${on ? "on" : ""}`} aria-sort=${on ? (sort.dir > 0 ? "ascending" : "descending") : "none"}
      title=${`Sort by ${c.label.toLowerCase()}`}
      onClick=${() => setSort(on ? (sort.dir > 0 ? { key: c.key, dir: -1 } : { key: "", dir: 1 }) : { key: c.key, dir: 1 })}>
      ${c.label}${on ? html`<span aria-hidden="true"> ${sort.dir > 0 ? "↓" : "↑"}</span>` : null}</button>`;
  };

  return html`<${Stage} class="rs">
    <${Lede} title=${d?.lede?.title || "Research"} support=${d?.lede?.support} tag=${tag}
      right=${html`<${Btn} kind="secondary" label="Find work" title="Repos, one row each" onClick=${() => navigate("/repos")} />
        <${Btn} kind="secondary" label="All open slots" onClick=${() => navigate("/repos", "#slots")} />`} />
    ${d && !d.error ? html`<div class="rs-filters" role="group" aria-label="Show problems that are">
      <${Facet} label="Claude in one go" field="one_shot" f=${f} word=${oneGoWord}
        opts=${options(all, f, counts, "one_shot", ONE_SHOT_ORDER, (r) => [r.one_shot])} title="How likely Claude is to get it right in one pass" />
      <${Facet} label="Size" field="size" f=${f} word=${(v) => sizeWords(v) || v}
        opts=${options(all, f, counts, "size", SIZE_ORDER, (r) => [r.size])} title="How big the bounded job is" />
      <${Facet} label="Task" field="action" f=${f} word=${actionWord}
        opts=${options(all, f, counts, "action", ACTION_ORDER, (r) => r.actions || [])} />
      <${Facet} label="Domain" field="domain" f=${f} word=${domainWord}
        opts=${options(all, f, counts, "domain", DOMAIN_ORDER, (r) => r.domains || [])} />
      <${Facet} label="Codebase" field="codebase_type" f=${f} word=${scaleWord}
        opts=${options(all, f, counts, "codebase_type", ["Personal-scale", "Enterprise-grade"], (r) => r.scales || [])} />
      <${Facet} label="Repo" field="verdict" f=${f} word=${verdictWord}
        opts=${options(all, f, counts, "verdict", ["dive", "maybe", "none", "skip"], (r) => [r.verdict || "none"])} title="Claude's verdict on the repo" />
      <${LanguageFacet} f=${f} opts=${options(all, f, counts, "language", null, (r) => r.languages || [])} />
      ${researchers.length > 1 ? html`<${Facet} label="Researched by" field="researcher" f=${f} word=${(v) => v}
        opts=${options(all, f, counts, "researcher", ["Claude", "GPT"], (r) => [r.researcher || "Claude"])} title="Who wrote the repo's dossier" />` : null}
      <${Facet} label="Slot" field="slot" f=${f} word=${slotWord}
        opts=${options(all, f, counts, "slot", ["open", "none"], (r) => [openOf(r, counts) ? "open" : "none"])}
        title="Whether a slot with the problem's seeds is open in Studio right now. Prompt and Mode narrow which slots count." />
      <${HowFacet} label="Prompt" field="prompt_style" cells=${cells} f=${f} word=${styleWord} order=${STYLE_ORDER} />
      <${HowFacet} label="Mode" field="interaction_mode" cells=${cells} f=${f} word=${modeWord} order=${MODE_ORDER} />
      <button type="button" class=${`rs-toggle ${f.yours ? "on" : ""}`} aria-pressed=${!!f.yours} title="Leave out problems you already have a task for"
        onClick=${() => setFilters({ yours: f.yours ? "" : "hide" })}>Hide ones already yours</button>
      <input class="rs-search" type="search" placeholder="Search problems" aria-label="Search problems" value=${f.q}
        onInput=${(e) => setFilters({ q: e.currentTarget.value })} />
      ${anyFilter(f) ? html`<button type="button" class="linkbtn rs-clear" onClick=${() => setFilters(null)}>Clear filters</button>` : null}
    </div>
    <div class="rs-sum">${summaryWords(rows, all, counts, repos).map((t) => html`<span>${t}</span>`)}
      ${d.synced ? html`<span class="faint">${d.synced}</span>` : null}</div>` : null}
    ${d?.error ? html`<${Empty} title="Research could not load." body=${d.error} />` : null}
    ${d && !d.error && !all.length ? html`<${Empty} title="Nothing is researched yet." body="Research finds and reads repos that fit the open slots."
        action=${html`<${Btn} label="Find work" onClick=${() => navigate("/repos")} />`} />` : null}
    ${d && !d.error && all.length && !rows.length ? html`<${Empty} title="No researched problem matches these filters."
        action=${html`<${Btn} label="Show every problem" onClick=${() => setFilters(null)} />`} />` : null}
    ${rows.length ? html`<div class="rs-table" ref=${listRef}>
      <div class="rs-head">${COLUMNS.map(head)}</div>
      ${rows.map((r) => html`<${Row} key=${r.id} r=${r} f=${f} open=${opens.get(r.id) || 0} sel=${sel?.id === r.id}
        onOpen=${() => open(r)} onSel=${() => setSelId(r.id)} />`)}
    </div>` : null}
  <//>`;
}
