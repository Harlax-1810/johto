# Repo dossiers: spec and contract

A dossier is a page about one promising repository. It builds Austin's understanding of the project
and lays out the big problem areas, so he can propose hard tasks himself. It answers one question:

> If this project had an expert team with millions in budget to make major improvements, what would
> they do that the current maintainers don't have the capacity for?

We *want* problems Claude (Opus 5.5, high effort) will fail on. Easy, local, one-file fixes are
worthless here. The dossier is research. It never drafts an opening prompt, a task goal, decision
points or codebase-understanding text (Johto's LLM policy, `docs/context.md` section 2).

## 1. Lifecycle

- A **pool** of vetted candidate repos is ranked by promise (`johto/scout/pool.py`, section 6).
- A **worker** keeps 10 dossiers ready or generating. It runs one generation at a time (it shares
  Austin's Claude subscription with his lane runs), always taking the best-ranked repo that has no dossier.
- When Austin **acts** on a dossier (starts a task from it) or **declines** it, it leaves the ready
  set and the worker starts the next repo right away.
- Austin can **pause** the worker. On restart, an item left in `generating` goes back to `queued`.

## 2. Storage

- `data/dossiers/index.json`:
  ```json
  {"target": 10, "paused": false,
   "items": [{"id": "acme__chatd__5ad0c0de", "repo": "acme/chatd", "commit": "<40 hex>",
              "status": "queued|generating|ready|failed|actioned|declined",
              "rank": 1, "promise": 0.82, "promise_reasons": ["..."],
              "queued_at": "...", "started_at": null, "finished_at": null,
              "progress": {"step": "reading the code", "elapsed_s": 340, "log_tail": ["..."]},
              "cost_usd": null, "error": null,
              "decline": {"reason": "too_easy|too_big|cant_run|not_interested|ai_generated|other", "note": ""} ,
              "actioned": {"task_short": "t4sk0001", "opportunity_id": "o2"}}]}
  ```
- `data/dossiers/<id>.json`: the dossier (section 3). `<id>` is `owner__name__<commit[:8]>`, lowercase.
- `data/dossiers/runs/<id>.log`: the generation's stderr and progress lines.
- Clones go in `data/cache/dossier-clones/<id>/` and are deleted after the run.

## 3. The dossier document (`johto.dossier/1`)

```json
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
 "area": "≤ 4 words, e.g. 'group state consistency'",
 "ambition": "≤ 45 words: what a funded expert team would build or fix here",
 "why_not_done": "≤ 25 words: why the maintainers haven't (size, risk, expertise)",
 "evidence": [{"pointer": "room/room.go:573-640", "fact": "≤ 22 words, what the code does now"}],
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
  the work in the third person ("Lock state survives group reload and expiry").
- `where_models_fail` describes traps in the code, not grading criteria.

Validation (`johto/dossier/schema.py`): types, enums, word limits, pointer format, that every pointer's
path exists in the clone and its line range is within the file, ≤ 30 blocks, 3-7 opportunities,
architecture node ids referenced by edges exist, `html` blocks contain no `<script`, `on*=` handlers or
`http(s)://` URLs. Failing documents get one repair pass (the generator sends the errors back to the same
session); a second failure marks the item `failed` with the errors.

## 4. Generation (`johto/dossier/generate.py`)

- Shallow-clone the repo at the pinned commit (`git clone --filter=blob:none`, then checkout).
- Run headless Claude Code in the clone: the real binary at `~/.local/bin/claude` (not the zsh alias),
  `-p`, `--model opus`, `--effort high`, read-only tools only (Read, Grep, Glob, and Bash limited to
  `git log`, `git show`, `ls`, `wc`, `find`, `rg`, `gh issue list/view`, `gh pr list/view`, `gh search`),
  no Edit/Write, `--strict-mcp-config`, settings from the project only. Hard timeout 35 minutes.
  Check `claude --help` for exact flag names before relying on them.
- The final message must contain the dossier JSON between the lines `<<<DOSSIER` and `DOSSIER>>>`.
- Record elapsed time and cost (from `--output-format json`) on the item.
- Delete the clone afterwards, even on failure.

### The prompt (embed verbatim, then append the schema from section 3 and the repo facts)

> You are researching an open-source application for an engineer who writes very hard evaluation tasks
> for AI coding agents. Your output is a structured research dossier, not prose.
>
> Imagine this project just got an expert engineering team with millions in budget for a year. What would
> they fix, rebuild or add that the current maintainers don't have the capacity, expertise or risk
> appetite for? Look for the deep problems: correctness under concurrency and partial failure, state that
> must stay consistent across layers (database, API, client, cache, background jobs), data migrations that
> must keep old data working, performance cliffs (N+1 queries, unbounded memory, recomputation), security
> and permission models with gaps, sync and offline behavior, protocol and spec conformance, scheduling and
> background work that overlaps or races, error handling that hides failures, and missing subsystems users
> keep asking for. Read the code to find them. Read open and closed issues and PRs with `gh` to see what
> users hit and what was tried and abandoned.
>
> For each problem area, find the single bounded job inside it that one engineer could finish and verify in
> a working session, where a strong AI agent would most likely produce something plausible but wrong:
> fixing the symptom instead of the cause, missing an interaction three calls away, breaking existing data
> or behavior, or skipping the part that is hard to test. Prefer problems whose difficulty comes from the
> real codebase, not from obscurity. Skip anything a careful model would finish in one pass in under
> 15 minutes; skip work that needs paid services, special hardware or live third-party APIs that can't be
> mocked; skip problems already fixed on the default branch or in an open PR.
>
> Cover all four kinds of task the engineer can write, wherever the project supports them, and label each
> opportunity's kind in `slot_fit`: Create (build something new), Modify (fix or change code), Operate (run,
> configure, deploy or diagnose a running system, reproducible in Docker from a stated initial state; if the
> fix is application code it is Modify), and Understand / Assess (a review, audit, explanation or plan with
> no code changes, only for projects with 200+ source files; it needs ground truth a reviewer can check,
> ideally a real latent problem you confirmed in the code that a strong agent should find unprompted while
> weaker answers miss it, invent issues or hedge). For Operate, put the initial-state recipe in
> `bounded_task.check_in_session`; for Understand / Assess, put the ground truth there.
>
> Also teach the reader the project: what it is, how it is built, how a request or job flows through it,
> which subsystems are solid and which are fragile. Ground every claim in a file and line range you read.
>
> Write only facts, labels and short clauses within the word limits. Never write a task prompt, ticket,
> goal statement, acceptance criteria or a better/worse comparison, and never address a model or a person.
> The engineer writes all of that himself.

