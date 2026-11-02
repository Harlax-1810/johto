// The task understanding (johto/understand.py), at two levels. The glance sits beside the prompt box: the task in a
// nutshell, the scene, Claude's job, what done looks like, and the one fact per type he needs before writing (the
// starting state for Operate, whether the bug happens for Modify, and so on). The page behind it
// (/task/<short>/understand) has the detail: real or staged, the product today, upstream threads, words, the hard
// part (folded, kept out of the prompt), the code, and what Mercor expects for this type with judged tasks.
// All of it is Claude's research, shown with the AI mark. Nothing here writes his fields.
import { useEffect, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { navigate, toast, jobFor, isLive } from "../../lib/store.js";
import { AiTag, Btn, Dot, Pill } from "../../components/primitives.js";
import { actionWord } from "../../lib/words.js";
import { clip } from "./words.js";

// --- data -------------------------------------------------------------------------------------------------

export function useUnderstand(short) {
  const [u, setU] = useState(null);
  const [err, setErr] = useState(null);
  const load = async () => {
    try { setU(await api.get(`/api/tasks/${short}/understand`)); setErr(null); } catch (e) { setErr(e); }
  };
  useEffect(() => { setU(null); load(); }, [short]);
  const busy = !!u && (u.state === "queued" || u.state === "generating");
  useEffect(() => {
    if (!busy) return undefined;
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, [busy, short]);
  const j = jobFor(["understand"], short);
  useEffect(() => { if (j && !isLive(j)) load(); }, [j?.state]);
  return { u, err, reload: load };
}

async function researchAgain(short, reload) {
  try {
    const r = await api.post(`/api/tasks/${short}/understand/refresh`, {});
    toast(r?.toast?.text || "Researching the task again.", { level: "info" });
    setTimeout(reload, 600);
  } catch (e) { toast(e.message, { level: "error" }); }
}

// --- words ------------------------------------------------------------------------------------------------

const STEP = { waiting: "Waiting its turn", cloning: "Copying the repo", upstream: "Reading upstream threads",
  researching: "Reading the code and threads", writing: "Writing it up" };
const REAL = { real: ["Real", "ok"], staged: ["Staged for the task", "wait"], mixed: ["Partly real, partly staged", "wait"] };
const UP = { open: ["Not fixed upstream", "ok"], partly: ["Partly fixed upstream", "wait"],
  fixed: ["Already fixed upstream", "stop"], none_found: ["No upstream threads found", "neutral"] };
const REPRO = { confirmed: ["Happens at the pinned code", "ok"], not_yet_confirmed: ["Not yet confirmed", "wait"],
  does_not_reproduce: ["Doesn't happen at the pinned code", "stop"] };
const CONF = { confirmed: ["Confirmed in the code", "ok"], not_yet_confirmed: ["Not yet confirmed", "wait"] };
const KIND_HEAD = { "Operate": "The starting state", "Modify": "The bug", "Create": "What's there and what's missing",
  "Understand / Assess": "The question" };

function minsAgo(iso) {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const m = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 24 ? `${h} h ago` : `${Math.round(h / 24)} d ago`;
}

const T = ({ v }) => (v ? html`<${Pill} tone=${v[1]}>${v[0]}<//>` : null);

// --- the glance (Prompt step, right column) ---------------------------------------------------------------

/** One fact per type that he needs before writing the prompt. */
function KindStrip({ action, k }) {
  if (!k) return null;
  if (action === "Operate") {
    return html`<div class="un-strip">
      <div class="label-sm">The starting state · built in the lane before the run</div>
      <ol class="un-seq">${(k.starting_state || []).map((p) => html`<li title=${p.what} tabindex="0">${p.part}</li>`)}</ol>
    </div>`;
  }
  if (action === "Modify") {
    return html`<div class="un-strip">
      <div class="label-sm">What a user sees <${T} v=${REPRO[k.reproduces?.status]} /></div>
      <p>${k.symptom}</p>
    </div>`;
  }
  if (action === "Create") {
    return html`<div class="un-strip"><div class="label-sm">Missing today</div><p>${k.missing}</p></div>`;
  }
  return html`<div class="un-strip"><div class="label-sm">The question Claude answers <${T} v=${CONF[k.confirmed]} /></div>
    <p>${k.question}</p></div>`;
}

function Busy({ u }) {
  const j = u.job || {};
  return html`<p class="un-busy"><${Dot} tone="work" />Claude is researching this task: ${STEP[j.step] || "starting"}${j.started_at ? html`, started ${minsAgo(j.started_at)}` : null}. Usually 2 to 5 minutes.</p>`;
}

const NO_SUBJECT = "There's nothing to research yet: this task has no brief idea, scout lead or research notes.";

/** The repo brief's version while the understanding is written (or if it failed). */
function Fallback({ f }) {
  if (!f?.title && !f?.problem) return null;
  return html`<div class="un-fallback">
    ${f.title ? html`<p class="wb-origin-t">${f.title}</p>` : null}
    ${f.problem ? html`<p class="wb-origin-p">${f.problem}</p>` : null}
    ${(f.today || []).length ? html`<div class="wb-origin-s"><div class="label-sm">How it works today</div>
      <ul>${f.today.map((x) => html`<li>${x}</li>`)}</ul></div>` : null}
  </div>`;
}

export function Glance({ short, origin }) {
  const { u, err, reload } = useUnderstand(short);
  const open = () => navigate(`/task/${short}/understand`);
  if (err && !u) return html`<div class="wb-origin none"><h3 class="wb-h3">The task</h3><p class="faint">The research didn't load: ${err.message}</p></div>`;
  if (!u) return html`<div class="wb-origin none"><p class="faint">Loading the research…</p></div>`;
  const d = u.doc;
  const more = html`<button type="button" class="linkbtn un-open" onClick=${open}>Understand this task →</button>`;
  if (!d) {
    return html`<section class="ai-block wb-origin un-glance">
      <div class="ai-head"><${AiTag} label="Claude's research" />${u.state === "failed" ? null : more}</div>
      ${u.state === "queued" || u.state === "generating" ? html`<${Busy} u=${u} />` : null}
      ${u.state === "missing" && u.subject === false ? html`<p class="faint">${NO_SUBJECT}</p>` : null}
      ${u.state === "failed" ? html`<p class="un-failed">The task research failed: ${clip(u.error, 220)}
        <button type="button" class="linkbtn" onClick=${() => researchAgain(short, reload)}>Try again</button></p>` : null}
      <${Fallback} f=${u.fallback?.title || u.fallback?.problem ? u.fallback : origin} />
    </section>`;
  }
  const up = d.upstream || {};
  const real = d.real || {};
  return html`<section class="ai-block wb-origin un-glance">
    <div class="ai-head"><${AiTag} label="Claude's research" />${more}</div>
    ${u.meta?.title ? html`<p class="un-title">${u.meta.title}</p>` : null}
    <p class="un-headline">${d.headline}</p>
    ${u.state === "queued" || u.state === "generating" ? html`<${Busy} u=${u} />` : null}
    <dl class="un-three">
      <div><dt>The scene</dt><dd>${d.story}</dd></div>
      <div><dt>Claude's job</dt><dd>${d.job}</dd></div>
      <div><dt>Done looks like</dt><dd>${d.done}</dd></div>
    </dl>
    <${KindStrip} action=${u.action} k=${d.kind} />
    <div class="un-flags">
      <div class="un-flag"><${T} v=${REAL[real.verdict]} />${(real.made_up || [])[0] ? html`<span>${real.made_up[0]}</span>` : null}</div>
      <div class="un-flag"><${T} v=${UP[up.verdict]} />${up.line ? html`<span>${up.line}</span>` : null}</div>
    </div>
    ${(d.terms || []).length ? html`<div class="un-terms"><span class="label-sm">Words</span>
      ${d.terms.map((t) => html`<span class="un-term" tabindex="0" title=${t.means}><b>${t.term}</b><span class="un-term-m">${t.means}</span></span>`)}</div>` : null}
    <p class="un-foot">The hard part, upstream threads, the code and how Mercor judged ${actionWord(u.action)} tasks are on the
      <button type="button" class="linkbtn" onClick=${open}>full page</button>.</p>
  </section>`;
}

// --- the page ---------------------------------------------------------------------------------------------

function Fold({ title, tone, open: start = false, note, children }) {
  const [open, setOpen] = useState(start);
  return html`<section class=${`un-fold ${tone || ""} ${open ? "open" : ""}`}>
    <button type="button" class="un-fold-h" aria-expanded=${open} onClick=${() => setOpen(!open)}>
      <span class="wb-tri">${open ? "▾" : "▸"}</span><span class="un-fold-t">${title}</span>${note ? html`<span class="un-fold-n">${note}</span>` : null}
    </button>
    ${open ? html`<div class="un-fold-b">${children}</div>` : null}
  </section>`;
}

function List({ items, cls }) {
  return (items || []).length ? html`<ul class=${`un-list ${cls || ""}`}>${items.map((x) => html`<li>${x}</li>`)}</ul>` : null;
}

function KindBand({ action, k }) {
  if (!k) return null;
  let body;
  if (action === "Operate") {
    body = html`
      <p class="un-now"><span class="label-sm">In the lane right now</span>${k.lane_now}</p>
      <ol class="un-steps">${(k.starting_state || []).map((p, i) => html`<li><span class="un-n">${i + 1}</span><b>${p.part}</b><p>${p.what}</p></li>`)}</ol>
      <div class="un-two">
        <div><div class="label-sm">How a reviewer rebuilds it</div><p>${k.rebuild}</p></div>
        <div><div class="label-sm">Operate, not Modify</div><p>${k.system_not_code}</p></div>
      </div>`;
  } else if (action === "Modify") {
    body = html`
      <p class="un-big">${k.symptom}</p>
      <div class="un-two">
        <div><div class="label-sm">Does it happen at the pinned code? <${T} v=${REPRO[k.reproduces?.status]} /></div><p>${k.reproduces?.how}</p></div>
        <div><div class="label-sm">Who runs into it</div><p>${k.who_hits_it}</p></div>
      </div>
      <div class="label-sm">Has to keep working</div><${List} items=${k.keep_working} />`;
  } else if (action === "Create") {
    body = html`
      <div class="un-two">
        <div><div class="label-sm">Already in the product</div><${List} items=${k.exists} /></div>
        <div><div class="label-sm">Missing today</div><p>${k.missing}</p></div>
      </div>
      <div class="un-two">
        <div><div class="label-sm">Where it plugs in</div><p>${k.fits_into}</p></div>
        <div>${(k.similar || []).length ? html`<div class="label-sm">Similar features</div><${List} items=${k.similar} />` : null}</div>
      </div>`;
  } else {
    // The answer key is what his prompt must not give away, so it stays folded like the hard part.
    body = html`
      <p class="un-big">${k.question}</p>
      <${Fold} title="The answer key" tone="stop" note=${html`Keep it out of the prompt: a strong answer finds these unprompted. <${T} v=${CONF[k.confirmed]} />`}>
        <div class="label-sm">What a right answer contains</div><${List} items=${k.ground_truth} />
        <div class="label-sm">How a reviewer tells right from wrong</div><p>${k.check}</p>
      <//>`;
  }
  return html`<section class="un-band"><h2 class="un-h2">${KIND_HEAD[action] || "This task"}</h2>${body}</section>`;
}

function Thread({ t, f }) {
  const fx = f || {};
  const kind = fx.kind === "pull" ? "Pull request" : fx.kind === "issue" ? "Issue" : (t.url.includes("/pull/") ? "Pull request" : "Issue");
  const dates = [fx.opened && `opened ${fx.opened}`, fx.merged ? `merged ${fx.merged}` : fx.closed ? `closed ${fx.closed}` : null].filter(Boolean).join(" · ");
  const inPinned = fx.in_pinned === true ? ["In the pinned code", "neutral"] : fx.in_pinned === false ? ["Not in the pinned code", "neutral"] : null;
  return html`<li class="un-thread">
    <div class="un-thread-h">
      <span class="un-thread-k">${kind}${fx.number ? ` #${fx.number}` : ""}</span>
      <a href=${t.url} target="_blank" rel="noopener">${fx.title || t.url} ↗</a>
    </div>
    <div class="un-thread-m">${fx.state ? html`<span>${fx.state}</span>` : null}${dates ? html`<span>${dates}</span>` : null}
      ${inPinned ? html`<${T} v=${inPinned} />` : null}${fx.first_release ? html`<span>shipped in ${fx.first_release}</span>` : null}</div>
    <p><span class="label-sm">What it changed</span>${t.changed}</p>
    <p><span class="label-sm">What it means for this task</span>${t.bearing}</p>
  </li>`;
}

function codeLink(repo, commit, path) {
  const m = /^(.*?)(?::(\d+)(?:-(\d+))?)?$/.exec(path || "");
  if (!m || !repo || !commit) return null;
  const lines = m[2] ? `#L${m[2]}${m[3] ? `-L${m[3]}` : ""}` : "";
  return `https://github.com/${repo}/blob/${commit}/${m[1]}${lines}`;
}

function Judged({ j }) {
  const [open, setOpen] = useState(false);
  const fb = j.feedback || "";
  return html`<li class=${`un-judged ${open ? "open" : ""}`}>
    <button type="button" class="un-judged-h" aria-expanded=${open} onClick=${() => setOpen(!open)}>
      <${Pill} tone=${j.status === "Accepted" ? "ok" : "stop"}>${j.status}<//>
      <b>${(j.repo || "").split("/").pop()}</b>
      <span class="faint">${[j.slot?.codebase_type?.split("-")[0], j.slot?.prompt_style, j.slot?.interaction_mode?.split("/")[0]].filter(Boolean).join(" · ")}</span>
      ${j.mine ? html`<${Pill} tone="outline">Yours<//>` : null}
      <span class="un-judged-x">${open ? "" : clip(fb.replace(/\s+/g, " "), 150)}</span>
    </button>
    ${open ? html`<div class="un-judged-b">
      ${fb ? html`<div class="label-sm">What the reviewer wrote</div><p class="un-pre">${fb}</p>` : html`<p class="faint">No reviewer note saved.</p>`}
      ${j.prompt ? html`<div class="label-sm">Its opening prompt</div><p class="un-pre">${j.prompt}</p>` : null}
      ${j.initial_state ? html`<div class="label-sm">Its initial state</div><p class="un-pre">${j.initial_state}</p>` : null}
    </div>` : null}
  </li>`;
}

function Expect({ u }) {
  const p = u.precedent || {};
  const c = p.counts || {};
  const judged = p.judged || [];
  const name = actionWord(u.action);
  const count = [c.Accepted ? `${c.Accepted} accepted` : null, c.Rejected ? `${c.Rejected} rejected` : null,
    c["Awaiting Review"] || c["In Review"] ? `${(c["Awaiting Review"] || 0) + (c["In Review"] || 0)} waiting for review` : null].filter(Boolean).join(", ");
  return html`<section class="un-band un-expect">
    <h2 class="un-h2">What Mercor expects for ${name} tasks</h2>
    <${List} items=${u.notes} />
    <${List} items=${(u.rules || []).map((r) => r.text)} cls="un-rules" />
    <div class="un-judged-head"><span class="label-sm">How reviewers judged ${name} tasks so far</span>
      ${count ? html`<span class="faint">${count}, all writers${p.at ? `, read ${minsAgo(p.at)}` : ""}</span>` : null}</div>
    ${judged.length ? html`<ul class="un-judged-l">${judged.map((j) => html`<${Judged} j=${j} />`)}</ul>`
      : html`<p class="faint">${p.at ? `No ${name} task has been accepted or rejected yet.` : "Reading the judged tasks from Studio…"}</p>`}
  </section>`;
}

export function UnderstandPage({ short, wb }) {
  const { u, err, reload } = useUnderstand(short);
  const back = () => navigate(`/task/${short}`);
  if (err && !u) return html`<div class="un-page"><p class="faint">The research didn't load: ${err.message}</p></div>`;
  if (!u) return html`<div class="un-page"><p class="faint">Loading the research…</p></div>`;
  const d = u.doc;
  const busy = u.state === "queued" || u.state === "generating";
  const top = html`<div class="un-top">
    <button type="button" class="linkbtn" onClick=${back}>← Back to the prompt</button>
    <span class="un-top-r">
      ${u.meta?.generated_at ? html`<span class="faint">Researched ${minsAgo(u.meta.generated_at)}${u.state === "stale" ? ", from older research" : ""}</span>` : null}
      ${busy || (!d && u.subject === false) ? null : html`<${Btn} size="sm" kind="quiet" label="Research again" onClick=${() => researchAgain(short, reload)} />`}
    </span>
  </div>`;
  if (!d) {
    return html`<div class="un-page">${top}
      <div class="un-hero"><${AiTag} label=${`Claude's research · ${u.fallback?.title || "this task"}`} /></div>
      ${busy ? html`<${Busy} u=${u} />` : null}
      ${!busy && u.state === "missing" && u.subject === false ? html`<p class="un-none">${NO_SUBJECT}</p>` : null}
      ${!busy && u.state === "missing" && u.subject !== false ? html`<p class="un-none">${u.auto ? "The research hasn't started yet." : "This task has no research yet. Its prompt is written, so research starts only when asked."}
        <${Btn} size="sm" kind="primary" label="Research this task" onClick=${() => researchAgain(short, reload)} /></p>` : null}
      ${u.state === "failed" ? html`<p class="un-failed">The task research failed: ${u.error}</p>` : null}
      <${Fallback} f=${u.fallback} />
      <${Expect} u=${u} />
    </div>`;
  }
  const real = d.real || {};
  const up = d.upstream || {};
  const today = d.today || {};
  const facts = u.facts || {};
  const threads = facts.threads || {};
  const pin = facts.pinned || {};
  return html`<div class="un-page">
    ${top}
    <header class="un-hero">
      <${AiTag} label=${`Claude's research · ${u.meta?.title || "this task"}`} />
      <p class="un-lede">${d.headline}</p>
      ${busy ? html`<${Busy} u=${u} />` : null}
    </header>

    <section class="un-band un-plain">
      <div><h3>The scene</h3><p>${d.story}</p></div>
      <div><h3>Claude's job</h3><p>${d.job}</p></div>
      <div><h3>Done looks like</h3><p>${d.done}</p></div>
    </section>

    <div class="un-grid">
      <section class="un-band">
        <h2 class="un-h2">Real or staged <${T} v=${REAL[real.verdict]} /></h2>
        <div class="label-sm">True of the real product and the world</div>
        <${List} items=${real.real} cls="un-real" />
        ${(real.made_up || []).length ? html`<div class="label-sm">Made up for the task</div><${List} items=${real.made_up} cls="un-staged" />` : null}
      </section>
      <section class="un-band">
        <h2 class="un-h2">The product today</h2>
        ${(today.works || []).length ? html`<div class="label-sm">Works</div><${List} items=${today.works} cls="un-works" />` : null}
        <div class="label-sm">Missing or broken</div><${List} items=${today.missing} cls="un-missing" />
      </section>
    </div>

    <${KindBand} action=${u.action} k=${d.kind} />

    <section class="un-band">
      <h2 class="un-h2">Upstream <${T} v=${UP[up.verdict]} /></h2>
      <p class="un-big">${up.line}</p>
      ${pin.date ? html`<p class="faint">The pinned code is from ${pin.date}${pin.after_release ? `, after the ${pin.after_release} release` : ""}.</p>` : null}
      ${(up.threads || []).length ? html`<ol class="un-threads">${up.threads.map((t) => html`<${Thread} t=${t} f=${threads[t.url]} />`)}</ol>` : null}
    </section>

    ${(d.terms || []).length ? html`<section class="un-band"><h2 class="un-h2">Words</h2>
      <dl class="un-words">${d.terms.map((t) => html`<div><dt>${t.term}</dt><dd>${t.means}</dd></div>`)}</dl></section>` : null}

    ${(d.unsure || []).length ? html`<section class="un-band un-unsure"><h2 class="un-h2">Not confirmed</h2><${List} items=${d.unsure} /></section>` : null}

    <${Fold} title="The hard part" tone="stop" note="Keep it out of the prompt: the prompt describes the need, not the diagnosis.">
      <${List} items=${d.hard} />
    <//>
    ${(d.code || []).length ? html`<${Fold} title="Where in the code" note=${`${d.code.length} places at the pinned commit`}>
      <ul class="un-code">${d.code.map((c) => {
        const href = codeLink(u.meta?.repo, u.meta?.commit, c.path);
        return html`<li>${href ? html`<a href=${href} target="_blank" rel="noopener"><code>${c.path}</code> ↗</a>` : html`<code>${c.path}</code>`}<p>${c.what}</p></li>`;
      })}</ul>
    <//>` : null}

    <${Expect} u=${u} />
  </div>`;
}
