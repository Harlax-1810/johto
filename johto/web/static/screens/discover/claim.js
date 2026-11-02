// The claim (spec.md 3.4): pinned at the inspector's foot. Studio is written only when he presses
// "Claim in Studio" (or C); the server's toast then offers a 30 second undo.
import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { toast, navigate, refresh, putJob, waitJob, jobs, jobLine, isLive, hash } from "../../lib/store.js";
import { slotSentence } from "../../lib/words.js";
import { Btn, SlotPill, Dot } from "../../components/primitives.js";
import { groupWords, sharedScale } from "./words.js";
import { loadListing, loadDetail, facets, matches } from "./data.js";

const gkey = (g) => `${g.action}|${g.domain}|${g.codebase_type}`;
const vkey2 = (v) => `${v.prompt_style}|${v.interaction_mode}`;
const vkey = (g, v) => `${gkey(g)}|${vkey2(v)}`;

/**
 * The default pick ({g, v} keys): the largest open variant that matches all his Find work filters, else one that
 * matches his prompt and mode filters, else the variant with the most open slots in the largest group.
 */
function best(fits, f = facets.value) {
  const how = { prompt_style: f.prompt_style, interaction_mode: f.interaction_mode };
  for (const want of [f, how]) {
    let top = null;
    for (const g of fits || []) {
      for (const v of g.variants || []) {
        if (v.open > 0 && matches({ ...g, ...v }, want) && (!top || v.open > top.n)) top = { g: gkey(g), v: vkey2(v), n: v.open };
      }
    }
    if (top) return { g: top.g, v: top.v };
  }
  for (const g of fits || []) {
    const v = (g.variants || []).find((x) => x.open > 0);
    if (v) return { g: gkey(g), v: vkey2(v) };
  }
  return null;
}

/** The slot a link asked for (#slot=Action|Domain|Codebase|Style|Mode, from Now's slot picks), when it is open here. */
function wanted(fits) {
  const k = hash.peek().get("slot");
  if (!k) return null;
  const parts = k.split("|");
  const g = (fits || []).find((x) => gkey(x) === parts.slice(0, 3).join("|"));
  const v = g && g.variants.find((x) => x.open > 0 && vkey2(x) === parts.slice(3).join("|"));
  return v ? { g: gkey(g), v: vkey2(v) } : null;
}

/** {g, v} keys -> the group and variant objects in this render's data (polling replaces the objects). */
function resolve(groups, k) {
  const g = k && groups.find((x) => gkey(x) === k.g);
  const v = g && g.variants.find((x) => vkey2(x) === k.v && x.open > 0);
  return g && v ? { g, v } : null;
}

/** What the picked slot means, folded: he has read it on every claim. */
function SlotMeans({ text }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  return html`<div class="cl-means">
    <button type="button" class="cl-more" aria-expanded=${open} onClick=${() => setOpen(!open)}>What this slot means ${open ? "▾" : "▸"}</button>
    ${open ? html`<p class="cl-sentence">${text}</p>` : null}
  </div>`;
}

/**
 * Claim panel. `what` is {kind: "seed", opportunity_id, title} or {kind: "place", place_id, name}. `fits` are the
 * slot groups it fits, largest first, each with variants. `claimRef.current` is set to the claim function (key C).
 */
