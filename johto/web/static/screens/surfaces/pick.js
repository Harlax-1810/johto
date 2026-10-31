// Home card for the 'pick' queue row: the top ready repo dossier and its best opportunities.
import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { Surface } from "../../components/surface.js";
import { navigate, app } from "../../lib/store.js";
import { pushContext } from "../../lib/keys.js";
import { Kbd } from "../../components/ui.js";
import { OneShot, Size, Fit } from "../../components/blocks.js";
import { useDossiers, loadDossier, openFor, repoShort, WorkerLine, slots, readyOf, byRank, setPaused } from "../repos.js";

export default function Pick({ item, setPrimary }) {
  const d = useDossiers();
  slots.value;
  const ready = readyOf(d);
  const top = ready[0];
  const next = ready.slice(1, 3);
  const queued = (d?.items || []).filter((i) => i.status === "queued").sort(byRank).slice(0, 3);
  const [doc, setDoc] = useState(null);
  useEffect(() => {
    setDoc(null);
    if (top) loadDossier(top.id).then((r) => setDoc(r.dossier)).catch(() => {});
  }, [top?.id]);

  const opps = doc ? doc.blocks.filter((b) => b.type === "opportunity").slice(0, 3) : (top?.top || []);
  const st = useRef(null);
  st.current = { top, opps, paused: !!d?.paused };
  const open = (o) => st.current.top && navigate(`/repos/${st.current.top.id}`, o ? `#o=${o}` : "");
  const openN = (i) => { const o = st.current.opps[i]; if (o) open(o.id); };
  const allRepos = () => navigate("/repos");
  const resume = () => setPaused(false);
  const primaryRun = () => (st.current.top ? open() : st.current.paused ? resume() : allRepos());
  useEffect(() => setPrimary?.(primaryRun), []);
  useEffect(() => pushContext("pick-surface", {
    Enter: () => (st.current.top ? open() : allRepos()),
    p: () => !st.current.top && st.current.paused && resume(),
    1: () => openN(0), 2: () => openN(1), 3: () => openN(2),
  }), []);

  const headline = top?.headline || doc?.headline;
  const verdict = top?.verdict || doc?.verdict;
  const ov = doc?.blocks.find((b) => b.type === "overview");
  const total = app.value?.slots?.open;
  const sub = [ready.length ? `${ready.length} ${ready.length === 1 ? "repo" : "repos"} researched and ready` : null,
               total != null ? `${total.toLocaleString()} open slots` : null].filter(Boolean).join(" · ");
  const primary = top ? { label: `Open ${repoShort(top.repo)}`, keys: "↵", run: () => open() }
    : d?.paused ? { label: "Resume research", keys: "p", run: resume }
    : { label: "All repos", keys: "↵", run: allRepos };
  const secondary = !top && d?.paused ? [{ label: "All repos", key: "↵", run: allRepos }] : [];
  return html`<${Surface} title="Pick the next problem" slotLine=${sub} reason=${item.reason} primary=${primary} secondary=${secondary}>
    <div class="sbody"><div class="scol pk">
      ${top ? html`
        <div class="pk-repo">
          <span class="pk-name" onClick=${() => open()}><span class="faint">${top.repo.split("/")[0]}/</span><b>${repoShort(top.repo)}</b></span>
          <a class="mono faint" href=${doc?.pinned_url || `https://github.com/${top.repo}/tree/${top.commit}`} target="_blank" rel="noreferrer">@${(top.commit || "").slice(0, 8)} ↗</a>
          ${ov ? html`<span class="sep">·</span><span class="muted">${(ov.size?.languages || []).join(", ")}</span>
            ${ov.size?.source_files != null ? html`<span class="sep">·</span><span class="muted"><span class="num">${ov.size.source_files}</span> source / <span class="num">${ov.size.test_files ?? 0}</span> test files</span>` : null}` : null}
        </div>
        ${headline || verdict ? html`<div class="pk-verdict ai-rail">
          <div class="pk-ailab ai-tag">AI research</div>
          ${headline ? html`<div class="pk-headline">${headline}</div>` : null}
          ${verdict?.why ? html`<div class="pk-why">${verdict.promise ? html`<span class=${`rp-prom ${verdict.promise}`}>${verdict.promise} promise</span>` : null}<span>${verdict.why}</span></div>` : null}
        </div>` : null}
        <div class="pk-sec pk-cols"><span></span><span class="label">Best opportunities</span>
          <span class="label">One-shot</span><span class="label">Size</span><span class="label">Best fit · open slots</span></div>
        <div class="pk-opps">
          ${opps.map((o, i) => {
            const fit = (o.slot_fit || [])[0];
            return html`<div class="pk-opp" onClick=${() => open(o.id)}>
              <span class="pk-n"><${Kbd} k=${String(i + 1)} /></span>
              <div class="pk-ob">
                <div class="pk-ot"><b>${o.title}</b><span class="faint">${o.area}</span></div>
                ${o.ambition ? html`<div class="pk-amb">${o.ambition}</div>` : null}
                ${o.bounded_task?.shape ? html`<div class="pk-shape"><span class="faint">Bounded task</span>${o.bounded_task.shape}</div>` : null}
              </div>
              <span class="pk-os"><${OneShot} o=${o.one_shot} /></span>
              <span class="pk-sz"><${Size} s=${o.size || o.bounded_task?.size} /></span>
              ${fit ? html`<${Fit} f=${fit} open=${openFor(fit)} />` : html`<span></span>`}
            </div>`;
          })}
        </div>
        ${next.length ? html`<div class="pk-sec"><span class="label">Next ready</span></div>
          <div class="pk-next">${next.map((r) => html`<div class="pk-nrow" onClick=${() => navigate(`/repos/${r.id}`)}>
            <span class="pk-nname"><span class="faint">${r.repo.split("/")[0]}/</span><b>${repoShort(r.repo)}</b></span>
            <span class="pk-nhead">${r.headline}</span>
            <span class="pk-nmeta">${r.verdict?.promise ? html`<span class=${`rp-prom ${r.verdict.promise}`}>${r.verdict.promise} promise</span>` : null}</span>
          </div>`)}</div>` : null}
        ${(d?.items || []).some((i) => i.status === "generating") ? html`<div class="pk-foot"><${WorkerLine} d=${d} /></div>` : null}
      ` : html`<div class="pk-empty"><${WorkerLine} d=${d} />
        ${queued.length ? html`<div class="pk-sec"><span class="label">Next to research</span></div>
          <div class="pk-next">${queued.map((r) => html`<div class="pk-nrow pk-q">
            <span class="pk-nname">${r.repo}</span>
            <span class="pk-nhead">${(r.promise_reasons || []).filter((x) => !/^Fits \d/.test(x)).map((x) => x.replace(/, which the lane can build$/, "")).slice(0, 3).join(" · ")}</span><span></span>
          </div>`)}</div>` : null}</div>`}
    </div></div>
  <//>`;
}
