// Now (spec.md 3.1): what should I do next? One sentence (the lede), one focus card for the top queue item with its
// body by kind, up to three "After this" rows, the three best open slots to take (johto/slot_options.py), the
// strongest ready repo when there are none and the lane is free, and what reviewers said. Built from GET /api/now (johto/web/api_now.py); the queue's tiers and order are the server's.
//
// ⌘↵ (and ⌘⇧↵) do the focus card's one button while Now is mounted (setNextHandler / setPrimaryHandler).
// Claude's question shows here, but the answer is typed in the lane: "Answer in the lane" opens the drawer with the
// caret in the terminal. The app types only his saved prompt, /login and /exit, as before (spec 13.1).
import { useEffect, useRef, useState } from "preact/hooks";
import { effect } from "@preact/signals";
import { html } from "../lib/html.js";
import { api } from "../lib/api.js";
import { act } from "../lib/actions.js";
import { app, lane, queue, tick, navigate, openDrawer, select, selectedId, toast, fieldFocused, markSurfaceChange, jobs, isLive } from "../lib/store.js";
import { pushContext } from "../lib/keys.js";
import { clock, mins } from "../lib/format.js";
import { Stage, Lede, Btn, Dot, Pill, SlotPill, AiTag, SectionHead, Sheet, Empty, PathStrip, Card, Seg } from "../components/primitives.js";
import { focusTerminal } from "../components/drawer.js";
import { post, startDrop, cancelDrop, drops } from "./surfaces/decide.js";
import { setNextHandler, setPrimaryHandler, hideItem } from "../app.js";
import { ReviewText, LESSON_WORDS, LESSON_TONE } from "./finished.js";
import { slotWords } from "../lib/words.js";
import { setFacets } from "./discover/data.js";

const kindShort = (id) => (id || "").split(":").slice(0, 2).join(":");
const sameItem = (a, b) => !!a && !!b && (a === b || (kindShort(a) === kindShort(b) && kindShort(a).includes(":")));

/** "about 25 min" from seconds. */
export function about(s) {
  if (s == null) return "";
  const m = Math.max(1, Math.round(s / 60));
  return m >= 90 ? `about ${Math.round(m / 60)} h` : `about ${m} min`;
}

/** What the queue and lane look like, as one string: Now reads again when it changes. */
function stateKey() {
  const a = app.value;
  if (!a) return "";
  const l = a.lane || {};
  const m = l.monitor || {};
  return [
    (a.queue || []).map((q) => q.id).join(","),
    l.state, l.since, l.run?.run, m.turn_class, m.turns, l.input?.empty,
    (a.tasks || []).map((t) => `${t.short}:${t.stage}:${t.studio?.status || ""}`).join(","),
    a.server?.mode,
    // A slot rating that finished changes the picks; slot research that starts, moves on or ends changes them too.
    [...jobs.value.values()].filter((j) => j.kind === "slot_options" && !isLive(j)).length,
    [...jobs.value.values()].filter((j) => j.kind === "slot_research").map((j) => `${j.state}:${j.progress?.step || ""}`).join(","),
  ].join("|");
}

// ---------- Actions: one function per action id the server names ----------

let endArmedAt = 0;

