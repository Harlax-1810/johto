import { api } from "./api.js";
import { app } from "./store.js";

// Coming back to this tab (often from editing in Studio) asks the server to bring Studio's edits in.
// The server also checks once a minute; this just makes the round trip feel immediate.
let last = 0;
function sync() {
  if (document.visibilityState !== "visible" || app.value?.server?.mode !== "real") return;
  const now = Date.now();
  if (now - last < 5000) return;
  last = now;
  api.post("/api/studio/sync", {}).catch(() => {});
}
document.addEventListener("visibilitychange", sync);
window.addEventListener("focus", sync);
