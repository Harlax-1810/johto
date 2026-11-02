# Johto rules

Every rule johto knows, grouped by stage. The line under each rule reads
`id · applies to · source · date · status`. When two sources disagree, the newer rule wins and the
older one is marked `superseded by <id>`. `johto rules` prints this file; `johto rules --field goal`
prints one field's rules; `johto rules --lessons` prints what reviewers asked for on your tasks.

Sources: the onboarding doc (Project Johto Onboarding Doc, 2026-10-30), Studio's form help text, the
AutoQC rubric, Mercor's LLM policy and reviewer feedback, and the task order one writer agreed with
Mercor staff (no calibration runs). Check that order with Mercor before relying on it for your own tasks.

## Picking a repo and slot

- The slot's task type, domain, codebase type, prompt style and interaction mode are seeded and can't
  be changed. The task has to fit all of them; one that drifts into another category is sent back.
  `slot-fit · slot · Onboarding doc, Task Scope; Studio form help · 2026-10-30 · active`
- The task's subject belongs to the seeded domain (an AI agents slot involves LLMs or agents, a Games
  slot a game, and so on).
  `slot-domain · slot · AutoQC rubric, Seeded Domain Match · 2026-10-30 · active`
- If the core ask is a code change to the app, it's Modify, even when it starts from an ops symptom.
  Operate is work on the running system, with at most a small code fix at the end.
  `slot-modify-vs-operate · slot · Onboarding doc, Task Types; AutoQC rubric, Seeded Action Match · 2026-10-30 · active`
- Understand / Assess means read and judge only. A task that asks for code changes is Modify.
  `slot-understand-no-code · slot · Onboarding doc, Task Types · 2026-10-30 · active`
- The repo is a real application people use: not a library, framework, SDK, runtime, build tool,
  tutorial, algorithm collection or curated list.
  `repo-real-app · repo · Onboarding doc, Codebase; AutoQC Codebase Check · 2026-10-30 · active`
- The repo URL is pinned to a commit: https://github.com/<owner>/<repo>/tree/<40-character sha>.
  Studio rejects branch links.
  `repo-pinned · repo · Studio form, Repo URL help · 2026-10-30 · active`
- Enterprise-grade: 200 or more source files, real dependencies and a real test suite.
  `repo-size-enterprise · repo · Onboarding doc, Codebase; AutoQC Codebase Check · 2026-10-30 · active`
- Personal-scale: a real personal project of tens to low hundreds of files with a runnable test
  suite. Codebase Check fails under 20 source files or above about 1,500. Empty repos fail AutoQC.
  `repo-size-personal · repo · Onboarding doc, Codebase; AutoQC Codebase Check · 2026-10-30 · active`
- Everything runs from the repo or Docker. No paid services, private data, special hardware or live
  third-party APIs (mock them).
  `repo-self-contained · repo · Onboarding doc, Codebase · 2026-10-30 · active`
- There is a way to check the work: tests, a build, a typecheck, or comparing output before and after.
  `repo-verifiable · repo · Onboarding doc, Codebase · 2026-10-30 · active`
- No SWE-bench-Verified repos (django, sympy, scikit-learn, sphinx, matplotlib, pytest, xarray,
  astropy, pylint, requests, seaborn, flask), no forks of someone else's project, and no repos that
  would need heavy cleanup.
  `repo-avoid · repo · Onboarding doc, Codebase · 2026-10-30 · active`
- No mostly AI-generated repos, such as ones with CLAUDE.md or AGENTS.md files or many Claude or
  Codex co-authored commits.
  `repo-not-ai · repo · Onboarding doc, Codebase · 2026-10-30 · active`
- One repo can carry any number of tasks, as long as each is a new problem and the seeds still fit.
  `repo-reuse · repo · Onboarding doc, Codebase · 2026-10-30 · active`
- The prompt is in English; prefer repos whose code, comments and README are mostly English. Work
  around license-restricted folders.
  `repo-english · repo · Onboarding doc, Codebase · 2026-10-30 · active`
- One bounded problem with one clear target. It can run deep across the codebase, but not three
  projects stapled together.
  `task-one-problem · task · Onboarding doc, Task Scope; AutoQC rubric, Task Scope · 2026-10-30 · active`
- Success can be checked during the session. Nothing like "works at scale", several machines, or
  hours-long criteria.
  `task-in-session · task · Onboarding doc, Task Scope · 2026-10-30 · active`
- Not already solved: no merged fix upstream, and the premise is true at the pinned commit. Not a
  near-duplicate of another task.
  `task-not-solved · task · Onboarding doc, Task Scope · 2026-10-30 · active`
