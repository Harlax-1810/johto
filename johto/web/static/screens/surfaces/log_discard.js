import { useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { Surface } from "../../components/surface.js";
import { fullSlot, hhmm, mins } from "../../lib/format.js";
import { post, usePrimary, useKeys, runName, RunNotes, Stat } from "./decide.js";

const MONO = new Set(["Repo"]);
const SKIP = new Set(["Slot"]); // the header already shows the slot

// Kept at module level so moving to another row and back keeps where he is.
const opened = new Set();   // shorts whose form was opened in this session
const loggedAt = new Map(); // short -> ISO time "Mark logged" was pressed

/**
 * Discarded Run Log: the app fills the form's factual fields; the three free-text answers are his,
 * written in the form itself. Nothing here offers wording for them.
 * Until the form is opened, opening it is the primary; after that the primary is "Mark logged".
 */
export default function LogDiscard({ item, task, setPrimary }) {
  const s = task.summary;
  const d = task.discard || {};
  const facts = Object.entries(d.facts || {}).filter(([k]) => !SKIP.has(k));
  const questions = d.yours || [];
  const run = [...(task.runs || [])].reverse().find((r) => r.facts || r.notes?.length) || null;
  const prompt = task.sections?.opening_prompt?.text || "";
  const [wasOpened, setOpened] = useState(() => opened.has(s.short) || !d.url);
  const [doneAt, setDoneAt] = useState(() => d.logged_at || loggedAt.get(s.short) || null);

  const open = () => {
    if (!d.url) return;
    window.open(d.url, "_blank", "noopener");
    opened.add(s.short);
    setOpened(true);
  };
  async function logged() {
    if (doneAt) return;
    const r = await post(`/api/tasks/${s.short}/discard-logged`, {});
    if (!r) return;
    const at = r.logged_at || new Date().toISOString();
    loggedAt.set(s.short, at);
    setDoneAt(at);
  }
  const primaryRun = () => (wasOpened ? logged() : open());
  usePrimary(setPrimary, primaryRun);
  useKeys("log_discard", { f: open });

  const formBtn = { label: "Open the form ↗", key: "f", run: open, title: "Opens the Discarded Run Log with these fields filled in" };
  const primary = doneAt
    ? { label: `Logged ${hhmm(doneAt)}`, run: logged, disabled: true }
    : wasOpened
      ? { label: "Mark logged", run: logged, why: "Removes this row; press it after you submit the form" }
      : { label: formBtn.label, run: open, why: `${formBtn.title} (f)` };
  const secondary = d.url && (wasOpened || doneAt) ? [{ ...formBtn, kind: "quiet" }] : [];

  return html`<${Surface} title="Log the discarded run" label=${s.label} dossier=${s.dossier} slotLine=${fullSlot(s.slot)} reason=${item.reason}
      secondary=${secondary} primary=${primary}>
    <div class="sbody ld">
      <div class="scol ld-main">
        ${questions.length ? html`<section class="sa-sec">
          <div class="sa-h"><span class="label">You answer in the form</span></div>
          <ol class="ld-qs">${questions.map((q, i) => html`<li><span class="n num">${i + 1}</span><span>${q}</span></li>`)}</ol>
        </section>` : null}
        ${facts.length ? html`<section class="sa-sec">
          <div class="sa-h"><span class="label">Filled in for you</span></div>
          <dl class="ld-facts">
            ${facts.map(([k, v]) => html`<div class="ld-f"><dt>${k}</dt><dd class=${MONO.has(k) ? "mono" : ""}>${v}</dd></div>`)}
          </dl>
        </section>` : null}
      </div>
      ${prompt || run ? html`<div class="scol ld-side">
        ${prompt ? html`<section class="sa-sec">
          <div class="sa-h"><span class="label">Your opening prompt</span><span class="sm faint num">${task.sections.opening_prompt.words} words</span></div>
          <div class="ld-prompt">${prompt}</div>
        </section>` : null}
        ${run?.facts ? html`<section class="sa-sec">
          <div class="sa-h"><span class="label">${runName(run)}</span></div>
          <div class="sa-stats sm-stats">
            <${Stat} n=${run.facts.turns} unit=${run.facts.turns === 1 ? "your turn" : "your turns"} />
            <${Stat} n=${mins(run.facts.active_min)} unit="active min" />
            <${Stat} n=${mins(run.facts.wall_min)} unit="wall min" />
          </div>
        </section>` : null}
        <${RunNotes} notes=${run?.notes} />
      </div>` : null}
    </div>
  <//>`;
}
