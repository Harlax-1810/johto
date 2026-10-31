import { render } from "preact";
import { html } from "./lib/html.js";
import {
  app, route, refresh, connectEvents, navigate, queue, select, selected, openDrawer, closeDrawer, toggleDrawer, drawer,
  palette, overlay, nextChangedAt, toast, undoLast, lane, surfaceChangedAt,
} from "./lib/store.js";
import { registerChord, registerSequence, pushContext, zone } from "./lib/keys.js";
import { api } from "./lib/api.js";
import { act } from "./lib/actions.js";
import "./lib/studio_sync.js";
import { TopBar } from "./components/topbar.js";
import { LaneDrawer, runDrawerPrimary, focusTerminal } from "./components/drawer.js";
import { Palette, KeyOverlay, Toasts, registerPalette } from "./components/overlays.js";
import Home from "./screens/home.js";
import Repos from "./screens/repos.js";
import Scout from "./screens/scout.js";
import Finished from "./screens/finished.js";
import TaskPage from "./screens/task.js";

// The open surface registers its primary action here; ⌘⇧↵ runs it.
let primary = null;
export function setPrimaryHandler(fn) { primary = fn; }

const inDrawer = () => !!document.activeElement?.closest?.(".drawer");

function doNext() {
  if (Date.now() - nextChangedAt.value < 800) return;
  const n = queue.value[0];
  if (!n) return;
  if (route.value !== "/") navigate("/");
  select(n.id);
  if (n.lane || n.surface === "lane") { openDrawer("row"); setTimeout(() => focusTerminal(), 160); }
  else if (drawer.value.open && inDrawer()) closeDrawer();
}

/** × on a rail row: hide it until its state changes (a lane row holds its pings for 10 min instead). */
export async function hideItem(it) {
  if (!it) return;
  if (it.lane) {
    const r = await act(`/api/items/${encodeURIComponent(it.id)}/hold`, { minutes: 10 }, { quiet: true });
    if (r != null) toast("Notifications held for 10 min. The strip keeps counting.");
    return;
  }
  const r = await act(`/api/items/${encodeURIComponent(it.id)}/hide`, {}, { quiet: true });
  if (r == null) return;
  toast(`Hidden until it changes: ${it.title}`, {
    undo: { label: "Show it" },
    onUndo: async () => { await act(`/api/items/${encodeURIComponent(it.id)}/hide`, {}, { method: "DELETE", quiet: true }); },
  });
}

registerChord("mod+enter", doNext);
registerChord("mod+shift+enter", () => {
  if (inDrawer() && runDrawerPrimary()) return;
  if (Date.now() - surfaceChangedAt < 500) return;
  primary?.();
});
registerChord("ctrl+`", toggleDrawer);
registerChord("ctrl+shift+`", () => (drawer.value = { ...drawer.value, open: true, full: !drawer.value.full }));
registerChord("mod+k", () => (palette.value = !palette.value));
registerChord("alt+n", () => { openDrawer("user"); setTimeout(() => document.querySelector(".notes input")?.focus(), 150); });
registerSequence("g h", () => navigate("/"));
registerSequence("g p", () => navigate("/repos"));
registerSequence("g r", () => navigate("/repos"));
registerSequence("g s", () => navigate("/scout"));
registerSequence("g f", () => navigate("/finished"));

// ⌘Z outside text fields undoes the newest undo window (claims, hides). Inside a field it stays the
// browser's own undo. Drop countdowns catch ⌘Z themselves, before this runs.
window.addEventListener("keydown", (e) => {
  if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey || e.code !== "KeyZ") return;
  if (zone() !== "list") return;
  e.preventDefault();
  undoLast();
});

pushContext("global", {
  "?": () => (overlay.value = !overlay.value),
  j: () => moveSel(1), k: () => moveSel(-1),
  x: () => route.value === "/" && hideItem(selected.value),
  t: () => selected.value?.short && navigate(`/task/${selected.value.short}`),
  o: () => {
    const s = selected.value?.short && (app.value?.tasks || []).find((t) => t.short === selected.value.short);
    if (s?.studio?.url) window.open(s.studio.url, "_blank", "noopener");
  },
  Escape: () => { palette.value = false; overlay.value = false; },
});

function moveSel(d) {
  const q = queue.value; if (!q.length) return;
  const i = Math.max(0, q.findIndex((x) => x.id === selected.value?.id));
  select(q[Math.min(q.length - 1, Math.max(0, i + d))].id);
}

/**
 * A palette entry for a two-press write. Picking it asks the server (first press); the toast then
 * carries the second press for the 4 s the server holds the nonce.
 */
function twoPress(id, group, label, method, path, body, what) {
  const send = async () => {
    try {
      const r = await api.confirmable(method, path, body, {
        onArm: (summary, secs) => toast(summary || `${label}?`, {
          id: `arm:${path}`, level: "warn", ms: secs * 1000, action: { label: "Press again", run: send } }),
      });
      if (r?.armed) return;
      if (r?.toast) toast(r.toast.text, r.toast); else toast(what);
    } catch (e) { toast(e.message, { level: "error" }); }
  };
  return { id, group, label, run: send };
}

