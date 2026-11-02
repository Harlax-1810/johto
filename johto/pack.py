"""Review packs: one repomix file (≤ 200k tokens) about a lane run, for pre-grading with another model.

A pack holds the task (slot, repo, Austin's prompt, the checkpoint it shows), Claude's approach (a condensed
transcript), the diff and the files that matter at their changed state. With `ai`, it also holds an
outside-the-lane Claude pass, last in the pack (3-outside-review/candidate-claims.md) and labeled as claims to
check, so the reviewer judges the run before reading it. The guide asks for one reply format (REPLY_FORMAT:
digest, findings table, detail, then a ```findings JSON block), shared with follow-up packs (johto/delta.py).

It works mid-run (live transcript and `git diff` from the lane container) or after collect. The code comes from
a clean clone at the pinned commit with the run's diff applied: a clone or checkout that fails stops the build,
and hunks that don't apply mark the pack PARTIAL (first line, guide, header and packs.json), which
johto.reviews never sends on its own. Each build works in its own folder under data/cache/packs/ and removes
only that. Each pack is recorded in runs/<run>/packs.json (johto/delta.py), so the next turn can get a
follow-up pack instead.

This is difficulty research (allowed by Johto's LLM policy). Nothing here writes Austin's fields.
"""
from __future__ import annotations

import collections
import json
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import config, lanes, transcript, workspace

REPOMIX = ["npx", "--yes", "repomix@1.18.1"]
CLAUDE_BIN = config.CLAUDE_BIN   # JOHTO_CLAUDE_BIN; tests point it at a stub so no paid review runs
PACKS = config.DATA / "cache" / "packs"
MAX_TOKENS = 200_000

REPLY_FORMAT = """## Reply format

Reply in this order:

1. `## Digest`, 250-400 words: whether the material evidence changed, the findings that changed most (up to
   3), what the run deserves credit for, the verification limits, and only the uncertainty that could change
   the judgment.
2. `## Findings`, a table with one row per finding:
   | ID | Finding | Status | Severity | Run result | How known | Evidence |
3. `## Detail`: the evidence for each finding, by ID, with file:line references.
4. Last, a fenced block tagged `findings` with a JSON array, one object per finding:

```findings
[{"id": "F1", "finding": "one factual sentence", "kind": "defect|credit|behavior|claim|limitation",
  "status": "new|unchanged|resolved|worsened|withdrawn", "severity": "high|medium|low",
  "result": "credit|partial|missed|unknown", "basis": "executed|isolated|traced|claimed",
  "refs": ["path:line", "turn 2"], "limits": "what was not verified", "supersedes": null}]
```

- kind: defect (wrong or missing in the run's work), credit (done right, worth crediting), behavior (what the
  code does on a path the request touches, as a code fact, and whether the run covered it), claim (something
  Claude said, checked against the evidence), limitation (what this pack can't show).
- result: how the run did on it (credit, partial, missed, unknown).
- basis, how you know: executed (the full behavior was run and the output shows it), isolated (only a piece
  was run, such as one test or a snippet), traced (by reading the code), claimed (only Claude says so).
- limits: what you could not verify for this finding. Keep qualifications: an isolated run with a stand-in is
  not "verified in the app".
- status: new the first time. In a later reply, give every earlier ID again as unchanged, resolved, worsened
  or withdrawn, and number new findings from the next free ID.
- supersedes: the ID this finding replaces, else null.

IDs stay the same for the whole chat. Keep separate findings separate.
"""

RULES = """## Rules

- Research only: your review is evidence for the engineer, who judges the run and writes every field himself.
- Don't give ratings, and don't write or suggest wording for the opening prompt, task goal, decision points,
  difficulty notes or validation notes.
- Don't suggest replies or prompts to send to Claude.
- Don't frame findings as what a strong or weak model or engineer would do, or as what the task or a solution
  must do. State code facts and run facts, with file:line.
"""