- Hard for experts: as hard as the hardest task in a typical work week. Enterprise is senior (L5+)
  work that takes a model 30+ minutes; personal-scale is the hardest week of a personal project.
  `task-hard · task · Onboarding doc, Difficulty Check · 2026-10-30 · active`
- Fair: the difficulty comes from the real codebase and a realistic ask, a skilled engineer could
  solve it with the information given, and nothing is contrived to trip the model.
  `task-fair · task · Onboarding doc, Behavioral signal; Before you submit · 2026-10-30 · active`
- There is an approach most senior engineers would prefer where models could differ. Coin-flip
  preferences don't count.
  `task-better-worse · task · Onboarding doc, Behavioral signal · 2026-10-30 · active`

## Opening prompt by style

- The opening prompt is the only text the model sees at the start. Studio needs at least 8 words.
  `prompt-only-text · opening_prompt · Onboarding doc, Casual Prose; Studio form · 2026-10-30 · active`
- No greetings, "please" or "could you", role-play openers ("You are a..."), form labels, or leftover
  markdown links or bold. Numbered requirements and headings are fine in explicit specs.
  `prompt-no-tells · opening_prompt · AutoQC rubric, No Formatting Tells · 2026-10-30 · active`
- It doesn't hand over the diagnosis, the fix, or the file and line to change. Debug prompts describe
  the symptom. Context like "started after the upgrade" or "only on CI" is fine.
  `prompt-no-solve · opening_prompt · AutoQC rubric, Doesn't Solve It · 2026-10-30 · active`
- Nothing private leaks: no checklist, rubric, goal or decision points, in full or in part.
  `prompt-no-leak · opening_prompt · AutoQC rubric, Nothing Private Leaks · 2026-10-30 · active`
- Casual prose reads like a Slack message or ticket from a colleague: short, direct, points at
  something concrete in the repo, doesn't need polish. No step lists.
  `casual-voice · opening_prompt · Onboarding doc, Casual Prose · 2026-10-30 · active`
- Casual prose is about 20-60 words with at most one checkable clause, never a partial list of
  criteria. A casual prompt of about 200 words that reads like a template fails AutoQC.
  `casual-length · opening_prompt · Onboarding doc, Casual Prose; AutoQC rubric, Prompt Style Fit · 2026-10-30 · active`
- Casual prose leaves realistic ambiguity in.
  `casual-ambiguity · opening_prompt · Onboarding doc, Casual Prose · 2026-10-30 · active`
- Explicit spec reads like a written ticket: a title or summary line, context, numbered requirements,
  and relevant file paths or pasted context.
  `spec-ticket · opening_prompt · Onboarding doc, Explicit Spec · 2026-10-30 · active`
- Explicit spec has complete acceptance criteria: every requirement has a pass condition or is
  dropped. These criteria are part of the spec, not a leak.
  `spec-done-when · opening_prompt · Onboarding doc, Explicit Spec; AutoQC rubric, Nothing Private Leaks · 2026-10-30 · active`
- Explicit spec lists requirements, not procedures ("Step 1: open X" is out).
  `spec-no-steps · opening_prompt · Onboarding doc, Explicit Spec · 2026-10-30 · active`
- Explicit spec uses proper spelling and punctuation, never all lowercase, and is usually about
  450-1,200 words.
  `spec-clean · opening_prompt · Onboarding doc, Explicit Spec · 2026-10-30 · active`
- Error brief: paste a real error, stack trace or log first, then one short casual line.
  `error-artifact-first · opening_prompt · Onboarding doc, Error Brief · 2026-10-30 · active`
- Error brief: the artifact is the criterion. No acceptance criteria or extra requirements; success
  means the error is gone and its cause is fixed.
  `error-no-criteria · opening_prompt · Onboarding doc, Error Brief · 2026-10-30 · active`
- Error brief: the error really happens at the pinned commit. Don't fabricate or edit the log.
  `error-real · opening_prompt · Onboarding doc, Error Brief · 2026-10-30 · active`

## Lanes and runs

- No calibration runs. A task goes straight to the validation lane.
  `no-calib · run · Task order agreed with Mercor staff · 2026-10-31 · active`
- Run the prompt in a calibration lane before writing the goal, and note what happened in the
  Difficulty Check box.
  `calib-first · run · Onboarding doc, Workflow step 4; Difficulty Check · 2026-10-30 · superseded by no-calib`
- Only the opening prompt has to be written before the validation run.
  `prompt-before-run · run · Task order agreed with Mercor staff · 2026-10-31 · active`
