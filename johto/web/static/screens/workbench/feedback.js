// The sent-back checklist (johto/feedback.py): what the reviewer asked, item by item, with where each one is in
// his fields and whether his text deals with it yet. The asks and checks are AI text with the AI mark; it never
// writes his fields.
import { useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { toast, putJob, jobLine } from "../../lib/store.js";
import { Btn, AiTag, Flag } from "../../components/primitives.js";
import { FIELD_NAME } from "./words.js";
import { ReviewerWords } from "./after.js";
import { useLiveJob } from "./data.js";

const KIND = { write: "Write", run: "Run", attach: "Attach", fact: "Check a fact", decide: "Decide", rate: "Rate" };
const STATE = { done: ["ok", "Done"], addressed: ["ok", "Looks done"], partly: ["wait", "Partly"], open: ["outline", "To do"] };
const NAME = { ...FIELD_NAME, ratings: "Ratings", claude_minutes: "Claude minutes", final_output: "Final Output",
  trajectory_logs: "Trajectory Logs" };

/** Select `text` in the open field's editor (whitespace may differ). */
export function selectInEditor(text) {
  const ta = document.querySelector(".wb-main textarea:not([readonly])") || document.querySelector(".wb-main textarea");
  if (!ta || !text) return false;
  const esc = String(text).trim().split(/\s+/).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+");
  const m = new RegExp(esc, "i").exec(ta.value);
  if (!m) return false;
  ta.focus();
  ta.setSelectionRange(m.index, m.index + m[0].length);
  // Scroll the selection into view: a textarea scrolls itself; the page scrolls to the textarea.
  ta.blur(); ta.focus();
  ta.scrollIntoView({ block: "center", behavior: "smooth" });
  return true;
}

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (!(s >= 0)) return "";
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function Item({ it, n, short, wb, onField, onStep, reload }) {
  const [busy, setBusy] = useState(false);
  const [more, setMore] = useState(false);
  const [tone, word] = STATE[it.state] || STATE.open;
  const done = it.state === "done" || it.state === "addressed";
  const a = it.action || {};
  // Only his text fields: a span in Claude minutes or the ratings has no editor to show it in.
  const pick = (w) => (w?.text && FIELD_NAME[w.field] ? w : null);
  const where = pick(it.check_where) || pick(it.where);
  async function tick() {
    setBusy(true);
    try { await api.post(`/api/tasks/${short}/feedback/items/${it.id}`, { done: !it.manual }); reload?.(); }
    catch (e) { toast(e?.message || "Couldn't save the tick", { level: "error" }); }
    setBusy(false);
  }
  const studioLink = (label) => (wb.studio?.url
    ? html`<a class="btn sm secondary" href=${wb.studio.url} target="_blank" rel="noopener">${label}</a>` : null);
  // His words the item is about: shown in the app's editor when the app holds them, else they are only in Studio.
  const whereBtn = where
    ? (where.in === "studio" ? studioLink(`Open ${NAME[where.field]} in Studio`)
      : html`<${Btn} size="sm" kind="secondary" label=${`Show it in ${NAME[where.field]}`} onClick=${() => onField(where.field, where.text)} />`)
    : null;
  const fieldBtn = !whereBtn && a.type === "field" && a.field
    ? html`<${Btn} size="sm" kind="secondary" label=${`Open ${NAME[a.field] || a.field}`} onClick=${() => onField(a.field, null)} />` : null;
  const runName = a.run || wb.run?.name;
  const attachBtns = a.type === "attach" ? html`
    ${a.diff && runName ? html`<a class="btn sm secondary" href=${`/api/tasks/${short}/runs/${runName}/diff`} target="_blank" rel="noopener">Open the diff</a>` : null}
    <${Btn} size="sm" kind="quiet" label="Go to Save" title="Save to Studio uploads the diff as a gist and puts its link in Final Output" onClick=${() => onStep("submit")} />` : null;
  const runBtn = a.type === "run" ? html`<${Btn} size="sm" kind="secondary" label="Go to the run step" onClick=${() => onStep("run")} />` : null;
  const hasMore = (it.ask && it.quote) || (it.facts || []).length;
  return html`<li class=${`wb-fbl-i st-${it.state}`}>
    <button type="button" class=${`wb-fbl-tick ${done ? "on" : ""}`} aria-pressed=${it.manual} disabled=${busy}
      title=${it.manual ? "Untick" : "Tick it off yourself"} onClick=${tick}>${done ? "✓" : n}</button>
    <div class="wb-fbl-b">
      <p class="wb-fbl-ask">${it.ask || html`<span class="faint">The reviewer:</span> ${it.quote}`}</p>
      <div class="wb-fbl-meta">
        <span class=${`pill ${tone}`}>${it.manual ? "Ticked" : word}</span>
        <span class="wb-fbl-kind">${KIND[it.kind] || it.kind}${(it.fields || []).length ? ` · ${(it.fields || []).map((f) => NAME[f] || f).join(", ")}` : ""}</span>
      </div>
      ${it.why && !it.manual ? html`<p class="wb-fbl-why">${it.why}</p>` : null}
      ${where && !done ? html`<p class="wb-fbl-yours"><span class="label-sm">Your ${NAME[where.field]}</span> “${where.text}”</p>` : null}
      <div class="wb-fbl-acts">${whereBtn}${fieldBtn}${attachBtns}${runBtn}
        ${hasMore ? html`<button type="button" class="wb-asks-btn" aria-expanded=${more} onClick=${() => setMore(!more)}>${more ? "Less" : "The reviewer's words"} <span class="wb-tri">${more ? "▾" : "▸"}</span></button>` : null}</div>
      ${more && it.ask && it.quote ? html`<blockquote class="wb-fbl-q">${it.quote}</blockquote>` : null}
      ${more && (it.facts || []).length ? html`<ul class="wb-fbl-facts">${it.facts.map((f) => html`<li>${f}</li>`)}</ul>` : null}
    </div>
  </li>`;
}

/** The checklist panel at the top of the Write step. */
export function FeedbackChecklist({ short, wb, onField, onStep, reload }) {
  const fb = wb.feedback || {};
  const p = fb.plan;
  const job = useLiveJob(["feedback_plan", "feedback_check"], short);
  const [showAll, setShowAll] = useState(false);
  const [full, setFull] = useState(false);
  async function post(what) {
    try {
      const r = await api.post(`/api/tasks/${short}/feedback/${what}`, {});
      if (r?.job) putJob(r.job);
    } catch (e) { toast(e?.message || "Couldn't start it", { level: "error" }); }
  }
  const words = html`<div class="wb-fbl-full">
    <button type="button" class="wb-asks-btn" aria-expanded=${full} onClick=${() => setFull(!full)}>The reviewer's full words <span class="wb-tri">${full ? "▾" : "▸"}</span></button>
    ${full ? html`<blockquote><${ReviewerWords} text=${fb.words} /></blockquote>` : null}
  </div>`;
  if (!p || p.state !== "ready") {
    const planning = !p || p.state === "planning" || job?.kind === "feedback_plan";
    return html`<section class="wb-fbl" id="feedback" role="status">
      <div class="label-sm">What the reviewer asked</div>
      ${planning ? html`<p class="wb-fbl-busy"><span class="dot busy"></span> Making a checklist of what they asked${job ? `: ${jobLine(job) || "waiting"}` : ""}.</p>`
        : html`<p class="wb-fbl-busy">The checklist couldn't be made: ${p?.error || "no answer"}.
            <${Btn} size="sm" kind="secondary" label="Try again" onClick=${() => post("plan")} /></p>`}
      <blockquote><${ReviewerWords} text=${fb.words} /></blockquote>
    </section>`;
  }
  const items = p.items || [];
  const open = items.filter((i) => !["done", "addressed"].includes(i.state));
  const shown = showAll ? items : open.length ? open : items;
  const hidden = items.length - shown.length;
  const rv = p.review || {};
  const chk = p.check;
  const checking = job?.kind === "feedback_check";
  return html`<section class="wb-fbl" id="feedback">
    <div class="wb-fbl-h">
      <div>
        <div class="label-sm">What the reviewer asked</div>
        <h3 class="wb-fbl-n">${open.length ? `${open.length} of ${items.length} to do` : `All ${items.length} look done`}</h3>
      </div>
      <div class="wb-fbl-check">
        ${checking ? html`<span class="faint"><span class="dot busy"></span> Checking your fields</span>`
          : html`<${Btn} size="sm" kind=${chk?.stale || !chk ? "secondary" : "quiet"} label="Check my changes"
              title="Reads your fields now and marks what each item still needs. It never rewrites anything." onClick=${() => post("check")} />`}
        <span class="faint wb-fbl-when">${chk?.error ? `Last check failed: ${chk.error}` : chk?.at ? `Checked ${ago(chk.at)}${chk.stale ? ", you've changed fields since" : ""}` : "Not checked yet"}</span>
      </div>
    </div>
    ${rv.difficulty_ok || rv.understanding_ok ? html`<p class="wb-fbl-rv">
      ${rv.difficulty_ok ? html`<span>Difficulty: <b>${rv.difficulty_ok}</b></span>` : null}
      ${rv.understanding_ok ? html`<span>Codebase understanding: <b>${rv.understanding_ok}</b></span>` : null}
    </p>` : null}
    ${p.summary ? html`<p class="wb-fbl-sum"><${AiTag} label="Summary" /> ${p.summary}</p>` : null}
    ${p.drop_question ? html`<${Flag} tone="wait" title="Could this task be dropped?">${p.drop_question}<//>` : null}
    ${(p.studio_only || []).length ? html`<p class="wb-fbl-studio">Your ${p.studio_only.map((f) => f.label).join(", ")} ${p.studio_only.length > 1 ? "are" : "is"} only in Studio, so edit ${p.studio_only.length > 1 ? "them" : "it"} there. The check reads Studio's copy.
      ${wb.studio?.url ? html` <a href=${wb.studio.url} target="_blank" rel="noopener">Open Studio</a>` : null}</p>` : null}
    <ol class="wb-fbl-list">${shown.map((it) => html`<${Item} key=${it.id} it=${it} n=${items.indexOf(it) + 1} short=${short} wb=${wb}
        onField=${onField} onStep=${onStep} reload=${reload} />`)}</ol>
    ${hidden > 0 || showAll ? html`<button type="button" class="wb-asks-btn" onClick=${() => setShowAll(!showAll)}>${showAll ? "Hide the done ones" : `Show ${hidden} done`}</button>` : null}
    ${words}
    <p class="wb-fbl-foot faint"><${AiTag} label="AI checklist" /> Claude split the review into items and checks your fields against them. It never writes your fields.
      <button type="button" class="linkbtn" onClick=${() => post("plan")}>Make it again</button></p>
  </section>`;
}
