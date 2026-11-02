// The only places on the discovery screens with paths and real code (spec.md 3.3): "See it in the code" in a seed
// inspector, and "Full research" (the whole dossier) from the brief's ⋯ menu. Both are explicit drill-ins.
import { useEffect, useState } from "preact/hooks";
import { html } from "../../lib/html.js";
import { Sheet } from "../../components/primitives.js";
import { Block, OppDetail } from "../../components/blocks.js";
import { loadDossier } from "./data.js";

function useDossier(id, on) {
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    if (!on || !id) return;
    let alive = true;
    loadDossier(id).then((r) => alive && setRes(r)).catch((e) => alive && setErr(e.message));
    return () => { alive = false; };
  }, [id, on]);
  return { res, err };
}

const ctxOf = (id, doc) => ({ target: `d:${id}`, repo: doc?.repo, commit: doc?.commit });
const opps = (doc) => (doc?.blocks || []).filter((b) => b.type === "opportunity");

/** "See it in the code ▸": the research's own record of one problem, with pointers that open the real code. */
export function SeeInCode({ dossierId, opportunityId }) {
  const [open, setOpen] = useState(false);
  const { res, err } = useDossier(dossierId, open);
  useEffect(() => setOpen(false), [opportunityId]);
  const doc = res?.dossier;
  const o = opps(doc).find((x) => x.id === opportunityId);
  return html`<section class="rb-code">
    <button type="button" class="rb-drill" aria-expanded=${open} onClick=${() => setOpen(!open)}>See it in the code ${open ? "▾" : "▸"}</button>
    ${open ? html`<div class="rb-code-in">
      ${err ? html`<p class="faint">${err}</p>` : !doc ? html`<p class="faint">Opening the research…</p>`
        : o ? html`<${OppDetail} o=${o} ctx=${ctxOf(dossierId, doc)} fits=${false} />` : html`<p class="faint">The research has no code notes for this.</p>`}
    </div>` : null}
  </section>`;
}

/** The whole dossier in a wide sheet. */
export function FullResearch({ dossierId, open, onClose }) {
  const { res, err } = useDossier(dossierId, open);
  const doc = res?.dossier;
  const ctx = ctxOf(dossierId, doc);
  return html`<${Sheet} open=${open} onClose=${onClose} tag="Claude's research" title="Full research" width=${980} label="Full research">
    <div class="rb-full">
      ${err ? html`<p class="faint">${err}</p>` : !doc ? html`<p class="faint">Opening the research…</p>` : html`
        ${doc.headline ? html`<p class="rb-full-head">${doc.headline}</p>` : null}
        ${(doc.blocks || []).filter((b) => b.type !== "opportunity").map((b, i) => html`<${Block} key=${i} b=${b} ctx=${ctx} />`)}
        ${opps(doc).map((o) => html`<section class="blk"><${OppDetail} o=${o} ctx=${ctx} fits=${false} /></section>`)}`}
    </div>
  <//>`;
}
