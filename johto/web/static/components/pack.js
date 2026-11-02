import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { api } from "../lib/api.js";
import { toast, tick } from "../lib/store.js";
import { ago, clock } from "../lib/format.js";

/**
 * Review pack for one run: builds a repomix file (≤ 200k tokens) with the task, Claude's approach,
 * the diff, the code and an outside review, for pre-grading in another model.
 */
export function PackButton({ short, run, sm = true }) {
  const [job, setJob] = useState(null);
  const [latest, setLatest] = useState(null);
  const timer = useRef(null);

  async function refreshList() {
    try {
      const r = await api.get(`/api/tasks/${short}/packs`);
      setLatest((r.packs || []).find((p) => p.run === run) || null);
      const live = (r.running || []).find((j) => j.run === run);
      if (live) { setJob(live); poll(live.id); }
    } catch { /* demo mode or no runs */ }
  }
  useEffect(() => { if (short && run) refreshList(); return () => clearTimeout(timer.current); }, [short, run]);

  function poll(id) {
    clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      try {
        const { job: j } = await api.get(`/api/packs/${id}`);
        setJob(j);
        // The server announces "Review pack ready" or "failed" (with the reason) as a toast; don't repeat it.
        if (j.state === "running") poll(id);
        else refreshList();
      } catch (e) { toast(e.message, { level: "error" }); }
    }, 2000);
  }

  async function start() {
    try {
      const { job: j } = await api.send("POST", `/api/tasks/${short}/runs/${run}/pack`, {});   // outside review: settings pack_outside_review
      setJob(j); poll(j.id);
    } catch (e) { toast(e.message, { level: "error" }); }
  }
  async function act(kind) {
    try { const r = await api.send("POST", `/api/tasks/${short}/packs/${latest.name}/${kind}`); toast(r.toast?.text || "Done", { level: "ok" }); }
    catch (e) { toast(e.message, { level: "error" }); }
  }

  if (!short || !run) return null;
  const running = job?.state === "running";
  if (running) tick.value;
  return html`<span class="pack" style="display:inline-flex;align-items:center;gap:6px;min-width:0">
    ${running ? html`<span class="sm faint" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:360px" title=${(job.log || []).join("\n")}>Review pack building · <span class="num">${clock(job.started_at)}</span></span>`
      : html`<button class=${`btn ${sm ? "sm" : ""}`} onClick=${start} title="Task, Claude's approach, the diff, the code and an outside review, in one file (≤ 200k tokens) for pre-grading">Review pack</button>`}
    ${latest && !running ? html`
      <button class=${`btn quiet ${sm ? "sm" : ""}`} onClick=${() => act("copy")} title=${latest.path}>Copy</button>
      <button class=${`btn quiet ${sm ? "sm" : ""}`} onClick=${() => act("reveal")}>Finder</button>
      <span class="sm faint">${ago(latest.made_at)}</span>` : null}
  </span>`;
}