GUIDE = """# How to use this pack
@@BANNER@@
You are reviewing one run of Claude Code (Opus 5.5, high effort) on a coding task, for an engineer who
writes evaluation tasks for Mercor's Project Johto. Everything you need is in this file. Read it in this
order, and judge the run yourself before step 5:

1. `1-review/01-task.md`: the task slot, the repository at its pinned commit, the checkpoint this pack
   shows, what the pack left out, and the engineer's opening prompt (the only text Claude saw at the start;
   later human turns are in the transcript).
2. `1-review/02-transcript.md`: what Claude did, in order: the human's messages, Claude's messages, every tool
   call (commands, edits, reads) and shortened tool output. Test runs keep more output.
3. `1-review/03-final.diff`: the change Claude made so far, against the pinned commit.
4. `2-code/`: the files Claude changed (at their changed state) and the closely related files it read or
   that the change depends on, so you can check claims against the real code.
@@OUTSIDE@@

Please assess:

- Did Claude solve the actual problem (root cause, not symptom)? What did the run miss or get wrong, with
  file:line?
- Which edge cases, interactions across files or layers, existing data or behaviour it could break, paths
  it never tested, and claims it made without verifying them?
- What did it get right that a grader should credit?
- How much did the human have to steer it, and where?
- For Johto's scales (One-shot yes/no; Task Success 1-5: 1 failed, 2 partial with major issues, 3
  completed but highly inefficient or non-optimal with notable problems, 4 successful with minor or
  style issues, 5 completely successful; Steering 1-5: 1 constant input ... 5 none), give the evidence
  only: file:line references, what was missed, which claims are unverified, and where the human steered.

Be skeptical of the run's own summary of what it did, and say what you could not verify.

""" + REPLY_FORMAT + "\n" + RULES

OUTSIDE_STEP = """5. Last, `3-outside-review/candidate-claims.md`: claims from a separate Claude pass, run outside the
   sandbox on a clean checkout with the diff applied. They are candidates to check, not evidence. Read them
   only after your own judgment of the task, run and code; check each against the code, and keep, correct
   or drop it on what you find. Findings that come only from that file are basis "claimed"."""
NO_OUTSIDE = "5. There is no outside review in this pack: the findings are yours alone."


def guide(outside: bool, partial: str | None = None) -> str:
    """The pack's 00-grading-guide.md: the reading order (the outside review last, when there is one), what to
    assess, REPLY_FORMAT and RULES. A PARTIAL banner goes first when the checkout missed some of the diff."""
    banner = ""
    if partial:
        banner = (f"\n> **{partial}.** The code in `2-code/` (and the outside review's checkout) lacks those "
                  "changes; `1-review/03-final.diff` is complete. Judge those files from the diff.\n")
    return GUIDE.replace("@@BANNER@@", banner).replace("@@OUTSIDE@@", OUTSIDE_STEP if outside else NO_OUTSIDE)


REVIEW_PROMPT = """You are reviewing one run of an AI coding agent, from outside its sandbox. The current
directory is a clean checkout of the repository at the task's pinned commit with the agent's diff applied.

Read the task, the agent's transcript and its diff below. Then read the code yourself (the changed files,
their callers, the tests, related layers) and find what the run may have missed or got wrong: the real root
cause versus the symptom it fixed, edge cases, interactions three calls away, existing data or behaviour
the change could break, migrations, concurrency, paths it never tested, and any claim in its messages
("all tests pass", "this handles X") that the transcript does not back up. Also note what it did well.

Rules: research only. Write facts with file:line evidence, short and specific. Do not write ratings, a
grade, a review of the human, or text addressed to the human. Do not suggest wording for anything.

Return JSON between the lines <<<REVIEW and REVIEW>>> with this shape:
{"problem": "<= 60 words: the problem as the code shows it",
 "approach": ["<= 25 words each: what the agent did, in order"],
 "misses": [{"what": "<= 25 words", "evidence": ["path:line ..."], "severity": "high|medium|low",
             "why_it_matters": "<= 30 words"}],
 "unverified_claims": [{"claim": "<= 20 words, quoted or paraphrased", "reality": "<= 30 words"}],
 "did_well": ["<= 20 words each"],
 "extra_files": ["repo-relative paths a grader must read to judge this, beyond the changed files (<= 15)"]}

=== TASK ===
@@TASK@@

=== DIFF ===
@@DIFF@@

=== TRANSCRIPT (condensed) ===
@@TRANSCRIPT@@
"""


def _run(cmd, cwd=None, timeout=None, input=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input)


# --- Gathering the run ---------------------------------------------------------

def _pick_run(task_dir: Path, run_name: str | None) -> Path:
    runs = sorted((p for p in (task_dir / "runs").glob("*-*") if p.is_dir()), key=lambda p: p.stat().st_mtime)
    if run_name:
        p = task_dir / "runs" / run_name
        if not p.is_dir():
            raise SystemExit(f"No run {run_name} in {task_dir}")
        return p
    if not runs:
        raise SystemExit(f"No runs yet in {task_dir}")
    return runs[-1]


