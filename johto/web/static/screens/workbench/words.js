// Words the task page uses that lib/words.js doesn't hold. Mercor's rules are quoted from the onboarding doc as
// docs/rules.md records them (rules only, never examples of wording).
import { slotWords, variantWords } from "../../lib/words.js";

export const FIELD_NAME = {
  opening_prompt: "Opening prompt", goal: "Goal", decision_points: "Decision points", codebase: "Codebase understanding",
  initial_state: "Initial state", ratings: "Ratings", validation_notes: "Validation notes", reviewer_notes: "Notes for the reviewer",
};
export const FIELD_LOWER = Object.fromEntries(Object.entries(FIELD_NAME).map(([k, v]) => [k, v.charAt(0).toLowerCase() + v.slice(1)]));

/** "Understand · Backend · Enterprise · Casual · Interactive" */
export const slotLine = (slot) => [slotWords(slot, { scale: true }), variantWords(slot)].filter(Boolean).join(" · ");

export const ASKS = {
  goal: [
    "One to three sentences, more specific than the prompt. Name the real problem and what a proper solution has to do.",
    "Give the context a reviewer needs who knows the domain but not this repo.",
    "Checkable: a reviewer can tell from it whether the task was done.",
    "Outcomes, not methods, so other valid approaches still pass.",
    "Grade only what the prompt asks for.",
    "Name the shortcuts that don't count.",
  ],
  decision_points: [
    "Two or three points. Studio needs at least 15 words.",
    "Each point says what a strong model would do and what a weaker one would do, tied to a behavior.",
    "Specific to this repo.",
  ],
  codebase: [
    "Three to five sentences. Studio needs at least 15 words.",
    "Say what the app is for, its main stack and roughly how big it is.",
    "Name the modules, files or flows the task touches.",
    "Name the actual mechanism that makes it hard.",
    "Disclose related upstream issues and pull requests.",
  ],
  validation_notes: [
    "Say what happened in the validation run and why you gave these ratings.",
    "Did it fully solve the task, where did it struggle or go wrong, and how much did you steer.",
    "Write down every failure you saw, including small ones.",
    "The goal, decision points and validation notes share a common thread.",
  ],
  initial_state: ["Describe how to reach the starting state."],
  reviewer_notes: ["Optional. Use it to answer an automatic check you disagree with."],
  ratings: [
    "One-shot: did Claude complete the task from the opening prompt alone, in one turn? Yes drops the task.",
    "Task success 1 to 5: 1 failed, 2 partial with major issues, 3 done but poor, 4 minor issues, 5 complete.",
    "Steering 1 to 5 runs backwards: 1 is constant input, 5 is none.",
    "Rate honestly. A senior reviewer re-runs the final code.",
  ],
};

export const SCOPE_OPTIONS = [
  { value: "explicit", label: "Asked", hint: "The prompt asked for it" },
  { value: "necessary", label: "Needed", hint: "Needed to do what was asked" },
  { value: "uncertain", label: "Unsure" },
  { value: "outside", label: "Outside", hint: "Outside the task" },
];

export const numberWord = (n) => ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"][n] || String(n);

/** Oct 31, from an ISO time. */
export function dayWords(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(+d) ? "" : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export const clip = (s, n) => { const t = String(s || "").replace(/\s+/g, " ").trim(); return t.length > n ? `${t.slice(0, n - 1).trimEnd()}…` : t; };
