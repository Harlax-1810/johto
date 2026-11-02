// Find work data: GET /api/discover, GET /api/discover/{id}, GET /api/slots/map, and the research actions
// (decline, restore, refill, pause, slot sync). Owned by the discover builder.
import { useEffect } from "preact/hooks";
import { signal } from "@preact/signals";
import { api } from "../../lib/api.js";
import { toast } from "../../lib/store.js";

export const listing = signal(null);         // GET /api/discover
export const slotMap = signal(null);         // GET /api/slots/map
export const details = signal(new Map());    // id -> GET /api/discover/{id}
const dossiers = new Map();                  // id -> GET /api/dossiers/{id} (only for "See it in the code")

// The Find work filters: one choice per slot field, "" for any. Kept across visits (he sets them once).
// Until he changes them they show casual, async slots: the kind he prefers (2026-11-01).
export const FACETS = ["prompt_style", "interaction_mode", "codebase_type", "action", "domain"];
const NONE = Object.fromEntries(FACETS.map((k) => [k, ""]));
const PREFERRED = { ...NONE, prompt_style: "Casual prose", interaction_mode: "Async/autonomous" };
function readFacets() {
  try {
    const raw = localStorage.getItem("johto.fw.facets");
    if (raw == null) return { ...PREFERRED };
    const f = JSON.parse(raw);
    return Object.fromEntries(FACETS.map((k) => [k, typeof f?.[k] === "string" ? f[k] : ""]));
  } catch { return { ...PREFERRED }; }
}
export const facets = signal(readFacets());
export function setFacets(patch) {
  facets.value = patch ? { ...facets.value, ...patch } : { ...NONE };
  try { localStorage.setItem("johto.fw.facets", JSON.stringify(facets.value)); } catch {}
}
export const anyFacet = (f) => FACETS.some((k) => f[k]);

/** Does an open slot kind match the filters (`skip` names one filter to leave out, for that filter's own counts)? */
export const matches = (c, f, skip) => FACETS.every((k) => k === skip || !f[k] || c[k] === f[k]);

/** Only the filters on how he works (prompt style and mode), for a repo's own page where every group still shows. */
export const howOnly = (f) => ({ ...NONE, prompt_style: f.prompt_style, interaction_mode: f.interaction_mode });

/** Open slots in one fit ({action, domain, codebase_type, open, variants}) under the filters. */
export function fitOpen(fit, f) {
  if (!Array.isArray(fit?.variants)) return fit?.open || 0;
  return fit.variants.reduce((n, v) => n + (v.open > 0 && matches({ ...fit, ...v }, f) ? v.open : 0), 0);
}

/** Open slots per slot group under the filters: Map "action|domain|codebase_type" -> count. */
export function groupCounts(cells, f) {
  const m = new Map();
  for (const c of cells || []) {
    if (!matches(c, f)) continue;
    const k = keyOf(c);
    m.set(k, (m.get(k) || 0) + c.open);
  }
  return m;
}

/** One filter's choices with their open counts under the other filters, in a fixed order. */
export function facetOptions(cells, f, field, order) {
  const n = new Map();
  for (const c of cells || []) if (c[field] && matches(c, f, field)) n.set(c[field], (n.get(c[field]) || 0) + c.open);
  const seen = new Set((cells || []).map((c) => c[field]).filter(Boolean));
  const vals = [...order.filter((v) => seen.has(v)), ...[...seen].filter((v) => !order.includes(v)).sort()];
  return vals.map((v) => ({ value: v, open: n.get(v) || 0 }));
}

export const keyOf = (s) => (s ? `${s.action}|${s.domain}|${s.codebase_type}` : "");
export const slotOfKey = (k) => {
  const [action, domain, codebase_type] = String(k || "").split("|");
  return action ? { action, domain, codebase_type } : null;
};

