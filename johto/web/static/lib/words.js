// The fixed vocabulary (spec.md 5.2 and 5.4): the plain words every screen uses for data. FROZEN (lead).
// Screen-specific wording lives in each builder's own modules.
import { plural } from "./format.js";

export { plural };

// ---------- Slots ----------

export const ACTION_WORD = { "Create": "Create", "Modify": "Modify", "Operate": "Operate", "Understand / Assess": "Understand" };
export const DOMAIN_WORD = {
  "Backend / API": "Backend", "Web frontend": "Web", "Data / DB": "Data", "DevOps / cloud": "DevOps",
  "Dev tooling": "Dev tools", "Developer tools": "Dev tools", "AI agents": "AI agents", "Games": "Games",
  "Other software": "Other", "Mobile": "Mobile",
};
export const SCALE_WORD = { "Enterprise-grade": "Enterprise", "Personal-scale": "Personal" };
export const STYLE_WORD = { "Casual prose": "Casual", "Explicit spec": "Spec", "Error / artifact brief": "Error report" };
export const MODE_WORD = { "Interactive/pairing": "Interactive", "Async/autonomous": "Async", "Async": "Async" };

const w = (map, v) => (v == null ? "" : map[v] || String(v));
export const actionWord = (v) => w(ACTION_WORD, v);
export const domainWord = (v) => w(DOMAIN_WORD, v);
export const scaleWord = (v) => w(SCALE_WORD, v);
export const styleWord = (v) => w(STYLE_WORD, v);
export const modeWord = (v) => w(MODE_WORD, v);

/** "Understand · Backend · Enterprise", plus " · Casual · Interactive" when variant is true and present. */
export function slotWords(slot, { scale = true, variant = false } = {}) {
  if (!slot) return "";
  const parts = [actionWord(slot.action), domainWord(slot.domain)];
  if (scale) parts.push(scaleWord(slot.codebase_type));
  if (variant && slot.prompt_style) parts.push(styleWord(slot.prompt_style));
  if (variant && slot.interaction_mode) parts.push(modeWord(slot.interaction_mode));
  return parts.filter(Boolean).join(" · ");
}

/** "Casual · Interactive" */
export const variantWords = (slot) => [styleWord(slot?.prompt_style), modeWord(slot?.interaction_mode)].filter(Boolean).join(" · ");

const ACTION_SENTENCE = {
  "Create": "Create builds something that doesn't exist yet.",
  "Modify": "Modify changes how something already works.",
  "Operate": "Operate runs, configures or fixes a running system.",
  "Understand / Assess": "Understand asks for a review or explanation with no code changes.",
};
const STYLE_SENTENCE = {
  "Casual prose": "Casual is a short message, about 20 to 60 words.",
  "Explicit spec": "Spec is a fuller written spec.",
  "Error / artifact brief": "Error report is a pasted error or file with a short note.",
};
const MODE_SENTENCE = {
  "Interactive/pairing": "Interactive means you answer Claude while it works.",
  "Async/autonomous": "Async means Claude works alone until it is done.",
};
/** The claim panel's one sentence on what a slot means (spec 5.4). */
export function slotSentence(slot) {
  if (!slot) return "";
  return [ACTION_SENTENCE[slot.action], STYLE_SENTENCE[slot.prompt_style], MODE_SENTENCE[slot.interaction_mode]].filter(Boolean).join(" ");
}

/** An open count is a good sign only at 20 or more. */
export const countTone = (n) => (n >= 20 ? "ok" : "neutral");

// ---------- Findings ----------

export const RESULT_WORDS = { credit: "Got it right", partial: "Partly right", missed: "Claude missed it", unknown: "Not known yet" };
export const BASIS_WORDS = {
  executed: "Seen in a real run", isolated: "Seen by running part of the code", traced: "Read in the code only",
  claimed: "Claude's claim, not checked", none: "Not checked",
};
export const resultWords = (v) => RESULT_WORDS[v] || RESULT_WORDS.unknown;
export const basisWords = (v) => BASIS_WORDS[v] || BASIS_WORDS.none;
export const severityWords = (v) => (v === "high" ? "Serious" : "");

// ---------- Research ----------

export const ONE_SHOT_WORDS = { unlikely: "unlikely", possible: "possible", likely: "likely", not_checked: "not checked" };
export const oneShotWords = (v) => `Claude in one go: ${ONE_SHOT_WORDS[v] || "not checked"}`;
export const SIZE_WORDS = { S: "Small", M: "Medium", L: "Large" };
export const sizeWords = (v) => SIZE_WORDS[v] || "";
export const VERDICT_WORDS = { dive: "Worth diving into", maybe: "Maybe", skip: "Probably skip" };
export const VERDICT_TONE = { dive: "ok", maybe: "wait", skip: "neutral" };

// ---------- Runs and tests ----------

/** "validate-1" -> "validation 1", "calib-2" -> "calibration 2". */
export function runWords(name) {
  const m = /^(calib|validate)-(\d+)$/.exec(name || "");
  return m ? `${m[1] === "calib" ? "calibration" : "validation"} ${m[2]}` : "";
}

