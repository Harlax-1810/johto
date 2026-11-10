"""The generation prompt: docs/dossier-spec.md plus the revisions after the first review.

SPEC_PROMPT is section 4's prompt and SPEC_SCHEMA is section 3 (the johto.dossier/1 contract), both
copied verbatim. PROMPT and SCHEMA, which the generator sends, are those with PROMPT_REVISIONS and
SCHEMA_REVISIONS applied. Each revision is (spec text, revised text); one whose revised text is already
in the spec is skipped, so the spec can take them over one by one. test_prompt_is_verbatim_from_spec
and test_schema_is_section_3_verbatim in tests/test_dossier_generate.py fail when the spec, plus these
revisions, and this module drift apart.
"""

SPEC_PROMPT = r"""You are researching an open-source application for an engineer who writes very hard evaluation tasks
for AI coding agents. Your output is a structured research dossier, not prose.

Imagine this project just got an expert engineering team with millions in budget for a year. What would
they fix, rebuild or add that the current maintainers don't have the capacity, expertise or risk
appetite for? Look for the deep problems: correctness under concurrency and partial failure, state that
must stay consistent across layers (database, API, client, cache, background jobs), data migrations that
must keep old data working, performance cliffs (N+1 queries, unbounded memory, recomputation), security
and permission models with gaps, sync and offline behavior, protocol and spec conformance, scheduling and
background work that overlaps or races, error handling that hides failures, and missing subsystems users
keep asking for. Read the code to find them. Read open and closed issues and PRs with `gh` to see what
users hit and what was tried and abandoned.

For each problem area, find the single bounded job inside it that one engineer could finish and verify in
a working session, where a strong AI agent would most likely produce something plausible but wrong:
fixing the symptom instead of the cause, missing an interaction three calls away, breaking existing data
or behavior, or skipping the part that is hard to test. Prefer problems whose difficulty comes from the
real codebase, not from obscurity. Skip anything a careful model would finish in one pass in under
15 minutes; skip work that needs paid services, special hardware or live third-party APIs that can't be
mocked; skip problems already fixed on the default branch or in an open PR.

Cover all four kinds of task the engineer can write, wherever the project supports them, and label each
opportunity's kind in `slot_fit`: Create (build something new), Modify (fix or change code), Operate (run,
configure, deploy or diagnose a running system, reproducible in Docker from a stated initial state; if the
fix is application code it is Modify), and Understand / Assess (a review, audit, explanation or plan with
no code changes, only for projects with 200+ source files; it needs ground truth a reviewer can check,
ideally a real latent problem you confirmed in the code that a strong agent should find unprompted while
weaker answers miss it, invent issues or hedge). For Operate, put the initial-state recipe in
`bounded_task.check_in_session`; for Understand / Assess, put the ground truth there.

Also teach the reader the project: what it is, how it is built, how a request or job flows through it,
which subsystems are solid and which are fragile. Ground every claim in a file and line range you read.

Write only facts, labels and short clauses within the word limits. Never write a task prompt, ticket,
goal statement, acceptance criteria or a better/worse comparison, and never address a model or a person.
The engineer writes all of that himself."""

