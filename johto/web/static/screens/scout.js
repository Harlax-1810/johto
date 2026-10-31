import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { api } from "../lib/api.js";
import { app, loadLeads, leads as leadsSig, navigate, select, toast, tick, palette, overlay,
         jobs as jobsSig, jobFor, jobLine, waitJob, putJob, isLive } from "../lib/store.js";
import { pushContext } from "../lib/keys.js";
import { Kbd, Chip } from "../components/ui.js";
import { hhmm, plural, clock } from "../lib/format.js";
import { setPrimaryHandler } from "../app.js";

// Matrix order: rows are task type × codebase type, columns are domains (biggest first in the onboarding doc).
const ROW_ORDER = [
  ["Create", "Personal-scale"], ["Modify", "Personal-scale"], ["Operate", "Personal-scale"], ["Understand / Assess", "Personal-scale"],
  ["Create", "Enterprise-grade"], ["Modify", "Enterprise-grade"], ["Operate", "Enterprise-grade"], ["Understand / Assess", "Enterprise-grade"],
];
const DOMAIN_ORDER = ["Backend / API", "Web frontend", "DevOps / cloud", "Dev tooling", "Data / DB", "Games", "Other software", "AI agents"];
const TINT_MIN = 25;                // tinted: this many open slots and no leads
const SCOUT_REPOS = 3;
const USD_PER_REPO = 0.77;          // measured: one agent lead run per repo
const MIN_PER_REPO = 1.5;

const LANG = { typescript: "TypeScript", javascript: "JavaScript", csharp: "C#", cpp: "C++", php: "PHP" };
const langName = (l) => (l ? LANG[l.toLowerCase()] || l : "");
const shortAction = (a) => (a === "Understand / Assess" ? "Understand" : a);
const shortCt = (c) => (c || "").replace("-scale", "").replace("-grade", "");
const key3 = (a, d, c) => `${a}|${d}|${c}`;
const money = (n) => `$${(n || 0).toFixed(2)}`;
const about = (n) => `$${(Math.round(n * 10) / 10).toFixed(2)}`;
const STYLE = { "Casual prose": "Casual", "Explicit spec": "Spec", "Error / artifact brief": "Error brief" };
const MODE = { "Interactive/pairing": "interactive", "Async/autonomous": "async" };

/** Build {rows, domains, cells: key -> {open, leads, variants}} from /api/slots (groups or combos) plus lead counts. */
function matrix(slots, leadsData) {
  const cells = new Map();
  const cell = (a, d, c) => {
    const k = key3(a, d, c);
    if (!cells.has(k)) cells.set(k, { action: a, domain: d, codebase_type: c, open: 0, leads: 0, variants: [] });
    return cells.get(k);
  };
  if (slots?.combos) {
    for (const c of slots.combos) {
      const x = cell(c.action, c.domain, c.codebase_type);
      x.open += c.open || 0; x.leads += c.leads || 0;
      x.variants.push(...(c.variants || []));
    }
  } else {
    for (const g of slots?.groups || []) {
      const s = g.slot;
      const x = cell(s.action, s.domain, s.codebase_type);
      x.open += g.count;
      x.variants.push({ prompt_style: s.prompt_style, interaction_mode: s.interaction_mode, count: g.count });
    }
    for (const c of leadsData?.combos || []) {
      if (cells.has(key3(c.action, c.domain, c.codebase_type))) cell(c.action, c.domain, c.codebase_type).leads = c.leads || 0;
    }
  }
  const seenRows = new Set([...cells.values()].map((c) => `${c.action}|${c.codebase_type}`));
  const rows = ROW_ORDER.filter(([a, c]) => seenRows.has(`${a}|${c}`));
  const doms = new Set([...cells.values()].map((c) => c.domain));
  const domains = [...DOMAIN_ORDER.filter((d) => doms.has(d)), ...[...doms].filter((d) => !DOMAIN_ORDER.includes(d)).sort()];
  return { rows, domains, cells };
}

const isTinted = (c) => c && c.open >= TINT_MIN && !c.leads;

function pickDefault(m) {
  let best = null;
  for (const c of m.cells.values()) if (isTinted(c) && (!best || c.open > best.open)) best = c;
  return best ? key3(best.action, best.domain, best.codebase_type) : null;
}

/* ---------------------------------------------------------------- matrix */