export async function runAction(a) {
  if (!a) return;
  const args = a.args || {};
  const show = (r, fallback) => { if (r == null) return; if (r.toast) toast(r.toast.text, r.toast); else if (fallback) toast(fallback); };
  switch (a.action) {
    case "lane_open":
      openDrawer("row"); setTimeout(() => focusTerminal(), 160); return;
    case "lane_type": {
      const r = await act("/api/lane/type", { what: args.what }, { quiet: true });
      show(r, args.what === "login" ? "Login started. Finish it in the browser tab it opens."
        : "Pasted your saved prompt. Press Enter in the lane to send it.");
      if (r != null) { openDrawer("row"); setTimeout(() => focusTerminal(), 160); }
      return;
    }
    case "lane_start": {
      try {
        const r = await api.send("POST", "/api/lane/start", { short: args.short, kind: args.kind || "validate" });
        show(r, "Setting up the lane");
      } catch (e) {
        // The lane wants the prompt in Studio and checked first: the task's Prompt step offers that button.
        if (["prompt_qc", "sync_conflict", "same_prompt"].includes(e.code) && args.short) {
          const reason = e.code === "prompt_qc" ? (e.details?.reason || "differs") : e.code === "sync_conflict" ? "conflict" : "same_prompt";
          navigate(`/task/${args.short}`, `#step=prompt&gate=${reason}`);
          return;
        }
        toast(e.message || "The run didn't start.", { level: "error" });
      }
      return;
    }
    case "lane_end": {
      if (Date.now() - endArmedAt > 3000) { endArmedAt = Date.now(); toast("Press again to end the run. The app types /exit, then collects it.", { id: "end-arm", ms: 3000 }); return; }
      endArmedAt = 0;
      const r = await act("/api/lane/end", {}, { quiet: true });
      show(r, "Ending the run. It is collected next.");
      return;
    }
    case "collect":
      await act(`/api/tasks/${args.short}/collect`, { run: args.run, pack: true }, { what: "Collecting the run" }); return;
    case "studio_open":
      window.open(args.href || "https://studio.mercor.com/", "_blank", "noopener");
      await act("/api/studio/wake", {}, { quiet: true });
      return;
    case "open": {
      const href = args.href || "/";
      const at = href.indexOf("#");
      navigate(at < 0 ? href : href.slice(0, at), at < 0 ? "" : href.slice(at));
      return;
    }
    case "decision":
      await post(`/api/tasks/${args.short}/decision`, { run: args.run, decision: args.decision }); return;
    case "discard_form":
      if (args.url) window.open(args.url, "_blank", "noopener");
      return;
    default:
  }
}

/** Where an After row's Open goes: the task page, the lane, or this card (it becomes the focus). */
function openItem(it, pinIt) {
  const p = it.place || {};
  if (p.kind === "task" && p.href) return runAction({ action: "open", args: { href: p.href } });
  if (p.kind === "lane") return runAction({ action: "lane_open" });
  pinIt(it.id);
  document.querySelector(".stage")?.scrollTo({ top: 0, behavior: "smooth" });
}

// ---------- Pieces ----------

/** The task's four steps as a quiet strip (the card's own path); a step opens the task there. */
function MiniPath({ steps, short }) {
  if (!steps?.length || !short) return null;
  return html`<div class="nw-path"><${PathStrip} steps=${steps} value=${null}
    onPick=${(id) => runAction({ action: "open", args: { href: `/task/${short}#step=${id}` } })} /></div>`;
}

function Primary({ item, big = true }) {
  const p = item?.primary;
  if (!p) return null;
  return html`<${Btn} kind="primary" size=${big ? "lg" : "md"} label=${p.label} k="⌘↵" onClick=${() => runAction(p)} />`;
}

/** Claude's last message, in its own words with code taken out (AI text: lilac). */
function ClaudeSays({ body }) {
  const [all, setAll] = useState(false);
  const msg = body.message || [];
  const q = body.question;
  if (!msg.length && !q) {
    return html`<div class="nw-says">
      <div class="nw-says-head"><${AiTag} label="Claude's message" /></div>
      <p class="nw-says-none">${body.local ? "Open the lane to read what Claude said." : "Claude's message is in the lane."}</p>
    </div>`;
  }
  return html`<div class="nw-says">
    <div class="nw-says-head"><${AiTag} label="Claude's message" />
      ${body.full ? html`<button type="button" class="linkbtn" onClick=${() => setAll(true)}>The whole message</button>` : null}</div>
    ${msg.map((p) => html`<p>${p}</p>`)}
    ${q ? html`<blockquote class="nw-ask">${q}</blockquote>` : null}
    <${Sheet} open=${all} onClose=${() => setAll(false)} tag="Claude's message" title=${body.context} width=${680}>
      <div class="nw-full">${(body.full || []).map((p) => html`<p>${p}</p>`)}</div>
    <//>
  </div>`;
}