SPEC_SCHEMA = r"""```json
{
  "schema": "johto.dossier/1",
  "id": "acme__chatd__5ad0c0de",
  "repo": "acme/chatd", "commit": "<40 hex>", "pinned_url": "https://github.com/acme/chatd/tree/<sha>",
  "generated_at": "ISO 8601", "model": "claude-opus-5-5", "effort": "high", "elapsed_s": 900, "cost_usd": 4.1,
  "headline": "≤ 16 words: what the project is, for whom",
  "verdict": {
    "promise": "high|medium|low",
    "why": "≤ 30 words: why this repo can yield tasks Claude fails on (or why not)",
    "best_fits": [{"action": "Modify", "domain": "Backend / API", "codebase_type": "Enterprise-grade", "why": "≤ 20 words"}]
  },
  "blocks": [ Block, ... ]
}
```

`blocks` is the page, in reading order. Every block has `"type"` and an optional `"title"`. Text limits
are hard limits, enforced by the validator. Pointers are `"path:line"` or `"path:start-end"` relative to
the repo root at the pinned commit; the app shows the real code for them, so blocks never copy code.

| type | fields | renders as |
|---|---|---|
| `overview` | `what` (≤ 60 words), `users` (≤ 30 words), `stack` [str ≤ 6 words, ≤ 8 items], `size` {`source_files`, `test_files`, `languages` [str]}, `run` {`setup` [cmd], `test` [cmd], `notes` (≤ 40 words)} | a fact sheet |
| `architecture` | `summary` (≤ 50 words), `nodes` [{`id`, `label` ≤ 4 words, `kind`: service\|module\|store\|ui\|worker\|external\|config, `paths` [str], `note` ≤ 15 words}], `edges` [{`from`, `to`, `label` ≤ 5 words}] (≤ 14 nodes) | a layered box-and-arrow diagram drawn by the app |
| `flow` | `summary` (≤ 30 words), `steps` [{`label` ≤ 8 words, `pointer`, `note` ≤ 20 words}] (≤ 10 steps) | a numbered walkthrough; each step's pointer expands to real code |
| `subsystems` | `items` [{`name` ≤ 4 words, `paths` [str], `what` ≤ 20 words, `health`: solid\|fragile\|neglected, `evidence` [pointer]}] | a table with a health chip |
| `opportunity` | see below | the core card |
| `code` | `pointer`, `caption` ≤ 20 words | a real-code excerpt fetched by the app |
| `facts` | `items` [{`label` ≤ 4 words, `value` ≤ 20 words}] | a definition list |
| `callout` | `tone`: info\|warn, `text` ≤ 40 words | a tinted note |
| `questions` | `items` [str ≤ 25 words] (≤ 6) | things Austin should check by hand before claiming |
| `html` | `html` (≤ 12 KB; inline SVG and CSS only, no scripts, no external URLs), `caption` ≤ 20 words | escape hatch for a diagram nothing else can draw; rendered in a sandboxed iframe |

**`opportunity`** (3 to 7 per dossier, ordered best first):

```json
{"type": "opportunity", "id": "o1",
 "title": "≤ 10 words, a label not a sentence",
 "area": "≤ 4 words, e.g. 'room state consistency'",
 "ambition": "≤ 45 words: what a funded expert team would build or fix here",
 "why_not_done": "≤ 25 words: why the maintainers haven't (size, risk, expertise)",
 "evidence": [{"pointer": "room/room.go:120-180", "fact": "≤ 22 words, what the code does now"}],
 "where_models_fail": ["≤ 25 words each: a concrete trap, wrong-but-plausible fix, or missed interaction"],
 "bounded_task": {
   "exists": true,
   "shape": "≤ 35 words: the single bounded job inside this area that fits one Johto task",
   "size": "S|M|L",
   "check_in_session": "≤ 30 words: how a reviewer can verify it within one session (tests, run, compare)"
 },
 "slot_fit": [{"action": "Modify", "domain": "Backend / API", "codebase_type": "Personal-scale"}],
 "one_shot": {"likelihood": "unlikely|possible|likely", "why": "≤ 25 words"},
 "upstream": {"related": [{"url": "...", "title": "...", "state": "open|closed|merged"}], "looks_solved": false},
 "self_contained": {"ok": true, "needs": ["≤ 6 words each, e.g. 'mock Last.fm API'"]}}
```

Rules for every text field:

- Facts, labels and short clauses. No paragraphs beyond the limits, no marketing words, no em dashes.
- Never written as an instruction to a model or a person ("Add X", "Please"), never as a ticket, user
  story, acceptance criteria, goal statement, or better/worse comparison. `ambition` and `shape` describe
  the work in the third person ("Limits survive a room reload and expiry").
- `where_models_fail` describes traps in the code, not grading criteria.

Validation (`johto/dossier/schema.py`): types, enums, word limits, pointer format, that every pointer's
path exists in the clone and its line range is within the file, ≤ 30 blocks, 3-7 opportunities,
architecture node ids referenced by edges exist, `html` blocks contain no `<script`, `on*=` handlers or
`http(s)://` URLs. Failing documents get one repair pass (the generator sends the errors back to the same
session); a second failure marks the item `failed` with the errors."""