def _remote(run_dir: Path) -> tuple[str, str] | None:
    """(ssh host, run folder there) when this run's lane lives on another machine, such as the PC."""
    try:
        runs = workspace.load(run_dir.parent.parent).get("runs", [])
    except (OSError, ValueError):
        return None
    rec = next((r for r in runs if r.get("dir") == f"runs/{run_dir.name}"), {})
    return lanes.remote_of(rec)


def _live_transcript(run_dir: Path) -> Path | None:
    m = lanes.manifest(run_dir)
    key = lanes.lane_key((m.get("lane") or {}).get("container"))
    if not key:
        return None
    projects = Path(config.PREWORK_HOME) / "cc-config" / f"{key}_model_a" / "projects"
    remote = _remote(run_dir)
    if remote:
        # Copy the lane's transcripts from the other machine (read-only there) into the run folder.
        projects = run_dir / ".live-transcripts"
        projects.mkdir(exist_ok=True)
        r = _run(["rsync", "-a", f"{remote[0]}:.mercor-prework/cc-config/{key}_model_a/projects/", f"{projects}/"],
                 timeout=300)
        if r.returncode != 0:
            return None
    found = sorted(projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def _live_diff(run_dir: Path) -> str | None:
    """`git diff` against the base commit inside a running lane, untracked files included. Read-only."""
    m = lanes.manifest(run_dir)
    container, base, ws = (m.get("lane") or {}).get("container"), m.get("base_commit"), (m.get("lane") or {}).get("workspace")
    if not (container and base and ws):
        return None
    script = (f'cd "{ws}" && git -c core.quotepath=off diff --binary {base} -- . ":(exclude)node_modules" && '
              'git ls-files --others --exclude-standard -z | while IFS= read -r -d "" f; do '
              'git -c core.quotepath=off diff --no-index -- /dev/null "$f"; done; true')
    cmd = ["docker", "exec", "-u", "lane", container, "bash", "-c", script]
    remote = _remote(run_dir)
    if remote:
        up = _run(["ssh", remote[0], f"docker ps -q --filter name=^{container}$"], timeout=60)
        if up.returncode != 0 or not up.stdout.strip():
            return None
        cmd = ["ssh", remote[0], shlex.join(cmd)]
    elif container not in lanes.running_lanes():
        return None
    r = _run(cmd, timeout=120)
    return r.stdout if r.returncode == 0 else None


def gather(task_dir: Path, run_name: str | None, *, live: bool | None = None) -> dict:
    """The run's transcript and diff for a pack. A collected run uses what was collected (the transcript and
    final.diff the submission holds), unless `live`; a run under way reads its lane. Fake mode never reaches a
    lane or the PC. Stops with a plain message when the diff can't be read: a pack of an unknown tree would show
    "no changes" and GPT would review code Claude never wrote."""
    run_dir = _pick_run(task_dir, run_name)
    meta = workspace.load(task_dir)
    rec = next((r for r in meta.get("runs", []) if r.get("dir") == f"runs/{run_dir.name}"), {})
    if live is None:
        live = not rec.get("collected_at")
    live = bool(live) and not config.FAKE
    live_t = _live_transcript(run_dir) if live else None
    live_d = _live_diff(run_dir) if live else None
    logs = transcript.find(run_dir)
    tpath = live_t or (logs[-1] if logs else None)
    diff_file = run_dir / "prework_output" / "final.diff"
    if not tpath:
        raise SystemExit(f"No transcript for {run_dir.name} yet (start Claude in the lane first).")
    if live_d is not None:
        diff = live_d
    elif diff_file.exists():
        diff = diff_file.read_text(errors="replace")
    else:
        raise SystemExit(f"{run_dir.name}'s diff can't be read: its lane isn't running and the run isn't collected. "
                         f"Collect it first: johto collect {Path(task_dir).name} {run_dir.name}")
    return {"meta": meta, "run_dir": run_dir, "transcript_path": tpath, "diff": diff,
            "live": live_t is not None, "sub": workspace.parse_submission(task_dir)}


# --- Condensing the transcript ---------------------------------------------------

def _clip(text: str, head: int, tail: int = 0) -> str:
    lines = text.splitlines()
    if len(lines) <= head + tail:
        return text
    cut = len(lines) - head - tail
    return "\n".join(lines[:head] + [f"… [{cut} lines cut] …"] + (lines[-tail:] if tail else []))


def _after(e: dict, since) -> bool:
    """True when entry e is later than `since` (a datetime). Entries without a timestamp go with their neighbours."""
    ts = transcript._ts(e.get("timestamp"))
    return ts is None or ts > since


def condense(tpath: Path, out_lines: int = 40, since: str | None = None,
             brief_compactions: bool = False) -> tuple[str, dict]:
    """Markdown of the run: human turns, Claude's text, tool calls with shortened output.

    `since` (an ISO timestamp, for follow-up packs) keeps only entries after it; earlier entries still number the
    turns and match tool results to their calls. `brief_compactions` turns Claude Code's context-compaction
    summaries (pages of recap sent as a user message) into one line."""
    entries = []
    for line in Path(tpath).read_text(errors="ignore").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    if brief_compactions:
        compacted = {id(e) for e in entries if e.get("isCompactSummary") and e.get("type") == "user"}
    else:
        compacted = set()
    after = transcript._ts(since) if since else None
    started = after is None
    calls, reads, md = {}, collections.Counter(), []
    turn = 0
    cont = None   # a turn that was under way at `since`: its heading goes before its next tool call or message
    humans = iter(transcript.human_messages([e for e in entries if id(e) not in compacted]))

    def emit(text: str) -> None:
        nonlocal cont
        if cont:
            md.append(cont)
            cont = None
        md.append(text)

    for e in entries:
        if e.get("isSidechain"):
            continue
        ts = (e.get("timestamp") or "")[11:19]
        if not started and _after(e, after) and e.get("timestamp"):
            started = True
            if turn:
                label = "opening prompt" if turn == 1 else f"follow-up {turn - 1}"
                cont = f"\n## Continuing the {label}, after the last pack\n"
        if id(e) in compacted:
            if started:
                md.append(f"\n*(Claude Code compacted its context at {ts}.)*\n")
            continue
        if e.get("type") == "user":
            human = transcript._human_text(e)
            if human is not None and e.get("timestamp"):
                hm = next(humans, None)
                cont = None
                if hm and not hm["counted"]:
                    if started:
                        md.append(f"\n## Human ({ts}, not counted: {hm['note']})\n\n{human}\n")
                else:
                    turn += 1
                    label = "opening prompt" if turn == 1 else f"follow-up {turn - 1}"
                    if started:
                        md.append(f"\n## Human, {label} ({ts})\n\n{human}\n")
                continue
            if not started:
                continue
            for x in transcript._content(e) or []:
                if isinstance(x, dict) and x.get("type") == "tool_result":
                    call = calls.get(x.get("tool_use_id"), {})
                    content = x.get("content")
                    if isinstance(content, list):
                        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                    content = str(content or "")
                    is_test = transcript.TEST_CMD.search(call.get("cmd", "") or "")
                    n = out_lines * 3 if is_test else out_lines
                    err = " (error)" if x.get("is_error") else ""
                    if call.get("name") in ("Read", "Glob", "Grep") and not err:
                        emit(f"→ {len(content.splitlines())} lines{err}\n")
                    else:
                        emit(f"→ output{err}:\n```\n{_clip(content, n * 2 // 3, n // 3)}\n```\n")
        elif e.get("type") == "assistant":
            for x in transcript._content(e) or []:
                if not isinstance(x, dict):
                    continue
                if x.get("type") == "tool_use":   # always noted, so a later tool result finds its call
                    inp = x.get("input") or {}
                    calls[x.get("id")] = {"name": x.get("name"), "cmd": inp.get("command") or ""}
                if not started:
                    continue
                if x.get("type") == "text" and x.get("text", "").strip():
                    emit(f"\n**Claude ({ts}):** {x['text'].strip()}\n")
                elif x.get("type") == "tool_use":
                    name, inp = x.get("name"), x.get("input") or {}
                    cmd = inp.get("command") or ""
                    if name == "Bash":
                        emit(f"\n`{name}` ({ts}): `{cmd[:400]}`\n")
                    elif name in ("Read",):
                        reads[inp.get("file_path", "")] += 1
                        rng = f" lines {inp.get('offset')}+{inp.get('limit')}" if inp.get("offset") else ""
                        emit(f"\n`Read` {inp.get('file_path')}{rng}\n")
                    elif name in ("Edit", "MultiEdit", "Write"):
                        body = inp.get("new_string") or inp.get("content") or json.dumps(inp.get("edits", ""))[:1500]
                        emit(f"\n`{name}` {inp.get('file_path')}:\n```\n{_clip(str(body), 30, 5)}\n```\n")
                    else:
                        emit(f"\n`{name}` {json.dumps(inp)[:300]}\n")
    return "".join(md), {"reads": reads, "turns": turn}


# --- Outside review ----------------------------------------------------------------

GIT_URL = "https://github.com/{}.git"   # tests point this at a local repo


def _hunks(diff: str) -> dict[str, int]:
    """Hunks per file in a diff, keyed by the path `git apply` names (a file with no hunks, such as a binary
    patch, counts as one)."""
    out: dict[str, int] = {}
    for part in re.split(r"(?m)^(?=diff --git )", diff or ""):
        m = re.match(r"diff --git a/(.+?) b/(.+)", part)
        if m:
            out[m.group(2).strip()] = max(1, len(re.findall(r"(?m)^@@ ", part)))
    return out


def apply_report(stderr: str, diff: str) -> str | None:
    """"PARTIAL: N hunks rejected in a, b" from `git apply --reject`'s output, or None when all of it applied.
    A file git couldn't patch at all (missing, already there) counts all its hunks."""
    rejected: collections.Counter = collections.Counter()
    current = None
    for line in stderr.splitlines():
        m = re.match(r"Applying patch (.+?) with \d+ rejects?\.\.\.$", line)
        if m:
            current = m.group(1)
        elif line.startswith("Rejected hunk #") and current:
            rejected[current] += 1
    checked = re.findall(r"(?m)^Checking patch (.+?)\.\.\.$", stderr)
    applied = set(re.findall(r"(?m)^Appl(?:ying|ied) patch (.+?)(?: with \d+ rejects?\.\.\.| cleanly\.)$", stderr))
    hunks = _hunks(diff)
    for f in checked:
        if f not in applied:
            rejected[f] += hunks.get(f, 1)
    if not rejected:
        return None
    n, files = sum(rejected.values()), sorted(rejected)
    names = ", ".join(files[:8]) + (f" and {len(files) - 8} more" if len(files) > 8 else "")
    return f"PARTIAL: {n} hunk{'' if n == 1 else 's'} rejected in {names}"


def checkout(meta: dict, diff: str, dest: Path) -> tuple[Path, str, str | None]:
    """Clean clone at the pinned commit with the run's diff applied. Returns (path, apply note, partial), where
    partial is apply_report()'s "PARTIAL: ..." when some hunks didn't apply. A clone or checkout that fails,
    or lands on another commit, raises SystemExit: a pack of the wrong tree would mislead."""
    repo = meta["repo"]
    if dest.exists():
        shutil.rmtree(dest)
    r = _run(["git", "clone", "-q", "--filter=blob:none", GIT_URL.format(repo["full_name"]), str(dest)], timeout=900)
    if r.returncode != 0:
        raise SystemExit(f"Couldn't clone {repo['full_name']}, so no pack was made: {r.stderr.strip()[-300:]}")
    r = _run(["git", "checkout", "-q", repo["commit"]], cwd=dest, timeout=300)
    head = _run(["git", "rev-parse", "HEAD"], cwd=dest, timeout=60).stdout.strip()
    if r.returncode != 0 or not head or not head.startswith(str(repo["commit"]).strip()[:40]):
        raise SystemExit(f"Couldn't check out {repo['full_name']} at {str(repo['commit'])[:12]}, so no pack was made"
                         + (f": {r.stderr.strip()[-300:]}" if r.stderr.strip() else f" (HEAD is {head[:12] or '?'})."))
    if not diff.strip():
        strip_claude_settings(dest)
        return dest, "no changes", None
    patch = dest.parent / "run.diff"
    patch.write_text(diff if diff.endswith("\n") else diff + "\n")
    r = _run(["git", "apply", "--reject", "--whitespace=nowarn", str(patch)], cwd=dest, timeout=120)
    strip_claude_settings(dest)
    if r.returncode == 0:
        return dest, "applied", None
    partial = apply_report(r.stderr, diff) or f"PARTIAL: the diff didn't apply ({r.stderr.strip()[:200]})"
    return dest, partial, partial


def cached_repo(full_name: str, commit: str) -> Path:
    """A small bare clone holding one commit's trees (blobs arrive on demand), kept in data/cache/repos/ so
    follow-up packs can rebuild a run's states without a full clone each time."""
    dest = config.DATA / "cache" / "repos" / (full_name.replace("/", "__") + ".git")
    if _run(["git", "--git-dir", str(dest), "cat-file", "-e", f"{commit}^{{commit}}"]).returncode == 0:
        return dest
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "init", "-q", "--bare", str(dest)])
        _run(["git", "--git-dir", str(dest), "remote", "add", "origin", GIT_URL.format(full_name)])
    r = _run(["git", "--git-dir", str(dest), "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", commit],
             timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"git fetch of {full_name}@{commit[:8]} failed: {r.stderr.strip()[-300:]}")
    return dest


