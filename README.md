# johto

Busywork automation for writers on Mercor's Project Johto pre-work tasks. You write the opening
prompt, goal, decision points, codebase notes and validation notes yourself. `johto` handles the
rest:
- finds open slots and candidate repos;
- claims a slot and sets up Docker lanes for Claude Code;
- collects runs and builds a review pack for each one;
- uploads gists, fills the Studio fields, runs AutoQC and submits.

It comes as a CLI and a local web app (`johto web`). The app is a keyboard-driven queue of the next
thing that needs a person. Nothing in it writes your submission text.

## How it talks to Studio

Studio calls run as `fetch` inside your logged-in Studio tab, through
[browser-harness](https://github.com/browser-use/browser-harness) attached to your Chrome. Keep one
Studio tab open and signed in. Gists go through the `gh` CLI. Lanes use Mercor's prework harness
(`~/.mercor-prework`).

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[web]'
cp studio-ids.example.json studio-ids.json   # then fill in your project's ids
ln -s "$PWD/bin/mercor-prework" ~/.local/bin/mercor-prework
bin/johto --help
```

`bin/mercor-prework` is the command johto runs to set up and collect lanes. It runs Mercor's
`prework.py`, which it expects at `~/.local/share/mercor-prework/prework-v2/prework.py` (set
`PREWORK_PY` to use another path, and `PREWORK_IMAGE_FILE` to load the lane image from a file).

`studio-ids.json` is git-ignored. It holds the project's Studio IDs (campaign, statuses, QC specs),
the Discarded Run Log form, your name and the Chrome profile that holds the Studio login.
Everything the tool caches or creates goes in `data/` and `tasks/`, which are git-ignored too.

## Try it without Studio

```sh
mkdir -p /tmp/johto-fake
JOHTO_FAKE=1 JOHTO_ROOT=/tmp/johto-fake bin/johto web   # fake Studio, gh and lanes on http://127.0.0.1:8766/
```

Fake mode refuses to run on the checkout itself, so its data stays in the scratch folder.

Add `?demo=1` to the URL for a demo queue with every kind of step.

## Daily loop (CLI)

One task is three planned sittings plus short replies between them. This order skips calibration runs, which one
writer agreed with Mercor staff; check with Mercor before doing the same. Set `calibrate_first` to `true` in
`data/settings.json` to show them in the web app. Keep two tasks in flight, a third when both are waiting on machine work.

**1. Start (about 10 min)**

```sh
johto deck                      # three ready picks (familiar repos, open slots)
johto start --lead <id>         # claim a matching slot, make tasks/<t>/
#   write the Opening Prompt (Studio or tasks/<t>/submission.md); nothing else is needed before the run
johto lane <t> validate         # checks the prompt, then a fresh lane with the prompt on the clipboard
```

`lane <t> validate` checks the prompt first and refuses when:
- Studio's Opening Prompt differs from the local one. `--qc` brings Studio's edits in, saves the prompt, runs
  AutoQC and starts the lane if the prompt passes.
- AutoQC hasn't passed this prompt. Only the prompt's own checks count; empty goal or notes never block.
  `--qc` runs it now; `--skip-qc` starts anyway (a later prompt change then needs another run).
- An earlier validation run used the same prompt. The message names that run, when it started and its
  resume command; `--again` starts another one on purpose.

**Between sittings (about 8 min in all)**

```sh
johto watch                     # packs each meaningful finished turn and sends it to GPT Pro; sounds on questions
johto where                     # every task: what needs you now, and what the machines are doing
johto time <t> [run]            # Claude's working minutes, turn by turn, background jobs apart
johto review list <t>           # saved GPT Pro reviews; each starts with a short digest
```

Answer Claude's real questions like a colleague. Review findings are private: they never become hints to Claude.
The goal may be written while the run goes.

**2. Finish the submission (about 30 min)**

```sh
johto collect <t>               # diff, transcript, summary; also writes the kit
johto where <t>                 # the card: run, current text (Studio or local), what changed, what's left
johto grading <t>               # findings from reviews, the dossier and the run; scope, result, how known
johto grading <t> mark R3 R4 --goal     # your marks: --goal, --dp 1|2|3, --notes, --scope ..., --unselect
johto kit <t>                   # facts beside your writing: boundary, repo map, your rows, run evidence, limits
#   write Goal, Decision Points, Difficulty & Codebase, ratings and Validation Notes in Studio
johto check <t>                 # the saved draft: Studio's text, uploads, minutes, the run, lint
johto check <t> --deep          # once, after writing: adds AI flags (locations and evidence, never wording)
```

After edits, `johto check <t> --deep` looks again only at the fields that changed (`--full` for all of them).
`--all` shows the optional issues; `--refresh` reads Studio's text again first.

**3. Close (about 2 min)**

```sh
johto submit <t>                # check, gists, Studio fields, AutoQC, Trajectory Logs status, confirm, submit
johto clean <t>                 # remove the task's lanes (containers, volumes, lane logins)
```

Other commands: `johto status` (your tasks, reviewer feedback, focused minutes per accepted task; `--local`
skips Studio), `johto focus <t> start|stop|show`, `johto rules [--field goal] [--lessons]`, `johto review
pack|send|fetch|show|add`, `johto push` (save fields without submitting), `johto qc`, `johto resume <t> [run]`,
`johto submission-run <t> [run]`, `johto slots`, `johto next`, `johto scout ...`, `johto web`.

The web app (`johto web`) does the same loop with buttons and reads the same record. **Now** shows the one next
step as a card (`⌘↵` does it), the steps after it and what reviewers said. **Find work** ranks the researched repos
by open slots; a repo's **brief** offers three problems to start from and places to find your own, and claims a
slot from the inspector. The **task page** is one path: Prompt (Start the validation run, with Mercor's prompt
check when Studio needs it), Validation run, Write (the fields, ratings and the review's findings beside them) and
Submit (what's left, Save to Studio, Submit for review). **Submitted** keeps the history.

### The watcher as a LaunchAgent

Edit the paths in `tools/local.johto.watch.plist` first (they are placeholders).

```sh
cp tools/local.johto.watch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.johto.watch.plist
launchctl bootout gui/$(id -u)/local.johto.watch     # to stop it
```

It logs to `data/turnwatch.log`. Only one watcher runs at a time. `johto watch --once --dry-run` shows what it
would do without writing anything. Settings in `data/settings.json`: `review_policy` (`meaningful`, the default,
or `every_turn`), `pack_outside_review` (off), `auto_review`, `budget_nudges`.

## Rules it enforces

- Every run gets a new folder under `tasks/<name>/runs/` (`validate-N`).
- Up to two lanes run on one machine (see below). `lane` never stops a lane while Claude is active in it.
- Lanes start with `--model claude-opus-5-5 --effort high`; `check` reads the transcript to confirm.
- One rule picks the submission run everywhere: the run you chose (`johto submission-run`), else the newest
  collected validation run.
- Gist links are bound to the transcript and diff they hold; a changed file is uploaded again.
- `submit` asks before uploading gists and again before the irreversible Submit for Review, and shows whether
  Studio has the Trajectory Logs upload.
- Nothing here writes submission text. Research, reviews, the grading table, the kit and checks are facts saved
  under the task folder, so the overlap guard can compare your fields against them.

## Two lanes on one machine

Mercor's `prework.py` gives every lane the same host ports (34000-34019 and 35000-35009) and the
same first CPUs, so its setup refuses a second lane while one is running. johto gets around this
without changing `prework.py`.

- `johto lane <task> <kind>` starts a second lane beside a running one, even while Claude is working
  in the first. Only when both slots are taken does it stop idle lanes, or refuse when Claude is
  running in both. `--stop-others` stops every running lane first (their files are kept).
- The second lane's dev servers open at host ports 36000-36019 and 37000-37009, and it runs on the
  next CPUs. Inside the lane nothing changes: dev servers still use ports 3000-3019 and 8000-8009.
- The run's entry in `tasks/<task>/task.json` gets `"port_slot": 1`, which `johto collect` and
  `johto resume` read.
- **How it works:** johto sets `PREWORK_PORT_SLOT=1`, and `bin/mercor-prework` then runs
  `prework.py` through `johto/prework_slot.py`. That script moves the host ports and CPUs before
  prework's own code runs, including in the `claude-dev` launcher that setup writes.
- **Limits:**
  - Two lanes per machine, because each may use 45% of Docker's memory. On a Docker engine under
    8 GB, a second lane is refused.
  - The web app's lane page handles one lane at a time, so start the second lane from the terminal.
  - If Mercor ships a `prework.py` with a changed launcher, a second lane is refused, and
    `tests/test_lane_slots.py` fails until `prework_slot.py` is updated.

## Lanes on another machine (SSH)

A lane can run on any machine with Docker that you can reach over SSH, for example a Linux box when
your laptop's lane slots are busy or a repo's images are Intel/AMD only. You set up and launch these
lanes by hand; `johto collect` fetches their runs.

**Once, on the other machine:**
- Install Mercor's prework tool for that machine's architecture (`prework.py` and its lane image)
  at the default path, `~/.local/share/mercor-prework/prework-v2/prework.py`. The SSH commands
  below don't set `PREWORK_PY`.
- Copy `bin/mercor-prework` to `~/.local/bin/mercor-prework`, and `johto/prework_slot.py` and `johto/lane_login.py`
  to `~/.local/share/mercor-prework/`. Copy them again whenever they change.
- Make sure `ssh <host>` works without a password prompt, and that rsync is installed on both machines.

**Per run:**

1. Set up the run in a new, empty folder on the other machine:

   ```sh
   ssh <host> 'mkdir -p ~/johto-lanes/<task>/<run> && cd ~/johto-lanes/<task>/<run> &&
     ~/.local/bin/mercor-prework setup --task "Task <task>" --repo <pinned url> </dev/null'
   ```

   For a second lane on that machine, put `PREWORK_PORT_SLOT=1 ` before `~/.local/bin/mercor-prework`.

2. Record the run in johto. Copy its manifest into the task folder:

   ```sh
   mkdir -p tasks/<task>/runs/<run>
   scp <host>:johto-lanes/<task>/<run>/prework-manifest.json tasks/<task>/runs/<run>/
   ```

   Then add the run to the `runs` list in `tasks/<task>/task.json`, with an absolute `remote_dir`:

   ```json
   {"kind": "validate", "dir": "runs/validate-1", "created_at": "2026-10-31T09:00:00-0700",
    "host": "<host>", "remote_dir": "/home/<you>/johto-lanes/<task>/validate-1"}
   ```

   Add `"port_slot": 1` if it's the second lane on that machine.

3. Launch Claude there. Running the same command again reattaches to the same session:

   ```sh
   ssh -t <host> 'cd ~/johto-lanes/<task>/<run>/model_a && ./claude-dev --model claude-opus-5-5 --effort high'
   ```

4. Collect as usual with `johto collect <task> <run>`. It runs prework's collect on the other
   machine over SSH, then copies `prework_output/` back with rsync.

5. Clean up on the other machine by hand, because `johto clean` only reaches this one. After
   collecting, run the following there. `<container>` is `prework-<key>-model_a`, from the run's
   `prework-manifest.json`.

   ```sh
   docker rm -f -v <container>
   docker volume rm <container> <container>-dind <container>-ctr
   docker network rm prework-<key>
   rm -rf ~/.mercor-prework/cc-config/<key>_model_a ~/.mercor-prework/lane-harness/<key>_model_a \
          ~/johto-lanes/<task>/<run>
   ```

## Lane login without /login

Run `claude setup-token` once and save the token as an owner-only file at `~/.mercor-prework/lane-token`. After each
successful `setup`, `bin/mercor-prework` runs `johto/lane_login.py`, which writes the new lane's `.credentials.json`
from that file. It never replaces a login that's already there, and it does nothing without the token file.

## Layout

- `johto/` code: `studio.py` (API), `slots.py`, `workspace.py`, `lanes.py`, `transcript.py`,
  `lint.py`, `gists.py`, `cli.py`, `scout/` (repo discovery, vetting, leads, ranking), `where.py` (task card),
  `grading.py`, `kit.py`, `draftcheck.py`, `reviews.py`, `turnwatch.py`, `budget.py`, `focus.py`, `deck.py`
- `data/` caches: `slots.json`, `studio_tasks.json`, `scout/`
- `tasks/<name>/` one folder per claimed task: `task.json`, `submission.md`, `runs/`, `grading.json`, `kit.md`,
  `checks/`, `reviews/`, `notes/ai-*.md`, `events.jsonl`
- `johto/web/`: the web app (Starlette, Preact and htm with no build step) and `fakes/` for test mode.
- `tests/`: `PYTHONPATH=.:tests .venv/bin/python -m unittest discover -s tests`

The code is published as-is, with no license; all rights reserved.
