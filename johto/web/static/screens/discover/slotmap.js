// The slot map sheet (spec.md 3.2): where Mercor's open slots are, by action and domain, one scale at a time.
// Clicking a cell filters Find work to that slot group and closes the sheet. With `count`, cells count only the slots
// matching his prompt and mode filters (`how`).
import { useEffect, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { putJob, waitJob } from "../../lib/store.js";
import { actionWord, domainWord, scaleWord, styleWord, modeWord, plural } from "../../lib/words.js";
import { Sheet, Seg, Btn } from "../../components/primitives.js";
import { slotMap, loadSlotMap, loadListing, syncSlots, keyOf } from "./data.js";

export function SlotMapSheet({ open, onClose, onPick, count, how, scale: scale0 }) {
  const [scale, setScale] = useState(null);
  const [syncing, setSyncing] = useState(false);
  useEffect(() => { if (open) { loadSlotMap(); setScale(null); } }, [open]);
  const m = slotMap.value;
  const n = (c) => (count ? count(c) : c.open);
  const cells = m?.cells || [];
  const scales = m?.scales || ["Enterprise-grade", "Personal-scale"];
  const total = (s) => cells.filter((c) => c.codebase_type === s).reduce((t, c) => t + n(c), 0);
  const sc = scale || (scales.includes(scale0) ? scale0 : total(scales[0]) >= total(scales[1]) ? scales[0] : scales[1]);
  const here = cells.filter((c) => c.codebase_type === sc);
  const cell = (a, d) => here.find((c) => c.action === a && c.domain === d);
  const doms = (m?.domains || []).filter((d) => here.some((c) => c.domain === d && n(c)));
  const only = count && how ? [styleWord(how.prompt_style), modeWord(how.interaction_mode)].filter(Boolean).join(", ").toLowerCase() : "";
  const sync = async () => {
    setSyncing(true);
    const r = await syncSlots();
    if (r?.job) { putJob(r.job); await waitJob(r.job); }
    await Promise.all([loadSlotMap(), loadListing()]);
    setSyncing(false);
  };
  const foot = html`<div class="smap-foot">
    <span class="faint">${syncing ? "Reading Studio…" : m?.synced_words || ""}</span>
    <${Btn} size="sm" label="Sync Studio now" onClick=${sync} disabled=${syncing} title="Reads the open slots from Studio. Nothing is written." />
  </div>`;
  return html`<${Sheet} open=${open} onClose=${onClose} title="Where the open slots are" width=${880} foot=${foot} label="Open slots">
    <div class="smap-top">
      <${Seg} label="Codebase" value=${sc} onChange=${setScale}
        options=${scales.map((s) => ({ value: s, label: `${scaleWord(s)} ${total(s)}` }))} />
      <span class="faint smap-hint">${only ? `Counting ${only} slots only. ` : ""}Pick a group to see the repos that fit it.</span>
    </div>
    ${m?.error ? html`<p class="faint">${m.error}</p>` : null}
    <div class="smap" role="grid" aria-label=${`Open slots, ${scaleWord(sc)}`}
      style=${`grid-template-columns: 112px repeat(${doms.length || 1}, minmax(0, 1fr))`}>
      <div class="smap-h"></div>
      ${doms.map((d) => html`<div class="smap-h" title=${d}>${domainWord(d)}</div>`)}
      ${(m?.actions || []).map((a) => html`
        <div class="smap-rl">${actionWord(a)}</div>
        ${doms.map((d) => {
          const c = cell(a, d);
          const k = c ? n(c) : 0;
          const repos = (c?.repos || []).length;
          return html`<button type="button" role="gridcell" class=${`smap-c ${k ? "" : "zero"}`} disabled=${!k}
              title=${k ? `${actionWord(a)} · ${domainWord(d)} · ${scaleWord(sc)}: ${k} open${repos ? `, ${plural(repos, "researched repo")} fit` : ""}` : ""}
              onClick=${() => onPick?.(keyOf(c))}>
            <span class="smap-n">${k || "·"}</span>
            ${repos ? html`<span class="smap-r">${plural(repos, "repo")}</span>` : null}
          </button>`;
        })}`)}
    </div>
  <//>`;
}