export function ClaimPanel({ dossierId, repo, what, fits, used, claimRef }) {
  const groups = (fits || []).filter((g) => (g.variants || []).length);
  const ident = `${dossierId}|${what?.opportunity_id || ""}|${what?.place_id || ""}`;
  const [pick, setPick] = useState(() => wanted(groups) || best(groups));
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(null);       // progress line while the claim job runs
  const [alt, setAlt] = useState(null);         // {key, task}: Studio's next open task after "taken"
  useEffect(() => { setPick(wanted(groups) || best(groups)); setExpanded(false); setAlt(null); }, [ident]);
  const cur = resolve(groups, pick) || resolve(groups, best(groups));
  const scale = sharedScale(groups);
  const shown = expanded || groups.length <= 1 ? groups : groups.filter((g) => cur && gkey(g) === gkey(cur.g));
  const others = groups.length - shown.length;

  const st = useRef({});
  st.current = { cur, busy, alt };
  const claim = async () => {
    const { cur: c, busy: b, alt: a } = st.current;
    if (!c || b || used) return;
    const slot = { action: c.g.action, domain: c.g.domain, codebase_type: c.g.codebase_type };
    const origin = what.kind === "seed" ? { kind: "seed", opportunity_id: what.opportunity_id }
      : { kind: "place", place_id: what.place_id, name: what.name };
    const body = { lead_id: null, dossier_id: dossierId, repo, origin, slot, prompt_style: c.v.prompt_style,
                   interaction_mode: c.v.interaction_mode, expect_status: "Ready to Claim" };
    if (what.kind === "seed") body.opportunity_id = what.opportunity_id;
    if (a && a.key === vkey(c.g, c.v)) body.task_id = a.task.task_id;
    setAlt(null);
    setBusy("Claiming in Studio…");
    try {
      const r = await api.send("POST", "/api/claim", body);
      if (r?.toast && !r?.job) { toast(r.toast.text, r.toast); setBusy(null); return; }
      if (!r?.job) { setBusy(null); return; }
      putJob(r.job);
      const tick = setInterval(() => { const j = jobs.value.get(r.job.id); if (isLive(j)) setBusy(`${jobLine(j)}…`); }, 400);
      const j = await Promise.race([waitJob(r.job), new Promise((res) => setTimeout(() => res(null), 180000))]).finally(() => clearInterval(tick));
      if (!j || isLive(j)) { toast("The claim is still running. It shows on Now when it finishes."); setBusy(null); return; }
      if (j.state === "succeeded" || j.state === "done") {
        const short = j.result?.short;
        loadListing(); loadDetail(dossierId).catch(() => {});
        await refresh().catch(() => {});
        setBusy(null);
        if (short) navigate(`/task/${short}`, "#step=prompt");
        return;
      }
      const nextTask = j.error?.details?.alternative || null;
      if (j.error?.code === "taken") {
        if (nextTask) setAlt({ key: vkey(c.g, c.v), task: nextTask });
        toast(nextTask ? "Someone just took that slot. Press Claim in Studio again for the next one."
          : "Someone just took that slot, and no other is open in this group.", { level: "error" });
      } else {
        toast(j.error?.message || "The claim didn't go through.", { level: "error" });
      }
    } catch (e) { toast(e.message, { level: "error" }); }
    setBusy(null);
  };
  if (claimRef) claimRef.current = claim;

  if (used) {
    return html`<div class="cl">
      <p class="cl-head">Already yours, ${used.stage_word}.</p>
      <${Btn} kind="primary" size="lg" full=${true} label="Open the task" onClick=${() => navigate(`/task/${used.short}`)} />
    </div>`;
  }
  return html`<div class="cl">
    <p class="cl-head">${what.kind === "place" ? html`Your own idea in <b>${what.name}</b>. ` : null}Claim one open slot in Studio${scale ? html`<span class="cl-scale"> · all ${scale}</span>` : null}</p>
    ${!groups.length ? html`<p class="cl-none">No open slot fits this right now. Sync Studio from the slot map to check again.</p>` : html`
      <div class="cl-rows">
        ${shown.map((g) => html`<div class="cl-row">
          <span class="cl-grp">${scale ? groupWords(g) : `${groupWords(g)} · ${g.codebase_type.replace(/-grade|-scale/, "")}`}</span>
          <span class="cl-vars">${g.variants.map((v) => html`<${SlotPill} slot=${v} variant=${true} count=${v.open} size="sm"
            selected=${!!cur && gkey(cur.g) === gkey(g) && vkey2(cur.v) === vkey2(v)} disabled=${!v.open || !!busy}
            onClick=${() => setPick({ g: gkey(g), v: vkey2(v) })} />`)}</span>
        </div>`)}
        ${others > 0 ? html`<button type="button" class="cl-more" onClick=${() => setExpanded(true)}>
          ${others === 1 ? "1 other slot group fits" : `${others} other slot groups fit`} ▸</button>` : null}
      </div>
      ${cur ? html`<${SlotMeans} text=${slotSentence({ ...cur.g, ...cur.v })} />` : null}`}
    ${busy ? html`<div class="cl-busy"><${Dot} tone="work" /><span>${busy}</span></div>` : html`
      <${Btn} kind="primary" size="lg" full=${true} k="C" label="Claim in Studio" disabled=${!cur} onClick=${claim} />
      <p class="cl-undo">You can undo for 30 seconds.</p>`}
  </div>`;
}