# Revisions after the review of the first real dossier (acme/playlog, 2026-10-30). The text
# fields came out shaped like the fields Austin writes himself: where_models_fail as the worse half of a
# decision point, shape prescribing the fix, ambition as a goal statement, check_in_session as acceptance
# tests. One-shot ratings were overconfident and most opportunities were single bugs.
PROMPT_REVISIONS = [
    ("Skip anything a careful model would finish in one pass in under\n15 minutes; skip work that needs paid services",
     "Skip anything a careful model would finish in one pass in under\n15 minutes. The agent under test is Claude "
     "Opus at high effort. An async task is one prompt it works on alone for about 45 minutes with no replies; an "
     "interactive task is a session where the engineer answers as it works. A bounded task takes most of those "
     "45 minutes of a strong agent's work and is plausibly finished inside them. Skip work that needs paid services"),
    ("skip problems already fixed on the default branch or in an open PR.",
     "skip problems already fixed on the default branch or in an open PR. Rate one_shot against an agent that "
     "is told the symptom and the area. Rate unlikely only with a named reason the fix fails even when the "
     "agent knows where to look: an interaction outside the files the symptom points to, a non-obvious "
     "invariant, or existing data or behavior that breaks. Pattern bugs with a well-known fix (offset "
     "pagination over a mutating set, a missing await, a wrong variable, a filename mismatch) are likely; "
     "leave them out or mention them in a facts block. At most two opportunities may be single bugs; the rest "
     "are area-level problems with a bounded job inside. Include a Create opportunity when users asked for a "
     "missing subsystem with a mockable boundary."),
    ("for Understand / Assess, put the ground truth there.",
     "for Understand / Assess, put the ground truth there. Understand / Assess ground truth lists only defects "
     "you confirmed reachable from a production code path, with the path; unconfirmed suspicions go in "
     "questions."),
    ("Ground every claim in a file and line range you read.",
     "Ground every claim in a file and line range you read. Include one facts or code block that lays out the "
     "core data model and its lifecycle states, with pointers to the schema and the state transitions."),
    ("The engineer writes all of that himself.",
     "The engineer writes all of that himself. Do not describe how to fix anything. where_models_fail names "
     "hazards in the code, never wrong or right fixes."),
]

SCHEMA_REVISIONS = [
    ('"ambition": "≤ 45 words: what a funded expert team would build or fix here",',
     '"ambition": "≤ 45 words: the area-level gap a funded team would take on, larger than the bounded task, '
     'stated as what is missing or broken now and its consequence",'),
    ('"where_models_fail": ["≤ 25 words each: a concrete trap, wrong-but-plausible fix, or missed interaction"],',
     '"where_models_fail": ["≤ 25 words each: a hazard in the code that any change must account for (an '
     'interaction, hidden caller, invariant, side effect or test gap), stated as a fact about the code with a '
     'file name; never a candidate fix, approach or \'X instead of Y\'"],'),
    ('"shape": "≤ 35 words: the single bounded job inside this area that fits one Johto task",',
     '"shape": "≤ 35 words: the single bounded job inside this area that fits one Johto task, as its scope '
     '(code paths, behaviors, data covered) and the observable defect or missing capability; never the '
     'technique or design of a fix",'),
    ('"check_in_session": "≤ 30 words: how a reviewer can verify it within one session (tests, run, compare)"',
     '"check_in_session": "≤ 30 words: what a reviewer can observe in one session: the reproduction recipe '
     'with concrete data, the existing test files or commands, the run to compare; not assertions to write"'),
    ('`ambition` and `shape` describe\n  the work in the third person ("Limits survive a room reload and expiry").',
     '`ambition` and `shape` state\n  what is missing or broken now, in the third person ("Limits are lost on '
     'room reload and expiry").'),
    ("- `where_models_fail` describes traps in the code, not grading criteria.",
     "- `where_models_fail` names hazards in the code, never a fix, an approach or grading criteria."),
    ("| `questions` | `items` [str ≤ 25 words] (≤ 6) |",
     "| `questions` | `items` [str ≤ 25 words, each a question ending in ?] (≤ 6) |"),
]


def revise(text: str, revisions: list[tuple[str, str]]) -> str:
    for old, new in revisions:
        if new in text:
            continue
        if text.count(old) != 1:
            raise ValueError(f"revision does not match the spec text exactly once: {old[:60]!r}")
        text = text.replace(old, new)
    return text


PROMPT = revise(SPEC_PROMPT, PROMPT_REVISIONS)
SCHEMA = revise(SPEC_SCHEMA, SCHEMA_REVISIONS)
