// Words for Find work and the repo brief. The fixed vocabulary is lib/words.js; this holds screen wording only.
import { actionWord, domainWord, scaleWord, plural } from "../../lib/words.js";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "Oct 30" (this year) or "Oct 30, 2025". */
export function dayWords(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const s = `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  return d.getFullYear() === new Date().getFullYear() ? s : `${s}, ${d.getFullYear()}`;
}

/** "2024" or "2024-05" -> "2024" / "May 2024" for the heading list. */
export function whenWords(w) {
  const m = /^(\d{4})(?:-(\d{2}))?$/.exec(w || "");
  if (!m) return w || "";
  return m[2] ? `${MONTHS[+m[2] - 1]} ${m[1]}` : m[1];
}

/** "Modify · Backend" (scale left out when the header line names it). */
export const groupWords = (f) => [actionWord(f?.action), domainWord(f?.domain)].filter(Boolean).join(" · ");

/** "1 task done here", "1 task in progress here", both joined. */
export function yoursWords(y) {
  if (!y) return "";
  const out = [];
  if (y.in_flight) out.push(`${plural(y.in_flight, "task")} in progress here`);
  if (y.done) out.push(`${plural(y.done, "task")} done here`);
  return out.join(", ");
}

/** The one scale all these fits share ("Enterprise"), else "". */
export function sharedScale(fits) {
  const s = new Set((fits || []).map((f) => f.codebase_type));
  return s.size === 1 ? scaleWord([...s][0]) : "";
}

/** "Already yours: repeat scans double-count loans, in review" / "Already yours: an Understand task, in progress". */
export function usedWords(u) {
  const what = u.label || `an ${actionWord(u.action) || "earlier"} task`.replace(/^an ([CM])/, "a $1");
  return `${what}, ${u.stage_word}`;
}

export const ONE_SHOT_TONE = { likely: "wait", possible: "", unlikely: "", not_checked: "" };

const VERDICT_PREFIX = /^(worth diving into|worth a dive|dive|dive in|maybe|probably skip|skip)\s*[:.,-]\s*/i;

/** The brief's lede without a leading "Worth diving into:" when the verdict pill beside it says the same. */
export function ledeBody(lede) {
  const s = String(lede || "").trim();
  const m = s.match(VERDICT_PREFIX);
  if (!m) return s;
  const rest = s.slice(m[0].length);
  return rest ? rest.charAt(0).toUpperCase() + rest.slice(1) : s;
}

/**
 * An open count in words. A big group is no better than one slot; a few left is a reason to move first,
 * because someone else can take them. {text, scarce}.
 */
export const SCARCE_AT = 3;
export function openWords(n) {
  if (!n) return { text: "no open slot", scarce: false };
  if (n <= SCARCE_AT) return { text: n === 1 ? "1 slot left" : `${n} slots left`, scarce: true };
  return { text: `${n} open slots`, scarce: false };
}
