"""Follow-up review packs, and the record of every pack built for a run.

Austin sends each lane turn to GPT Pro as a review pack. The first is a full pack (johto/pack.py). For later
turns he wants a reply in the same ChatGPT conversation that doesn't restate what hasn't changed: a follow-up
pack with the new turns, the interdiff (what changed in Claude's diff since the reviewer last saw it) and the
current text of the files that changed. `next_pack` picks a full pack, a follow-up, or an existing pack when
nothing changed.

runs/<run>/packs.json lists every pack built for the run, oldest first: {path, kind: "full"|"followup", turn,
built_at, transcript_entries, last_entry_at, diff_sha, diff_path, ...}. `turn` is the last of Austin's counted
prompts the pack covers; `transcript_entries` and `last_entry_at` say how far into the transcript it reached
(main-thread user and assistant entries); `diff_path` is a saved copy of the diff it showed (runs/<run>/pack-diffs/),
so the next follow-up can show what changed. Packs made before this file existed are added from their XML
(`backfill`). reviews.py adds `sent_at` and `chat_url` when a pack goes to ChatGPT.

A follow-up's read-me asks for the same reply format as a full pack (pack.REPLY_FORMAT) and lists the findings
GPT gave so far in this run (reviews.last_findings: ID, kind, severity, status, one line), so it keeps the IDs
and marks each one's status. `checkpoint_packed` says whether a pack already shows the run as it is now;
`reviewed_checkpoint` and `diff_changed` say whether Claude's diff moved since the pack GPT last saw.

Research only (Johto's LLM policy): nothing here writes Austin's fields.
"""
from __future__ import annotations

import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, pack, settings, stages, transcript

FILE = "packs.json"
DIFFS = "pack-diffs"
PACK_GLOBS = ("review-pack-*.xml", "review-followup-*.xml")
CONTEXT = (25, 10, 3)        # interdiff context lines, widest first; narrowed until it fits
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
MAX_FILE_BYTES = 400_000

SUMMARY = """This file is a merged representation of a follow-up review pack, combined into a single document in Repomix's format.

<file_summary>
This section contains a summary of this file.

<purpose>
This file continues an earlier review pack of the same coding-agent run. It holds only what changed since that
pack. Start with 1-review/00-read-me.md.
</purpose>

<file_format>
The content is organized as follows:
1. This summary section
2. The header
3. Directory structure
4. Multiple file entries, each consisting of:
  - File path as an attribute
  - Full contents of the file
</file_format>
</file_summary>
"""


# --- packs.json ----------------------------------------------------------------------

def _run_dir(task_dir: Path, run: str) -> Path:
    return Path(task_dir) / "runs" / run