function Matrix({ m, sel, setSel, synced, onSync, syncing, children }) {
  const total = [...m.cells.values()].reduce((n, c) => n + c.open, 0);
  const colTotal = (d) => m.rows.reduce((n, [a, c]) => n + (m.cells.get(key3(a, d, c))?.open || 0), 0);
  const leadsTotal = [...m.cells.values()].reduce((n, c) => n + c.leads, 0);
  const style = `grid-template-columns: 196px repeat(${m.domains.length}, minmax(0, 1fr)) 88px`;
  return html`<section class="pg-panel sc-matrix">
    <header class="pg-ph">
      <span class="label">Open slots</span>
      <span class="pg-sub num"><b>${total.toLocaleString()}</b> open · <b>${leadsTotal}</b> ${leadsTotal === 1 ? "lead" : "leads"}</span>
      <span class="pg-legend"><i class="sc-sw"></i>${TINT_MIN}+ open, no leads</span>
      <span class="pg-right">
        <span class="pg-keys faint sm"><${Kbd} k="←↑↓→" /> move</span>
        ${syncing ? html`<span class="faint sm" title=${jobLine(syncing)}>syncing…</span>`
          : synced ? html`<span class="faint sm">synced ${hhmm(synced)}</span>` : null}
        <button class="btn sm quiet" onClick=${onSync} disabled=${!!syncing} title="Pull open slots from Studio (reads only)">Sync</button>
      </span>
    </header>
    <div class="sc-grid" role="grid" aria-label="Open slots by task type and domain" style=${style}>
      <div class="sc-h sc-corner"><span>open</span><span class="sc-lk">leads</span></div>
      ${m.domains.map((d) => html`<div class="sc-h" title=${d}>${d}</div>`)}
      <div class="sc-h sc-all">all</div>
      ${m.rows.map(([a, ct]) => {
        const rowOpen = m.domains.reduce((n, d) => n + (m.cells.get(key3(a, d, ct))?.open || 0), 0);
        return html`
          <div class="sc-rl" title=${`${a} · ${ct}`}><b>${shortAction(a)}</b><span class="sep">·</span>${shortCt(ct)}</div>
          ${m.domains.map((d) => {
            const k = key3(a, d, ct);
            const c = m.cells.get(k);
            const open = c?.open || 0;
            const cls = ["sc-c", isTinted(c) ? "tint" : "", !open ? "zero" : "", sel === k ? "sel" : ""].join(" ");
            return html`<button class=${cls} role="gridcell" aria-selected=${sel === k} disabled=${!open}
                title=${open ? `${a} · ${d} · ${ct}: ${open} open, ${plural(c.leads, "lead")}` : ""}
                onClick=${() => setSel(k)}>
              <span class="n num">${open || "·"}</span>${c?.leads ? html`<span class="sc-lk num">${c.leads}</span>` : null}
            </button>`;
          })}
          <div class="sc-c sc-all num">${rowOpen}</div>`;
      })}
      <div class="sc-rl sc-foot">all</div>
      ${m.domains.map((d) => html`<div class="sc-c sc-foot num">${colTotal(d)}</div>`)}
      <div class="sc-c sc-all sc-foot num">${total}</div>
    </div>
    ${children}
  </section>`;
}

/**
 * The selected cell: its variants, its leads, and the scout action with its cost first.
 * Leads already in the cell are the cheaper next step, so picking from them is the primary then.
 * ⌘⇧↵ is bound only once Austin has chosen the cell himself (chosen), never on the default highlight.
 */
