// Repos: the ready dossiers (/repos) and one dossier (/repos/<id>). Dossiers are AI research about a
// repository; the app never turns them into text for a protected field.
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { signal } from "@preact/signals";
import { html } from "../lib/html.js";
import { api } from "../lib/api.js";
import { app, navigate, toast, tick, hash, select, overlay, refresh, jobs, jobLine, waitJob, putJob, isLive } from "../lib/store.js";
import { pushContext } from "../lib/keys.js";
import { clock, ago, plural, shortSlot, hhmm } from "../lib/format.js";
import { Kbd, Dot, Collapsible } from "../components/ui.js";
import { Block, OppRow, OppDetail, OneShot, Size, Fit } from "../components/blocks.js";

// --- data -----------------------------------------------------------------------------

export const dossiers = signal(null);   // GET /api/dossiers body
export const slots = signal(null);      // GET /api/slots body
const docs = new Map();                 // id -> GET /api/dossiers/{id} body

let slotsSyncedAt = null;
export async function loadSlots() {
  const at = app.value?.slots?.synced_at || "";
  if (slots.value && at === slotsSyncedAt) return slots.value;
  slotsSyncedAt = at;
  try { slots.value = await api.get("/api/slots"); } catch { /* counts show as – */ }
  return slots.value;
}

export async function loadDossiers() {
  try { dossiers.value = await api.get("/api/dossiers"); } catch (e) { if (!dossiers.value) dossiers.value = { items: [], error: e.message }; }
  return dossiers.value;
}

export async function loadDossier(id, force = false) {
  if (!force && docs.has(id)) return docs.get(id);
  const d = await api.get(`/api/dossiers/${encodeURIComponent(id)}`);
  docs.set(id, d);
  return d;
}

/** Keep the dossier list fresh: faster while something is generating. */
export function useDossiers() {
  useEffect(() => {
    let t, alive = true;
    const run = async () => {
      const d = await loadDossiers();
      loadSlots();
      if (!alive) return;
      const busy = (d?.items || []).some((i) => i.status === "generating");
      t = setTimeout(run, busy ? 4000 : 20000);
    };
    run();
    return () => { alive = false; clearTimeout(t); };
  }, []);
  return dossiers.value;
}

export const byRank = (a, b) => (a.rank ?? 1e6) - (b.rank ?? 1e6);
export const readyOf = (d) => (d?.items || []).filter((i) => i.status === "ready").sort(byRank);

const same = (g, f) => g.slot.action === f.action && g.slot.domain === f.domain && g.slot.codebase_type === f.codebase_type;
/** Open Studio slots for an action · domain · codebase type combination, from data/slots.json. */
export function openFor(f) {
  const s = slots.value;
  if (!s || !f) return null;
  return s.groups.filter((g) => same(g, f)).reduce((n, g) => n + g.count, 0);
}

const STYLE = { "Casual prose": "Casual", "Explicit spec": "Spec", "Error / artifact brief": "Error brief" };
const MODE = { "Interactive/pairing": "Interactive", "Async/autonomous": "Async" };
function variantNote(s) {
  const a = s.prompt_style === "Explicit spec" ? "Long written spec" : s.prompt_style.startsWith("Error") ? "Starts from a real error" : "Short prompt";
  const b = s.interaction_mode.startsWith("Inter") ? "you reply during runs" : "runs alone";
  return `${a}, ${b}`;
}
/** Slot variants (prompt style · interaction mode) for the opportunity's fits, largest first. */
export function variantsFor(fits) {
  const s = slots.value;
  if (!s) return [];
  return (fits || []).flatMap((f) => s.groups.filter((g) => same(g, f) && g.count > 0)
    .sort((a, b) => b.count - a.count)
    .map((g) => ({ slot: g.slot, count: g.count, combo: shortSlot(f), task: (g.tasks || [])[0] || null,
                   label: `${STYLE[g.slot.prompt_style] || g.slot.prompt_style} · ${MODE[g.slot.interaction_mode] || g.slot.interaction_mode}`,
                   note: variantNote(g.slot) })));
}

