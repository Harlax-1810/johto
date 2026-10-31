import { html } from "../../lib/html.js";
import { StudioSurface } from "./studio.js";

// A task the reviewer sent back: the studio surface, with the reviewer's words pinned on top.
export default function Feedback(props) {
  return html`<${StudioSurface} ...${props} />`;
}
