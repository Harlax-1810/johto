// The repo brief (/repos/<id>, spec.md 3.3): is this repo worth it, and where could he take it? Three seeds to
// start from, places to find his own direction, and the claim in the inspector. All of it is Claude's research.
import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { navigate, hash, toast, putJob } from "../../lib/store.js";
import { useJob } from "../../lib/actions.js";
import { pushContext } from "../../lib/keys.js";
import { actionWord, oneShotWords, sizeWords, VERDICT_WORDS, VERDICT_TONE } from "../../lib/words.js";
import { StageWithInspector, Pill, SlotPill, AiTag, Btn, Menu, Card, SectionHead, Empty, Dot } from "../../components/primitives.js";
import { useDetail, loadDetail, restore, facets, howOnly, fitOpen } from "./data.js";
import { NotForMe } from "./find.js";
import { SeedInspector, PlaceInspector } from "./inspector.js";
import { FullResearch } from "./code.js";
import { dayWords, whenWords, usedWords, ledeBody, openWords, ONE_SHOT_TONE } from "./words.js";

const FITS_SHOWN = 2;   // the two largest groups; the rest one click away

/** Fits recounted with his prompt and mode filters, largest first. The repo's page still shows every group. */
const recounted = (fits) => (fits || []).map((x) => ({ ...x, open: fitOpen(x, howOnly(facets.value)) }))
  .sort((a, b) => b.open - a.open);

function setSel(id, sel) {
  const h = sel ? `#${sel.kind}=${sel.id}` : "";
  history.replaceState({}, "", `/repos/${id}${h}`);
  hash.value = new URLSearchParams(h.slice(1));
}

function readSel() {
  const h = hash.value;
  if (h.get("seed")) return { kind: "seed", id: h.get("seed") };
  if (h.get("o")) return { kind: "seed", id: h.get("o") };     // older links (#o=o2)
  if (h.get("place")) return { kind: "place", id: h.get("place") };
  return null;
}

function OneGo({ v }) {
  return html`<span class=${`rb-onego ${ONE_SHOT_TONE[v] || ""}`}>${oneShotWords(v)}</span>`;
}

function SeedCard({ s, sel, onOpen }) {
  const top = recounted(s.fits)[0];
  const o = top ? openWords(top.open) : null;
  return html`<${Card} ai=${true} selected=${sel} onClick=${onOpen} class="rb-seed" title=${s.title}>
    <div class="rb-seed-top">
      <span class="rb-kind">${actionWord(top?.action) || "Problem"}</span>
      ${o ? html`<span class=${`rb-open ${o.scarce ? "left" : ""}`}>${top.open ? o.text : "no slot with your filters"}</span>` : null}
    </div>
    <h3 class="title-serif">${s.title}</h3>
    ${s.problem ? html`<p class="rb-problem">${s.problem}</p>` : s.summary ? html`<p class="rb-problem clamp">${s.summary}</p>` : null}
    ${s.models_miss ? html`<p class="rb-trap">${s.models_miss}</p>` : null}
    <div class="rb-seed-foot"><${OneGo} v=${s.one_shot} />${s.size ? html`<span>${sizeWords(s.size)}</span>` : null}</div>
  <//>`;
}

function PlaceCard({ p, sel, onOpen, used }) {
  const r = (p.rivals || [])[0];
  return html`<${Card} ai=${true} selected=${sel} onClick=${onOpen} class="rb-place" title=${p.name}>
    <div class="rb-place-top"><h3 class="card-title">${p.name}</h3>${used ? html`<${Pill} tone="outline">Already yours<//>` : null}</div>
    <p class="rb-today">${p.today}</p>
    ${(p.questions || []).length ? html`<ul class="rb-qs">${p.questions.slice(0, 2).map((q) => html`<li>${q}</li>`)}</ul>` : null}
    ${r ? html`<p class="rb-rivals"><span class="rb-rl">Rivals:</span> <b>${r.who}</b> ${r.has}</p>` : null}
  <//>`;
}