export const repoShort = (r) => (r || "").split("/").pop();
const ownerOf = (r) => (r || "").split("/")[0];
const fitsOf = (it) => ((it.fits || []).length ? it.fits : (it.verdict?.best_fits || []).map((f) => ({ ...f, open: openFor(f) })));
const fitKey = (f) => (f ? `${shortSlot(f)}|${f.open ?? openFor(f)}` : "");

function elapsed(it) {
  tick.value;
  if (it.started_at) return clock(it.started_at);
  const s = it.progress?.elapsed_s;
  return s != null ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "";
}

/** One line saying what the worker is doing. Also the whole empty state. With `countsOnly` (a
 * researching row is on screen) it leaves the researching part to that row. */
export function WorkerLine({ d, compact, countsOnly }) {
  if (!d) return null;
  const items = d.items || [];
  const gen = items.find((i) => i.status === "generating");
  const ready = items.filter((i) => i.status === "ready").length;
  const queued = items.filter((i) => i.status === "queued").length;
  const w = d.worker || {};
  let dot, text;
  if (gen && countsOnly) { dot = null; text = d.paused ? html`<b>Research pauses after this one</b>` : null; }
  else if (gen) { dot = "work"; text = html`<b>Researching ${gen.repo}</b>${gen.progress?.step ? html`<span class="sep">·</span>${gen.progress.step}` : null}<span class="sep">·</span><span class="num">${elapsed(gen)}</span>`; }
  else if (d.paused) { dot = "idle"; text = html`<b>Research paused</b>${w.paused_reason && !/in the app/.test(w.paused_reason) ? html`<span class="sep">·</span>${w.paused_reason}` : null}`; }
  else if (w.backoff_until) { dot = "wait"; text = html`<b>Research waits until ${hhmm(w.backoff_until)}</b>${w.paused_reason ? html`<span class="sep">·</span>${w.paused_reason}` : null}`; }
  else if (w.running) { dot = "busy"; text = html`<b>Research worker idle</b>`; }
  else { dot = "idle"; text = html`<b>Research worker stopped</b>`; }
  if (gen && d.paused && !countsOnly) text = html`${text}<span class="sep">·</span><span>paused after this one</span>`;
  return html`<span class="rp-worker">
    ${dot ? html`<${Dot} kind=${dot} />` : null}${text}
    ${!compact ? html`${text ? html`<span class="sep">·</span>` : null}<span><span class="num">${ready}</span> of ${d.target || 10} ready</span>
      ${queued ? html`<span class="sep">·</span><span><span class="num">${queued}</span> queued</span>` : null}` : null}
  </span>`;
}

async function post(path, body, okText) {
  try {
    const r = await api.send("POST", path, body);
    if (r?.toast) { toast(r.toast.text, r.toast); return null; }
    if (okText) toast(okText);
    return r || {};
  } catch (e) { toast(e.message, { level: "error" }); return null; }
}

export async function setPaused(p) {
  try {
    const r = await api.send("PUT", "/api/dossiers/worker", { paused: p });
    if (r?.toast) toast(r.toast.text, r.toast); else toast(p ? "Research paused" : "Research resumed");
    loadDossiers();
  } catch (e) { toast(e.message, { level: "error" }); }
}

const REASON = { too_easy: "too easy", too_big: "too big", cant_run: "can't run it", not_interested: "not interested",
                 ai_generated: "AI-generated", other: "other" };

/** A research failure in plain words; the engine's message stays in the tooltip. */
function failWords(err) {
  const e = (err || "").toLowerCase();
  if (/schema|json|parse|invalid|repair/.test(e)) return "the research came back incomplete";
  if (/timed? ?out|timeout/.test(e)) return "the research ran out of time";
  if (/clone|checkout|git /.test(e)) return "the repo could not be cloned";
  if (/budget|cost|\$/.test(e)) return "the research hit its cost cap";
  if (/rate.?limit|429|overloaded|usage limit/.test(e)) return "Claude was busy or rate limited";
  return "the research stopped with an error";
}

/** "language · files · stack" from the queue reasons, without the repeated build note. */
function queueFacts(it) {
  return (it.promise_reasons || []).filter((r) => !/^Fits \d/.test(r))
    .map((r) => r.replace(/, which the lane can build$/, ""))
    .slice(0, 3);
}