@contextlib.contextmanager
def _locked(run_dir: Path):
    """packs.json is written by pack builds (web jobs, the turn watcher, the CLI) and by sends: one at a time."""
    with open(run_dir / ".packs.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _load(run_dir: Path) -> list[dict]:
    try:
        recs = json.loads((run_dir / FILE).read_text())
    except (FileNotFoundError, ValueError):
        return []
    return recs if isinstance(recs, list) else []


def _write(run_dir: Path, recs: list[dict]) -> None:
    p = run_dir / FILE
    tmp = p.with_name(f".{FILE}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(recs, indent=1))
    os.replace(tmp, p)


def _rel(run_dir: Path, p) -> str:
    """Paths inside the run folder are stored relative to it, so a copied task folder still finds them."""
    p = Path(p)
    try:
        return str(p.resolve().relative_to(run_dir.resolve()))
    except ValueError:
        return str(p)


def _abs(run_dir: Path, p: str | None) -> str | None:
    if not p:
        return None
    return str(p) if Path(p).is_absolute() else str(run_dir / p)


def _view(run_dir: Path, rec: dict) -> dict:
    return {**rec, "path": _abs(run_dir, rec.get("path")), "diff_path": _abs(run_dir, rec.get("diff_path"))}


def _when(iso: str | None) -> float:
    ts = transcript._ts(iso)
    return ts.timestamp() if ts else 0.0


def packs(task_dir: Path, run: str) -> list[dict]:
    """Every pack built for the run, oldest first, with absolute `path` and `diff_path`."""
    run_dir = _run_dir(task_dir, run)
    if not run_dir.is_dir():
        return []
    backfill(task_dir, run)
    return [_view(run_dir, r) for r in _load(run_dir)]


def record_pack(task_dir: Path, run: str, path, kind: str, turn: int | None, **extra) -> dict:
    """Add (or replace) the record for one pack. `diff=<text>` saves a copy of the diff the pack showed.
    Logs a pack_built stage event unless `backfilled=True`."""
    run_dir = _run_dir(task_dir, run)
    path = Path(path)
    diff = extra.pop("diff", None)
    rec = {"path": _rel(run_dir, path), "kind": kind, "turn": turn,
           "built_at": extra.pop("built_at", None) or stages.now_iso()}
    if diff is not None:
        dp = run_dir / DIFFS / f"{path.stem}.diff"
        dp.parent.mkdir(exist_ok=True)
        dp.write_text(diff)
        rec["diff_sha"], rec["diff_path"] = _sha(diff), _rel(run_dir, dp)
    rec.update({k: v for k, v in extra.items() if v is not None and k not in rec})
    with _locked(run_dir):
        recs = [r for r in _load(run_dir) if r.get("path") != rec["path"]] + [rec]
        recs.sort(key=lambda r: _when(r.get("built_at")))
        _write(run_dir, recs)
    if not rec.get("backfilled"):
        stages.log(task_dir, "pack_built", run=run, path=f"runs/{run}/{rec['path']}", pack_kind=kind, turn=turn)
    return _view(run_dir, rec)


def update_pack(task_dir: Path, run: str, path, **fields) -> dict | None:
    """Merge fields (such as sent_at, chat_url) into one pack's record."""
    run_dir = _run_dir(task_dir, run)
    key = _rel(run_dir, path)
    with _locked(run_dir):
        recs = _load(run_dir)
        rec = next((r for r in recs if r.get("path") == key), None)
        if rec is None:
            return None
        rec.update(fields)
        _write(run_dir, recs)
    return _view(run_dir, rec)


def pack_record(task_dir: Path, run: str, path) -> dict | None:
    key = str(Path(path))
    return next((r for r in packs(task_dir, run) if r["path"] == key or Path(r["path"]).name == Path(key).name), None)


# --- How far a pack reaches ------------------------------------------------------------

def _sha(diff: str) -> str:
    """A diff's fingerprint. Trailing blank lines don't count (repomix drops the last newline of a file)."""
    return hashlib.sha256((diff or "").rstrip().encode()).hexdigest()[:16]


def _entries(tpath: Path) -> list[dict]:
    out = []
    for line in Path(tpath).read_text(errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _material(e: dict) -> bool:
    """A main-thread entry a pack's transcript shows: Claude's messages and tool calls, tool results, and Austin's
    prompts. Not timers, titles, compaction summaries, background notices or local commands such as /exit."""
    if e.get("isSidechain") or not transcript._ts(e.get("timestamp")):
        return False
    if e.get("type") == "assistant":
        return True
    if e.get("type") != "user" or e.get("isMeta") or e.get("isCompactSummary"):
        return False
    if transcript._human_text(e) is not None:
        return True
    c = transcript._content(e)
    return isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c)


def _main(entries: list[dict]) -> list[dict]:
    return [e for e in entries if _material(e)]


def _prompts(entries: list[dict]) -> list[tuple]:
    """Austin's counted prompts, (datetime, text). Context-compaction summaries are not his."""
    return transcript.counted_prompts([e for e in entries if not e.get("isCompactSummary")])


def _state(entries: list[dict], diff: str, until: datetime | None = None) -> dict:
    main = [e for e in _main(entries) if until is None or transcript._ts(e["timestamp"]) <= until]
    last = max(main, key=lambda e: transcript._ts(e["timestamp"]), default=None)
    end = transcript._ts(last["timestamp"]) if last else None
    turn = sum(1 for ts, _ in _prompts(entries) if end is not None and ts <= end)
    return {"turn": turn, "transcript_entries": len(main), "last_entry_at": last["timestamp"] if last else None,
            "diff_sha": _sha(diff)}


def state(tpath: Path, diff: str) -> dict:
    """{turn, transcript_entries, last_entry_at, diff_sha} for a transcript and diff as they are now."""
    return _state(_entries(tpath), diff)


def _unchanged(rec: dict, st: dict) -> bool:
    return (rec.get("last_entry_at") == st["last_entry_at"] and rec.get("transcript_entries") == st["transcript_entries"]
            and rec.get("diff_sha") == st["diff_sha"])


# --- Backfill: packs made before packs.json --------------------------------------------

def _stamp(name: str) -> float | None:
    """review-pack-20261031-020945.xml -> its build time (the Mac's local time when it was made)."""
    m = re.search(r"(\d{8}-\d{6})", name)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S"))
    except ValueError:
        return None


def xml_file(xml: str, path: str) -> str | None:
    """One file's contents from a repomix XML pack."""
    tag = f'<file path="{path}">\n'
    i = xml.find(tag)
    if i < 0:
        return None
    j = xml.find("\n</file>\n", i + len(tag))
    return xml[i + len(tag): j if j >= 0 else len(xml)]


def _run_transcript(run_dir: Path) -> Path | None:
    """The run's transcript on this machine (collected, else the last live copy), without reaching any lane."""
    logs = transcript.find(run_dir)
    if logs:
        return logs[-1]
    live = sorted((run_dir / ".live-transcripts").glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)
    return live[-1] if live else None


def backfill(task_dir: Path, run: str) -> list[dict]:
    """Add records for pack files in the run folder that packs.json doesn't list (packs made before it existed,
    or by hand). Coverage comes from the file's time stamp against the transcript; full packs' diffs come from
    their 03-final.diff. Hand-made follow-ups have no full diff, so the next follow-up shows code changes from the
    newest pack that has one. Local files only: no lane, network or clone."""
    run_dir = _run_dir(task_dir, run)
    files = sorted({f for g in PACK_GLOBS for f in run_dir.glob(g)}, key=lambda f: _stamp(f.name) or f.stat().st_mtime)
    known = {r.get("path") for r in _load(run_dir)}
    new = [f for f in files if _rel(run_dir, f) not in known]
    if not new:
        return []
    tpath = _run_transcript(run_dir)
    entries = _entries(tpath) if tpath else []
    added = []
    for f in new:
        made = _stamp(f.name) or f.stat().st_mtime
        until = datetime.fromtimestamp(made, timezone.utc)
        kind = "followup" if "followup" in f.name else "full"
        diff = xml_file(f.read_text(errors="replace"), "1-review/03-final.diff") if kind == "full" else None
        if diff is not None and diff.strip() == "(no changes yet)":
            diff = ""
        st = _state(entries, diff or "", until)
        added.append(record_pack(
            task_dir, run, f, kind, st["turn"] if entries else None, diff=diff, backfilled=True,
            built_at=datetime.fromtimestamp(made).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
            transcript_entries=st["transcript_entries"] if entries else None,
            last_entry_at=st["last_entry_at"] if entries else None))
    return added


# --- Interdiff -----------------------------------------------------------------------------

def _git(git_dir: Path, *args, env=None, timeout=600) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "--git-dir", str(git_dir), "-c", "core.quotepath=off", *args],
                          capture_output=True, text=True, env=env, timeout=timeout)