function Selected({ c, chosen, spend, onScout, onPick, scouting, overCap }) {
  if (!c) return null;
  const est = overCap?.estimate_usd ?? SCOUT_REPOS * USD_PER_REPO;
  const variants = [...(c.variants || [])].sort((a, b) => b.count - a.count);
  const pickFirst = c.leads > 0;
  return html`<div class="sc-sel">
    <div class="sc-sel-t">
      <b>${c.action} · ${c.domain} · ${c.codebase_type}</b>
      <span class="num">${c.open} open</span>
    </div>
    <div class="sc-vars">${variants.map((v) => html`<span class="sc-var" title=${`${v.prompt_style} · ${v.interaction_mode}`}>
      ${STYLE[v.prompt_style] || v.prompt_style} · ${MODE[v.interaction_mode] || v.interaction_mode} <b class="num">${v.count}</b></span>`)}</div>
    <div class="sc-sel-act">
      ${scouting ? html`<span class="sc-scouting faint" title=${(scouting.progress?.log_tail || []).join("\n")}>
          Scouting · ${jobLine(scouting)}${scouting.started_at ? html` · <span class="num">${clock(scouting.started_at)}</span>` : null}</span>`
        : html`<button class=${`btn ${pickFirst ? "" : "primary"} ${overCap ? "armed" : ""}`} onClick=${onScout}
          title=${overCap ? `Today's cap has ${money(overCap.remaining_usd)} left` : `About ${about(est)} and ${Math.round(SCOUT_REPOS * MIN_PER_REPO)} min (estimate)`}>
        ${chosen && !pickFirst ? html`<${Kbd} k="⌘⇧↵" />` : null}${overCap
          ? `Over today's cap: press again to spend about ${about(est)}`
          : `Scout ${SCOUT_REPOS} repos · about ${about(est)}`}</button>`}
      ${pickFirst ? html`<button class="btn primary" onClick=${onPick}>
        ${chosen ? html`<${Kbd} k="⌘⇧↵" />` : null}Pick from ${plural(c.leads, "lead")}</button>` : null}
    </div>
  </div>`;
}

/* ---------------------------------------------------------------- repos */

const CHECK_WORD = {
  size: "size", ai_generated: "AI-written", verification: "no tests", application: "library", upstream: "fork",
  reachable: "commit", studio_overuse: "overused", english: "not English", self_contained: "paid APIs",
};

function evidence(check, r, ct) {
  const n = r.counts?.source_files;
  switch (check) {
    case "size":
      if (ct === "Enterprise-grade") return `${n} source files; Enterprise-grade needs 200 or more`;
      if (n > 1500) return `${n.toLocaleString()} source files; Personal-scale allows up to 1,500`;
      if (n > 500) return `${n} source files; over 500 is large for Personal-scale`;
      return `${n} source files; Personal-scale needs 20 or more`;
    case "ai_generated": return `AI authorship signals in the history: ${r.ai_level}`;
    case "verification": return "no test files and no test command found";
    case "application": return "reads like a library, not an application";
    case "upstream": return "a fork, or part of SWE-bench";
    case "reachable": return "the pinned commit could not be read";
    case "studio_overuse": return `already used in ${plural(r.studio_usage, "Studio task")}`;
    case "english": return "README is not in English";
    case "self_contained": return "README mentions paid APIs";
    default: return check;
  }
}

function Verdict({ v, onClick, open }) {
  if (!v) return html`<span class="faint">·</span>`;
  if (v.overall === "ok") return html`<${Chip} tone="green">✓ fits<//>`;
  const list = v.overall === "reject" ? v.fails : v.borderline;
  const tone = v.overall === "reject" ? "red" : "amber";
  const mark = v.overall === "reject" ? "✗" : "~";
  return html`<button class=${`sc-vbtn ${open ? "on" : ""}`} onClick=${onClick} aria-expanded=${open}
      title="Show the evidence"><${Chip} tone=${tone}>${mark} ${list.map((c) => CHECK_WORD[c] || c).join(", ")}<//></button>`;
}

function group(r) {
  const v = Object.values(r.verdicts || {}).map((x) => x.overall);
  return v.includes("ok") ? "fit" : v.includes("maybe") ? "maybe" : "reject";
}