// --- list ---------------------------------------------------------------------------------

/** Summary fields, filled from the full dossier when the list entry lacks them. */
function useSummary(it) {
  const [doc, setDoc] = useState(null);
  const missing = !it.headline && !(it.top || []).length;
  useEffect(() => { if (missing) loadDossier(it.id).then((r) => setDoc(r.dossier)).catch(() => {}); }, [it.id, missing]);
  if (!missing || !doc) return it;
  const ops = (doc.blocks || []).filter((b) => b.type === "opportunity");
  return { ...it, headline: doc.headline, verdict: doc.verdict,
           top: ops.slice(0, 3).map((o) => ({ id: o.id, title: o.title, area: o.area, one_shot: o.one_shot, slot_fit: o.slot_fit, size: o.bounded_task?.size })) };
}

function ReadyRow({ it: raw, n, sel }) {
  const it = useSummary(raw);
  const open = (o) => navigate(`/repos/${it.id}`, o ? `#o=${o}` : "");
  return html`<div class=${`rp-row ${sel ? "sel" : ""}`} data-id=${it.id} onClick=${() => open()}>
    <span class="rp-rank num">${n}</span>
    <div class="rp-repo">
      <div class="rp-name"><span class="faint">${ownerOf(it.repo)}/</span><b>${repoShort(it.repo)}</b>
        <span class="mono faint">@${(it.commit || "").slice(0, 8)}</span>
        ${it.verdict?.promise ? html`<span class=${`rp-prom ${it.verdict.promise}`} title=${it.verdict.why || ""}>${it.verdict.promise} promise</span>` : null}</div>
      <div class="rp-head ai-rail">${it.headline}</div>
    </div>
    <div class="rp-opps ai-rail">
      ${(it.top || []).map((o) => {
        const f = (o.slot_fit || [])[0];
        return html`<div class="rp-opp" onClick=${(e) => { e.stopPropagation(); open(o.id); }}>
          <span class="rp-ot"><span class="rp-ott">${o.title}</span><span class="rp-oa">${o.area}</span></span>
          <span><${OneShot} o=${o.one_shot} /></span><span><${Size} s=${o.size} /></span>
          ${f ? html`<${Fit} f=${f} open=${openFor(f)} />` : html`<span></span>`}
        </div>`;
      })}
    </div>
  </div>`;
}

function GeneratingRow({ it }) {
  const tail = (it.progress?.log_tail || []).slice(-1)[0];
  return html`<div class="rp-row gen">
    <span class="rp-rank"><${Dot} kind="work" /></span>
    <div class="rp-repo"><div class="rp-name"><span class="faint">${ownerOf(it.repo)}/</span><b>${repoShort(it.repo)}</b>
      ${it.commit ? html`<span class="mono faint">@${it.commit.slice(0, 8)}</span>` : null}</div>
      <div class="rp-meta">Researching<span class="sep">·</span><span class="num">${elapsed(it)}</span></div></div>
    <div class="rp-prog">
      <div>${it.progress?.step || "starting"}</div>
      ${tail ? html`<div class="mono faint rp-tail">${tail}</div>` : null}
    </div>
  </div>`;
}

function QueuedRow({ it, n, showFit }) {
  const f = fitsOf(it)[0];
  return html`<div class=${`rp-qrow ${showFit ? "" : "nofit"}`}>
    <span class="num">${n}</span>
    <span class="rp-qname">${it.repo}</span>
    <span class="rp-qwhy" title=${(it.promise_reasons || []).join("\n")}><span class="rp-qtext">${queueFacts(it).join(" · ")}</span></span>
    ${showFit ? html`<span class="rp-qfit">${f ? html`<${Fit} f=${f} open=${f.open ?? openFor(f)} />` : null}</span>` : null}
  </div>`;
}