- Run AutoQC on the opening prompt before starting the validation lane. A prompt change after the
  run means a new run (t4sk0003 needed a second one).
  `prompt-qc-first · run · johto plan, history analysis · 2026-10-31 · active`
- Before a second validation run, check the prompt changed. t4sk0004's validate-2 re-ran the same
  prompt by mistake.
  `rerun-changed-prompt · run · johto plan, history analysis · 2026-10-31 · active`
- Every run gets a new lane, set up in a new, empty folder. A used lane picks up the earlier
  conversation (even with --new it keeps the code changes), and collect then merges both runs into
  one that can't be graded.
  `lane-new-folder · lane · Onboarding doc, Setup · 2026-10-30 · active`
- Give setup the pinned Repo URL; without a commit it starts from the latest one.
  `lane-pinned-url · lane · Onboarding doc, Setup · 2026-10-30 · active`
- Log in with your own Claude account and use Opus 5.5 at High effort, set before pasting the
  prompt. johto starts lanes with that model and effort.
  `lane-model · lane · Onboarding doc, Workflow step 4; Setup · 2026-10-30 · active`
- Start clean: get the repo running in the lane and pull in what the prompt depends on (the pasted
  error, seeded state), but add no CLAUDE.md, notes or hints that point at the answer.
  `lane-clean-start · lane · Onboarding doc, Difficulty Check · 2026-10-30 · active`
- If Claude solves it cleanly on the first try, stop, rework the prompt or pick a harder problem, and
  log it in the Discarded Run Log (johto discard).
  `one-shot-drop · run · Onboarding doc, Time; Difficulty Check · 2026-10-30 · active`
- At most 1-1.5 hours of Claude time per task, about 2 hours end to end. Keep more than one task
  going.
  `time-budget · run · Onboarding doc, Time · 2026-10-30 · active`
- A useful target is 1 hour of focused time per task. Waiting on GPT Pro isn't focused time.
  `time-target · run · Austin, johto plan · 2026-10-31 · active`
- Claude minutes is the total time Claude Code was running across all turns. johto counts the
  minutes Claude was working, not the minutes it sat waiting for you.
  `minutes-working · minutes · Studio form, Claude minutes help; johto plan · 2026-10-31 · active`

## Interaction modes

- Run by the slot's seeded interaction mode, even when it isn't the style's usual one. Studio seeds
  casual prose slots as Async/autonomous too (t4sk0003).
  `mode-follows-slot · mode · Studio seeds · 2026-10-30 · active`
- Casual prose tasks are iterative, not async; explicit specs are mostly async.
  `casual-iterative · mode · Onboarding doc, Casual Prose; Explicit Spec · 2026-10-30 · superseded by mode-follows-slot`
- Async/autonomous: paste the prompt once, unchanged, and let it run to the end. No follow-ups or
  steering; one prompt in the run.
  `async-one-prompt · mode · Onboarding doc, Difficulty Check; Before you submit · 2026-10-30 · active`
- Interactive/pairing: reply as a real colleague would. Answer its questions, but don't hand it the
  answer or add new requirements. Keep track of turns and steering.
  `interactive-colleague · mode · Onboarding doc, Difficulty Check · 2026-10-30 · active`
- Work with it the way you really would: realistic follow-ups, no artificially withheld hints.
  `realistic-followups · mode · Onboarding doc, Difficulty Check · 2026-10-30 · active`
- The difficulty is in the opening prompt, not added through follow-ups.
  `hard-from-start · mode · Onboarding doc, Difficulty Check · 2026-10-30 · active`

## High-level goal

- The goal may be written after the validation run starts.
  `goal-after-start · goal · Task order agreed with Mercor staff · 2026-10-31 · active`
- Write the goal and decision points before the validation run.
  `goal-before-run · goal · Onboarding doc, Workflow step 5; Before you submit · 2026-10-30 · superseded by goal-after-start`
- Give the context a reviewer needs who knows the domain but not this repo: background and
  repo-specific concepts, especially when the prompt is casual prose.
  `goal-context · goal · Onboarding doc, High-Level Task Goal · 2026-10-30 · active`
- 1-3 sentences, more specific than the prompt. Name the real problem and what a proper solution has
  to do. More sentences only to add context.
  `goal-specific · goal · Onboarding doc, High-Level Task Goal; AutoQC rubric · 2026-10-30 · active`
- Checkable: a reviewer can tell from it whether the task was done. Nothing like "make it better".
  `goal-checkable · goal · Onboarding doc, High-Level Task Goal · 2026-10-30 · active`
- Outcomes, not methods, so other valid approaches still pass.
  `goal-outcomes · goal · Onboarding doc, High-Level Task Goal · 2026-10-30 · active`