def strip_claude_settings(dest: Path) -> None:
    """The outside review runs headless Claude on this Mac with project settings on. The repo's (or the lane
    Claude's diff's) .claude/settings*.json could add hooks that run here, outside the lane: remove them."""
    for name in ("settings.json", "settings.local.json"):
        (dest / ".claude" / name).unlink(missing_ok=True)


def outside_review(clone: Path, task_md: str, diff: str, condensed: str, budget_chars: int = 400_000) -> dict | None:
    t = condensed if len(condensed) < budget_chars else condensed[: budget_chars // 2] + "\n…\n" + condensed[-budget_chars // 2:]
    prompt = (REVIEW_PROMPT.replace("@@TASK@@", task_md).replace("@@DIFF@@", _clip(diff, 4000))
              .replace("@@TRANSCRIPT@@", t))
    strip_claude_settings(clone)
    cmd = [CLAUDE_BIN, "-p", "--model", "opus", "--effort", "high", "--output-format", "json",
           "--allowedTools", "Read,Grep,Glob,Bash(git log:*),Bash(git show:*),Bash(git diff:*),Bash(ls:*),Bash(rg:*),Bash(wc:*)",
           "--disallowedTools", "Edit,Write,MultiEdit,NotebookEdit",
           "--strict-mcp-config", "--setting-sources", "project"]
    r = _run(cmd, cwd=clone, timeout=40 * 60, input=prompt)
    try:
        out = json.loads(r.stdout)
        text = out.get("result", "")
    except json.JSONDecodeError:
        text = r.stdout
    m = re.search(r"<<<REVIEW\s*(\{.*\})\s*REVIEW>>>", text, re.S)
    if not m:
        return {"error": (r.stderr or text)[-1500:]}
    try:
        res = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        return {"error": f"bad JSON from the review: {e}"}
    res["_cost_usd"] = (out.get("total_cost_usd") if isinstance(out, dict) else None)
    return res


def render_review(r: dict | None) -> str:
    if not r:
        return "# Outside review\n\nSkipped (--no-ai).\n"
    if r.get("error"):
        return f"# Outside review\n\nThe review run failed:\n\n```\n{r['error']}\n```\n"
    out = ["# Outside review (Claude Opus 5.5, outside the sandbox)\n", f"**Problem as the code shows it:** {r.get('problem', '')}\n",
           "## What the run did\n"] + [f"{i + 1}. {a}" for i, a in enumerate(r.get("approach", []))]
    out += ["", "## What it may have missed\n"]
    for m in sorted(r.get("misses", []), key=lambda m: {"high": 0, "medium": 1, "low": 2}.get(m.get("severity"), 3)):
        out.append(f"- **[{m.get('severity')}] {m.get('what')}** {m.get('why_it_matters', '')}  \n  evidence: {', '.join(m.get('evidence', []))}")
    if r.get("unverified_claims"):
        out += ["", "## Claims the transcript doesn't back up\n"]
        out += [f"- \"{c.get('claim')}\": {c.get('reality')}" for c in r["unverified_claims"]]
    if r.get("did_well"):
        out += ["", "## Done well\n"] + [f"- {d}" for d in r["did_well"]]
    return "\n".join(out) + "\n"


# --- Assembly ----------------------------------------------------------------------

def _changed_files(diff: str) -> list[str]:
    return list(dict.fromkeys(m.group(1) for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.M)))


