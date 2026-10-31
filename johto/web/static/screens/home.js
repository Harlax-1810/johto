import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { queue, running, selected, select, loadTask, tasks, openDrawer, rememberShown, markSurfaceChange } from "../lib/store.js";
import { Rail } from "../components/rail.js";
import { focusTerminal } from "../components/drawer.js";
import { SURFACES } from "./surfaces/index.js";
import { setPrimaryHandler, hideItem } from "../app.js";

/** Home: the queue on the left, the selected step's work surface on the right. */
export default function Home() {
  const item = selected.value;
  const [err, setErr] = useState(null);
  const short = item?.short;
  useEffect(() => {
    setErr(null);
    if (short) loadTask(short).catch((e) => setErr(e.message));
  }, [short]);
  const task = short ? tasks.value.get(short) : null;
  const Comp = item ? SURFACES[item.surface] : null;
  const onSelect = (it) => {
    select(it.id);
    if (it.lane || it.surface === "lane") { openDrawer("row"); setTimeout(() => focusTerminal(), 160); }
  };
  // Keyed by surface and task, not by id or kind: an id carries a hash of the task's state, and the kind
  // flips between save and submit as he edits; a remount would drop his caret while he types.
  const key = item ? `${item.surface}:${item.short || item.kind}` : "none";
  rememberShown(item);
  const lastKey = useRef(key);
  if (lastKey.current !== key) { lastKey.current = key; markSurfaceChange(); }
  let body;
  if (!item) body = html`<section class="surface"></section>`;
  else if (err && !task) body = html`<section class="surface"><div class="scol">${err}</div></section>`;
  else if (short && !task) body = html`<section class="surface"></section>`;
  else if (!Comp) body = html`<section class="surface"><div class="scol faint">${item.title}</div></section>`;
  else body = html`<${Comp} key=${key} item=${item} task=${task} setPrimary=${setPrimaryHandler} />`;
  return html`<div class="frame">
    <${Rail} queue=${queue.value} running=${running.value} selectedId=${item?.held ? item.held.id : item?.id} onSelect=${onSelect} onHide=${hideItem} />
    ${body}
  </div>`;
}
