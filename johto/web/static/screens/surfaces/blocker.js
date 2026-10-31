import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { Surface } from "../../components/surface.js";
import { Kbd } from "../../components/ui.js";
import { act } from "../../lib/actions.js";
import { focusTerminal } from "../../components/drawer.js";
import { est } from "../../lib/format.js";
import { app, lane, queue, select, openDrawer } from "../../lib/store.js";
import { pushContext } from "../../lib/keys.js";

// Blocker surface (design 3.12): the problem in one line, what it holds up, and its fix as
// the primary action. Every blocker kind maps to one fix endpoint.

const STUDIO_STEPS = new Set(["studio", "feedback", "pick"]);

const BLOCKERS = {
  blocker_studio: {
    why: "Studio calls run inside your Studio tab in the Chrome profile named in studio-ids.json.",
    after: "Sign in there. The waiting calls run as soon as the tab has a session.",
    // Opens Studio in this browser for the sign-in, then asks the Studio worker to try again now.
    fix: { href: "https://studio.mercor.com/", then: "/api/studio/wake" },
    waiting: (q) => q.filter((i) => STUDIO_STEPS.has(i.surface)),
    waitingLabel: "Needs Studio",
  },
  blocker_docker: {
    why: "Lanes run in Docker containers.",
    after: "Start Docker Desktop. The lane starts once Docker is up.",
    waiting: (q, l) => (l.queue || []).map((r) => ({ title: `${r.kind === "calib" ? "Calibration" : "Validation"}`, label: r.label })),
    waitingLabel: "Waiting for the lane",
  },
  blocker_gh: {
    why: "The conversation log and final diff are uploaded as secret gists from the austeane account.",
    waiting: (q) => q.filter((i) => i.surface === "studio" || i.surface === "feedback"),
    waitingLabel: "Needs gists",
  },
  setup_failed: {
    why: "The lane's setup stopped with an error.",
    fix: { path: "/api/lane/start", body: (item) => item.primary?.args || {} },
  },
  collect_failed: {
    why: "The run ended but its transcript and diff were not collected. Collecting copies them out of the lane and builds the review pack.",
    fix: { path: (item) => `/api/tasks/${item.short}/collect`, body: (item) => ({ run: item.run || item.primary?.args?.run, pack: true }) },
  },
  wrong_model: {
    why: "Johto requires Opus 5.5 at effort high on every reply. The lane's transcript shows something else.",
    after: "Open the lane and switch with /model before the next reply.",
    fix: { open: true },
  },
};

export default function Blocker({ item, setPrimary }) {
  const spec = BLOCKERS[item.kind] || {};
  const l = lane.value;
  const others = queue.value.filter((i) => i.id !== item.id);
  const waiting = spec.waiting ? spec.waiting(others, l) : [];
  const jumps = waiting.filter((w) => w.id);
  const label = item.primary?.label || "Fix";
  // ↑/↓ selects a waiting row; ↵ then jumps to it. With nothing selected, ↵ runs the fix.
  const [sel, setSel] = useState(-1);
  const selId = sel >= 0 ? jumps[Math.min(sel, jumps.length - 1)]?.id : null;

  // Blockers with no fix the app can run (Docker, gh) show the problem and what to do, without a button.
  const hasFix = !!(spec.fix || item.primary?.action === "lane_open");
  async function fix() {
    if (!hasFix) return;
    if (spec.fix?.open || item.primary?.action === "lane_open") { openDrawer("row"); setTimeout(() => focusTerminal(), 160); return; }
    if (spec.fix?.href) {
      window.open(spec.fix.href, "_blank", "noopener");
      if (spec.fix.then) await act(spec.fix.then, {}, { quiet: true });
      return;
    }
    const p = spec.fix?.path;
    const path = typeof p === "function" ? p(item) : p;
    await act(path, spec.fix?.body ? spec.fix.body(item) : {}, { what: `${label}: started` });
  }

  const live = useRef({});
  live.current = { fix, selId, n: jumps.length };
  useEffect(() => {
    setPrimary?.(() => live.current.fix());
    const pop = pushContext(`blocker:${item.id}`, {
      Enter: () => {
        const a = document.activeElement;
        if (a && a !== document.body && a.matches?.("button, a")) a.click();
        else if (live.current.selId) select(live.current.selId);
        else live.current.fix();
      },
      ArrowDown: () => setSel((s) => Math.min(live.current.n - 1, s + 1)),
      ArrowUp: () => setSel((s) => Math.max(-1, s - 1)),
    });
    return () => { pop(); setPrimary?.(null); };
  }, [item.id]);
  useEffect(() => setSel(-1), [item.id]);

  const pending = item.kind === "blocker_studio" ? app.value?.studio?.pending : null;
  const what = html`${item.reason}${pending ? html`<span class="faint"> · ${pending} queued now</span>` : null}`;
  return html`<${Surface} title=${item.title} slotLine=${what} primary=${hasFix ? { label, keys: selId ? "⌘⇧↵" : "↵", run: fix } : null}>
    <div class="sbody">
      <div class="scol sb-blk">
        <div class="sb-blkcard">
          <span class="sb-blkic">✗</span>
          <div>
            <p>${spec.why || item.title}</p>
            ${spec.after ? html`<p class="muted">${spec.after}</p>` : null}
          </div>
        </div>
        ${waiting.length ? html`<section class="sb-blkwait">
          <div class="label">${spec.waitingLabel || "Waiting"}</div>
          ${waiting.map((w) => html`<button class=${`sb-wrow ${w.id && w.id === selId ? "sel" : ""}`} disabled=${!w.id}
              onClick=${() => w.id && select(w.id)} title=${w.id ? "Go to this step" : undefined}>
            <span class="sb-wt">${w.title}</span><span class="sb-wl">${w.label}</span><span class="faint num">${est(w.est_s)}</span>
            <span class="sb-wgo">${w.id ? (w.id === selId ? html`<${Kbd} k="↵" />Go` : "→") : null}</span>
          </button>`)}
        </section>` : null}
      </div>
    </div>
  <//>`;
}