def _rel(path: str, clone: Path) -> str | None:
    """Map a path from the run (e.g. /exp/<task>/model_a/src/x.ts) to a repo-relative path in the clone."""
    parts = Path(path).parts
    for i in range(len(parts)):
        rel = "/".join(parts[i:])
        if rel and (clone / rel).is_file():
            return rel
    return None


def _covered(tpath: Path, diff: str) -> dict | None:
    """How far into the run a pack reaches (delta.state), so the next follow-up starts where this one stopped."""
    try:
        from . import delta
        return delta.state(tpath, diff)
    except Exception:
        return None


def _record(task_dir: Path, run: str, out: Path, covered: dict | None, diff: str, tokens: int, log, **extra) -> None:
    """Add the pack to runs/<run>/packs.json (delta.record_pack). A failure here never loses the pack."""
    try:
        from . import delta
        st = dict(covered or {})
        delta.record_pack(task_dir, run, out, "full", st.pop("turn", None), tokens=tokens, diff=diff, **st, **extra)
    except Exception as e:
        log(f"warning: the pack is made but isn't recorded in packs.json ({e})")


def _tokens(text: str) -> int:
    return len(text) // 4  # repomix reports the real count; this is the budget estimate


def build(task_dir: Path, run_name: str | None = None, max_tokens: int = MAX_TOKENS, ai: bool = True,
          log=print) -> Path:
    """Make the pack in a work folder of its own under PACKS (the clone and the pack's files), removed
    afterwards, even on failure. Other builds running at the same time keep theirs."""
    task_dir = Path(task_dir)
    PACKS.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{task_dir.name}-{run_name or 'run'}-", dir=PACKS))
    try:
        return _build(task_dir, run_name, max_tokens, ai, log, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _host(run_dir: Path) -> str:
    remote = _remote(run_dir)
    return f"{remote[0]} (the lane runs there)" if remote else "this Mac"


def _checkpoint(run_dir: Path, covered: dict | None) -> list[str]:
    """01-task.md's checkpoint lines: where the run is and how far into it this pack reaches."""
    c = covered or {}
    reach = (f"turn {c.get('turn')}, {c.get('transcript_entries')} transcript entries, the last at "
             f"{c.get('last_entry_at')}" if c.get("last_entry_at") else "unknown")
    return [f"- Host: {_host(run_dir)}",
            f"- Checkpoint: {reach}; diff fingerprint {c.get('diff_sha') or '?'}"]


def _build(task_dir: Path, run_name: str | None, max_tokens: int, ai: bool, log, work: Path) -> Path:
    g = gather(task_dir, run_name)
    meta, run_dir, sub = g["meta"], g["run_dir"], g["sub"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pack = work / "pack"
    rv, code = pack / "1-review", pack / "2-code"
    rv.mkdir(parents=True)
    code.mkdir(parents=True)
    log(f"{'Live' if g['live'] else 'Collected'} run {run_dir.name}: transcript {g['transcript_path'].name}, "
        f"diff {len(g['diff'].splitlines())} lines")

    slot, repo = meta["slot"], meta["repo"]
    prompt = sub["sections"].get("Opening Prompt", "").strip()
    first = transcript.summarize(g["transcript_path"]).get("first_prompt") or ""
    covered = _covered(g["transcript_path"], g["diff"])   # what this pack shows, read before condensing
    head = [f"# Task {meta['task_name']}", "",
            f"- Slot: {' · '.join(str(slot[k]) for k in ('action', 'domain', 'codebase_type', 'prompt_style', 'interaction_mode'))}",
            f"- Repository: {repo['pinned_url']} ({repo.get('language')})",
            f"- Run: {run_dir.name} ({'in progress' if g['live'] else 'collected'})",
            *_checkpoint(run_dir, covered)]
    opening = ["", "## Opening prompt (what Claude saw first)", "", first.strip() or prompt or "(not found)", ""]
    brief = (task_dir / "brief.md")
    notes = ("\n## Research notes behind this task (AI research, for context)\n\n" + brief.read_text()
             if brief.exists() else "")

    condensed, stats = condense(g["transcript_path"])
    log(f"Transcript: {stats['turns']} human turn{'' if stats['turns'] == 1 else 's'}, {len(condensed):,} characters condensed")

    clone, note, partial = checkout(meta, g["diff"], work / "repo")
    log(f"Checkout at {repo['commit'][:8]}: {note}")
    task_md = "\n".join(head + opening) + notes

    review = None
    if ai:
        log("Outside review running (headless Opus, read-only; a few minutes)…")
        review = outside_review(clone, task_md, g["diff"], condensed)
        if review and not review.get("error"):
            log(f"Outside review: {len(review.get('misses', []))} possible misses"
                + (f", ${review['_cost_usd']:.2f}" if review.get("_cost_usd") else ""))
        else:
            log("Outside review failed; the pack goes without it.")

    outside = bool(review and not review.get("error"))
    (rv / "00-grading-guide.md").write_text(guide(outside, partial))
    (rv / "03-final.diff").write_text(g["diff"] or "(no changes yet)\n")
    if outside:
        # Last in the pack (repomix puts folders in name order), so the reviewer meets it after judging.
        (pack / "3-outside-review").mkdir()
        (pack / "3-outside-review" / "candidate-claims.md").write_text(render_review(review))

    # Files: changed first, then the review's picks, then the most-read files.
    changed = _changed_files(g["diff"])
    picks = (review or {}).get("extra_files", []) if review and not review.get("error") else []
    read_rel = [r for r in (_rel(p, clone) for p, _ in stats["reads"].most_common(40)) if r]
    ordered = list(dict.fromkeys(changed + picks + read_rel))
    fixed = sum(_tokens(f.read_text()) for f in (*rv.glob("*"), *(pack / "3-outside-review").glob("*.md")))
    budget = max_tokens - fixed - _tokens(task_md) - 1_000 - 3_000   # 1,000: the coverage lines added below
    t_budget = int(budget * 0.45)
    if _tokens(condensed) > t_budget:
        for n in (20, 10, 5, 2):
            condensed, _ = condense(g["transcript_path"], out_lines=n)
            if _tokens(condensed) <= t_budget:
                break
        if _tokens(condensed) > t_budget:
            keep = t_budget * 4
            condensed = condensed[: keep // 2] + "\n\n… [middle of the run cut to fit] …\n\n" + condensed[-keep // 2:]
    cut = "cut to fit" in condensed
    (rv / "02-transcript.md").write_text(condensed)
    left = budget - _tokens(condensed)
    included, skipped = [], []
    for rel in ordered:
        src = clone / rel
        if not src.is_file() or src.stat().st_size > 400_000:
            continue
        text = src.read_text(errors="replace")
        if _tokens(text) <= left:
            dst = code / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text)
            left -= _tokens(text)
            included.append(rel)
        else:
            skipped.append(rel)
    log(f"Files: {len(included)} included, {len(skipped)} left out for size")
    coverage = ["", "## What this pack leaves out", "",
                f"- Checkout: {partial + ' (the diff in 03-final.diff is complete)' if partial else note}",
                f"- Transcript: {'shortened, and the middle cut to fit' if cut else 'tool output shortened; every message and tool call kept'}",
                "- Files left out for size: " + (", ".join(f"`{p}`" for p in skipped[:30])
                                                 + (f" and {len(skipped) - 30} more" if len(skipped) > 30 else "")
                                                 if skipped else "none")]
    (rv / "01-task.md").write_text("\n".join(head + coverage + opening) + notes)

    out = run_dir / f"review-pack-{stamp}.xml"
    header = (f"Johto review pack · {meta['task_name']} · {run_dir.name} · {repo['full_name']}@{repo['commit'][:8]} · "
              f"made {time.strftime('%Y-%m-%d %H:%M')}. Start with 1-review/00-grading-guide.md.")
    if partial:
        header = f"{partial}. " + header
    r = _run(REPOMIX + [str(pack), "--style", "xml", "--output", str(out), "--header-text", header,
                        "--no-gitignore", "--no-git-sort-by-changes", "--no-security-check", "--top-files-len", "8"],
             timeout=600)
    if r.returncode != 0:
        raise SystemExit(f"repomix failed: {r.stderr[-800:]}")
    if partial:
        out.write_text(f"{partial}. Some of the run's diff is missing from 2-code/; see 1-review/00-grading-guide.md.\n\n"
                       + out.read_text())
    m = re.search(r"Total Tokens:\s*([\d,]+)", r.stdout)
    tokens = int(m.group(1).replace(",", "")) if m else _tokens(out.read_text())
    log(f"Pack: {out} ({tokens:,} tokens)")
    if tokens > max_tokens:
        log(f"warning: {tokens:,} tokens is over the {max_tokens:,} budget; rerun with --max-tokens lower to shrink files")
    _record(task_dir, run_dir.name, out, covered, g["diff"], tokens, log, outside_review=outside, partial=partial)
    if partial:
        log(f"{partial}: the pack says so, and it won't be sent to GPT Pro without --force.")
    return out
