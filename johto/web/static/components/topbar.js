import { html } from "../lib/html.js";
import { app, lane, route, navigate, toggleDrawer, palette, overlay, tick, jobs, isLive, afterRunLine } from "../lib/store.js";
import { clock, mins, cutWords } from "../lib/format.js";
import { Dot } from "./ui.js";

const NAV = [["/", "Home"], ["/repos", "Repos"], ["/scout", "Scout"], ["/finished", "Finished"]];

const runName = (r) => r ? `${r.kind === "calib" ? "calibration" : "validation"} ${r.run?.split("-")[1] || ""}` : "";

export function LaneStrip() {
  tick.value;
  jobs.value;
  const l = lane.value;
  const run = l.run;
  const who = run ? `${run.label} · ${runName(run)}` : "";
  // After a run leaves the lane, its collect and review pack keep going: one short line ("Review pack ·
  // validation 1 0:05"), the pack's own log lines only in the tooltip. A live pack job wins over the
  // watcher's entry for the same run.
  const live = [...jobs.value.values()].filter((j) => isLive(j) && ["lane_collect", "collect", "pack", "review_pack"].includes(j.kind))
    .sort((a, b) => (["pack", "review_pack"].includes(b.kind) - ["pack", "review_pack"].includes(a.kind))
      || (b.created_at || "").localeCompare(a.created_at || ""));
  const after = live[0];
  const afterTip = after ? [...(after.progress?.log_tail || [])].slice(-6).join("\n") : "";
  let cls = "strip", dot = null, st = "", rest = "";
  switch (l.state) {
    case "free": case "collected":
      dot = "idle"; st = "Lane free";
      if (after) { dot = "busy"; rest = `${afterRunLine(after)} ${clock(after.started_at || after.created_at)}`; }
      else if (l.foreign) { cls += " bad"; st = "Lane ports held"; rest = `by ${l.foreign.container}, which the app doesn't know`; }
      else if (l.queue?.length) rest = `next: ${l.queue[0].label} ${l.queue[0].kind === "calib" ? "calibration" : "validation"}${l.auto_prepare === false ? "" : " (preparing)"}`;
      break;
    case "preparing": cls += " neutral"; dot = "busy"; st = "Setting up"; rest = who; break;
    case "needs_login": cls += " wait"; st = "Log in to Claude"; rest = who; break;
    case "ready_for_prompt": cls += " wait"; st = l.input?.empty === false ? "Send your prompt" : "Paste your prompt"; rest = who; break;
    case "working": {
      dot = "work"; st = `Claude working ${clock(l.since)}`;
      const f = l.facts;
      const bits = [who];
      if (f?.follow_ups) bits.push(`${f.follow_ups} follow-up${f.follow_ups === 1 ? "" : "s"}`);
      if (f?.tests?.runs && f.tests.last_ok != null) bits.push(f.tests.last_ok ? "tests passed" : "tests failing");
      rest = bits.join(" · ");
      break;
    }
    case "waiting": cls += " wait"; st = `Claude is waiting ${clock(l.since)}`;
      rest = `${run?.label || ""} · “${cutWords(l.question || "", 80)}”`; break;
    case "permission": cls += " wait"; st = "Claude asks permission"; rest = l.evidence || who; break;
    case "setup_failed": cls += " bad"; st = "Lane setup failed"; rest = who; break;
    case "exited": case "collecting": cls += " neutral"; dot = "busy"; st = "Collecting"; rest = who; break;
    default: dot = "idle"; st = "Lane free";
  }
  if (l.rate_limited) { cls = "strip bad"; st = "Rate limited"; rest = l.rate_limited.until ? `resets ${l.rate_limited.until}` : ""; }
  const tip = [l.evidence ? `Lane (⌃\`) · ${l.evidence}` : "Lane (⌃\`)", afterTip].filter(Boolean).join("\n");
  return html`<button class=${cls} onClick=${toggleDrawer} title=${tip}>
    ${dot ? html`<${Dot} kind=${dot} />` : null}
    <span class="state">${st}</span>${rest ? html`<span class="rest">${rest}</span>` : null}
  </button>`;
}

export function TopBar() {
  const r = route.value;
  const on = (p) => (p === "/" ? r === "/" : r.startsWith(p));
  return html`<header class="topbar">
    <span class="brand">Johto</span>
    <nav class="nav">
      ${NAV.map(([p, l]) => html`<a href=${p} class=${on(p) ? "on" : ""}
        onClick=${(e) => { e.preventDefault(); navigate(p); }}>${l}</a>`)}
    </nav>
    <${LaneStrip} />
    <span class="spacer"></span>
    ${app.value?.server?.mode === "demo" ? html`<span class="chip ghost" title="Example data. /?demo=0 shows your real tasks.">Demo data</span>` : null}
    <span class="tools">
      <button class="iconbtn" title="Commands (⌘K)" onClick=${() => (palette.value = true)}>⌘K</button>
      <button class="iconbtn" title="Keys (?)" onClick=${() => (overlay.value = true)}>?</button>
    </span>
  </header>`;
}
