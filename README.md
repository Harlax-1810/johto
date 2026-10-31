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

```sh
johto next                      # ranked problem leads that fit open slots
johto start --lead <id>         # claim a matching slot, save repo/URL/language, make tasks/<name>/
#   read tasks/<name>/brief.md, write the Opening Prompt in tasks/<name>/submission.md
johto lane <name> validate      # fresh lane, prompt on clipboard, drops you into Claude
johto collect <name>            # diff + transcript + summary (turns, minutes, model, tests)
#   one-shot? -> johto discard <name>   (unclaims, pre-filled Discarded Run Log link)
#   otherwise write Goal, Decision Points, Difficulty & Codebase, ratings and notes
johto check <name>              # local lint (style rules, ratings matrix, model/effort, leaks)
johto submit <name>             # gists -> Studio fields -> AutoQC -> confirm -> Submit for Review
johto clean <name>              # remove the task's lanes (containers, volumes, lane logins)
```

Other commands: `johto slots`, `johto status` (your tasks plus reviewer feedback),
`johto push` (save fields without submitting), `johto pull` (bring Studio edits back),
`johto qc` (run AutoQC now), `johto resume <name> [run]` (re-enter a lane),
`johto lane <name> calib` (an optional calibration run), `johto scout ...` (discovery).

## Rules it enforces

- Every run gets a new folder under `tasks/<name>/runs/` (`calib-N`, `validate-N`).
- Up to two lanes run on one machine (see below). `lane` never stops a lane while Claude is active in it.
- Lanes start with `--model claude-opus-5-5 --effort high`; `check` reads the transcript to confirm.
- `submit` asks before uploading gists, and again before the irreversible Submit for Review.
- Edits made in Studio come back into `submission.md`. A field changed in both places is never overwritten.
- Save is blocked when a field shares a 6-word run with AI research text you were shown.
- Nothing here writes submission text. `brief.md` and repo dossiers are research notes for you only.

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
- Copy `bin/mercor-prework` to `~/.local/bin/mercor-prework`, and `johto/prework_slot.py` to
  `~/.local/share/mercor-prework/prework_slot.py`. Copy `prework_slot.py` again whenever it changes.
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

## Layout

- `johto/`: the CLI and its modules: `studio.py` (API), `actions.py` (the task steps), `lanes.py`,
  `transcript.py`, `lint.py`, `sync.py`, `gists.py`, `pack.py` (review packs), `scout/` (repo
  discovery, vetting, leads, ranking) and `dossier/` (AI repo research).
- `johto/web/`: the web app (Starlette, Preact and htm with no build step) and `fakes/` for test mode.
- `tests/`: `PYTHONPATH=.:tests .venv/bin/python -m unittest discover -s tests`

The code is published as-is, with no license; all rights reserved.