def _tree(git_dir: Path, commit: str, diff: str) -> str | None:
    """The tree of the pinned commit with a run's diff applied, built in a scratch index (no checkout)."""
    with tempfile.TemporaryDirectory(prefix="johto-tree-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if _git(git_dir, "read-tree", commit, env=env).returncode != 0:
            return None
        if diff.strip():
            patch = Path(tmp) / "run.diff"
            patch.write_text(diff if diff.endswith("\n") else diff + "\n")
            if _git(git_dir, "apply", "--cached", "--whitespace=nowarn", str(patch), env=env).returncode != 0:
                return None
        r = _git(git_dir, "write-tree", env=env)
        return r.stdout.strip() if r.returncode == 0 else None


def _sections(diff: str) -> dict[str, str]:
    """A diff split per file, keyed by the new path."""
    out = {}
    for part in re.split(r"(?m)^(?=diff --git )", diff or ""):
        m = re.match(r"diff --git a/(.+?) b/(.+)", part)
        if m:
            out[m.group(2).strip()] = part
    return out


class Interdiff:
    """What changed in Claude's diff between two packs.

    First by rebuilding both states from the pinned commit (pack.cached_repo, a scratch index each) and diffing
    the trees, which gives a real diff and the files' current text. If the repo can't be fetched or a diff doesn't
    apply, by comparing the two diffs file by file."""

    def __init__(self, meta: dict, old: str, new: str):
        self.old, self.new = old or "", new or ""
        self.git_dir = self.a = self.b = None
        self.note = None
        repo = meta.get("repo") or {}
        try:
            self.git_dir = pack.cached_repo(repo["full_name"], repo["commit"])
            self.a = _tree(self.git_dir, repo["commit"], self.old)
            self.b = _tree(self.git_dir, repo["commit"], self.new)
        except (KeyError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            self.note = f"the repository couldn't be fetched ({str(e)[:120]})"
        if not (self.a and self.b) and not self.note:
            self.note = "a diff didn't apply to the pinned commit"
        self.method = "trees" if self.a and self.b else "diffs"

    def files(self) -> list[tuple[str, str]]:
        """[(status, path)]: A added, M modified, D deleted (since the last pack)."""
        if self.method == "trees":
            r = _git(self.git_dir, "diff", "--name-status", "--no-renames", self.a, self.b)
            return [tuple(line.split("\t", 1)) for line in r.stdout.splitlines() if "\t" in line]
        o, n = _sections(self.old), _sections(self.new)
        order = list(n) + [p for p in o if p not in n]
        return [("A" if p not in o else "D" if p not in n else "M", p) for p in order if o.get(p) != n.get(p)]

    def stat(self) -> str:
        if self.method == "trees":
            return _git(self.git_dir, "diff", "--stat=100", self.a, self.b).stdout.rstrip()
        return "\n".join(f" {p} ({s})" for s, p in self.files())

    def text(self, context: int) -> str:
        if self.method == "trees":
            return _git(self.git_dir, "diff", "--no-color", f"-U{context}", self.a, self.b).stdout
        o, n = _sections(self.old), _sections(self.new)
        out = []
        for _, p in self.files():
            out += difflib.unified_diff(o.get(p, "").splitlines(), n.get(p, "").splitlines(),
                                        f"last pack/{p}", f"now/{p}", n=context, lineterm="")
        return "\n".join(out) + ("\n" if out else "")

    def content(self, path: str) -> str | None:
        """A changed file's text now, or None (deleted, binary, too big, or no trees)."""
        if self.method != "trees":
            return None
        r = subprocess.run(["git", "--git-dir", str(self.git_dir), "cat-file", "blob", f"{self.b}:{path}"],
                           capture_output=True, timeout=600)
        if r.returncode != 0 or len(r.stdout) > MAX_FILE_BYTES or b"\0" in r.stdout[:8192]:
            return None
        return r.stdout.decode(errors="replace")


# --- Building a follow-up ----------------------------------------------------------------------

def _reviewed(task_dir: Path, run: str) -> set[str]:
    """Pack file names a saved review answers."""
    try:
        from . import reviews
        return {Path(r["pack"]).name for r in reviews.list_reviews(task_dir) if r.get("run") == run and r.get("pack")}
    except Exception:
        return set()


def _pick_base(task_dir: Path, run: str, recs: list[dict], base=None) -> dict | None:
    """The pack the reviewer last saw: the newest one sent to ChatGPT or answered by a saved review, else the
    newest pack. `base` (a path, file name or index into packs.json) overrides."""
    if base is not None:
        if isinstance(base, int):
            return recs[base] if -len(recs) <= base < len(recs) else None
        return next((r for r in recs if r["path"] == str(base) or Path(r["path"]).name == Path(str(base)).name), None)
    seen = _reviewed(task_dir, run)
    for r in reversed(recs):
        if r.get("sent_at") or Path(r["path"]).name in seen:
            return r
    return recs[-1] if recs else None


def _base_diff(recs: list[dict], base: dict) -> tuple[str | None, dict | None]:
    """The diff the reviewer last saw: the base pack's saved copy, else the newest earlier pack that has one."""
    upto = recs[: recs.index(base) + 1] if base in recs else recs
    for r in reversed(upto):
        p = r.get("diff_path")
        if p and Path(p).exists():
            return Path(p).read_text(errors="replace"), r
    return None, None


def _label(index: int) -> str:
    return "Opening prompt" if index == 1 else f"Follow-up {index - 1}"


def _hms(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%H:%M:%S")


def _turn_facts(entries: list[dict], tpath: Path, since: str | None) -> list[dict]:
    """One row per turn with entries after `since`: what Austin asked, how long Claude worked, tool calls."""
    after = transcript._ts(since)
    prompts = _prompts(entries)
    main = [e for e in entries if not e.get("isSidechain") and transcript._ts(e.get("timestamp"))]
    try:
        timed = {t["index"]: t for t in (transcript.summarize(tpath).get("turns") or [])}
    except Exception:
        timed = {}
    out = []
    for i, (start, text) in enumerate(prompts):
        end = prompts[i + 1][0] if i + 1 < len(prompts) else None
        in_turn = [e for e in main if transcript._ts(e["timestamp"]) >= start
                   and (end is None or transcript._ts(e["timestamp"]) < end)]
        new = [e for e in _main(in_turn) if after is None or transcript._ts(e["timestamp"]) > after]
        if not new:
            continue
        tools = [x for e in new if e.get("type") == "assistant" for x in (transcript._content(e) or [])
                 if isinstance(x, dict) and x.get("type") == "tool_use"]
        timer = sum(int(e.get("durationMs") or 0) for e in in_turn
                    if e.get("type") == "system" and e.get("subtype") == "turn_duration")
        t = timed.get(i + 1) or {}
        minutes = t.get("working_minutes") if t else (round(timer / 60000, 1) if timer else None)
        background = t.get("background_minutes") or 0
        if t.get("ended"):
            ended = t["ended"]
        elif end is not None:
            ended = "end_turn"
        else:
            last = next((e for e in reversed(in_turn) if e.get("type") == "assistant"), None)
            ended = "end_turn" if (timer or ((last or {}).get("message") or {}).get("stop_reason") == "end_turn") else "open"
        out.append({"index": i + 1, "label": _label(i + 1), "start": start, "text": text,
                    "continuing": after is not None and start <= after, "tool_calls": len(tools),
                    "edits": sum(1 for x in tools if x.get("name") in EDIT_TOOLS), "minutes": minutes,
                    "background_minutes": background, "ended": ended})
    return out


def _quote(text: str, n: int = 300) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _made(rec: dict) -> str:
    ts = transcript._ts(rec.get("built_at"))
    return ts.strftime("%Y-%m-%d %H:%M") if ts else "earlier"


def _fid_num(fid) -> int:
    m = re.match(r"F(\d+)$", str(fid or ""))
    return int(m.group(1)) if m else 0


def _findings_lines(findings: list[dict] | None) -> list[str]:
    """The read-me's "Your findings so far": one line per earlier finding, then which ID to number from."""
    rows = [f for f in findings or [] if f.get("id")]
    if not rows:
        return ["## Your findings so far", "",
                "None are on record in the format below, so number this reply's findings from F1, and give each "
                "earlier point you still hold as a finding.", ""]
    out = ["## Your findings so far", "",
           "From your replies in this run, with each one's latest status. Give every ID again with its status now, "
           f"and number new findings from F{max(_fid_num(f['id']) for f in rows) + 1}.", ""]
    for f in sorted(rows, key=lambda f: _fid_num(f["id"])):
        tags = ", ".join(x for x in (f.get("kind"), f.get("severity"), f.get("status")) if x)
        out.append(f"- **{f['id']}**{f' ({tags})' if tags else ''}: {_quote(f.get('finding') or '(no text)', 220)}")
    return out + [""]


def _readme(meta: dict, run: str, base: dict, diff_rec: dict | None, turns: list[dict], inter: Interdiff | None,
            changed: list[tuple[str, str]], included: list[str], skipped: list[str], last_review: str | None,
            findings: list[dict] | None = None) -> str:
    kind = "follow-up pack" if base.get("kind") == "followup" else "pack"
    covered = f"turn {base['turn']}" if base.get("turn") else "the run so far"
    lines = [f"# Follow-up to your review of {meta['task_name']}, {run}", "",
             f"This pack continues your review of {covered} (the {kind} made {_made(base)}). The task, opening prompt, "
             "grading guide and scales are unchanged, and so is the code, except for what `02-interdiff.diff` shows. "
             "This pack holds only what's new.", "", "## What happened since your review", ""]
    for t in turns:
        calls = f"{t['tool_calls']} tool call{'' if t['tool_calls'] == 1 else 's'}"
        edits = f", {t['edits']} edit{'' if t['edits'] == 1 else 's'}" if t["edits"] else ""
        worked = f"Claude worked {t['minutes']:g} min" if t["minutes"] is not None else ""
        if worked and t.get("background_minutes", 0) >= 1:
            worked += f" plus {round(t['background_minutes'])} min waiting on a background job"
        still = " Still running when this pack was made." if t["ended"] == "open" else ""
        if t["continuing"]:
            whole = f" {worked} on this turn in all." if worked else ""
            lines.append(f"- **{t['label']}, continued** (started {_hms(t['start'])} UTC, before your last pack): "
                         f"{calls}{edits} since then.{whole}{still}")
        else:
            facts = ", ".join(x for x in (worked, calls + edits) if x)
            lines.append(f"- **{t['label']}** ({_hms(t['start'])} UTC; {facts}): \"{_quote(t['text'])}\"{still}")
    if not turns:
        lines.append("- No new turns. Only the code changed.")
    n = len(changed)
    lines += ["", "## Files", "", "1. `01-transcript.md`: the new turns, condensed the same way as before."]
    if inter is None:
        lines.append("2. `02-interdiff.diff`: no code changes since your last pack.")
    elif inter.method == "trees":
        lines.append(f"2. `02-interdiff.diff`: changes from the code in your last pack to now ({n} file{'' if n == 1 else 's'}).")
    else:
        lines.append(f"2. `02-interdiff.diff`: your last pack's diff and the current diff, compared file by file "
                     f"({n} file{'' if n == 1 else 's'}; {inter.note}, so lines inside are diff lines themselves).")
    if diff_rec is not None and diff_rec is not base:
        lines.append(f"   Code changes are shown since the pack made {_made(diff_rec)}; the later pack has no saved diff.")
    elif diff_rec is None and inter is not None:
        lines.append("   No earlier diff was saved, so this shows the whole change against the pinned commit.")
    k = 3
    if included:
        lines.append(f"{k}. `2-code/`: the {len(included)} changed file{'' if len(included) == 1 else 's'} at their current state.")
        k += 1
    if skipped:
        lines.append(f"   Left out (too big, or binary): {', '.join(f'`{p}`' for p in skipped)}.")
    if last_review:
        lines.append(f"{k}. `1-review/03-your-last-review.md`: your earlier review, since this may be a new conversation.")
    lines += [""] + _findings_lines(findings)
    lines += ["## What to assess", "",
              "- **Your earlier findings:** for each ID, whether the new turns left it unchanged, resolved it, made it "
              "worse, or whether you withdraw it, with file:line evidence.",
              "- **The new code** in `02-interdiff.diff`: what's newly wrong or still missing, and what deserves credit.",
              "- **Claims in Claude's new messages** that the transcript doesn't back up.",
              "- **Steering:** what each new human message asked, and what Claude did because of it that it wouldn't "
              "have done otherwise.",
              "- **Verification limits:** what the new turns ran, ran in isolation, or only claimed.", ""]
    if any(t["ended"] == "open" for t in turns):
        lines += ["The last turn was still running when this pack was made: judge only what's here.", ""]
    lines += ["In the digest, say first whether the material evidence changed since your last review.", "",
              pack.REPLY_FORMAT.rstrip(), "", pack.RULES.rstrip()]
    return "\n".join(lines) + "\n"


def _dir_tree(paths: list[str]) -> str:
    """Repomix's directory listing: folders first, then files, two spaces per level."""
    root: dict = {}
    for p in paths:
        node = root
        for part in p.split("/")[:-1]:
            node = node.setdefault(part + "/", {})
        node[p.split("/")[-1]] = None
    out = []

    def walk(node: dict, depth: int) -> None:
        for name in sorted(k for k in node if node[k] is not None):
            out.append("  " * depth + name)
            walk(node[name], depth + 1)
        for name in sorted(k for k in node if node[k] is None):
            out.append("  " * depth + name)
    walk(root, 0)
    return "\n".join(out)


def repomix_xml(files: list[tuple[str, str]], header: str) -> str:
    """The same shape as repomix's XML output (pack.py's full packs), written directly: files stay in the order
    given, so the read-me comes first."""
    parts = [SUMMARY, f"\n<user_provided_header>\n{header}\n</user_provided_header>\n",
             f"\n<directory_structure>\n{_dir_tree([p for p, _ in files])}\n</directory_structure>\n",
             "\n<files>\nThis section contains the contents of the repository's files.\n"]
    for p, text in files:
        parts.append(f'\n<file path="{p}">\n{text.rstrip(chr(10))}\n</file>\n')
    parts.append("\n</files>\n")
    return "".join(parts)


def _fit_transcript(tpath: Path, since: str | None, cap: int) -> str:
    """The new turns condensed like the full pack, shortening tool output (then the middle) to fit `cap` tokens."""
    for n in (40, 20, 10, 5, 2):
        md, _ = pack.condense(tpath, out_lines=n, since=since, brief_compactions=True)
        if pack._tokens(md) <= cap:
            return md
    keep = cap * 4
    return md[: keep // 2] + "\n\n… [middle of these turns cut to fit] …\n\n" + md[-keep // 2:]


def _fit_interdiff(inter: Interdiff, cap: int) -> tuple[str, int]:
    """(interdiff text, context lines), with the widest context that fits `cap` tokens, then cut in the middle."""
    for ctx in CONTEXT:
        text = inter.text(ctx)
        if pack._tokens(text) <= cap:
            return text, ctx
    half = cap * 4 // 80 // 2   # about 80 characters a line: half the lines from the start, half from the end
    return pack._clip(text, half, half) + "\n", ctx


def _last_review(task_dir: Path, run: str) -> str | None:
    """Austin's latest saved review of this run, when no ChatGPT conversation is on record for it (so a follow-up
    may land in a chat that hasn't seen it)."""
    try:
        from . import reviews
        if reviews.chat_for(task_dir, run):
            return None
        r = reviews.latest(task_dir, run)
        return reviews.body(Path(r["path"])) if r else None
    except Exception:
        return None


def _findings(task_dir: Path, run: str) -> list[dict]:
    """GPT's findings so far in this run (reviews.last_findings), or [] when none can be read."""
    try:
        from . import reviews
        return reviews.last_findings(task_dir, run)
    except Exception:
        return []


def build_followup(task_dir: Path, run: str | None = None, log=print, *, base=None,
                   max_tokens: int = pack.MAX_TOKENS) -> Path:
    """A follow-up pack for the run: what's new since the pack the reviewer last saw (or `base`)."""
    task_dir = Path(task_dir)
    return _followup(task_dir, pack.gather(task_dir, run), log, base, max_tokens)


def _followup(task_dir: Path, g: dict, log, base, max_tokens: int) -> Path:
    run_dir, meta = g["run_dir"], g["meta"]
    run = run_dir.name
    recs = packs(task_dir, run)
    b = _pick_base(task_dir, run, recs, base)
    if b is None:
        raise SystemExit(f"No earlier pack for {run} to follow up on. Make a full one first: "
                         f"johto review pack {task_dir.name} {run} --full")
    with tempfile.TemporaryDirectory(prefix="johto-followup-") as tmp:
        snap = Path(tmp) / "transcript.jsonl"   # one read of a transcript that may still be growing
        shutil.copyfile(g["transcript_path"], snap)
        entries = _entries(snap)
        st = _state(entries, g["diff"])
        if _unchanged(b, st):
            raise SystemExit(f"Nothing new in {run} since {Path(b['path']).name}.")
        since = b.get("last_entry_at")
        turns = _turn_facts(entries, snap, since)
        old_diff, diff_rec = _base_diff(recs, b)
        inter = None
        if _sha(old_diff or "") != _sha(g["diff"]) or (old_diff is None and g["diff"].strip()):
            inter = Interdiff(meta, old_diff or "", g["diff"])
            log(f"Interdiff: {inter.method}" + (f" ({inter.note})" if inter.note else ""))
        changed = inter.files() if inter else []
        last_review = _last_review(task_dir, run)
        found = _findings(task_dir, run)

        draft = _readme(meta, run, b, diff_rec, turns, inter, changed, [p for s, p in changed if s != "D"], [],
                        last_review, found)
        budget = max_tokens - 3_000 - pack._tokens(draft + SUMMARY) - pack._tokens(last_review or "")
        condensed = _fit_transcript(snap, since, int(budget * 0.45))
        left = budget - pack._tokens(condensed)
        inter_text = ""
        if inter:
            cap = int(left * 0.6) if any(s != "D" for s, _ in changed) else left
            body, ctx = _fit_interdiff(inter, cap)
            where = ("The full files at that state are in that pack's 2-code/." if inter.method == "trees" else
                     "Each line inside is a line of the run's diff: compare the two versions.")
            inter_text = (f"# Changes since your last review: the code in the pack made {_made(diff_rec or b)} -> "
                          f"the run now.\n# Context is {ctx} lines. {where}\n#\n"
                          + "\n".join(f"# {line}" for line in inter.stat().splitlines()) + "\n\n" + body)
        left -= pack._tokens(inter_text)
        files, included, skipped = [], [], []
        for s, p in changed:
            if s == "D":
                continue
            text = inter.content(p)
            if text is not None and pack._tokens(text) <= left:
                files.append((f"2-code/{p}", text))
                included.append(p)
                left -= pack._tokens(text)
            elif inter.method == "trees":
                skipped.append(p)
        readme = _readme(meta, run, b, diff_rec, turns, inter, changed, included, skipped, last_review, found)

    t_head = "# Lane transcript since your last review\n\n"
    review_files = [("1-review/00-read-me.md", readme),
                    ("1-review/01-transcript.md", t_head + (condensed.strip() or "(no new turns)") + "\n"),
                    ("1-review/02-interdiff.diff", inter_text or "(no code changes since your last pack)\n")]
    if last_review:
        review_files.append(("1-review/03-your-last-review.md", last_review))
    repo = meta["repo"]
    header = (f"Johto review pack, follow-up · {meta['task_name']} · {run} · {repo['full_name']}@{repo['commit'][:8]} · "
              f"made {time.strftime('%Y-%m-%d %H:%M %Z')}. Start with 1-review/00-read-me.md.")
    xml = repomix_xml(review_files + files, header)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = run_dir / f"review-followup-{stamp}.xml"
    n = 2
    while out.exists():
        out = run_dir / f"review-followup-{stamp}-{n}.xml"
        n += 1
    out.write_text(xml)
    tokens = pack._tokens(xml)
    record_pack(task_dir, run, out, "followup", st["turn"], diff=g["diff"], tokens=tokens,
                transcript_entries=st["transcript_entries"], last_entry_at=st["last_entry_at"],
                base=_rel(run_dir, b["path"]), interdiff=inter.method if inter else None)
    log(f"Follow-up: {out} (about {tokens:,} tokens; {len(turns)} turn{'' if len(turns) == 1 else 's'}, "
        f"{len(changed)} changed file{'' if len(changed) == 1 else 's'}, {len(included)} included)")
    if tokens > max_tokens:
        log(f"warning: about {tokens:,} tokens is over the {max_tokens:,} budget")
    return out


@contextlib.contextmanager
def _building(run_dir: Path):
    """One next_pack per run at a time (the web lane watcher, the turn watcher, `johto review send`): a second
    caller at the same checkpoint waits, then finds the first caller's pack unchanged and uses it."""
    with open(run_dir / ".next-pack.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def next_pack(task_dir: Path, run: str | None = None, log=print, *, full: bool = False, ai: bool | None = None) -> Path:
    """The pack to send next: the newest pack when nothing changed since it, a full pack when the run has none
    (or `full`), else a follow-up. `ai` is the full pack's outside Claude review (default: settings
    "pack_outside_review", off unless set, and never in fake mode without a stub claude)."""
    task_dir = Path(task_dir)
    with _building(pack._pick_run(task_dir, run)):
        return _next_pack(task_dir, run, log, full, ai)


def _next_pack(task_dir: Path, run: str | None, log, full: bool, ai: bool | None) -> Path:
    g = pack.gather(task_dir, run)
    run = g["run_dir"].name
    recs = packs(task_dir, run)
    st = state(g["transcript_path"], g["diff"])
    pick = [r for r in recs if Path(r["path"]).exists() and (r["kind"] == "full" or not full)]
    if pick and _unchanged(pick[-1], st):
        log(f"Nothing changed since {Path(pick[-1]['path']).name}; using it.")
        return Path(pick[-1]["path"])
    if full or not recs:
        if ai is None:
            ai = bool(settings.get("pack_outside_review")) and (not config.FAKE or config.CLAUDE_BIN_SET)
        return pack.build(task_dir, run, ai=ai, log=log)
    return _followup(task_dir, g, log, None, pack.MAX_TOKENS)


# --- Checkpoints: what's packed, what GPT saw -------------------------------------------------------

def checkpoint_packed(task_dir: Path, run: str) -> dict | None:
    """The newest pack (its file still there) that shows the run's transcript as it is now: the same number of
    entries and the same last entry. The diff isn't compared, since prework's final.diff is written differently
    from the live `git diff` a pack made mid-run reads. None when no pack reaches this far."""
    run_dir = _run_dir(task_dir, run)
    tpath = _run_transcript(run_dir) if run_dir.is_dir() else None
    if not tpath:
        return None
    st = state(tpath, "")
    for r in reversed(packs(task_dir, run)):
        if (r.get("last_entry_at") == st["last_entry_at"] and r.get("transcript_entries") == st["transcript_entries"]
                and Path(r["path"]).exists()):
            return r
    return None


def reviewed_checkpoint(task_dir: Path, run: str) -> dict | None:
    """The newest pack GPT saw: sent to ChatGPT, or answered by a saved review. None before the first."""
    seen = _reviewed(task_dir, run)
    return next((r for r in reversed(packs(task_dir, run))
                 if r.get("sent_at") or Path(r["path"]).name in seen), None)


def _diff_key(diff: str) -> str:
    """A diff's fingerprint by content: file headers and the added and removed lines only, so the live
    `git diff --binary` and prework's final.diff of the same change match (index lines and binary bodies
    don't count)."""
    keep = [ln for ln in (diff or "").splitlines()
            if ln.startswith("diff --git ") or (ln[:1] in "+-" and ln[:4] not in ("+++ ", "--- "))]
    return hashlib.sha256("\n".join(keep).encode()).hexdigest()[:16]


def current_diff(task_dir: Path, run: str) -> str:
    """Claude's diff now: from the running lane (read-only `git diff`), else the collected final.diff."""
    run_dir = _run_dir(task_dir, run)
    live = pack._live_diff(run_dir) if not config.FAKE else None   # fake mode never reaches a lane
    if live is not None:
        return live
    f = run_dir / "prework_output" / "final.diff"
    return f.read_text(errors="replace") if f.exists() else ""


def diff_changed(task_dir: Path, run: str, diff: str | None = None) -> bool | None:
    """Whether Claude's diff (`diff`, default current_diff) differs from the one in the pack GPT last saw.
    None when GPT hasn't seen a pack of this run yet."""
    rec = reviewed_checkpoint(task_dir, run)
    if rec is None:
        return None
    diff = current_diff(task_dir, run) if diff is None else diff
    p = rec.get("diff_path")
    if p and Path(p).exists():
        return _diff_key(Path(p).read_text(errors="replace")) != _diff_key(diff)
    return rec.get("diff_sha") != _sha(diff)