function taskCommands() {
  const it = selected.value;
  const short = it?.short;
  if (!short) return [];
  const s = (app.value?.tasks || []).find((t) => t.short === short);
  const label = s?.label || it.label;
  const name = s?.task_name || short;
  const out = [
    { id: "t-page", group: "Task", label: `Task page for ${label}`, key: "t", run: () => navigate(`/task/${short}`) },
    { id: "t-save", group: "Studio", label: `Save to Studio: ${label}`, run: () => act(`/api/tasks/${short}/studio/save`, {}, { what: "Saving to Studio" }) },
    { id: "t-qc", group: "Studio", label: `Rerun AutoQC: ${label}`, run: () => act(`/api/tasks/${short}/studio/qc`, { spec: "AutoQC" }, { what: "AutoQC started" }) },
    { id: "t-refresh", group: "Studio", label: `Refresh from Studio: ${label}`, run: () => act(`/api/tasks/${short}/studio/refresh`, {}, { what: "Refreshing from Studio" }) },
    twoPress("t-unclaim", "Studio", `Unclaim ${name} in Studio`, "POST", `/api/tasks/${short}/studio/unclaim`, {}, "Unclaiming"),
    { id: "t-clean", group: "Lane", label: `Clean lanes for ${label}`, run: () => act(`/api/tasks/${short}/clean`, {}, { what: "Removing lanes" }) },
  ];
  if (s?.studio?.url) out.push({ id: "t-studio", group: "Studio", label: `Open ${name} in Studio`, key: "o", run: () => window.open(s.studio.url, "_blank", "noopener") });
  if (it.kind === "write_prompt") {
    // The palette follows settings.calibrate_first, like the prompt's own buttons.
    if (app.value?.settings?.calibrate_first) {
      out.push({ id: "t-skip", group: "Task", label: `Skip calibration for ${label}`, run: () => act(`/api/tasks/${short}/skip-calibration`, {}, { what: "Skipped calibration" }) });
    } else {
      out.push({ id: "t-calib", group: "Task", label: `Calibrate first: ${label}`, run: () => act(`/api/tasks/${short}/done`, { surface: "prompt", kind: "calib" }, { what: "Queued calibration" }) });
    }
  }
  return out;
}

registerPalette(() => {
  const l = lane.value;
  const s = app.value?.settings || {};
  return [
    { id: "nav-home", group: "Go", label: "Home", key: "g h", run: () => navigate("/") },
    { id: "nav-repos", group: "Go", label: "Repos", key: "g p", run: () => navigate("/repos") },
    { id: "nav-scout", group: "Go", label: "Scout", key: "g s", run: () => navigate("/scout") },
    { id: "nav-fin", group: "Go", label: "Finished", key: "g f", run: () => navigate("/finished") },
    { id: "lane", group: "Lane", label: "Open the lane", key: "⌃`", run: () => openDrawer("user") },
    { id: "ghostty", group: "Lane", label: "Open lane in Ghostty", enabled: !!l.run, why: "no lane is running",
      run: () => act("/api/lane/ghostty", {}, { quiet: true }) },
    ...(l.input_locked ? [{ id: "unlock", group: "Lane", label: "Unlock lane input", run: () => act("/api/lane/unlock", {}, { what: "Lane input unlocked" }) }] : []),
    { id: "collect", group: "Lane", label: "Collect the lane's run again", enabled: !!l.run, why: "no run in the lane",
      run: () => act("/api/lane/collect", {}, { what: "Collecting" }) },
    { id: "autoprep", group: "Lane", label: `Auto-prepare lane: ${l.auto_prepare === false ? "off" : "on"}`,
      run: () => act("/api/lane/auto-prepare", { on: l.auto_prepare === false }, { method: "PUT", quiet: true }) },
    ...(queue.value.find((q) => q.lane) ? [{ id: "hold", group: "Lane", label: "Hold notifications 10 min",
      run: () => hideItem(queue.value.find((q) => q.lane)) }] : []),
    { id: "sync", group: "Studio", label: "Sync Studio now", run: () => act("/api/slots/sync", {}, { what: "Syncing Studio" }) },
    { id: "filling", group: "Studio", label: `Studio filling: ${s.studio_filling || "automatic"}`,
      run: () => act("/api/settings", { studio_filling: s.studio_filling === "manual" ? "automatic" : "manual" }, { method: "PUT", quiet: true }) },
    ...taskCommands(),
    { id: "demo", group: "View", label: app.value?.server?.mode === "demo" ? "Show my real tasks" : "Show the demo afternoon",
      run: () => { location.href = `/?demo=${app.value?.server?.mode === "demo" ? 0 : 1}`; } },
    ...queue.value.map((q) => ({ id: `q-${q.id}`, group: "Queue", label: `${q.title} · ${q.label}`, run: () => { navigate("/"); select(q.id); } })),
  ];
});

function App() {
  const r = route.value;
  if (!app.value) return html`<div></div>`;
  let screen;
  if (r === "/") screen = html`<${Home} />`;
  else if (r.startsWith("/repos") || r.startsWith("/pick")) screen = html`<div class="frame"><${Repos} id=${r.split("/")[2] || null} /></div>`;
  else if (r.startsWith("/scout")) screen = html`<div class="frame"><${Scout} /></div>`;
  else if (r.startsWith("/finished")) screen = html`<div class="frame"><${Finished} /></div>`;
  else if (r.startsWith("/task/")) screen = html`<div class="frame"><${TaskPage} short=${r.split("/")[2]} /></div>`;
  else screen = html`<${Home} />`;
  return html`<${TopBar} />
    <div class="frame" style="flex-direction:column">${screen}<${LaneDrawer} /></div>
    <${Palette} /><${KeyOverlay} /><${Toasts} />`;
}

async function boot() {
  try { await refresh(); } catch (e) {
    document.getElementById("app").textContent = e.status === 401 ? "Open the link `johto web` printed." : e.message;
    return;
  }
  render(html`<${App} />`, document.getElementById("app"));
  connectEvents();
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("item")) select(h.get("item"));
  if (h.get("drawer") === "open") openDrawer("user");
  if (h.get("palette") === "open") palette.value = true;
  if (h.get("keys") === "open") overlay.value = true;
}
boot();