function LaneFocus({ item }) {
  tick.value;
  const b = item.body || {};
  const talk = ["lane_waiting", "lane_answer", "lane_problem", "lane_async_done"].includes(item.eff_kind);
  const waiting = b.since ? clock(b.since) : "";
  const over = b.claude_min != null && b.budget && b.claude_min >= b.budget;
  const close = b.claude_min != null && b.budget && b.claude_min >= b.budget * 2 / 3;
  const left = html`<div class="nw-side">
    <div class="nw-context">${b.context}</div>
    <div class="nw-status">
      ${waiting ? html`<${Pill} tone=${item.tone === "stop" ? "stop" : "wait"}>Waiting <span class="num">${waiting}</span><//>` : null}
      ${b.claude_min != null ? html`<span class=${`nw-budget ${over ? "stop" : close ? "wait" : ""}`}>Claude <b class="num">${mins(b.claude_min)}</b> of ${b.budget} min</span>` : null}
    </div>
    ${b.sentence ? html`<p class="nw-sentence">${b.sentence}</p>` : null}
    ${b.prompt ? html`<div class="nw-prompt"><div class="nw-prompt-h">Your opening prompt</div><p>${b.prompt}</p></div>` : null}
    ${b.facts?.length ? html`<div class="nw-turn">${b.facts.map((f) => html`<span>${f}</span>`)}</div>` : null}
    ${!b.local ? html`<p class="nw-sentence faint">The ${b.host || "PC"} lane is waiting for you.</p>` : null}
  </div>`;
  const acts = html`<div class="nw-acts">
    <${Primary} item=${item} />
    ${b.local && ["lane_answer", "lane_async_done"].includes(item.eff_kind)
      ? html`<${Btn} kind="quiet" label="End the run" title="Types /exit in the lane, then collects the run (press twice)"
          onClick=${() => runAction({ action: "lane_end" })} />` : null}
  </div>`;
  // Wide: the button under the context, Claude's message beside both. Narrow: the message, then the button.
  if (!talk) return html`<article class=${`nw-focus tone-${item.tone} solo`}>${left}${acts}</article>`;
  return html`<article class=${`nw-focus tone-${item.tone} split`}>${left}<${ClaudeSays} body=${b} />${acts}</article>`;
}

function WritingFocus({ item }) {
  const b = item.body || {};
  return html`<article class=${`nw-focus tone-${item.tone} solo`}>
    <div class="nw-row">
      <div class="nw-main">
        <h2 class="nw-field">${b.field_words || item.verb}</h2>
        <div class="nw-context">${item.what}</div>
        ${b.quote ? html`<blockquote class="nw-quote">${b.quote}</blockquote>` : null}
        ${b.fact ? html`<p class="nw-fact">${b.fact}</p>` : null}
        ${b.checklist?.todo && b.checklist?.first ? html`<p class="nw-fb-next"><span class="label-sm">First</span> ${b.checklist.first}</p>` : null}
      </div>
      <div class="nw-go">
        <${Primary} item=${item} />
        ${item.est_s ? html`<span class="nw-est">${about(item.est_s)}</span>` : null}
      </div>
    </div>
    <${MiniPath} steps=${b.path} short=${item.short} />
  </article>`;
}

const BLOCKER_HELP = {
  blocker_docker: "Start Docker Desktop. The lane starts once it is up.",
  blocker_disk: "Free some space on this Mac. Lanes need about 10 GB.",
  blocker_gh: "Uploads to Studio wait until GitHub is signed in again.",
  blocker_studio: "Sign in to Studio in its tab. Waiting Studio steps run once it has a session.",
  wrong_model: "Open the lane and switch the model before Claude's next reply.",
  setup_failed: "A fresh folder usually fixes it.",
  collect_failed: "Collecting copies the conversation and changes out of the lane.",
};

function BlockerFocus({ item }) {
  const help = BLOCKER_HELP[item.eff_kind];
  return html`<article class="nw-focus tone-stop solo">
    <div class="nw-row">
      <div class="nw-main">
        ${item.short ? html`<div class="nw-context">${item.what}</div>` : null}
        <p class="nw-what">${item.fact}</p>
        ${help ? html`<p class="nw-fact">${help}</p>` : null}
      </div>
      <div class="nw-go"><${Primary} item=${item} /></div>
    </div>
  </article>`;
}

function StartFocus({ item }) {
  const b = item.body || {};
  return html`<article class="nw-focus tone-ink solo">
    <div class="nw-row">
      <div class="nw-main">
        <h2 class="nw-field">Validation run</h2>
        <div class="nw-context">${item.what}</div>
        <p class="nw-fact">${b.busy_with ? `The lane is running ${b.busy_with}. A second lane starts beside it.`
          : "Your opening prompt is saved. The goal can wait until Claude is working."}</p>
      </div>
      <div class="nw-go"><${Primary} item=${item} /></div>
    </div>
    <${MiniPath} steps=${b.path} short=${item.short} />
  </article>`;
}

function DecideFocus({ item }) {
  drops.value;
  const b = item.body || {};
  const short = item.short;
  const dropping = !!drops.value[short];
  return html`<article class="nw-focus tone-ink solo">
    <div class="nw-row">
      <div class="nw-main">
        <h2 class="nw-field">Keep or drop</h2>
        <div class="nw-context">${item.what}${b.run_words ? ` · ${b.run_words}` : ""}</div>
        ${b.facts?.length ? html`<p class="nw-fact">${b.facts.join(" · ")}</p>` : null}
      </div>
      <div class="nw-go">
        <${Primary} item=${item} />
        ${dropping ? html`<${Btn} kind="danger" label="Cancel the drop" onClick=${() => cancelDrop(short)} />`
          : html`<${Btn} kind="danger" label="Drop" title="Unclaims the task in Studio after 10 seconds. You can undo until then."
              onClick=${() => startDrop({ short, run: item.run, label: `unclaims ${item.what} in Studio` })} />`}
        <${Btn} kind="quiet" label="Edit the prompt" onClick=${() => post(`/api/tasks/${short}/decision`, { run: item.run, decision: "rework" })} />
      </div>
    </div>
  </article>`;
}

function LogDiscardFocus({ item }) {
  const b = item.body || {};
  const [opened, setOpened] = useState(false);
  const form = () => { runAction(item.primary); setOpened(true); };
  return html`<article class="nw-focus tone-ink solo">
    <div class="nw-row">
      <div class="nw-main">
        <div class="nw-context">${item.what}</div>
        ${b.questions?.length ? html`<p class="nw-fact">You answer these in the form. The app fills in the rest.</p>
          <ol class="nw-qs">${b.questions.map((q) => html`<li>${q}</li>`)}</ol>` : null}
      </div>
      <div class="nw-go">
        ${item.primary ? html`<${Btn} kind=${opened ? "secondary" : "primary"} size="lg" label="Open the form ↗" k=${opened ? null : "⌘↵"} onClick=${form} />` : null}
        <${Btn} kind=${opened ? "primary" : "quiet"} label="I sent it" title="Takes this off Now"
          onClick=${() => post(`/api/tasks/${item.short}/discard-logged`, {})} />
      </div>
    </div>
  </article>`;
}

function FocusCard({ item }) {
  if (!item) return null;
  switch (item.kind_card) {
    case "lane": return html`<${LaneFocus} item=${item} />`;
    case "blocker": return html`<${BlockerFocus} item=${item} />`;
    case "start": return html`<${StartFocus} item=${item} />`;
    case "decide": return html`<${DecideFocus} item=${item} />`;
    case "log_discard": return html`<${LogDiscardFocus} item=${item} />`;
    default: return html`<${WritingFocus} item=${item} />`;
  }
}

function AfterRows({ items, pinIt }) {
  if (!items?.length) return null;
  return html`<section class="nw-sec">
    <${SectionHead} title="After this" serif=${false} />
    <div class="nw-after">
      ${items.map((it) => html`<div class="nw-arow" key=${it.id}>
        <${Dot} tone=${it.tone} />
        <div class="nw-averb"><b>${it.verb}</b><span>${it.what}</span></div>
        <div class="nw-afact">${it.fact}</div>
        <div class="nw-aest">${it.est_s ? about(it.est_s) : ""}</div>
        <${Btn} size="sm" label="Open" onClick=${() => openItem(it, pinIt)} />
        <button type="button" class="nw-x" title=${it.lane ? "Hold its notifications for 10 minutes" : "Hide until it changes"}
          aria-label="Hide" onClick=${() => hideItem(it)}>×</button>
      </div>`)}
    </div>
  </section>`;
}

/** Keep the lane busy: the strongest ready repo, when the lane is free and few tasks wait before their run. */
function IdlePick({ pick, lead }) {
  if (!pick) return null;
  const open = () => navigate(`/repos/${pick.id}`);
  return html`<section class="nw-sec">
    ${lead ? null : html`<${SectionHead} title="Keep the lane busy" serif=${false} />`}
    <div class=${`nw-pick ${lead ? "lead" : ""}`}>
      <div class="nw-pick-main">
        <h2>${pick.name} is ready.</h2>
        ${pick.line ? html`<p class="nw-pick-line"><${AiTag} label=${pick.brief ? "Claude's research" : "Claude's research, brief coming"} />${pick.line}</p>` : null}
        ${pick.fit ? html`<div class="nw-pick-fit"><${SlotPill} slot=${pick.fit} count=${pick.fit.open} /></div>` : null}
      </div>
      <div class="nw-go">
        <${Btn} kind=${lead ? "primary" : "secondary"} size=${lead ? "lg" : "md"} label=${`Open ${pick.name}`} k=${lead ? "⌘↵" : null} onClick=${open} />
        <${Btn} kind="quiet" label="All repos" onClick=${() => navigate("/repos")} />
      </div>
    </div>
  </section>`;
}

/** Where a slot pick opens: its problem on the repo's page, with that slot already picked in the claim panel. */
export function pickLink(p) {
  const s = p.slot || {};
  const slot = [s.action, s.domain, s.codebase_type, s.prompt_style, s.interaction_mode].join("|");
  return [`/repos/${p.dossier_id}`, `#${new URLSearchParams({ seed: p.opportunity_id, slot })}`];
}

