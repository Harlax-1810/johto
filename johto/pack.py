"""Review packs: one repomix file (≤ 200k tokens) about a lane run, for pre-grading with another model.

A pack holds the task (slot, repo, Austin's prompt), Claude's approach (a condensed transcript), the diff,
the files that matter at their changed state, and an outside-the-lane Claude pass on what the run may
have missed. It works mid-run (live transcript and `git diff` from the lane container) or after collect.

This is difficulty research (allowed by Johto's LLM policy). Nothing here writes Austin's fields.
"""
from __future__ import annotations

import collections
import json
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from . import config, lanes, transcript, workspace

REPOMIX = ["npx", "--yes", "repomix@1.18.1"]
CLAUDE_BIN = config.CLAUDE_BIN   # JOHTO_CLAUDE_BIN; tests point it at a stub so no paid review runs
PACKS = config.DATA / "cache" / "packs"
MAX_TOKENS = 200_000

GUIDE = """# How to use this pack

You are pre-grading one run of Claude Code (Opus 5.5, high effort) on a coding task, for an engineer who
writes evaluation tasks for Mercor's Project Johto. Everything you need is in this file:

1. `1-review/01-task.md`: the task slot, the repository at its pinned commit, and the engineer's opening prompt
   (the only text Claude saw at the start; later human turns are in the transcript).
2. `1-review/02-transcript.md`: what Claude did, in order: the human's messages, Claude's messages, every tool
   call (commands, edits, reads) and shortened tool output. Test runs keep more output.
3. `1-review/03-final.diff`: the change Claude made so far, against the pinned commit.
4. `1-review/04-outside-review.md`: a separate Claude pass, run outside the sandbox on a clean checkout with the
   diff applied, listing what the run may have missed, with file and line evidence. Treat it as leads,
   not truth; verify against the code in `2-code/`.
5. `2-code/`: the files Claude changed (at their changed state) and the closely related files it read or
   that the change depends on, so you can check claims against the real code.

Please assess:

- Did Claude solve the actual problem (root cause, not symptom)? What would a strong senior engineer
  have done differently?
- What did it miss: edge cases, interactions across files or layers, existing data or behaviour it
  could break, paths it never tested, claims it made without verifying them?
- How much did the human have to steer it, and where?
- For Johto's scales (One-shot yes/no; Task Success 1-5: 1 failed, 2 partial with major issues, 3
  completed but highly inefficient or non-optimal with notable problems, 4 successful with minor or
  style issues, 5 completely successful; Steering 1-5: 1 constant input ... 5 none), list the evidence
  only: file:line references, what was missed, which claims are unverified, and where the human
  steered. Do not write prose explanations of ratings; the engineer writes his own notes.

Give concrete file and line references. Be skeptical of the run's own summary of what it did.
"""

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


def gather(task_dir: Path, run_name: str | None) -> dict:
    run_dir = _pick_run(task_dir, run_name)
    meta = workspace.load(task_dir)
    live_t, live_d = _live_transcript(run_dir), _live_diff(run_dir)
    logs = transcript.find(run_dir)
    tpath = live_t or (logs[-1] if logs else None)
    diff_file = run_dir / "prework_output" / "final.diff"
    diff = live_d if live_d is not None else (diff_file.read_text(errors="replace") if diff_file.exists() else "")
    if not tpath:
        raise SystemExit(f"No transcript for {run_dir.name} yet (start Claude in the lane first).")
    return {"meta": meta, "run_dir": run_dir, "transcript_path": tpath, "diff": diff,
            "live": live_t is not None, "sub": workspace.parse_submission(task_dir)}


# --- Condensing the transcript ---------------------------------------------------

def _clip(text: str, head: int, tail: int = 0) -> str:
    lines = text.splitlines()
    if len(lines) <= head + tail:
        return text
    cut = len(lines) - head - tail
    return "\n".join(lines[:head] + [f"… [{cut} lines cut] …"] + (lines[-tail:] if tail else []))


