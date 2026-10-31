export const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;

export function est(s) {
  if (s == null) return "";
  if (s < 60) return "~1m";
  if (s < 3600) return `~${Math.round(s / 60)}m`;
  return `~${(s / 3600).toFixed(1).replace(/\.0$/, "")}h`;
}

export function clock(sinceIso, now = Date.now()) {
  if (!sinceIso) return "";
  const s = Math.max(0, Math.floor((now - Date.parse(sinceIso)) / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${m}:${String(ss).padStart(2, "0")}`;
}

export function mins(m) {
  if (m == null) return "";
  return m < 10 ? m.toFixed(1).replace(/\.0$/, "") : String(Math.round(m));
}

export function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function hhmm(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export const shortSlot = (s) => !s ? "" :
  [s.action === "Understand / Assess" ? "Understand" : s.action,
   (s.domain || "").replace(" / API", "").replace("Web frontend", "Web"),
   (s.codebase_type || "").replace("-scale", "").replace("-grade", "")].join(" · ");

export const fullSlot = (s) => !s ? "" :
  [s.action, s.domain, s.codebase_type, s.prompt_style, s.interaction_mode].filter(Boolean).join(" · ");

export const oneShotWord = (o) => ({ likely: "likely", possible: "possible", unlikely: "unlikely", not_checked: "not checked" }[o?.word] || "not checked");

export function cutWords(text, n) {
  if (!text || text.length <= n) return text || "";
  const cut = text.slice(0, n);
  return cut.slice(0, cut.lastIndexOf(" ")) + "…";
}