- Grade only what the prompt asks for.
  `goal-only-asked · goal · Onboarding doc, High-Level Task Goal · 2026-10-30 · active`
- Name the shortcuts that don't count, like loosening tests, hardcoding, or claiming success with
  known gaps.
  `goal-must-nots · goal · Onboarding doc, High-Level Task Goal · 2026-10-30 · active`

## Decision points

- Decision points, difficulty notes, ratings and validation notes can all be written after the
  validation run.
  `fields-after-run · decision_points, codebase, ratings, validation_notes · Task order agreed with Mercor staff · 2026-10-31 · active`
- 2-3 points. Studio needs at least 15 words.
  `dp-count · decision_points · Onboarding doc, Decision Points; Studio form · 2026-10-30 · active`
- Each point says what a strong model would do and what a weaker one would do, tied to a behavior:
  scope, verification, honesty about what it did, root cause vs symptom, clarifying vs guessing,
  repo conventions, following instructions.
  `dp-better-worse · decision_points · Onboarding doc, Decision Points; Behavioral signal · 2026-10-30 · active`
- Specific to this repo. "Better runs the tests, worse doesn't" is true of any task.
  `dp-specific · decision_points · Onboarding doc, Decision Points; AutoQC rubric · 2026-10-30 · active`

## Difficulty & codebase

- 3-5 sentences. Studio needs at least 15 words.
  `cb-length · codebase · Onboarding doc, Difficulty & Codebase Understanding; Studio form · 2026-10-30 · active`
- Say what the app is for, its main stack and roughly how big it is.
  `cb-what · codebase · Onboarding doc, Difficulty & Codebase Understanding · 2026-10-30 · active`
- Name the modules, files or flows the task touches.
  `cb-touches · codebase · Onboarding doc, Difficulty & Codebase Understanding · 2026-10-30 · active`
- Name the actual mechanism that makes it hard. "It's a large codebase" doesn't count, and a
  sentence that could be pasted into another task is too generic.
  `cb-mechanism · codebase · Onboarding doc, Difficulty & Codebase Understanding; AutoQC rubric · 2026-10-30 · active`
- Disclose related upstream PRs and issues.
  `cb-upstream · codebase · Reviewers on other writers' tasks · 2026-10-30 · active`

## Ratings and pass matrix

- One-shot: did the model complete the task from the opening prompt alone, in a single turn? Yes
  means the task is too easy: drop it.
  `rate-one-shot · ratings · Onboarding doc, Difficulty Workflow; Studio form · 2026-10-30 · active`
- Task Success 1-5: 1 failed completely, 2 partial with major issues, 3 completed but inefficient or
  poor with notable problems, 4 successful with minor or style issues, 5 completely successful.
  `rate-task-success · ratings · Onboarding doc, Difficulty Workflow · 2026-10-30 · active`
- Steering 1-5 runs backwards: 1 is constant input, 2 frequent, 3 occasional, 4 one or two light
  nudges, 5 none.
  `rate-steering-scale · ratings · Onboarding doc, Difficulty Workflow · 2026-10-30 · active`
- Studio's Steering help ("Required for Enterprise 3 or Personal-scale 3-4. Only 1 or 2 passes")
  is two rules: you must fill in Steering at those ratings, and where Steering decides the result
  only 1 or 2 passes.
  `rate-steering-required · ratings · Studio form, Steering help; Austin asked how to read it · 2026-10-30 · active`
- Task Success 1-2 passes for any codebase.
  `pass-ts-1-2 · ratings · Onboarding doc, Passing scores · 2026-10-30 · active`
- Personal-scale Task Success 3 passes with any Steering (Studio still wants a value).
  `pass-personal-3 · ratings · Onboarding doc, Passing scores · 2026-10-30 · active`
- Personal-scale Task Success 4 passes only with Steering 1 or 2.
  `pass-personal-4 · ratings · Onboarding doc, Passing scores · 2026-10-30 · active`
- Enterprise Task Success 3 passes only with Steering 1 or 2.
  `pass-enterprise-3 · ratings · Onboarding doc, Passing scores · 2026-10-30 · active`
- Enterprise Task Success 4, or any 5, is rejected.
  `reject-ent-4-any-5 · ratings · Onboarding doc, Passing scores · 2026-10-30 · active`
- On personal-scale, a 3 is work that runs but a strong engineer would redo (an N+1 query, a cluttered
  page); a 4 is small real misses (ignores naming, duplicates a helper, wrong file location).
  `rate-personal-3-4 · ratings · Onboarding doc, What a 3 or 4 looks like · 2026-10-30 · active`