def condense(tpath: Path, out_lines: int = 40) -> tuple[str, dict]:
    """Markdown of the run: human turns, Claude's text, tool calls with shortened output."""
    entries = []
    for line in Path(tpath).read_text(errors="ignore").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    calls, reads, md = {}, collections.Counter(), []
    turn = 0
    humans = iter(transcript.human_messages(entries))
    for e in entries:
        if e.get("isSidechain"):
            continue
        ts = (e.get("timestamp") or "")[11:19]
        if e.get("type") == "user":
            human = transcript._human_text(e)
            if human is not None and e.get("timestamp"):
                hm = next(humans, None)
                if hm and not hm["counted"]:
                    md.append(f"\n## Human ({ts}, not counted: {hm['note']})\n\n{human}\n")
                else:
                    turn += 1
                    label = "opening prompt" if turn == 1 else f"follow-up {turn - 1}"
                    md.append(f"\n## Human, {label} ({ts})\n\n{human}\n")
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
                        md.append(f"→ {len(content.splitlines())} lines{err}\n")
                    else:
                        md.append(f"→ output{err}:\n```\n{_clip(content, n * 2 // 3, n // 3)}\n```\n")
        elif e.get("type") == "assistant":
            for x in transcript._content(e) or []:
                if not isinstance(x, dict):
                    continue
                if x.get("type") == "text" and x.get("text", "").strip():
                    md.append(f"\n**Claude ({ts}):** {x['text'].strip()}\n")
                elif x.get("type") == "tool_use":
                    name, inp = x.get("name"), x.get("input") or {}
                    cmd = inp.get("command") or ""
                    calls[x.get("id")] = {"name": name, "cmd": cmd}
                    if name == "Bash":
                        md.append(f"\n`{name}` ({ts}): `{cmd[:400]}`\n")
                    elif name in ("Read",):
                        reads[inp.get("file_path", "")] += 1
                        rng = f" lines {inp.get('offset')}+{inp.get('limit')}" if inp.get("offset") else ""
                        md.append(f"\n`Read` {inp.get('file_path')}{rng}\n")
                    elif name in ("Edit", "MultiEdit", "Write"):
                        body = inp.get("new_string") or inp.get("content") or json.dumps(inp.get("edits", ""))[:1500]
                        md.append(f"\n`{name}` {inp.get('file_path')}:\n```\n{_clip(str(body), 30, 5)}\n```\n")
                    else:
                        md.append(f"\n`{name}` {json.dumps(inp)[:300]}\n")
    return "".join(md), {"reads": reads, "turns": turn}


# --- Outside review ----------------------------------------------------------------

def checkout(meta: dict, diff: str, dest: Path) -> tuple[Path, str]:
    """Clean clone at the pinned commit with the run's diff applied. Returns (path, apply note)."""
    repo = meta["repo"]
    if dest.exists():
        shutil.rmtree(dest)
    _run(["git", "clone", "-q", "--filter=blob:none", f"https://github.com/{repo['full_name']}.git", str(dest)], timeout=900)
    _run(["git", "checkout", "-q", repo["commit"]], cwd=dest, timeout=300)
    if not diff.strip():
        strip_claude_settings(dest)
        return dest, "no changes"
    patch = dest.parent / "run.diff"
    patch.write_text(diff)
    r = _run(["git", "apply", "--reject", "--whitespace=nowarn", str(patch)], cwd=dest, timeout=120)
    strip_claude_settings(dest)
    return dest, "applied" if r.returncode == 0 else f"applied with rejects: {r.stderr.strip()[:300]}"


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


def _tokens(text: str) -> int:
    return len(text) // 4  # repomix reports the real count; this is the budget estimate


def build(task_dir: Path, run_name: str | None = None, max_tokens: int = MAX_TOKENS, ai: bool = True,
          log=print) -> Path:
    """Make the pack; the clone and work folder are always removed, even on failure."""
    before = set(PACKS.glob("*")) if PACKS.exists() else set()
    try:
        return _build(task_dir, run_name, max_tokens, ai, log)
    finally:
        for p in (set(PACKS.glob("*")) if PACKS.exists() else set()) - before:
            shutil.rmtree(p, ignore_errors=True)


