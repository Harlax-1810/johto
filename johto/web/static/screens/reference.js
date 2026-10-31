import { useEffect, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { api } from "../lib/api.js";
import { CodeView } from "../components/codeview.js";
import { AiPanel, Collapsible, Chip } from "../components/ui.js";
import { mins, oneShotWord } from "../lib/format.js";

const fileOf = (p) => (p || "").split(":")[0].trim();
/** A file chip's name: the base name, with its folder when another chip has the same base name. */
const chipName = (f, all) => {
  const base = f.split("/").pop();
  return all.filter((x) => x.split("/").pop() === base).length > 1 ? f.split("/").slice(-2).join("/") : base;
};
const lineOf = (p) => { const m = /:(\d+)/.exec(p || ""); return m ? +m[1] : null; };

/** Facts list that splits "path:line" tokens out of AI fact text into pointers. */
function Facts({ facts, onOpen }) {
  return html`<ul>${(facts || []).map((f) => {
    const m = /([\w./-]+\.(?:go|ts|tsx|js|py|rs|md|json|yml|yaml|sh)(?::\d+)?)/.exec(f);
    return html`<li><span>${f}</span>${m ? html`<button class="ptr" onClick=${() => onOpen(m[1])}>${m[1].split("/").pop()} ▸</button>` : html`<span></span>`}</li>`;
  })}</ul>`;
}

export function Research({ lead, onOpen }) {
  if (!lead) return null;
  const word = oneShotWord({ word: lead.source === "issue" ? "not_checked" : { low: "unlikely", medium: "possible", high: "likely" }[lead.one_shot_risk?.level] });
  return html`<${AiPanel} title="AI research" right=${html`<span class="sm" style="color:var(--ai-label)">lead scout · ${lead.source === "issue" ? "from issue" : "code read"}</span>`}>
    <${Facts} facts=${lead.facts} onOpen=${onOpen} />
    <dl class="facts" style="margin-top:10px">
      ${lead.mechanism ? html`<dt>Hard because</dt><dd>${lead.mechanism}</dd>` : null}
      <dt>One-shot</dt><dd>${word} <span class="faint">(estimate)</span>${lead.one_shot_risk?.why ? html`<div class="sm muted">${lead.one_shot_risk.why}</div>` : null}</dd>
      ${lead.verify ? html`<dt>Verify</dt><dd class="sm">${lead.verify}</dd>` : null}
      <dt>Upstream</dt><dd>${(lead.upstream?.related || []).length
        ? lead.upstream.related.map((r) => html`<div class="sm"><a href=${r.url} target="_blank">${r.title}</a> <span class="faint">${r.state}</span></div>`)
        : html`<span class="muted">nothing related found</span>`}</dd>
    </dl>
  <//>`;
}

/** The repo research opportunity a task was claimed from (AI research, tinted like the lead panel). */
export function OppResearch({ opp, onOpen }) {
  if (!opp) return null;
  const bt = opp.bounded_task || {};
  const os = opp.one_shot || {};
  return html`<${AiPanel} title="AI research" right=${html`<span class="sm" style="color:var(--ai-label)">repo research · ${opp.title}</span>`}>
    ${(opp.evidence || []).length ? html`<ul>${opp.evidence.map((e) => html`<li><span>${e.fact}</span>${e.pointer
      ? html`<button class="ptr" onClick=${() => onOpen(e.pointer)}>${fileOf(e.pointer).split("/").pop()}${lineOf(e.pointer) ? `:${lineOf(e.pointer)}` : ""} ▸</button>`
      : html`<span></span>`}</li>`)}</ul>` : null}
    <dl class="facts" style="margin-top:10px">
      ${opp.ambition ? html`<dt>Expert team</dt><dd>${opp.ambition}</dd>` : null}
      ${opp.why_not_done ? html`<dt>Not done because</dt><dd>${opp.why_not_done}</dd>` : null}
      ${(opp.where_models_fail || []).length ? html`<dt>Models fail at</dt><dd>${opp.where_models_fail.map((t) => html`<div class="sm">${t}</div>`)}</dd>` : null}
      ${bt.shape ? html`<dt>Bounded job</dt><dd class="sm">${bt.shape}${bt.check_in_session ? html`<div class="muted">Check: ${bt.check_in_session}</div>` : null}</dd>` : null}
      ${os.likelihood ? html`<dt>One-shot</dt><dd>${os.likelihood} <span class="faint">(estimate)</span>${os.why ? html`<div class="sm muted">${os.why}</div>` : null}</dd>` : null}
      ${(opp.upstream?.related || []).length ? html`<dt>Upstream</dt><dd>${opp.upstream.related.map((r) => html`<div class="sm"><a href=${r.url} target="_blank">${r.title}</a> <span class="faint">${r.state}</span></div>`)}</dd>` : null}
    </dl>
  <//>`;
}

/** Folders and files at the pinned commit, for a task whose research names no files. */
function Tree({ target, onOpen }) {
  const [dir, setDir] = useState("");
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    let live = true;
    setErr(null);
    const load = (n = 0) => api.get(`/api/code/${encodeURIComponent(target)}/tree?path=${encodeURIComponent(dir)}`)
      .then((r) => { if (!live) return; if (r?.job && n < 20) setTimeout(() => load(n + 1), 1500); else setRes(r); })
      .catch((e) => live && setErr(e.message));
    load();
    return () => { live = false; };
  }, [target, dir]);
  if (err) return html`<div class="sm faint" style="padding:12px 16px">${err}</div>`;
  if (!res?.entries) return html`<div class="sm faint" style="padding:12px 16px">Reading the repo…</div>`;
  const up = dir ? dir.split("/").slice(0, -1).join("/") : null;
  const join = (n) => (dir ? `${dir}/${n}` : n);
  return html`<div class="tree" style="padding:6px 16px 12px;font-family:var(--mono);font-size:12px">
    ${dir ? html`<div class="muted" style="padding:2px 0 6px">${dir}/</div>` : null}
    ${up != null ? html`<div><button class="ptr" onClick=${() => setDir(up)}>..</button></div>` : null}
    ${res.entries.map((e) => html`<div><button class="ptr" onClick=${() => (e.type === "dir" ? setDir(join(e.name)) : onOpen(join(e.name)))}>
      ${e.name}${e.type === "dir" ? "/" : ""}</button></div>`)}
  </div>`;
}