## 5. API (`johto/web/api_dossier.py`, routes registered by `app.py`)

| Method and path | Request | Response |
|---|---|---|
| `GET /api/dossiers` | | `{"target": 10, "paused": bool, "worker": {"running": bool, "current": id\|null}, "items": [DossierSummary]}` (ready first by rank, then generating, queued; actioned/declined last 20) |
| `GET /api/dossiers/{id}` | | `{"item": IndexItem, "dossier": Dossier\|null}` |
| `POST /api/dossiers/{id}/decline` | `{"reason", "note"?}` | `{"item"}`; kicks the worker |
| `POST /api/dossiers/{id}/act` | `{"opportunity_id"?, "task_short"?}` | `{"item"}`; kicks the worker |
| `POST /api/dossiers/{id}/restore` | | `{"item"}` (back to ready) |
| `POST /api/dossiers/refill` | | `202 {"worker"}` |
| `PUT /api/dossiers/worker` | `{"paused": bool}` | `{"worker", "paused"}` |

`DossierSummary` = `IndexItem` + `{"headline", "verdict", "top": [{"id", "title", "area", "one_shot", "slot_fit", "size"}] (≤ 3), "fits": [{"action", "domain", "codebase_type", "open"}]}`, the last from `data/slots.json`.

The worker is started from the app's lifespan (`api_dossier.start_worker()`), runs in a background
thread, and also runs standalone: `johto dossier run` (loop), `johto dossier once <owner/name>`,
`johto dossier list`.

## 6. Pool (`johto/scout/pool.py`)

`ranked_pool(limit=40) -> [{"full_name", "commit", "pinned_url", "language", "promise", "reasons": [str], "fits": [combo with open count]}]`

Promise ranks repos most likely to hold hard, bounded, self-contained problems:

- passes the codebase check for at least one codebase type that has open slots, weighted by open count;
- no AI-authorship signals; not a fork; not SWE-bench; not already used in Studio more than twice;
- lane-friendly language (Go, TypeScript, JavaScript, Python first; Rust, Java and others lower, since the
  lane image lacks their toolchains);
- real size: 150-3,000 source files is the sweet spot (enterprise-grade is welcome);
- a test suite, a docker-compose or other way to run it locally, recent activity (push in the last 6 months),
  open issues;
- data-heavy or stateful apps (databases, sync, jobs, multi-user) rank above stateless tools.

The pool is filled by the existing discover and vet code (`johto/scout/`), with new queries aimed at
large self-hostable applications across all eight domains.