def _build(task_dir: Path, run_name: str | None, max_tokens: int, ai: bool, log) -> Path:
    g = gather(task_dir, run_name)
    meta, run_dir, sub = g["meta"], g["run_dir"], g["sub"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = PACKS / f"{task_dir.name}-{run_dir.name}-{stamp}"
    pack = work / "pack"
    rv, code = pack / "1-review", pack / "2-code"
    rv.mkdir(parents=True)
    code.mkdir(parents=True)
    log(f"{'Live' if g['live'] else 'Collected'} run {run_dir.name}: transcript {g['transcript_path'].name}, "
        f"diff {len(g['diff'].splitlines())} lines")

    slot, repo = meta["slot"], meta["repo"]
    prompt = sub["sections"].get("Opening Prompt", "").strip()
    first = transcript.summarize(g["transcript_path"]).get("first_prompt") or ""
    task_md = "\n".join([
        f"# Task {meta['task_name']}", "",
        f"- Slot: {' · '.join(str(slot[k]) for k in ('action', 'domain', 'codebase_type', 'prompt_style', 'interaction_mode'))}",
        f"- Repository: {repo['pinned_url']} ({repo.get('language')})",
        f"- Run: {run_dir.name} ({'in progress' if g['live'] else 'collected'})", "",
        "## Opening prompt (what Claude saw first)", "", first.strip() or prompt or "(not found)", ""])
    lead = meta.get("lead_id")
    brief = (task_dir / "brief.md")
    if brief.exists():
        task_md += "\n## Research notes behind this task (AI research, for context)\n\n" + brief.read_text()

    condensed, stats = condense(g["transcript_path"])
    log(f"Transcript: {stats['turns']} human turn{'' if stats['turns'] == 1 else 's'}, {len(condensed):,} characters condensed")

    clone, note = checkout(meta, g["diff"], work / "repo")
    log(f"Checkout at {repo['commit'][:8]}: {note}")

    review = None
    if ai:
        log("Outside review running (headless Opus, read-only; a few minutes)…")
        review = outside_review(clone, task_md, g["diff"], condensed)
        if review and not review.get("error"):
            log(f"Outside review: {len(review.get('misses', []))} possible misses"
                + (f", ${review['_cost_usd']:.2f}" if review.get("_cost_usd") else ""))
        else:
            log("Outside review failed; the pack notes it.")

    (rv / "00-grading-guide.md").write_text(GUIDE)
    (rv / "01-task.md").write_text(task_md)
    (rv / "03-final.diff").write_text(g["diff"] or "(no changes yet)\n")
    (rv / "04-outside-review.md").write_text(render_review(review))

    # Files: changed first, then the review's picks, then the most-read files.
    changed = _changed_files(g["diff"])
    picks = (review or {}).get("extra_files", []) if review and not review.get("error") else []
    read_rel = [r for r in (_rel(p, clone) for p, _ in stats["reads"].most_common(40)) if r]
    ordered = list(dict.fromkeys(changed + picks + read_rel))
    fixed = sum(_tokens((rv / f).read_text()) for f in ("00-grading-guide.md", "01-task.md", "03-final.diff", "04-outside-review.md"))
    budget = max_tokens - fixed - 3_000
    t_budget = int(budget * 0.45)
    if _tokens(condensed) > t_budget:
        for n in (20, 10, 5, 2):
            condensed, _ = condense(g["transcript_path"], out_lines=n)
            if _tokens(condensed) <= t_budget:
                break
        if _tokens(condensed) > t_budget:
            keep = t_budget * 4
            condensed = condensed[: keep // 2] + "\n\n… [middle of the run cut to fit] …\n\n" + condensed[-keep // 2:]
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

    out = run_dir / f"review-pack-{stamp}.xml"
    header = (f"Johto review pack · {meta['task_name']} · {run_dir.name} · {repo['full_name']}@{repo['commit'][:8]} · "
              f"made {time.strftime('%Y-%m-%d %H:%M')}. Start with 1-review/00-grading-guide.md.")
    r = _run(REPOMIX + [str(pack), "--style", "xml", "--output", str(out), "--header-text", header,
                        "--no-gitignore", "--no-security-check", "--top-files-len", "8"], timeout=600)
    if r.returncode != 0:
        raise SystemExit(f"repomix failed: {r.stderr[-800:]}")
    m = re.search(r"Total Tokens:\s*([\d,]+)", r.stdout)
    tokens = int(m.group(1).replace(",", "")) if m else _tokens(out.read_text())
    log(f"Pack: {out} ({tokens:,} tokens)")
    if tokens > max_tokens:
        log(f"warning: {tokens:,} tokens is over the {max_tokens:,} budget; rerun with --max-tokens lower to shrink files")
    shutil.rmtree(work / "repo", ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    return out
