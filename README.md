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
bin/johto --help
```

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
- Lanes share host ports. `lane` never stops a lane while Claude is active in it.
- Lanes start with `--model claude-opus-5-5 --effort high`; `check` reads the transcript to confirm.
- `submit` asks before uploading gists, and again before the irreversible Submit for Review.
- Edits made in Studio come back into `submission.md`. A field changed in both places is never overwritten.
- Save is blocked when a field shares a 6-word run with AI research text you were shown.
- Nothing here writes submission text. `brief.md` and repo dossiers are research notes for you only.

## Layout

- `johto/`: the CLI and its modules: `studio.py` (API), `actions.py` (the task steps), `lanes.py`,
  `transcript.py`, `lint.py`, `sync.py`, `gists.py`, `pack.py` (review packs), `scout/` (repo
  discovery, vetting, leads, ranking) and `dossier/` (AI repo research).
- `johto/web/`: the web app (Starlette, Preact and htm with no build step) and `fakes/` for test mode.
- `tests/`: `PYTHONPATH=.:tests .venv/bin/python -m unittest discover -s tests`

The code is published as-is, with no license; all rights reserved.