- Rate honestly. A senior reviewer re-runs the final code; if a rating has to be stretched to pass,
  the prompt is too easy.
  `rate-honest · ratings · Onboarding doc, Difficulty Check · 2026-10-30 · active`

## Validation notes

- Say what happened in the validation run and why you gave these ratings: did it fully solve the
  task, where it struggled or went wrong, and how much you steered.
  `vn-content · validation_notes · Studio form, Validation Notes help · 2026-10-30 · active`
- Be concrete: "Stopped the duplicate charge by adding a retry instead of fixing the lock. Said all
  tests passed without running the webhook tests." Not "Claude did okay".
  `vn-concrete · validation_notes · Onboarding doc, Difficulty Check · 2026-10-30 · active`
- Write down every failure you saw, including small ones like a naming slip or an extra file. They
  back up a Task Success of 3 or 4.
  `vn-every-failure · validation_notes · Onboarding doc, Finding hard tasks · 2026-10-30 · active`
- The goal, decision points and validation notes share a common thread.
  `vn-common-thread · goal, decision_points, validation_notes · Reviewers on other writers' tasks · 2026-10-30 · active`

## Submitting

- Upload the validation run's transcript (.jsonl) to Trajectory Logs. Studio blocks Submit for Review
  without it: "Please resolve the following before submitting: Trajectory Logs".
  `submit-trajectory-logs · submit · Studio submit block; Austin, 2026-10-30 22:24 · 2026-10-30 · active`
- The transcript doesn't need to be uploaded.
  `submit-no-transcript · submit · Slack, 2026-10-30 · 2026-10-30 · superseded by submit-trajectory-logs`
- Attach the conversation log and final diff as links (secret gists) in Conversation Log and Final
  Output.
  `submit-links · submit · Onboarding doc, Setup; Studio form before 2026-10-30 · 2026-10-30 · superseded by submit-trajectory-logs`
- The transcript comes from the validation run's own lane.
  `submit-from-validation · submit · Onboarding doc, Before you submit · 2026-10-30 · active`
- After saving, check that Studio holds the whole text of every field: nothing cut off, nothing
  shorter than your local copy. johto check flags both.
  `submit-saved-whole · submit, fields · Reviewer, t4sk0003 · 2026-10-31 · active`
- AutoQC runs on submit and is advisory. Fix or justify every finding; reviewers reject tasks that
  do neither.
  `submit-autoqc · submit · AutoQC rubric; reviewers · 2026-10-30 · active`
- Reviewers check whether Claude one-shots it (sometimes with a few prompts), audit your notes and
  ratings against the log, and check the task fits its category and reads as a realistic ask.
  `submit-review · submit · Onboarding doc, Before you submit · 2026-10-30 · active`

## LLM policy

- No LLM may write the opening prompt, high-level goal, decision points or codebase understanding.
  If Mercor confirms one did, the task is rejected at once and the writer may be offboarded.
  `llm-protected · fields · Mercor LLM policy · 2026-10-30 · active`
- The AI flag checks every field, so validation notes and reviewer notes are human-written too.
  `llm-all-fields · fields · Reviewers · 2026-10-30 · active`
- An LLM may be a research aid: searching and navigating the codebase, and checking difficulty.
  Claude is only the test.
  `llm-research-ok · fields · Mercor LLM policy; Onboarding doc, Difficulty Check · 2026-10-30 · active`
- AutoQC's AI Detection flags text that outside detectors score as likely AI-written.
  `llm-ai-detection · fields · AutoQC rubric, AI Detection · 2026-10-30 · active`
- Don't carry AI research wording into your fields. johto keeps AI output in the task folder so the
  overlap guard can compare your fields against it, and it never drafts or rewrites field text.
  `llm-no-paraphrase · fields · johto build plan, rule 1 · 2026-10-31 · active`

## Reviewer lessons

- When a number or boundary separates a real fix from one that only looks like it passes, put it in
  the goal, not only in the difficulty notes (t4sk0001: more than 500 failed rows).
  `lesson-goal-threshold · goal · Reviewer, t4sk0001 · 2026-10-31 · active`
- Check what Studio saved after pasting. t4sk0003's validation notes were saved cut off mid-word
  ("when I ship the second half it fo").
  `lesson-saved-whole · validation_notes · Reviewer, t4sk0003 · 2026-10-31 · active`
- An interactive task Claude finishes in a few minutes, or in one turn, is a prompt to drop.
  `lesson-too-fast · run · Reviewers on other writers' tasks · 2026-10-30 · active`
- Define every term the goal relies on, and keep its criteria checkable within a session.
  `lesson-defined-terms · goal · Reviewers on other writers' tasks · 2026-10-30 · active`