/** Tests in words. A result hidden by a pipe (null) says nothing: never "failed" unless last_ok === false. */
export function testsWords(lastOk) {
  if (lastOk === true) return "tests passed";
  if (lastOk === false) return "tests failing";
  return "";
}

// ---------- Task labels ----------

/** "vidlabel · Task t4sk0002" -> {repo: "vidlabel", problem: ""}; "Shelfnote · Stale reminder" -> both parts. */
export function splitLabel(label) {
  const s = String(label || "");
  const at = s.indexOf(" · ");
  const repo = at < 0 ? s : s.slice(0, at);
  let problem = at < 0 ? "" : s.slice(at + 3);
  if (/^Task [a-z0-9]{6,}$/i.test(problem)) problem = "";   // a Studio task name is not a problem
  if (/^Task [a-z0-9]{6,}$/i.test(repo)) return { repo: "", problem };
  return { repo, problem };
}

/** The task's name in words: "Vidlabel · Repeat scans", else repo + slot action ("vidlabel · Understand"). */
export function taskName(t) {
  const { repo, problem } = splitLabel(t?.label);
  const name = repo || (t?.repo?.full_name || "").split("/").pop() || "Task";
  return problem ? `${name} · ${problem}` : `${name} · ${actionWord(t?.slot?.action) || "task"}`;
}

// ---------- The lane (one line per machine) ----------

function laneTurn(l) {
  const m = l.monitor && l.run && l.monitor.run === l.run.run ? l.monitor : null;
  if (!m) return "question";
  if ((m.turn_class === "substantive" || m.turn_class === "progress") && m.question) return "question";
  return m.turn_class || "question";
}

/** {text, tone} for the Mac lane: tone is wait | work | stop | idle | busy (dot kinds). */
export function laneWords(l) {
  if (!l) return { text: "free", tone: "idle" };
  if (l.rate_limited) return { text: "Claude rate limited", tone: "stop" };
  switch (l.state) {
    case "preparing": return { text: "setting up", tone: "busy" };
    case "needs_login": return { text: "log in to Claude", tone: "wait" };
    case "ready_for_prompt": return { text: "paste your prompt", tone: "wait" };
    case "working": return { text: "Claude working", tone: "work" };
    case "waiting": {
      const c = laneTurn(l);
      if (c === "question") return { text: "Claude waiting", tone: "wait" };
      if (c === "permission") return { text: "Claude asks permission", tone: "wait" };
      if (c === "problem") return { text: "Claude stopped", tone: "stop" };
      if (c === "background") return { text: "background work", tone: "busy" };
      return { text: "Claude ended its turn", tone: "idle" };
    }
    case "permission": return { text: "Claude asks permission", tone: "wait" };
    case "setup_failed": return { text: "setup failed", tone: "stop" };
    case "exited": case "collecting": return { text: "collecting", tone: "busy" };
    default:
      if (l.foreign) return { text: "ports held by another lane", tone: "stop" };
      return { text: "free", tone: "idle" };
  }
}

// ---------- Task stage for the rail ----------

const FLIGHT = {
  claiming: ["Claiming", "busy"], prompt: ["Prompt", "ink"], waiting_calib: ["Queued", "busy"], waiting_validate: ["Queued", "busy"],
  calibrating: ["Run", "work"], validating: ["Run", "work"], collecting: ["Run", "busy"], collect_failed: ["Run", "stop"],
  decide: ["Decide", "ink"], details: ["Write", "ink"], rate: ["Write", "ink"],
  ready_to_save: ["Submit", "ink"], saving: ["Submit", "busy"], qc_running: ["Submit", "busy"], fix_qc: ["Submit", "stop"],
  ready_to_submit: ["Submit", "ink"], submitting: ["Submit", "busy"], studio: ["Submit", "ink"], sent_back: ["Sent back", "wait"],
};
const DONE = { in_review: ["In review", "idle"], accepted: ["Accepted", "ok"], rejected: ["Rejected", "stop"] };

/**
 * Where a task sits for the rail: {group: "flight"|"submitted"|"gone", word, tone}. A lane that waits on him for
 * this task turns the word into "Asks you" (amber).
 */
export function taskStage(t, { queue = [], lane = null } = {}) {
  const s = t?.stage || "";
  if (DONE[s]) return { group: "submitted", word: DONE[s][0], tone: DONE[s][1] };
  if (["dropped", "discarded", "unclaimed"].includes(s)) return { group: "gone", word: "", tone: "idle" };
  const asks = queue.some((q) => q.short === t.short && (q.lane || (q.attention && q.lane_need)));
  if (asks) return { group: "flight", word: "Asks you", tone: "wait" };
  if (lane?.run?.short === t.short && lane.state === "working") return { group: "flight", word: "Run", tone: "work" };
  // "details" before any validation run means the prompt is done and the run is next, not writing.
  if (s === "details" && !/^validate-/.test(t?.stage_run || "")) return { group: "flight", word: "Start run", tone: "ink" };
  const f = FLIGHT[s] || [t?.stage_label || "In flight", "ink"];
  return { group: "flight", word: f[0], tone: f[1] };
}