const leftWords = (n) => (n === 1 ? "1 slot left" : n <= 3 ? `${n} slots left` : `${n} open`);

/** The three best open slots of the kind he prefers, each with the researched problem Claude rated best for it,
 * then the slots with no great option and the research aimed at them (johto/slot_research.py). */
function SlotPicks({ picks, onChange }) {
  const items = picks?.items || [];
  const research = picks?.research || null;
  const weak = research?.weak || [];
  if (!items.length && !picks?.rating && !weak.length && !research?.running) return null;
  const more = (p) => { setFacets({ action: p.slot.action, domain: p.slot.domain, codebase_type: p.slot.codebase_type }); navigate("/repos"); };
  const note = picks.rating ? "Claude is rating new options…"
    : !items.length ? "Casual, async" : items.every((p) => p.rated) ? "Casual, async · ranked by Claude" : "Casual, async · not rated yet";
  return html`<section class="nw-sec nw-slots-sec">
    <${SectionHead} title="Slots worth taking" serif=${false} right=${html`<span class="nw-slots-note">${note}</span>`} />
    ${items.length ? html`<div class="nw-slots">
      ${items.map((p) => html`<${Card} ai=${true} class="nw-slot" key=${`${p.dossier_id}#${p.opportunity_id}`}
          title=${`Open ${p.name}`} onClick=${() => navigate(...pickLink(p))}>
        <div class="nw-slot-top">
          <span class="nw-slot-what">${slotWords(p.slot)}</span>
          <span class=${`nw-slot-left ${p.scarce ? "scarce" : ""}`}>${leftWords(p.open)}</span>
        </div>
        <h3>${p.title}</h3>
        <p class="nw-slot-repo">${p.name}</p>
        ${p.why ? html`<p class="nw-slot-why">${p.why}</p>` : null}
        ${p.edge && p.edge_why ? html`<p class=${`nw-slot-edge ${p.edge > 0 ? "up" : "down"}`}>
          <b>${p.edge > 0 ? "Your edge" : "Harder for you"}</b> ${p.edge_why}</p>` : null}
        ${p.more ? html`<button type="button" class="linkbtn nw-slot-more" onKeyDown=${(e) => e.stopPropagation()}
          onClick=${(e) => { e.stopPropagation(); more(p); }}>
          ${p.more === 1 ? "1 other option" : `${p.more} other options`} for this slot</button>` : null}
      <//>`)}
    </div>` : picks.rating ? html`<p class="nw-slots-wait">Claude is rating the researched problems for each open slot.</p>` : null}
    ${research ? html`<${WeakSlots} research=${research} onChange=${onChange} />` : null}
  </section>`;
}

/** What one weak slot's research is doing, in a few words. */
function weakState(g, r) {
  const run = r.running;
  if (run && run.group === g.key) return `Researching ${short(run.repo)}…`;
  const last = g.last;
  if (last?.state === "failed") return `Research on ${short(last.repo)} failed`;
  if (last?.state === "nothing") return "No vetted repo fits yet";
  if (last?.state === "ready") return `Researched ${short(last.repo)}`;
  if (r.mode !== "auto") return "";
  return run ? "Next in line" : r.lane_busy ? "Starts when Claude finishes" : "Starts soon";
}
const short = (repo) => (repo || "").split("/").pop();

/** Open slots whose best option is under 8 (or that have none), and research aimed at them: auto starts it,
 * manual waits for the button. */
function WeakSlots({ research, onChange }) {
  const weak = research.weak || [];
  const [busy, setBusy] = useState(false);
  const setMode = async (m) => {
    if (m === research.mode) return;
    try { await api.post("/api/slots/research-mode", { mode: m }); onChange?.(); } catch (e) { toast(e.message); }
  };
  const go = async (g) => {
    setBusy(true);
    try {
      const r = await api.post("/api/slots/research", { group: g.key });
      if (r?.state === "nothing") toast("No vetted repo fits this slot yet.");
      onChange?.();
    } catch (e) { toast(e.message); } finally { setBusy(false); }
  };
  if (!weak.length && !research.running) return null;
  return html`<div class="nw-weak">
    <div class="nw-weak-head">
      <span>${weak.length ? `No great option yet for ${weak.length === 1 ? "1 slot" : `${weak.length} slots`}` : "Research for a slot"}</span>
      <${Seg} size="sm" label="Research for these slots" value=${research.mode} onChange=${setMode}
        options=${[{ value: "auto", label: "Auto", hint: "Claude researches a fitting repo by itself, one at a time" },
                   { value: "manual", label: "Manual", hint: "Research starts only when you press the button" }]} />
    </div>
    ${weak.map((g) => {
      const state = weakState(g, research);
      const mine = research.running?.group === g.key;
      return html`<div class="nw-weak-row" key=${g.key}>
        <span class="nw-weak-what">${slotWords(g)}</span>
        <span class="nw-weak-best">${g.best == null ? "no option" : `best ${g.best}/10`} · ${leftWords(g.open)}</span>
        <span class="nw-weak-state">${state}</span>
        ${mine ? null : html`<${Btn} size="sm" kind="quiet" disabled=${busy || !!research.running}
          title=${research.running ? "One research run at a time" : "Research the best-fitting repo for this slot now"}
          label=${g.last ? "Research another repo" : "Research a repo"} onClick=${() => go(g)} />`}
      </div>`;
    })}
    ${research.running && !weak.some((g) => g.key === research.running.group)
      ? html`<p class="nw-slots-wait">${`Researching ${short(research.running.repo)} for ${research.running.group.split("|").join(" · ")}…`}</p>` : null}
  </div>`;
}

/** What reviewers said: the improvement sentence from Studio feedback (human words), one card per task. */
function Lessons({ lessons, onSeen }) {
  const [open, setOpen] = useState(null);
  if (!lessons?.length) return null;
  const cur = lessons.find((l) => l.short === open);
  return html`<section class="nw-sec">
    <${SectionHead} title="What reviewers said" serif=${false} />
    <div class="nw-lessons">
      ${lessons.map((l) => html`<article class="nw-lesson" key=${l.short}>
        <div class="nw-lesson-top"><${Pill} tone=${LESSON_TONE[l.state]}>${LESSON_WORDS[l.state]}<//><span>${l.label}</span></div>
        <p>${l.sentence}</p>
        <div class="nw-lesson-foot">
          <button type="button" class="linkbtn" onClick=${() => setOpen(l.short)}>Full review</button>
          <${Btn} kind="quiet" size="sm" label="Hide" title="It stays on Submitted" onClick=${() => onSeen(l.short)} />
        </div>
      </article>`)}
    </div>
    <${Sheet} open=${!!cur} onClose=${() => setOpen(null)} title=${cur?.label} width=${640}
      meta=${cur ? html`<${Pill} tone=${LESSON_TONE[cur.state]}>${LESSON_WORDS[cur.state]}<//>` : null}>
      ${cur ? html`<${ReviewText} text=${cur.full} />` : null}
    <//>
  </section>`;
}

// ---------- The screen ----------

export default function Now() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [pin, setPin] = useState(null);
  const [bump, setBump] = useState(0);
  const [hidden, setHidden] = useState(() => new Set());
  const live = useRef({});
  const key = stateKey();
  const mode = app.value?.server?.mode;

  // Read /api/now when the queue or lane changes (debounced: several events often arrive together).
  useEffect(() => {
    let gone = false;
    const t = setTimeout(async () => {
      try {
        const r = await api.get(`/api/now${pin ? `?focus=${encodeURIComponent(pin)}` : ""}`);
        if (gone) return;
        setD(r); setErr(null);
        if (pin && !sameItem(r.focus?.id, pin)) setPin(null);
      } catch (e) { if (!gone) setErr(e.message); }
    }, d ? 150 : 0);
    return () => { gone = true; clearTimeout(t); };
  }, [key, pin, mode, bump]);

  // An explicit selection (a toast's link, j/k, the palette's queue rows, Open on an After row) pins that item.
  useEffect(() => {
    let first = true;
    return effect(() => {
      const v = selectedId.value;
      if (first) {
        // A /#item=<id> link (a toast, a notification) selected it before Now mounted.
        first = false;
        if (v && new URLSearchParams(location.hash.slice(1)).get("item") === v) setPin(v);
        return;
      }
      if (live.current.selfSelect === v) return;
      if (v) setPin(v);
    });
  }, []);

  // The focus is the selection, so x, t and o act on it.
  const focus = d?.focus || null;
  useEffect(() => {
    if (!focus?.id) return;
    live.current.selfSelect = focus.id;
    if (selectedId.peek() !== focus.id) select(focus.id);
    markSurfaceChange();
  }, [focus?.id]);

  // ⌘↵ and ⌘⇧↵ do the focus card's button; with nothing in focus, they open the best slot pick (else the
  // strongest ready repo).
  live.current.d = d;
  useEffect(() => {
    const go = () => {
      const cur = live.current.d;
      if (cur?.focus?.primary) return runAction(cur.focus.primary);
      if (cur?.focus?.place?.kind === "lane") return runAction({ action: "lane_open" });
      const sp = cur?.slot_picks?.items?.[0];
      if (!cur?.focus && sp) return navigate(...pickLink(sp));
      if (cur?.idle_pick) return navigate(`/repos/${cur.idle_pick.id}`);
      if (!cur?.focus) return navigate("/repos");
      return null;
    };
    setNextHandler(go);
    setPrimaryHandler(go);
    const pop = pushContext("now", { Enter: go });
    return () => { setNextHandler(null); setPrimaryHandler(null); pop(); };
  }, []);

  const seen = async (short) => {
    setHidden((h) => new Set([...h, short]));
    const r = await act(`/api/now/lessons/${encodeURIComponent(short)}/seen`, {}, { quiet: true });
    if (r == null) setHidden((h) => { const n = new Set(h); n.delete(short); return n; });
  };

  if (err && !d) return html`<${Stage} class="nw"><${Lede} title="Now couldn't load." support=${err} /><//>`;
  if (!d) return html`<${Stage} class="nw"><div class="nw-loading"></div><//>`;

  // Nothing moves under his hands: while a field has focus the After rows keep their order.
  let after = d.after || [];
  if (fieldFocused.value && live.current.afterIds) {
    const ids = live.current.afterIds;
    after = [...after].sort((a, b) => (ids.indexOf(a.id) + 1 || 99) - (ids.indexOf(b.id) + 1 || 99));
  } else live.current.afterIds = after.map((a) => a.id);

  const lessons = (d.lessons || []).filter((l) => !hidden.has(l.short));
  const pinned = pin && focus && queue.value[0] && !sameItem(queue.value[0].id, focus.id);
  return html`<${Stage} class="nw">
    <${Lede} title=${d.lede?.title} support=${d.lede?.support}
      right=${pinned ? html`<${Btn} kind="quiet" size="sm" label="Back to the top item" onClick=${() => { setPin(null); live.current.selfSelect = null; }} />` : null} />
    ${focus ? html`<${FocusCard} item=${focus} key=${focus.id} />` : null}
    ${!focus ? html`<${SlotPicks} picks=${d.slot_picks} onChange=${() => setBump((b) => b + 1)} />` : null}
    ${!focus && d.idle_pick ? html`<${IdlePick} pick=${d.idle_pick} lead=${true} />` : null}
    ${!focus && !d.idle_pick && !d.slot_picks && !after.length ? html`<${Empty} title="No repo is ready yet." body="Research finds the next ones."
      action=${html`<${Btn} label="Find work" onClick=${() => navigate("/repos")} />`} />` : null}
    <${AfterRows} items=${after} pinIt=${(id) => select(id)} />
    ${focus ? html`<${SlotPicks} picks=${d.slot_picks} onChange=${() => setBump((b) => b + 1)} />` : null}
    ${focus && d.idle_pick ? html`<${IdlePick} pick=${d.idle_pick} />` : null}
    <${Lessons} lessons=${lessons} onSeen=${seen} />
  <//>`;
}

/** Whether choosing a row opens the drawer on this Mac's lane. Not for a run on the PC (the drawer can't reach
 * it) and not for a validation run that hasn't started yet. */
export function opensLane(it) {
  if (it.lane) return true;
  if (it.surface !== "lane" || it.kind === "start_validation") return false;
  return !(it.host && it.host !== "Mac");
}