function Band({ d, b, onFull, folded, setFolded }) {
  const ready = d.state === "ready" || d.state === "stale";
  const v = b?.verdict || {};
  const head = b?.heading || [];
  const declined = d.status === "declined";
  const writing = useJob(["brief"], `b:${d.id}`);
  const stop = async () => {
    try {
      const r = await api.send("DELETE", `/api/jobs/${encodeURIComponent(writing.id)}`);
      if (r?.job) putJob(r.job);
      toast("Stopping the brief.");
    } catch (e) { toast(e.message, { level: "error" }); }
  };
  const more = [
    writing && writing.cancellable !== false ? { label: "Stop writing the brief", run: stop } : null,
    { label: "Full research", run: onFull, disabled: !d.drill?.dossier_available, why: "The research isn't written yet" },
    { label: "Research this again", run: async () => {
      try {
        const r = await api.post(`/api/briefs/${encodeURIComponent(d.id)}/regenerate`, {});
        toast(r?.toast?.text || "Claude will write this brief again.");
        loadDetail(d.id).catch(() => {});
      } catch (e) { toast(e.status === 409 ? "Claude is already writing this brief." : e.message, { level: e.status === 409 ? "info" : "error" }); }
    }, disabled: !d.drill?.dossier_available, why: "The research isn't written yet" },
  ];
  return html`<section class=${`rb-band ${folded ? "" : "open"}`}>
    <div class=${`rb-band-grid ${b?.how_it_works || head.length ? "" : "solo"}`}>
    <div class="rb-band-top">
      ${d.generated_at && d.drill?.dossier_available ? html`<${AiTag} label=${`Claude's research, ${dayWords(d.generated_at)}`} />` : null}
      ${declined ? html`<span class="rb-aside">Set aside${d.decline_words ? `: ${d.decline_words}` : ""}</span>
          <${Btn} size="sm" label="Bring it back" onClick=${() => restore(d.id, d.name)} />`
        : d.status === "ready" || d.status === "actioned" ? html`<${NotForMe} id=${d.id} name=${d.name} size="sm" onDone=${() => navigate("/repos")} />` : null}
      <${Menu} label="⋯" title="More" size="sm" items=${more} />
    </div>
      <div class="rb-b1">
        <div class="rb-title"><h1>${d.name}</h1><a href=${d.github_url} target="_blank" rel="noreferrer" title="Open on GitHub">${d.repo} ↗</a></div>
        ${b?.one_liner ? html`<p class="rb-one">${b.one_liner}</p>` : null}
        ${v.lede ? html`<p class="rb-lede">${v.level && v.level !== "dive" ? html`<${Pill} tone=${VERDICT_TONE[v.level] || "neutral"}>${VERDICT_WORDS[v.level]}<//> ` : null}${v.level ? ledeBody(v.lede) : v.lede}</p>` : null}
        ${!ready ? html`<p class="rb-coming">${["generating", "queued"].includes(d.state) ? html`<${Dot} tone="work" />Claude is writing the brief.`
          : d.state === "researching" ? html`<${Dot} tone="work" />Claude is researching this repo.`
          : d.state === "failed" ? "The brief couldn't be written."
          : "Brief coming."}</p>` : null}
        ${b?.how_it_works || head.length ? html`<button type="button" class="rb-about" aria-expanded=${!folded} onClick=${() => setFolded(!folded)}>
          About ${d.name} ${folded ? "▸" : "▾"}</button>` : null}
      </div>
      ${b?.how_it_works ? html`<div class="rb-b2"><h3>How it works</h3><p>${b.how_it_works}</p></div>` : null}
      ${head.length ? html`<div class="rb-b3"><h3>Where it's heading</h3>
        <dl class="rb-head">${head.map((h) => html`<dt>${whenWords(h.when)}</dt><dd>${h.what}</dd>`)}</dl></div>` : null}
    </div>
  </section>`;
}

function FitsStrip({ fits, fails }) {
  const [all, setAll] = useState(false);
  const list = recounted(fits).filter((x) => x.open);
  if (!(fits || []).length && !(fails || []).length) return null;
  const shown = all ? list : list.slice(0, FITS_SHOWN);
  return html`<div class="rb-fits">
    <span class="rb-fits-l">Fits</span>
    <div class=${`rb-fits-p ${all ? "all" : ""}`}>
      ${shown.map((f) => html`<${SlotPill} slot=${f} count=${f.open} size="sm" />`)}
      ${list.length > FITS_SHOWN && !all ? html`<button type="button" class="rb-more" onClick=${() => setAll(true)}>${list.length - FITS_SHOWN} more</button>` : null}
      ${(fits || []).length && !list.length ? html`<span class="rb-fails">No open slot with your prompt and mode filters.</span>` : null}
    </div>
    ${(fails || []).length ? html`<span class="rb-fails">${fails.join(". ")}.</span>` : null}
  </div>`;
}