function DoneRow({ it }) {
  let what;
  if (it.status === "declined") what = html`<span class="chip">declined · ${REASON[it.decline?.reason] || it.decline?.reason || ""}</span>${it.decline?.note ? html`<span class="rp-qtext">${it.decline.note}</span>` : null}`;
  else if (it.status === "actioned") what = html`<span class="chip green">started${it.actioned?.task_short ? ` · task ${it.actioned.task_short}` : ""}</span>`;
  else what = html`<span class="chip red">research failed</span><span class="rp-qtext" title=${it.error || ""}>${failWords(it.error)}</span>`;
  return html`<div class="rp-qrow done nofit" onClick=${() => navigate(`/repos/${it.id}`)}>
    <span></span><span class="rp-qname">${it.repo}</span><span class="rp-qwhy">${what}</span>
  </div>`;
}

function List() {
  const d = useDossiers();
  slots.value;
  const items = d?.items || [];
  const ready = readyOf(d);
  const gen = items.filter((i) => i.status === "generating");
  const queued = items.filter((i) => i.status === "queued").sort((a, b) => (b.manual ? 1 : 0) - (a.manual ? 1 : 0) || byRank(a, b));
  const done = items.filter((i) => ["declined", "actioned", "failed"].includes(i.status));
  const showFit = queued.length > 1 && new Set(queued.map((q) => fitKey(fitsOf(q)[0]))).size > 1;
  const [selId, setSelId] = useState(null);
  const sel = ready.find((r) => r.id === selId) || ready[0];
  const listRef = useRef(null);
  const st = useRef({});
  st.current = { ready, sel, d };
  const move = (k) => {
    const { ready: r, sel: s } = st.current;
    if (!r.length) return;
    const i = Math.max(0, r.indexOf(s));
    const n = r[Math.min(r.length - 1, Math.max(0, i + k))];
    setSelId(n.id);
    listRef.current?.querySelector(`[data-id="${n.id}"]`)?.scrollIntoView({ block: "nearest" });
  };
  const openSel = () => st.current.sel && navigate(`/repos/${st.current.sel.id}`);
  const togglePause = () => setPaused(!st.current.d?.paused);
  useEffect(() => pushContext("repos", { j: () => move(1), k: () => move(-1), Enter: openSel, p: togglePause }), []);
  return html`<section class="surface rp">
    <header class="shead">
      <h1>Repos</h1>
      <div class="actions">
        ${d ? html`<button class="btn quiet" onClick=${togglePause}><${Kbd} k="p" />${d.paused ? "Resume research" : "Pause research"}</button>` : null}
        ${ready.length ? html`<button class="btn primary" onClick=${openSel}><${Kbd} k="↵" />Open ${repoShort(sel?.repo)}</button>` : null}
      </div>
      <div class="sub"><${WorkerLine} d=${d} countsOnly=${gen.length > 0} /></div>
    </header>
    <div class="rp-scroll" ref=${listRef}>
      ${ready.length || gen.length ? html`<div class="rp-cols">
        <span></span><span class="label">Repo</span>
        <div class="rp-ocols">
          <span class="label"><span class="ai-tag">AI research</span><span class="sep">·</span>top opportunities</span>
          <span class="label">One-shot</span><span class="label">Size</span><span class="label">Best fit · open slots</span>
        </div>
      </div>` : null}
      ${ready.map((it, i) => html`<${ReadyRow} key=${it.id} it=${it} n=${i + 1} sel=${sel?.id === it.id} />`)}
      ${gen.map((it) => html`<${GeneratingRow} key=${it.id} it=${it} />`)}
      ${queued.length ? html`<div class=${`rp-grp ${showFit ? "" : "nofit"}`}><span></span>
          <span><span class="label">Queued</span><span class="faint sm rp-gsub">${plural(queued.length, "repo")} · researched one at a time, best first</span></span>
          <span></span>${showFit ? html`<span class="label">Best fit · open slots</span>` : null}</div>
        ${queued.map((it, i) => html`<${QueuedRow} key=${it.id} it=${it} n=${i + 1} showFit=${showFit} />`)}` : null}
      ${done.length ? html`<div class="rp-done"><${Collapsible} title="Set aside" count=${done.length}>
        ${done.map((it) => html`<${DoneRow} key=${it.id} it=${it} />`)}<//></div>` : null}
    </div>
  </section>`;
}

// --- one dossier ----------------------------------------------------------------------------

const DECLINE = [["too_easy", "Too easy"], ["too_big", "Too big"], ["cant_run", "Can't run it"],
                 ["not_interested", "Not interested"], ["ai_generated", "AI-generated"], ["other", "Other"]];

function DeclineBar({ repo, onPick, onCancel, other, setOther }) {
  const [note, setNote] = useState("");
  const input = useRef(null);
  useEffect(() => { if (other) input.current?.focus(); }, [other]);
  return html`<div class="rp-decline">
    <span class="rp-dl-t">Decline ${repo}</span>
    ${DECLINE.map(([k, l], i) => html`<button class=${`btn sm ${other && k === "other" ? "on" : ""}`}
        onClick=${() => (k === "other" ? setOther(true) : onPick(k))}><${Kbd} k=${String(i + 1)} />${l}</button>`)}
    ${other ? html`<input ref=${input} class="rp-note" value=${note} placeholder="Reason"
        onInput=${(e) => setNote(e.currentTarget.value)}
        onKeyDown=${(e) => { if (e.key === "Enter") onPick("other", note); if (e.key === "Escape") { e.preventDefault(); e.currentTarget.blur(); onCancel(); } }} />` : null}
    <button class="btn sm quiet" onClick=${onCancel}><${Kbd} k="Esc" />Cancel</button>
  </div>`;
}

/** The claim bar: slot variants with live counts, the claim key, and exactly what it writes. */
function ClaimBar({ variants, vi, setVi, onClaim, claiming, repo, commit, alt }) {
  const v = variants[vi];
  const task = alt || v?.task;
  const groups = [];
  variants.forEach((x, i) => {
    if (!groups.length || groups[groups.length - 1].combo !== x.combo) groups.push({ combo: x.combo, items: [] });
    groups[groups.length - 1].items.push([x, i]);
  });
  if (!variants.length) return html`<div class="rp-start"><div class="rp-st-foot muted">No open Studio slots fit this opportunity.</div></div>`;
  return html`<div class="rp-start">
    <div class="rp-vars">
      ${variants.length > 1 ? html`<span class="rp-vkeys"><${Kbd} k="←" /><${Kbd} k="→" /></span>` : null}
      <div class="rp-vlist">
        ${groups.map((gr) => {
          const chip = ([x, i]) => html`<button class=${`rp-var ${i === vi ? "on" : ""}`}
            title=${x.note} onClick=${() => setVi(i)}>${x.label}<span class="num">${x.count}</span></button>`;
          return html`<span class="rp-vgroup"><span class="rp-vfirst"><span class="rp-combo">${gr.combo}</span>${chip(gr.items[0])}</span>
            ${gr.items.slice(1).map(chip)}</span>`;
        })}
      </div>
      <button class="btn primary" disabled=${!v || !!claiming} onClick=${onClaim}><${Kbd} k="c" />Claim</button>
    </div>
    <div class="rp-st-foot">
      ${claiming ? html`<span class="rp-claiming"><${Dot} kind="work" />${claiming}</span>` : html`
        <span>${v.note}</span><span class="sep">·</span>
        <span><span class="kbd">c</span> claims <b>${task?.task_name || "the first open task"}</b>${alt ? " (the other was just taken)" : ""} in Studio and saves <b>${repo}</b> <span class="mono">@${(commit || "").slice(0, 8)}</span></span>
        <span class="sep">·</span><span><span class="kbd">⌘Z</span> unclaims for 30 s</span>`}
    </div>
  </div>`;
}

/**
 * Waits for a claim job, showing its steps on the claim line. Resolves to {short} on success,
 * {alternative} when the slot was just taken and Studio offered another, or null after a toast.
 */
async function waitClaim(job, onStep) {
  putJob(job);
  const show = setInterval(() => { const j = jobs.value.get(job.id); if (isLive(j)) onStep(`${jobLine(j)}…`); }, 400);
  const timeout = new Promise((r) => setTimeout(() => r(null), 180000));
  const j = await Promise.race([waitJob(job), timeout]).finally(() => clearInterval(show));
  if (!j || isLive(j)) { toast("The claim is still running; it shows on Home when it finishes"); return null; }
  if (j.state === "succeeded" || j.state === "done") return { short: j.result?.short || null, dossier: j.result?.dossier || null };
  const alt = j.error?.details?.alternative || null;
  toast(j.error?.code === "taken"
    ? `${j.error.message || "That task was just taken"}${alt ? `. c claims ${alt.task_name} instead` : ""}`
    : (j.error?.message || "Claim failed"), { level: "error" });
  return alt ? { alternative: alt } : null;
}

function Dossier({ id }) {
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  const [declining, setDeclining] = useState(false);
  const [other, setOther] = useState(false);
  const [vi, setVi] = useState(0);
  const [claiming, setClaiming] = useState(null);
  const [alt, setAlt] = useState(null);   // {vi, task}: Studio's other open task after "taken"
  const listRef = useRef(null);
  const detailRef = useRef(null);
  useDossiers();
  slots.value;
  const load = (force) => loadDossier(id, force).then(setRes).catch((e) => setErr(e));
  useEffect(() => { setRes(docs.get(id) || null); setErr(null); load(true); }, [id]);
  const doc = res?.dossier;
  const item = res?.item;
  const status = item?.status;
  const opps = useMemo(() => (doc?.blocks || []).filter((b) => b.type === "opportunity"), [doc]);
  const learn = useMemo(() => (doc?.blocks || []).filter((b) => b.type !== "opportunity"), [doc]);
  const startedId = status === "actioned" ? item.actioned?.opportunity_id : null;
  const hashO = hash.value.get("o");
  const sel = opps.find((o) => o.id === hashO) || opps.find((o) => o.id === startedId) || opps[0];
  const variants = sel ? variantsFor(sel.slot_fit) : [];
  useEffect(() => { setVi(0); setAlt(null); if (detailRef.current) detailRef.current.scrollTop = 0; }, [sel?.id]);
  const repo = doc?.repo || item?.repo || id;
  const commit = doc?.commit || item?.commit;
  const ctx = { target: `d:${id}`, repo, commit };

  const st = useRef({});
  st.current = { opps, sel, variants, vi, declining, item, repo, commit, claiming, alt };
  const selectOpp = (o) => {
    if (!o) return;
    history.replaceState({}, "", `/repos/${id}#o=${o.id}`);
    hash.value = new URLSearchParams(`o=${o.id}`);
    requestAnimationFrame(() => listRef.current?.querySelector(`[data-opp="${o.id}"]`)?.scrollIntoView({ block: "nearest" }));
  };
  const move = (k) => {
    const { opps: os, sel: s } = st.current;
    if (!os.length) return;
    const i = Math.max(0, os.indexOf(s));
    selectOpp(os[Math.min(os.length - 1, Math.max(0, i + k))]);
  };
  // Claim the selected variant's first open task (or the one Studio offered after a "taken"), then go
  // straight to writing the new task's opening prompt.
  const claim = async () => {
    const { sel: o, variants: vs, vi: i, item: it, claiming: busy, alt: a } = st.current;
    const v = vs[i];
    if (!o || !v || it?.status !== "ready" || busy) return;
    const task = a && a.vi === i ? a.task : v.task;
    setAlt(null);
    setClaiming(`Claiming ${task?.task_name || "the slot"}…`);
    try {
      const r = await api.send("POST", "/api/claim", {
        lead_id: null, task_id: task?.task_id || null, prompt_style: v.slot.prompt_style,
        interaction_mode: v.slot.interaction_mode, expect_status: "Ready to Claim",
        dossier_id: id, opportunity_id: o.id, repo: st.current.repo, commit: st.current.commit });
      if (r?.toast && !r?.job) toast(r.toast.text, r.toast);
      let out = r?.short || r?.result?.short ? { short: r.short || r.result.short } : null;
      if (!out && r?.job) out = await waitClaim(r.job, setClaiming);
      if (out?.alternative) setAlt({ vi: i, task: out.alternative });
      const short = out?.short;
      if (short) {
        // The claim job marks the dossier as started; older servers leave that to the page.
        const d = await loadDossier(id, true).catch(() => null);
        if (d?.item?.status !== "actioned") {
          await api.send("POST", `/api/dossiers/${id}/act`, { opportunity_id: o.id, task_short: short, slot: v.slot }).catch(() => {});
        }
        loadDossiers();
        await refresh().catch(() => {});
        select(`write_prompt:${short}`);
        navigate("/");
      }
    } catch (e) { toast(e.message, { level: "error" }); }
    setClaiming(null);
  };
  const decline = async (reason, note = "") => {
    setDeclining(false); setOther(false);
    const r = await post(`/api/dossiers/${id}/decline`, { reason, note }, `Declined ${st.current.repo} · ${REASON[reason]}`);
    if (r) { loadDossiers(); navigate("/repos"); }
  };
  const restore = async () => {
    const failed = st.current.item?.status === "failed";
    if (await post(`/api/dossiers/${id}/restore`, {}, failed ? `${st.current.repo} goes first in the research queue` : `Back in the list: ${st.current.repo}`)) { loadDossiers(); load(true); }
  };
  useEffect(() => pushContext("dossier", {
    j: () => !st.current.declining && move(1),
    k: () => !st.current.declining && move(-1),
    c: () => !st.current.declining && claim(),
    d: () => st.current.item?.status === "ready" && setDeclining(true),
    r: () => ["declined", "actioned", "failed"].includes(st.current.item?.status) && restore(),
    ArrowLeft: () => setVi((x) => Math.max(0, x - 1)),
    ArrowRight: () => setVi((x) => Math.min(Math.max(0, st.current.variants.length - 1), x + 1)),
    Escape: () => (overlay.value ? (overlay.value = false) : st.current.declining ? (setDeclining(false), setOther(false)) : navigate("/repos")),
    ...Object.fromEntries(DECLINE.map(([k], i) => [String(i + 1), () => {
      if (!st.current.declining) return;
      if (k === "other") setOther(true);
      else decline(k);
    }])),
  }), [id]);

  if (err) return html`<section class="surface"><div class="scol"><span class="muted">${err.status === 404 ? `No dossier ${id}.` : err.message}</span>
    <button class="btn quiet" onClick=${() => navigate("/repos")}><${Kbd} k="Esc" />All repos</button></div></section>`;
  if (!res) return html`<section class="surface"></section>`;

  const ov = learn.find((b) => b.type === "overview");
  const langs = ov?.size?.languages || [];
  const took = doc?.elapsed_s || item?.elapsed_s;
  const cost = doc?.cost_usd ?? item?.cost_usd;
  const started = startedId ? opps.find((o) => o.id === startedId) : null;
  const taskLink = (short, label) => html`<a class="chip green dz-task" href=${`/task/${short}`}
    onClick=${(e) => { e.preventDefault(); navigate(`/task/${short}`); }}>${label}</a>`;
  return html`<section class="surface rp dz">
    <header class="shead dz-head">
      <h1><span class="faint dz-owner">${ownerOf(repo)}/</span>${repoShort(repo)}
        ${commit ? html`<a class="dz-commit mono" href=${doc?.pinned_url || `https://github.com/${repo}/tree/${commit}`} target="_blank" rel="noreferrer">@${commit.slice(0, 8)} ↗</a>` : null}</h1>
      <div class="actions">
        <button class="btn quiet" onClick=${() => navigate("/repos")}><${Kbd} k="Esc" />All repos</button>
        ${status === "ready" ? html`<button class="btn" onClick=${() => setDeclining(true)}><${Kbd} k="d" />Decline</button>` : null}
        ${status === "declined" || status === "actioned" ? html`<button class="btn" onClick=${restore}><${Kbd} k="r" />Restore</button>` : null}
        ${status === "failed" ? html`<button class="btn primary" onClick=${restore} title="Puts it first in the research queue"><${Kbd} k="r" />Retry research</button>` : null}
      </div>
      <div class="sub">
        ${langs.length ? html`<span>${langs.join(", ")}</span><span class="sep">·</span>` : null}
        ${ov?.size?.source_files != null ? html`<span><span class="num">${ov.size.source_files}</span> source / <span class="num">${ov.size.test_files ?? 0}</span> test files</span><span class="sep">·</span>` : null}
        ${doc?.generated_at ? html`<span class="faint">researched ${ago(doc.generated_at)}${took ? ` · took ${Math.max(1, Math.round(took / 60))} min` : ""}${cost != null ? ` · $${cost.toFixed(2)}` : ""}</span>` : null}
        ${status === "declined" ? html`<span class="sep">·</span><span class="chip">declined · ${REASON[item.decline?.reason] || ""}</span>${item.decline?.note ? html`<span class="sep">·</span><span>${item.decline.note}</span>` : null}` : null}
        ${status === "actioned" ? html`<span class="sep">·</span>${item.actioned?.task_short
          ? taskLink(item.actioned.task_short, `started task ${item.actioned.task_short}${started ? ` from ${started.title}` : ""} ↗`)
          : html`<span class="chip green">started${started ? ` from ${started.title}` : ""}</span>`}` : null}
        ${status === "failed" ? html`<span class="chip red">research failed</span><span class="sep">·</span><span title=${item.error || ""}>${failWords(item.error)}</span>` : null}
        ${status === "generating" ? html`<${WorkerLine} d=${{ items: [item], worker: {}, target: 1 }} compact=${true} />` : null}
        ${status === "queued" ? html`<span><b>Queued for research</b></span>` : null}
      </div>
      ${doc ? html`<div class="dz-verdict ai-rail">
        <div class="dz-headline">${doc.headline}</div>
        ${doc.verdict?.why ? html`<div class="dz-why">${doc.verdict.promise ? html`<span class=${`rp-prom ${doc.verdict.promise}`}>${doc.verdict.promise} promise</span>` : null}<span>${doc.verdict.why}</span></div>` : null}
      </div>` : null}
    </header>
    ${declining ? html`<${DeclineBar} repo=${repo} onPick=${decline} other=${other} setOther=${setOther}
      onCancel=${() => { setDeclining(false); setOther(false); }} />` : null}
    ${status === "failed" && !doc && (item.errors || item.error) ? html`<div class="sbody"><div class="scol">
      <${Collapsible} title="What the research engine reported">
        <pre class="dz-err mono">${[item.error, ...(item.errors || [])].filter(Boolean).join("\n")}</pre><//></div></div>` : null}
    ${doc ? html`<div class="sbody dz-body">
      <div class="dz-opps">
        <div class="dz-colhead opp-grid">
          <span></span>
          <span class="dz-ct"><span class="label">Opportunities</span><span class="num faint sm">${opps.length}</span>
            <span class="ai-tag">AI research</span><span class="dz-keys"><${Kbd} k="j" /><${Kbd} k="k" /></span></span>
          <span class="label">One-shot</span><span class="label">Size</span><span class="label">Best fit · open</span>
        </div>
        <div class="dz-olist" ref=${listRef}>
          ${opps.map((o, i) => html`<${OppRow} key=${o.id} o=${o} n=${i + 1} openFor=${openFor} selected=${o === sel}
            onClick=${() => selectOpp(o)}
            mark=${o.id === startedId ? html`<span class="chip green opp-mark">started</span>` : null} />`)}
        </div>
        <div class="dz-detail" ref=${detailRef}>
          ${sel ? html`<${OppDetail} key=${sel.id} o=${sel} ctx=${ctx} openFor=${openFor} fits=${status !== "ready"} />` : null}
        </div>
        ${sel && status === "ready" ? html`<${ClaimBar} variants=${variants} vi=${Math.min(vi, Math.max(0, variants.length - 1))}
          setVi=${setVi} onClaim=${claim} claiming=${claiming} repo=${repo} commit=${commit}
          alt=${alt && alt.vi === Math.min(vi, Math.max(0, variants.length - 1)) ? alt.task : null} />` : null}
      </div>
      <div class="dz-learn">
        <div class="dz-colhead"><span class="label">The project</span>
          <span class="ai-tag">AI research</span>
          ${doc.model ? html`<span class="faint sm">${doc.model.replace("claude-", "").replace(/-(\d+)-(\d+)$/, " $1.$2")}${doc.effort ? `, ${doc.effort} effort` : ""}</span>` : null}</div>
        <div class="dz-lscroll">${learn.map((b, i) => html`<${Block} key=${i} b=${b} ctx=${ctx} />`)}</div>
      </div>
    </div>` : null}
  </section>`;
}

export default function Repos({ id }) {
  return id ? html`<${Dossier} key=${id} id=${id} />` : html`<${List} />`;
}