export function RunFacts({ run }) {
  if (!run?.facts) return null;
  const f = run.facts;
  return html`<div>
    <div class="label" style="margin-bottom:6px">${run.kind === "calib" ? "Calibration" : "Validation"} ${run.n}</div>
    <div style="display:flex;flex-wrap:wrap;gap:6px 14px;margin-bottom:10px">
      <span><b class="num">${f.turns}</b> <span class="muted">your turns</span></span>
      <span><b class="num">${mins(f.active_min)}</b> <span class="muted">active min</span></span>
      <span><b class="num">${mins(f.wall_min)}</b> <span class="muted">wall</span></span>
      <span>${f.model_ok && f.effort_ok ? "Opus 5.5 · high ✓" : html`<${Chip} tone="red">model or effort wrong<//>`}</span>
      ${f.tests?.runs ? html`<span>tests <span class="num">${f.tests.runs}</span> runs, last ${f.tests.last_ok ? html`<span style="color:var(--ok-fg)">passed</span>` : html`<span style="color:var(--bad-fg)">failed</span>`}</span>` : null}
    </div>
    ${(f.diffstat || []).length ? html`<div class="code" style="margin-bottom:12px">
      ${f.diffstat.map((d) => html`<div style="display:grid;grid-template-columns:1fr auto auto;gap:10px;padding:1px 0">
        <span>${d.path}${d.new ? html` <span class="faint">new</span>` : null}</span>
        <span style="color:var(--ok-fg)">+${d.add}</span><span style="color:var(--bad-fg);min-width:28px">${d.del ? `−${d.del}` : ""}</span></div>`)}
    </div>` : null}
    ${(run.notes || []).length ? html`<div style="margin-bottom:12px">
      <div class="label" style="margin-bottom:4px">Your run notes</div>
      ${run.notes.map((n) => html`<div class="nrow" style="display:grid;grid-template-columns:44px 1fr;gap:8px"><span class="faint num">${Math.floor(n.t_run_s / 60)}:${String(n.t_run_s % 60).padStart(2, "0")}</span><span>${n.text}</span></div>`)}
    </div>` : null}
    ${(run.moments || []).length ? html`<div style="margin-bottom:12px">
      <div class="label" style="margin-bottom:4px">Measured</div>
      ${run.moments.map((m) => html`<div style="display:grid;grid-template-columns:18px 1fr;gap:6px;padding:2px 0">
        <span style=${`color:${m.level === "warn" ? "var(--wait-dot)" : "var(--ok-dot)"};font-weight:700`}>${m.level === "warn" ? "⚠" : "✓"}</span><span>${m.text}</span></div>`)}
    </div>` : null}
    ${f.final_message_excerpt ? html`<${Collapsible} title="Claude's last message"><div class="muted">${f.final_message_excerpt}</div><//>` : null}
    <${Collapsible} title="Transcript"><div class="sm faint">Opens the full conversation.</div><//>
  </div>`;
}

/**
 * Reference pane: Code (real code at the pinned commit), Run, Research (AI), Issue.
 * Research is never the default on a writing surface.
 */
export function Reference({ target, lead, opportunity, runs, defaultTab = "code", title, pinned }) {
  // File chips: the lead's files, else the paths the chosen opportunity's evidence points at.
  const files = lead?.files?.length ? lead.files
    : [...new Set((opportunity?.evidence || []).map((e) => fileOf(e.pointer)).filter(Boolean))];
  const run = (runs || []).filter((r) => r.facts).slice(-1)[0];
  const research = !!(lead || opportunity);
  const tabs = [["code", "Code"], run ? ["run", "Run"] : null, research ? ["research", "Research"] : null,
                lead?.issue_url ? ["issue", `Issue #${lead.issue_url.split("/").pop()}`] : null].filter(Boolean);
  // A default tab this task doesn't have (no run yet) falls back to the code.
  const first = tabs.some(([k]) => k === defaultTab) ? defaultTab : "code";
  const [tab, setTab] = useState(first);
  const [path, setPath] = useState(files[0]);
  const [hl, setHl] = useState(null);
  useEffect(() => { setTab(first); setPath(files[0]); setHl(null); }, [target, first]);
  const open = (ptr) => { setPath(fileOf(ptr)); const l = lineOf(ptr); setHl(l ? [l] : null); setTab("code"); };
  const repo = lead?.repo || opportunity?.repo || "";
  const commit = lead?.commit || opportunity?.commit || "";
  const shown = path && !files.includes(path) ? [...files, path] : files;
  return html`<div class="ref" style="flex:1;min-width:0">
    ${pinned?.text ? html`<section class="ref-pin" style="padding:10px 16px;border-bottom:1px solid var(--line)">
      <div class="label" style="margin-bottom:4px">${pinned.label}</div>
      <div style="white-space:pre-wrap;max-height:9.5em;overflow-y:auto">${pinned.text}</div>
    </section>` : null}
    <div class="rhead">
      <div class="tabs">${tabs.map(([k, l]) => html`<button class=${`tab ${k === "research" ? "tab-ai" : ""} ${tab === k ? "on" : ""}`} onClick=${() => setTab(k)}>${l}</button>`)}</div>
      <span class="sm faint" style="display:flex;gap:12px;align-items:center">${title || (repo ? `${repo} @${commit.slice(0, 8)}` : "")}
        ${repo && tab === "code" ? html`<a href=${`https://github.com/${repo}/${path ? "blob" : "tree"}/${commit}/${path || ""}`} target="_blank">GitHub ↗</a>` : null}</span>
    </div>
    <div class="rbody">
      ${tab === "code" ? html`
        ${shown.length ? html`<div class="codehead">
          <div class="fchips">${shown.map((f) => html`<button class=${`fchip ${f === path ? "on" : ""}`} title=${f} onClick=${() => { setPath(f); setHl(null); }}>${chipName(f, shown)}</button>`)}
            ${path ? html`<button class="fchip" title="Browse the repo" onClick=${() => setPath(null)}>Files</button>` : null}</div>
        </div>` : null}
        ${path ? html`<div class="sm muted" style="padding:6px 16px 4px;font-family:var(--mono);position:sticky;left:0">${path}</div><${CodeView} target=${target} path=${path} highlight=${hl} />`
          : html`<${Tree} target=${target} onOpen=${open} />`}` : null}
      ${tab === "run" ? html`<div style="padding:14px 16px"><${RunFacts} run=${run} /></div>` : null}
      ${tab === "research" ? html`<div style="padding:14px 16px">${lead ? html`<${Research} lead=${lead} onOpen=${open} />` : html`<${OppResearch} opp=${opportunity} onOpen=${open} />`}</div>` : null}
      ${tab === "issue" ? html`<div style="padding:14px 16px"><a href=${lead.issue_url} target="_blank">${lead.title} ↗</a></div>` : null}
    </div>
  </div>`;
}