export default function Brief({ id }) {
  const d = useDetail(id);
  const [full, setFull] = useState(false);
  const [folded, setFolded] = useState(true);
  const claimRef = useRef(null);
  const sel = readSel();
  const b = d?.brief || null;
  const seeds = b?.seeds || [];
  const used = d?.used || [];
  const usedSeed = (s) => used.some((u) => u.kind === "seed" && u.id === s.opportunity_id);
  const cards = [...seeds.filter((s) => !usedSeed(s)).map((s) => ({ kind: "seed", id: s.opportunity_id })),
                 ...(b?.places || []).map((p) => ({ kind: "place", id: p.id }))];
  // The clicked card stays in view when the inspector narrows the stage and the grid reflows.
  const pick = (s) => {
    setSel(id, s);
    if (s) setTimeout(() => document.querySelector(`[data-card="${s.kind}:${s.id}"]`)?.scrollIntoView({ block: "nearest" }), 80);
  };
  const close = () => setSel(id, null);

  const st = useRef({});
  st.current = { cards, sel };
  useEffect(() => pushContext("brief", {
    j: () => step(1), k: () => step(-1),
    c: () => { if (readSel()) claimRef.current?.(); },
    Escape: () => (readSel() ? close() : navigate("/repos")),
  }), [id]);
  function step(k) {
    const { cards: cs, sel: s } = st.current;
    if (!cs.length) return;
    const i = s ? cs.findIndex((c) => c.kind === s.kind && c.id === s.id) : -1;
    const n = cs[Math.min(cs.length - 1, Math.max(0, i + k))];
    pick(n);
    requestAnimationFrame(() => document.querySelector(`[data-card="${n.kind}:${n.id}"]`)?.scrollIntoView({ block: "nearest" }));
  }

  if (!d) return html`<${StageWithInspector} inspector=${null}><a class="rb-back" href="/repos" onClick=${(e) => { e.preventDefault(); navigate("/repos"); }}>← Find work</a><//>`;
  if (d.error) {
    return html`<${StageWithInspector} inspector=${null}>
      <a class="rb-back" href="/repos" onClick=${(e) => { e.preventDefault(); navigate("/repos"); }}>← Find work</a>
      <${Empty} title=${d.error === "missing" ? "That repo isn't in the research list." : "This brief could not load."}
        body=${d.error === "missing" ? null : d.error} action=${html`<${Btn} label="Back to Find work" onClick=${() => navigate("/repos")} />`} />
    <//>`;
  }

  const findSeed = (oid) => seeds.find((s) => s.opportunity_id === oid) || (d.also_researched || []).find((s) => s.opportunity_id === oid);
  let inspector = null;
  if (sel?.kind === "seed" && findSeed(sel.id)) {
    inspector = html`<${SeedInspector} key=${`s${sel.id}`} d=${d} seed=${findSeed(sel.id)} onClose=${close} claimRef=${claimRef} />`;
  } else if (sel?.kind === "place") {
    const p = (b?.places || []).find((x) => x.id === sel.id);
    if (p) inspector = html`<${PlaceInspector} key=${`p${sel.id}`} d=${d} place=${p} onClose=${close} claimRef=${claimRef}
      onSeed=${(oid) => pick({ kind: "seed", id: oid })} />`;
  }
  const isSel = (kind, cid) => sel?.kind === kind && sel.id === cid;
  const shownSeeds = seeds.filter((s) => !usedSeed(s));
  const also = d.also_researched || [];
  const places = b?.places || [];
  const ready = d.state === "ready" || d.state === "stale";

  return html`<${StageWithInspector} inspector=${inspector} class="rb-stage">
    <div class="rb">
      <a class="rb-back" href="/repos" onClick=${(e) => { e.preventDefault(); navigate("/repos"); }}>← Find work</a>
      <${Band} d=${d} b=${b} onFull=${() => setFull(true)} folded=${folded} setFolded=${setFolded} />
      <${FitsStrip} fits=${d.fits} fails=${b?.facts?.fails} />
      ${seeds.length || used.length ? html`<section class="rb-sec">
        <${SectionHead} title=${ready ? "Start from one of these" : "Problems the research found"} right=${html`<${AiTag} label="Claude's research" />`} />
        ${shownSeeds.length ? html`<div class=${`rb-grid ${shownSeeds.length < 3 ? "two" : ""}`}>
          ${shownSeeds.map((s) => html`<div data-card=${`seed:${s.opportunity_id}`}><${SeedCard} s=${s}
            sel=${isSel("seed", s.opportunity_id)} onOpen=${() => pick({ kind: "seed", id: s.opportunity_id })} /></div>`)}
        </div>` : null}
        ${used.length ? html`<div class="rb-used">${used.map((u) => html`<p>
          <${Dot} tone="idle" />Already yours: <a href=${`/task/${u.short}`} onClick=${(e) => { e.preventDefault(); navigate(`/task/${u.short}`); }}>${usedWords(u)}</a></p>`)}</div>` : null}
        ${also.length ? html`<p class="rb-also">Also researched: ${also.map((o, i) => html`${i ? ", " : ""}<button type="button" class="rb-link"
            onClick=${() => pick({ kind: "seed", id: o.opportunity_id })}>${o.title}</button>`)}</p>` : null}
      </section>` : null}
      ${places.length ? html`<section class="rb-sec">
        <${SectionHead} title="Or find your own" />
        <div class="rb-grid">
          ${places.map((p) => html`<div data-card=${`place:${p.id}`}><${PlaceCard} p=${p} sel=${isSel("place", p.id)} used=${used.some((u) => u.kind === "place" && u.id === p.id)}
            onOpen=${() => pick({ kind: "place", id: p.id })} /></div>`)}
        </div>
      </section>` : null}
      ${d.state === "failed" && !seeds.length ? html`<${Empty} title="Research didn't finish for this repo."
        action=${html`<${Btn} label="Research again" onClick=${() => restore(d.id, d.name)} />`} />` : null}
    </div>
    <${FullResearch} dossierId=${d.id} open=${full} onClose=${() => setFull(false)} />
  <//>`;
}
