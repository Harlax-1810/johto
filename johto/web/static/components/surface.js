import { html } from "../lib/html.js";
import { follow } from "../lib/store.js";
import { Kbd } from "./ui.js";

/** An in-app link: a plain click stays in the app, ⌘-click opens a new tab. */
export function AppLink({ href, title, children }) {
  return html`<a href=${href} title=${title} onClick=${(e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
    e.preventDefault(); follow(href);
  }}>${children}</a>`;
}

/**
 * The task label. For a task claimed from a repo's research, "Repo · Opportunity" links each part:
 * the repo to its page, the opportunity to its entry on that page.
 */
export function TaskLabel({ label, dossier }) {
  if (!label) return null;
  if (!dossier?.repo_href) return html`<b>${label}</b>`;
  const at = label.indexOf(" · ");
  const opp = dossier.opportunity_href || dossier.repo_href;
  if (at < 0) return html`<b><${AppLink} href=${opp} title="The opportunity you chose">${label}<//></b>`;
  return html`<b><${AppLink} href=${dossier.repo_href} title="The repo page">${label.slice(0, at)}<//><span class="sep">·</span><${AppLink}
    href=${opp} title="The opportunity you chose">${label.slice(at + 3)}<//></b>`;
}

/**
 * Standard surface header: title, task label + slot line, one primary action with its key,
 * a few secondary verbs, and one line saying why this step ranks where it does.
 */
export function Surface({ title, label, dossier, slotLine, reason, primary, secondary = [], children, head }) {
  return html`<section class="surface">
    <header class="shead">
      <h1>${title}</h1>
      <div class="actions">
        ${secondary.map((s) => html`<button class=${`btn ${s.kind || "quiet"}`} onClick=${s.run} disabled=${s.disabled}
            title=${s.title}>${s.key ? html`<${Kbd} k=${s.key} />` : null}${s.label}</button>`)}
        ${primary ? html`<button class=${`btn ${primary.armed ? "armed" : primary.kind || "primary"}`} onClick=${primary.run}
            disabled=${primary.disabled} title=${primary.why || ""}>
            <${Kbd} k=${primary.keys || "⌘⇧↵"} />${primary.armed ? primary.armedLabel || "Press again" : primary.label}</button>` : null}
      </div>
      <div class="sub"><div class="sub-in">
        ${label ? html`<span class="sub-first"><${TaskLabel} label=${label} dossier=${dossier} /></span>` : null}
        ${slotLine ? html`<span class=${`sub-item ${label ? "" : "sub-first"}`}>${label ? html`<span class="sep">·</span>` : null}${slotLine}</span>` : null}
        ${reason ? html`<span class=${`sub-item sub-reason faint ${label || slotLine ? "" : "sub-first"}`}>${label || slotLine ? html`<span class="sep">·</span>` : null}${reason}</span>` : null}
      </div></div>
      ${head || null}
    </header>
    ${children}
  </section>`;
}
