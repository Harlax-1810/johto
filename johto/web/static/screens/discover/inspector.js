// The repo brief's inspector (spec.md 3.3): detail for one seed, one place to explore, or one problem the
// research found, with the claim pinned at its foot. Everything here except the claim is Claude's research.
import { useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { actionWord, oneShotWords, sizeWords } from "../../lib/words.js";
import { Inspector } from "../../components/primitives.js";
import { ClaimPanel } from "./claim.js";
import { SeeInCode } from "./code.js";
import { ONE_SHOT_TONE } from "./words.js";

const TAG = "Claude's research";

function Sec({ title, children }) {
  return html`<section><h3>${title}</h3>${children}</section>`;
}

/** A list that shows its first `n` items and "N more". */
function Bullets({ items, n = 4 }) {
  const [all, setAll] = useState(false);
  const list = items || [];
  const shown = all ? list : list.slice(0, n);
  return html`<ul>${shown.map((x) => html`<li>${x}</li>`)}</ul>
    ${list.length > n && !all ? html`<button type="button" class="rb-more" onClick=${() => setAll(true)}>${list.length - n} more</button>` : null}`;
}

function OneGo({ v }) {
  return html`<span class=${`rb-onego ${ONE_SHOT_TONE[v] || ""}`}>${oneShotWords(v)}</span>`;
}

function Upstream({ items }) {
  const [all, setAll] = useState(false);
  const list = items || [];
  if (!list.length) return null;
  const shown = all ? list : list.slice(0, 3);
  return html`<${Sec} title="Related upstream threads">
    <p class="rb-up-why">Name these in Codebase understanding.</p>
    <ul class="rb-up">${shown.map((u) => html`<li>
      <a href=${u.url} target="_blank" rel="noreferrer">${u.title}<span class="rb-up-st">${u.state ? ` · ${u.state}` : ""} ↗</span></a></li>`)}</ul>
    ${list.length > 3 && !all ? html`<button type="button" class="rb-more" onClick=${() => setAll(true)}>${list.length - 3} more</button>` : null}
  <//>`;
}

function meta(kind, size, oneShot) {
  return html`${kind ? html`<span>${kind}</span>` : null}${size ? html`<span>${sizeWords(size)}</span>` : null}<${OneGo} v=${oneShot} />`;
}

const usedFor = (used, kind, id) => (used || []).find((u) => u.kind === kind && u.id === id) || null;

/** A seed from the brief (or, before the brief exists, one of the research's first problems). */
export function SeedInspector({ d, seed, onClose, claimRef }) {
  const det = seed.detail;
  const kind = actionWord(seed.fits?.[0]?.action);
  return html`<${Inspector} onClose=${onClose} tag=${TAG} title=${seed.title} meta=${meta(kind, seed.size, seed.one_shot)} label=${seed.title}
    foot=${html`<${ClaimPanel} dossierId=${d.id} repo=${d.repo} fits=${seed.fits} claimRef=${claimRef}
      used=${usedFor(d.used, "seed", seed.opportunity_id)}
      what=${{ kind: "seed", opportunity_id: seed.opportunity_id, title: seed.title }} />`}><div class="rb-ib">
    ${det?.models_wrong?.length ? html`<${Sec} title="Where Claude tends to go wrong"><${Bullets} items=${det.models_wrong} n=${3} /><//>` : null}
    ${!seed.problem && seed.summary ? html`<${Sec} title="What the research found"><p>${seed.summary}</p><//>` : null}
    ${det?.today?.length ? html`<${Sec} title="What happens today"><${Bullets} items=${det.today} /><//>` : null}
    ${det?.funded_team ? html`<${Sec} title="What a funded team would build"><p>${det.funded_team}</p><//>` : null}
    ${det?.reviewer_check ? html`<${Sec} title="How a reviewer can check it"><p>${det.reviewer_check}</p><//>` : null}
    <${Upstream} items=${seed.upstream} />
    ${det?.check_by_hand?.length ? html`<${Sec} title="Check by hand before you write"><${Bullets} items=${det.check_by_hand} /><//>` : null}
    <${SeeInCode} dossierId=${d.id} opportunityId=${seed.opportunity_id} />
  </div><//>`;
}

/** A place to explore: material for his own direction, never a task. */
export function PlaceInspector({ d, place, onClose, claimRef, onSeed }) {
  const near = place.near_seed && ([...(d.brief?.seeds || []), ...(d.also_researched || [])].find((s) => s.opportunity_id === place.near_seed));
  return html`<${Inspector} onClose=${onClose} tag=${TAG} title=${place.name} label=${place.name}
    meta=${html`<span>Your own direction</span><${OneGo} v="not_checked" />`}
    foot=${html`<${ClaimPanel} dossierId=${d.id} repo=${d.repo} fits=${place.fits} claimRef=${claimRef}
      used=${usedFor(d.used, "place", place.id)}
      what=${{ kind: "place", place_id: place.id, name: place.name }} />`}><div class="rb-ib">
    <${Sec} title="What's there today">
      <p>${place.today}</p>
      ${(place.facts || []).length ? html`<ul class="rb-facts">${place.facts.map((f) => html`<li>${f}</li>`)}</ul>` : null}
    <//>
    ${(place.questions || []).length ? html`<${Sec} title="Open questions"><ul class="rb-qs">${place.questions.map((q) => html`<li>${q}</li>`)}</ul><//>` : null}
    ${(place.rivals || []).length ? html`<${Sec} title="What rivals have">
      ${place.rivals.map((r) => html`<p class="rb-rival"><b>${r.who}</b> ${r.has}${r.source ? html` <a class="rb-src" href=${r.source} target="_blank" rel="noreferrer" title="Where this comes from">↗</a>` : null}</p>`)}
    <//>` : null}
    ${(place.look_at || []).length ? html`<${Sec} title="Places to look"><div class="rb-look">${place.look_at.map((t) => html`<span>${t}</span>`)}</div><//>` : null}
    ${near ? html`<${Sec} title="Nearby problem"><button type="button" class="rb-link" onClick=${() => onSeed(near.opportunity_id)}>${near.title} ›</button><//>` : null}
  </div><//>`;
}
