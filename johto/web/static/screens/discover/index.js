// Find work, the repo brief and the slot map (spec.md 3.2 to 3.4). Owned by the discover builder.
// app.js renders <Discover id=... scout=... />: id is the dossier id on /repos/<id>; scout is true on /scout.
// /scout opens the slot map over Find work (/repos#slots); /pick goes to Find work.
import { useEffect } from "preact/hooks";
import { html } from "../../lib/html.js";
import { route, hash, navigate } from "../../lib/store.js";
import { registerPalette } from "../../components/overlays.js";
import FindWork from "./find.js";
import Brief from "./brief.js";
import { listing, refill, setPaused } from "./data.js";

function replaceTo(path, h = "") {
  history.replaceState({}, "", path + h);
  route.value = path;
  hash.value = new URLSearchParams(h.replace(/^#/, ""));
}

registerPalette(() => {
  const r = listing.value?.research;
  return [
    { id: "fw-slots", group: "Find work", label: "All open slots", key: "g s", run: () => navigate("/repos", "#slots") },
    { id: "fw-more", group: "Find work", label: "Research more repos", run: refill },
    r?.paused ? { id: "fw-resume", group: "Find work", label: "Resume research", run: () => setPaused(false) }
      : { id: "fw-pause", group: "Find work", label: "Pause research", run: () => setPaused(true) },
  ];
});

export default function Discover({ id, scout }) {
  const r = route.value;
  const redirect = scout ? ["/repos", "#slots"] : r.startsWith("/pick") ? ["/repos", ""] : null;
  useEffect(() => { if (redirect) replaceTo(...redirect); }, [redirect?.[0], redirect?.[1]]);
  if (redirect) return null;
  return id ? html`<${Brief} key=${id} id=${decodeURIComponent(id)} />` : html`<${FindWork} />`;
}