function RepoRow({ r, open, setOpen, showUsage, showAi }) {
  const P = r.verdicts?.["Personal-scale"], E = r.verdicts?.["Enterprise-grade"];
  const toggle = (ct) => setOpen(open === `${r.full_name}|${ct}` ? null : `${r.full_name}|${ct}`);
  const openCt = open?.startsWith(`${r.full_name}|`) ? open.split("|")[1] : null;
  const v = openCt ? r.verdicts?.[openCt] : null;
  return html`<div class="sc-repo">
    <div class="sc-rrow">
      <span class="sc-name">
        <a href=${r.pinned_url || `https://github.com/${r.full_name}`} target="_blank" title="Open at the pinned commit">${r.full_name}</a>
        ${r.description ? html`<span class="d">${r.description}</span>` : null}
      </span>
      <span class="muted">${langName(r.language)}</span>
      <span class="num r">${r.counts?.source_files ?? "?"} <span class="faint">/</span> ${r.counts?.test_files ?? "?"}</span>
      ${showAi ? html`<span class=${`sc-ai ${r.ai_level || ""}`}>${r.ai_level || "?"}</span>` : null}
      <span><${Verdict} v=${P} open=${openCt === "Personal-scale"} onClick=${() => toggle("Personal-scale")} /></span>
      <span><${Verdict} v=${E} open=${openCt === "Enterprise-grade"} onClick=${() => toggle("Enterprise-grade")} /></span>
      ${showUsage ? html`<span class="num r">${r.studio_usage || html`<span class="faint">·</span>`}</span>` : null}
      <span class="num r">${r.leads ? html`${r.claimed ? html`<span class="faint">${r.claimed} claimed · </span>` : null}<b>${r.leads}</b>` : html`<span class="faint">·</span>`}</span>
    </div>
    ${v ? html`<ul class="sc-ev">
      ${[...(v.fails || []).map((c) => [c, "fail"]), ...(v.borderline || []).map((c) => [c, "warn"])].map(([c, lv]) => html`
        <li class=${lv}><span class="ic">${lv === "fail" ? "✗" : "~"}</span><b>${CHECK_WORD[c] || c}</b>${evidence(c, r, openCt)}</li>`)}
    </ul>` : null}
  </div>`;
}

function Repos({ repos }) {
  const [open, setOpen] = useState(null);
  const [show, setShow] = useState("fit");    // which group the table lists; maybes and rejects stay out of the way
  const g = { fit: [], maybe: [], reject: [] };
  for (const r of repos) g[group(r)].push(r);
  g.fit.sort((a, b) => b.leads - a.leads || (b.counts?.test_files || 0) - (a.counts?.test_files || 0));
  const reasons = {};
  for (const r of g.reject) for (const v of Object.values(r.verdicts || {})) for (const f of v.fails || []) reasons[f] = (reasons[f] || 0) + 1;
  const topReason = Object.entries(reasons).sort((a, b) => b[1] - a[1])[0];
  const allFor = (k) => topReason && g.reject.every((r) => Object.values(r.verdicts || {}).every((v) => (v.fails || []).includes(k)));
  const tabs = [
    ["fit", `${g.fit.length} fit`],
    ["maybe", `${g.maybe.length} maybe`],
    ["reject", `${g.reject.length} rejected${allFor(topReason?.[0]) ? ` (all ${CHECK_WORD[topReason[0]] || topReason[0]})` : ""}`],
  ].filter(([k]) => g[k].length);
  const cur = g[show].length ? show : tabs[0]?.[0];
  // Columns with nothing in this tab are left out.
  const showAi = (g[cur] || []).some((r) => r.ai_level && r.ai_level !== "none");
  const showUsage = (g[cur] || []).some((r) => r.studio_usage);
  const cols = `minmax(280px, 1fr) 96px 112px ${showAi ? "72px " : ""}176px 176px ${showUsage ? "76px " : ""}120px`;
  return html`<section class="pg-panel sc-repos" style=${`--sc-cols: ${cols}`}>
    <header class="pg-ph">
      <span class="label">Vetted repos</span>
      <span class="tabs num">${tabs.map(([k, l]) => html`<button class=${`tab ${cur === k ? "on" : ""}`} aria-pressed=${cur === k}
        onClick=${() => { setShow(k); setOpen(null); }}>${l}</button>`)}</span>
    </header>
    <div class="sc-thead">
      <span>Repo</span><span>Language</span><span class="r">Source / tests</span>${showAi ? html`<span title="Signs of AI-written code in the git history">AI</span>` : null}
      <span title="20 to 1,500 source files">Personal-scale</span><span title="200 or more source files">Enterprise-grade</span>
      ${showUsage ? html`<span class="r" title="Studio tasks already using this repo">In Studio</span>` : null}<span class="r">Leads</span>
    </div>
    <div class="sc-tbody">
      ${(g[cur] || []).map((r) => html`<${RepoRow} key=${r.full_name} r=${r} open=${open} setOpen=${setOpen} showUsage=${showUsage} showAi=${showAi} />`)}
    </div>
  </section>`;
}

/* ---------------------------------------------------------------- jobs */

const JOB_WORD = { scout: "Lead scout", vet_repo: "Vet repo", check: "Difficulty check", slots_sync: "Slot sync", run_audit: "Run audit" };

const RESEARCH = new Set(Object.keys(JOB_WORD));

/** "action|domain|codebase_type" for a scout job, from its params, result or target, or null. */
function comboOf(j) {
  const c = j?.params?.combo || j?.result?.combo || (j?.target && typeof j.target === "object" ? j.target : null);
  if (c?.action) return key3(c.action, c.domain, c.codebase_type);
  const parts = typeof j?.target === "string" ? j.target.replace(/^combo:/, "").split("|") : [];
  return parts.length === 3 ? parts.join("|") : null;
}

function Jobs({ spend, settings }) {
  tick.value;
  // Every job the store has heard of (state snapshot plus job events), newest first.
  const jobs = [...jobsSig.value.values()].filter((j) => RESEARCH.has(j.kind))
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const live = jobs.filter(isLive);
  const recent = jobs.filter((j) => !isLive(j)).slice(0, 3);
  const bg = settings?.background_research ?? true;
  const setBg = async () => {
    try { const r = await api.send("PUT", "/api/settings", { background_research: !bg }); if (r?.toast) toast(r.toast.text, r.toast); }
    catch (e) { toast(e.message, { level: "error" }); }
  };
  const cancel = async (j) => {
    try {
      const r = await api.send("DELETE", `/api/jobs/${j.id}`);
      if (r?.job) putJob(r.job);
      if (r?.toast) toast(r.toast.text, r.toast);
    } catch (e) { toast(e.message, { level: "error" }); }
  };
  const target = (j) => {
    const c = comboOf(j);
    if (c) { const [a, d, ct] = c.split("|"); return `${shortAction(a)} · ${d} · ${shortCt(ct)}`; }
    return typeof j.target === "string" ? j.target.replace(/^[a-z]:/, "") : [j.target?.repo].filter(Boolean).join(" · ");
  };
  return html`<section class="pg-panel sc-jobs">
    <span class="label">AI research</span>
    <div class="sc-jl">
      ${[...live, ...recent].map((j) => html`<span class=${`sc-job ${j.state}`}>
        <b>${JOB_WORD[j.kind] || j.kind}</b>${target(j) ? html` · ${target(j)}` : null}
        <span class="faint">${j.state === "running" ? `running ${clock(j.started_at)}` : j.state}${j.cost_usd != null ? ` · ${money(j.cost_usd)}` : ""}</span>
        ${j.cancellable && live.includes(j) ? html`<button class="iconbtn" title="Cancel" onClick=${() => cancel(j)}>×</button>` : null}
      </span>`)}
    </div>
    <span class="pg-right sc-spend num">
      <span>Today <b>${money(spend?.today_usd)}</b> of ${money(spend?.cap_usd ?? 10).replace(".00", "")}</span>
      <button class="btn sm quiet sc-bg" onClick=${setBg} aria-pressed=${bg}
          title="Research open slots in the background, within the daily cap">
        Background research <${Chip} tone=${bg ? "green" : "ghost"}>${bg ? "on" : "off"}<//></button>
    </span>
  </section>`;
}

/* ---------------------------------------------------------------- page */

export default function Scout() {
  const [slots, setSlots] = useState(null);
  const [repos, setRepos] = useState(null);
  const [err, setErr] = useState(null);
  const [sel, setSel] = useState(null);
  const [overCap, setOverCap] = useState(null);   // {key, estimate_usd, remaining_usd}: the second press spends past the cap
  const loadSlots = () => api.get("/api/slots").then(setSlots).catch((e) => setErr(e.message));
  const loadRepos = () => api.get("/api/repos").then((r) => setRepos(r.repos || [])).catch((e) => setErr(e.message));
  useEffect(() => {
    loadSlots(); loadRepos();
    loadLeads().catch(() => {});
    api.get("/api/jobs?limit=50").then((r) => (r?.jobs || []).forEach(putJob)).catch(() => {});
  }, []);
  // A finished sync or scout changes the counts: read them again (the store already has the job).
  const synced = jobFor("slots_sync");
  const scouted = jobFor("scout");
  const doneKey = [synced, scouted].filter((j) => j && !isLive(j)).map((j) => `${j.id}:${j.state}`).join(",");
  useEffect(() => { if (doneKey) { loadSlots(); loadRepos(); loadLeads(true).catch(() => {}); } }, [doneKey]);

  const m = slots ? matrix(slots, leadsSig.value) : null;
  const selKey = sel || (m ? pickDefault(m) : null);
  const cur = m && selKey ? m.cells.get(selKey) : null;
  jobsSig.value;
  const syncing = isLive(synced) ? synced : null;
  const scoutJob = [...jobsSig.value.values()].filter((j) => j.kind === "scout" && isLive(j))
    .find((j) => !cur || comboOf(j) === key3(cur.action, cur.domain, cur.codebase_type) || !comboOf(j)) || null;
  useEffect(() => setOverCap(null), [selKey]);

  const pick = () => { navigate("/"); select("pick"); };
  const scout = async () => {
    if (!cur || scoutJob) return;
    const key = key3(cur.action, cur.domain, cur.codebase_type);
    const second = overCap?.key === key;
    setOverCap(null);
    try {
      const r = await api.send("POST", "/api/scout", {
        combo: { action: cur.action, domain: cur.domain, codebase_type: cur.codebase_type }, repos: SCOUT_REPOS, agent: true,
        ...(second ? { confirm: true } : {}) });
      if (r?.toast) toast(r.toast.text, r.toast);
      if (r?.job) {
        putJob(r.job);
        toast(`Scouting ${shortAction(cur.action)} · ${cur.domain} · ${shortCt(cur.codebase_type)}`);
        waitJob(r.job).then((j) => {
          if (j?.state === "succeeded") toast(`Scout finished${j.result?.leads != null ? `: ${plural(j.result.leads, "new lead")}` : ""}`, { level: "ok" });
          else if (j && !isLive(j) && j.state !== "cancelled") toast(j.error?.message || "The scout stopped", { level: "error" });
        });
      }
    } catch (e) {
      if (e.code === "budget") { setOverCap({ key, ...e.details }); toast(e.message, { level: "warn" }); }
      else toast(e.message, { level: "error" });
    }
  };
  const sync = async () => {
    if (syncing) return;
    try {
      const r = await api.send("POST", "/api/slots/sync");
      if (r?.toast) toast(r.toast.text, r.toast);
      if (r?.job) {
        putJob(r.job);
        waitJob(r.job).then((j) => {
          if (j?.state === "succeeded") toast("Open slots synced from Studio", { level: "ok" });
          else if (j && !isLive(j)) toast(j.error?.message || "The sync stopped", { level: "error" });
        });
      } else if (!r?.toast) loadSlots();
    } catch (e) { toast(e.message, { level: "error" }); }
  };
  const move = (dr, dc) => {
    if (!m || !cur) return;
    let r = m.rows.findIndex(([a, c]) => a === cur.action && c === cur.codebase_type);
    let c = m.domains.indexOf(cur.domain);
    for (let i = 0; i < 12; i++) {
      r = Math.min(m.rows.length - 1, Math.max(0, r + dr)); c = Math.min(m.domains.length - 1, Math.max(0, c + dc));
      const k = key3(m.rows[r][0], m.domains[c], m.rows[r][1]);
      if (m.cells.get(k)?.open) { setSel(k); return; }
      if ((dr && (r === 0 || r === m.rows.length - 1)) || (dc && (c === 0 || c === m.domains.length - 1))) return;
    }
  };
  const chosen = !!sel && !!cur;
  const primary = useRef(null);
  primary.current = chosen ? (cur.leads ? pick : scout) : null;
  useEffect(() => {
    setPrimaryHandler(chosen ? () => primary.current?.() : null);
    return () => setPrimaryHandler(null);
  }, [chosen]);
  useEffect(() => pushContext("scout", {
    ArrowLeft: () => move(0, -1), ArrowRight: () => move(0, 1), ArrowUp: () => move(-1, 0), ArrowDown: () => move(1, 0),
    Escape: () => { palette.value = false; overlay.value = false; setOverCap(null); },
  }), [selKey, !!m]);

  if (err) return html`<div class="pg"><div class="pg-err">${err}</div></div>`;
  const st = app.value || {};
  return html`<div class="pg sc">
    ${m ? html`<${Matrix} m=${m} sel=${selKey} setSel=${setSel} synced=${slots.synced_at} onSync=${sync} syncing=${syncing}>
      <${Selected} c=${cur} chosen=${chosen} spend=${st.spend} onScout=${scout} onPick=${pick} scouting=${scoutJob}
        overCap=${overCap && cur && overCap.key === key3(cur.action, cur.domain, cur.codebase_type) ? overCap : null} />
    <//>` : null}
    ${repos?.length ? html`<${Repos} repos=${repos} />` : null}
    ${m ? html`<${Jobs} spend=${st.spend} settings=${st.settings} />` : null}
  </div>`;
}