export async function loadListing() {
  try { listing.value = await api.get("/api/discover"); }
  catch (e) { if (!listing.value) listing.value = { error: e.message, repos: [], slots: [], lede: { title: "Find work" } }; }
  return listing.value;
}

/** Keep Find work fresh: every 20 s, every 5 s while a repo is being researched. */
export function useListing() {
  useEffect(() => {
    let t, alive = true;
    const run = async () => {
      const d = await loadListing();
      if (!alive) return;
      const busy = (d?.repos || []).some((r) => r.generating);
      t = setTimeout(run, busy ? 5000 : 20000);
    };
    run();
    return () => { alive = false; clearTimeout(t); };
  }, []);
  return listing.value;
}

export async function loadDetail(id) {
  const d = await api.get(`/api/discover/${encodeURIComponent(id)}`);
  const m = new Map(details.value); m.set(id, d); details.value = m;
  return d;
}

/** One repo's brief, refreshed every 60 s (every 8 s while its brief or research is being written). */
export function useDetail(id) {
  useEffect(() => {
    let t, alive = true;
    const run = async () => {
      let d = null;
      try { d = await loadDetail(id); } catch (e) {
        const m = new Map(details.value); m.set(id, { error: e.status === 404 ? "missing" : e.message }); details.value = m;
      }
      if (!alive) return;
      const busy = ["generating", "queued", "researching"].includes(d?.state);
      t = setTimeout(run, busy ? 8000 : 60000);
    };
    run();
    return () => { alive = false; clearTimeout(t); };
  }, [id]);
  return details.value.get(id) || null;
}

export async function loadSlotMap() {
  try { slotMap.value = await api.get("/api/slots/map"); } catch (e) { if (!slotMap.value) slotMap.value = { error: e.message, cells: [] }; }
  return slotMap.value;
}

/** The dossier behind a brief, for the "See it in the code" drill-in only. */
export async function loadDossier(id) {
  if (dossiers.has(id)) return dossiers.get(id);
  const p = api.get(`/api/dossiers/${encodeURIComponent(id)}`).catch((e) => { dossiers.delete(id); throw e; });
  dossiers.set(id, p);
  return p;
}

// ---------- research actions (existing /api/dossiers* routes) ----------

async function send(method, path, body) {
  try {
    const r = await api.send(method, path, body);
    if (r?.toast) { toast(r.toast.text, r.toast); return null; }   // demo mode: nothing was written
    return r || {};
  } catch (e) { toast(e.message, { level: "error" }); return null; }
}

export const DECLINE = [["too_easy", "Too easy"], ["too_big", "Too big"], ["cant_run", "Can't run it"],
                        ["not_interested", "Not interested"], ["ai_generated", "AI-written code"], ["other", "Other reason"]];

/** "Not for me": set the repo aside with a reason; the toast offers it back. */
export async function decline(id, name, reason) {
  const r = await send("POST", `/api/dossiers/${encodeURIComponent(id)}/decline`, { reason, note: "" });
  if (!r) return false;
  toast(`Set aside: ${name}.`, { undo: { label: "Bring it back" }, onUndo: () => restore(id, name) });
  loadListing();
  return true;
}

export async function restore(id, name) {
  const r = await send("POST", `/api/dossiers/${encodeURIComponent(id)}/restore`, {});
  if (!r) return false;
  toast(`${name} is back in Find work.`, { level: "ok" });
  loadListing();
  if (details.value.has(id)) loadDetail(id).catch(() => {});
  return true;
}

export async function refill() {
  const r = await send("POST", "/api/dossiers/refill", {});
  if (r) toast("Research will look for more repos.");
  loadListing();
}

export async function setPaused(paused) {
  const r = await send("PUT", "/api/dossiers/worker", { paused });
  if (r) toast(paused ? "Research paused." : "Research resumed.");
  loadListing();
}

export async function syncSlots() {
  const r = await send("POST", "/api/slots/sync", {});
  if (r) toast("Reading the open slots from Studio.");
  return r;
}
