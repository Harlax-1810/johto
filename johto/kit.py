"""The writing kit: one page of facts beside Austin's writing sitting, as a view of the grading table plus run facts.

tasks/<t>/kit.md has, in order: the task boundary (his saved opening prompt from the Studio snapshot, the slot,
the pinned commit), a repository map (files and symbols from the dossier, brief.md and the rows, cited), the
rows he marked (by G, decision point and N; none until he marks some), run evidence (Claude's minutes and turns, his turns, edits, the
diff stat, detected test commands), verification limits, upstream links, open decisions, and the rules and
reviewer lessons for each field.

Facts only: no outline, no "this field must say", nothing to paraphrase. The same inputs give the same page.
`lanes.write_kit` calls write() after every collect; `johto kit <task>` writes and prints it. The overlap
guard treats kit.md as AI text.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import grading, rules, stages, transcript, workspace

FILE = "kit.md"
OPEN_MAX = 8      # open decisions listed before "N more"
MAP_MAX = 14      # repository map entries
SLOT_KEYS = ("action", "domain", "codebase_type", "prompt_style", "interaction_mode")
RULE_FIELDS = (("goal", "High-level goal"), ("decision_points", "Decision points"),
               ("codebase", "Difficulty & codebase"), ("validation_notes", "Validation notes"))


def _norm(s: str | None) -> str:
    return " ".join((s or "").split())


def _clock(iso: str | None) -> str:
    """Local time, so the Studio snapshot (Pacific) and transcript turns (UTC) read on one clock."""
    t = transcript._ts(iso)
    return t.astimezone().strftime("%Y-%m-%d %H:%M") if t else (iso or "?")


def _snapshot(d: Path) -> dict:
    try:
        snap = json.loads((d / "app" / "studio-fields.json").read_text())
        return snap if isinstance(snap, dict) else {}
    except (OSError, ValueError):
        return {}


def _local(d: Path) -> dict:
    try:
        return workspace.parse_submission(d)["sections"]
    except (OSError, ValueError):
        return {}


def _quote(text: str) -> list[str]:
    return ["> " + ln if ln.strip() else ">" for ln in text.strip().splitlines()]


def _row_line(r: dict, n: int = 140) -> str:
    res = r.get("result") or {}
    turn = f" turn {res['turn']}" if res.get("turn") else ""
    refs = ", ".join(f"`{x}`" for x in (r.get("refs") or [])[:3])
    return (f"- {r['id']} · {grading.cut(r.get('finding'), n)} · result {res.get('value') or 'unknown'}"
            f" ({res.get('run') or '?'}{turn}) · known: {grading.known_label(r)}" + (f" · {refs}" if refs else ""))


# --- Sections ------------------------------------------------------------------------------------------

def _boundary(d: Path, meta: dict, run: str | None, ev: dict) -> list[str]:
    snap, local = _snapshot(d), _local(d)
    saved = ((snap.get("fields") or {}).get("field_opening_prompt") or "").strip()
    mine = (local.get("Opening Prompt") or "").strip()
    out = ["## Task boundary", ""]
    if saved:
        out += [f"Opening prompt, as saved in Studio (snapshot {_clock(snap.get('at'))}):", ""] + _quote(saved)
        if mine and _norm(mine) != _norm(saved):
            out += ["", "The local copy in submission.md differs from Studio's."]
    elif mine:
        out += ["Opening prompt, from submission.md (no Studio snapshot holds one):", ""] + _quote(mine)
    else:
        out += ["No opening prompt saved yet."]
    if ev.get("first_prompt") and (saved or mine):
        same = _norm(ev["first_prompt"]) == _norm(saved or mine)
        out += ["", f"{run}'s first prompt {'matches' if same else 'differs from'} this text."]
    slot, repo = meta.get("slot") or {}, meta.get("repo") or {}
    out += ["", "- Slot: " + " · ".join(str(slot.get(k) or "?") for k in SLOT_KEYS),
            f"- Pinned commit: {repo.get('full_name') or '?'} @ {repo.get('commit') or '?'}"]
    if run:
        rec = grading._run_rec(meta, run)
        where = rec.get("host") or "this Mac"
        state = f"collected {_clock(rec['collected_at'])}" if rec.get("collected_at") else "not collected"
        chosen = " (chosen for the submission)" if meta.get("submission_run") == run else ""
        out += [f"- Run: {run}{chosen} · {where} · {state}"]
    return out


_PTR = re.compile(r"`?([\w./-]+\.[A-Za-z][A-Za-z0-9]{0,6})(?::(\d+(?:-\d+)?))?`?")


def _brief_lead(d: Path) -> list[tuple[str, str]]:
    """(pointer, fact) pairs from brief.md's lead: bullets that name a file."""
    try:
        text = (d / "brief.md").read_text()
    except OSError:
        return []
    m = re.search(r"^## Lead\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        s = line.strip()
        if not s.startswith("- "):
            continue
        p = _PTR.search(s)
        if p and "/" in p.group(1) or (p and p.group(2)):
            ptr = p.group(1) + (f":{p.group(2)}" if p.group(2) else "")
            out.append((ptr, grading.cut(s[2:], 160)))
    return out


def _repo_map(d: Path, data: dict, rows: list[dict], ev: dict, opp: tuple | None) -> list[str]:
    out = []
    if opp:
        doc, o = opp
        for e in o.get("evidence") or []:
            out.append(f"- `{e.get('pointer')}`: {e.get('fact')} (dossier {doc.get('id')} {o.get('id')})")
    for ptr, fact in _brief_lead(d):
        out.append(f"- `{ptr}`: {fact} (brief.md, lead)")
    cited: dict[str, list[str]] = {}
    for r in rows:
        for ref in r.get("refs") or []:
            path = ref.split(":")[0]
            if "/" in path or "." in path.rsplit("/", 1)[-1]:
                if not ref.startswith("turn") and r["id"] not in cited.setdefault(path, []):
                    cited[path].append(r["id"])
    for path, ids in sorted(cited.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:8]:
        out.append(f"- `{path}`: cited by {', '.join(ids[:6])}" + (f" and {len(ids) - 6} more" if len(ids) > 6 else ""))
    edited = [f for f in ev.get("files_edited") or [] if not any(f in x for x in out)]
    if edited:
        out.append(f"- Edited by {ev['run']}: " + ", ".join(f"`{f}`" for f in edited[:8])
                   + (f" and {len(edited) - 8} more" if len(edited) > 8 else ""))
    if not out:
        return []
    return ["## Repository map", ""] + out[:MAP_MAX] + (
        [f"- {len(out) - MAP_MAX} more in brief.md and `johto grading {d.name}`"] if len(out) > MAP_MAX else [])


def _picked(data: dict, run: str | None) -> tuple[list[dict], bool]:
    """(rows Austin selected, whether there are any). Nothing stands in for his marks: an unvetted dossier
    hypothesis or a review finding is not selected evidence until he picks it."""
    rows = [r for r in data.get("rows") or [] if (r.get("marks") or {}).get("selected")]
    return (grading.ordered(rows), True) if rows else ([], False)


def _selected(data: dict, picked: list[dict], marked: bool, short: str = "TASK") -> list[str]:
    out = ["## Selected rows", ""]
    if not marked:
        if not data.get("rows"):
            return out + ["No rows in the grading table yet."]
        return out + [f"None marked yet: `johto grading {short}`."]
    # Grouped by Austin's mark letter; the rows are evidence he picked, not a field's contents.
    groups = [("Marked G", [r for r in picked if r["marks"].get("goal")])]
    groups += [(f"Marked {n}", [r for r in picked if r["marks"].get("dp") == n]) for n in (1, 2, 3)]
    groups += [("Marked N", [r for r in picked if r["marks"].get("notes")])]
    groups += [("Selected, no mark yet", [r for r in picked if not grading.marks_label(r).replace("sel", "")])]
    for title, rows in groups:
        if rows:
            out += [f"### {title}", ""] + [_row_line(r) for r in rows] + [""]
    stale = [r for r in picked if r.get("changed_since_mark")]
    if stale:
        out += ["Changed since you marked them: " + ", ".join(r["id"] for r in stale)]
    other = [r for r in picked if r.get("other_run")]
    if other:
        out += ["From another run than " + str(data.get("run")) + ": " + ", ".join(r["id"] for r in other)]
    return out[:-1] if out[-1] == "" else out


def _test_result(t: dict) -> str:
    """What is known about one test run: the output's own failure lines win over a pipe's exit 0."""
    if t.get("exit") is None:
        return "no result seen"
    if t["exit"] != 0:
        return f"exit {t['exit']}" + (", piped" if t.get("piped") else "")
    if t.get("output_failed"):
        return f"failed (from output: {t['output_failed']})"
    if t.get("piped"):
        return "result hidden by a pipe (exit 0 is the pipe's last command)"
    return "exit 0"


def _test_line(t: dict) -> str:
    return f"  - turn {t.get('turn') or '?'} · `{grading.cut(t.get('short') or t['cmd'], 80)}` · {_test_result(t)}"


def _evidence(d: Path, ev: dict) -> list[str]:
    if not ev.get("run"):
        return []
    out = ["## Run evidence", ""]
    if not ev.get("transcript"):
        return out + [f"{ev['run']} has no collected transcript yet."]
    turns = ev.get("turns") or []
    bg = ev.get("background_minutes") or 0
    out += [f"- Claude worked {ev['working_minutes']:.1f} min"
            + (f" (+{bg:.1f} min waiting on background jobs)" if bg >= 0.5 else "")
            + f" over {len(turns)} turn{'s' if len(turns) != 1 else ''} ({ev['run']}, transcript `{ev['transcript']}`)"]
    if turns:
        out += ["- Your turns:"]
        for t in turns:
            first = next((ln.strip() for ln in (t.get("prompt") or "").splitlines() if ln.strip()), "")
            out += [f"  - {t['index']}. {_clock(t.get('start'))} · {t['working_minutes']:.1f} min · "
                    f"{grading.cut(first, 110)}"]
    files = ev.get("files_edited") or []
    out += ["- Files edited: " + (", ".join(f"`{f}`" for f in files[:10]) + (f" and {len(files) - 10} more" if len(files) > 10 else "")
                                  if files else "none")]
    ds = ev.get("diffstat") or []
    if ev.get("diff_path"):
        add, rem = sum(x["add"] for x in ds), sum(x["del"] for x in ds)
        new = sum(1 for x in ds if x.get("new"))
        out += [f"- Diff (`{ev['diff_path']}`): {len(ds)} file{'s' if len(ds) != 1 else ''}, +{add} -{rem}"
                + (f", {new} new" if new else "")]
    tests = ev.get("tests") or []
    if tests:
        zero = [t for t in tests if t.get("exit") == 0]
        ok = sum(1 for t in zero if not t.get("piped") and not t.get("output_failed"))
        failed = sum(1 for t in zero if t.get("output_failed"))
        hidden = sum(1 for t in zero if t.get("piped") and not t.get("output_failed"))
        bits = [f"{ok} with exit 0"] + ([f"{failed} failed in their output"] if failed else []) + (
            [f"{hidden} hidden by a pipe"] if hidden else [])
        out += [f"- Test runs detected in Claude's commands: {len(tests)} ({', '.join(bits)})"]
        out += [_test_line(t) for t in tests[:10]]
        if len(tests) > 10:
            out += [f"  - {len(tests) - 10} more"]
    else:
        out += ["- Test runs detected in Claude's commands: none"]
    claims = grading.claim_problems(ev)
    for c in claims:
        out += [f"- {c['text']}"]
    return out


def _limits(picked: list[dict]) -> list[str]:
    rows = [r for r in picked if r.get("limits") or (r.get("verify") or {}).get("status") not in (None, "none")
            or r.get("basis") in ("claimed", "none")]
    if not rows:
        return []
    out = ["## Verification limits", ""]
    for r in rows:
        v = r.get("verify") or {}
        line = f"- {r['id']} · {r.get('basis') or 'none'}"
        if r.get("limits"):
            line += f" · {_norm(r['limits'])}"   # never shortened: the qualification is the point
        if v.get("status") not in (None, "none"):
            line += f" · verify {v['status']} {_clock(v.get('at'))} (`{v.get('path')}`)"
        out.append(line)
    return out


def _brief_upstream(d: Path) -> list[str]:
    try:
        text = (d / "brief.md").read_text()
    except OSError:
        return []
    m = re.search(r"^## Upstream[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return [ln.strip() for ln in (m.group(1).splitlines() if m else []) if ln.strip().startswith("- ")]


def _upstream(d: Path, meta: dict, opp: tuple | None) -> list[str]:
    out = []
    if opp:
        doc, o = opp
        up = o.get("upstream") or {}
        when = (doc.get("generated_at") or "")[:10] or "?"
        for x in up.get("related") or []:
            out.append(f"- [{x.get('state')}] {x.get('title')} {x.get('url')} (dossier, as of {when})")
        if up.get("looks_solved"):
            out.append(f"- The dossier recorded this opportunity as looking solved upstream (as of {when}).")
    brief = _brief_upstream(d)
    if brief:
        when = (meta.get("created_at") or "")[:10] or "?"
        out += [f"{ln} (brief.md, {when})" for ln in brief]
    return ["## Upstream context", ""] + out if out else []


def _open(d: Path, data: dict) -> list[str]:
    rows = grading.open_decisions(data)
    if not rows:
        return []
    out = ["## Open decisions", ""]
    for r in rows[:OPEN_MAX]:
        why = []
        if r.get("scope") is None:
            why.append("no scope set")
        if r.get("severity") == "high":
            why.append("high severity")
        if r.get("review_status") in ("new", "worsened"):
            why.append(f"{r['review_status']} in the latest review")
        out.append(f"- {r['id']} · {', '.join(why)} · {grading.cut(r.get('finding'), 130)}")
    if len(rows) > OPEN_MAX:
        out.append(f"- {len(rows) - OPEN_MAX} more: `johto grading {d.name}`")
    return out


def _rules() -> list[str]:
    try:
        every = rules.load()
    except Exception:   # a broken rules.md must not stop the kit
        return []
    out, shared = ["## Rules and lessons", ""], {}
    for key, title in RULE_FIELDS:
        rs = rules.for_field(key, rules=every)
        own = [r for r in rs if key in r["fields"] and len(r["fields"]) == 1]
        for r in rs:
            if r not in own:
                shared.setdefault(r["id"], r)
        if own:
            out += [f"### {title}", ""] + [f"- {r['id']}: {_norm(r['text'])}" for r in own] + [""]
    if shared:
        out += ["### More than one field", ""] + [f"- {r['id']}: {_norm(r['text'])}" for r in shared.values()] + [""]
    try:
        lessons = rules.lessons()
    except Exception:
        lessons = []
    if lessons:
        out += ["### What reviewers said", ""]
        out += [f"- {x.get('task')} ({x.get('date') or '?'}, {x.get('field') or 'general'}): {_norm(x['text'])}"
                for x in lessons]
    return out[:-1] if out[-1] == "" else out


# --- Page ----------------------------------------------------------------------------------------------

def render(task_dir: Path, run: str | None = None) -> str:
    """The kit as markdown, from the saved record only (no refresh, nothing written)."""
    d = Path(task_dir)
    meta = workspace.load(d)
    data = grading.load(d)
    run = run or data.get("run") or grading.selected_run(d, meta)
    if run != data.get("run"):
        data = {**data, "rows": [{**r, "other_run": bool(r.get("result", {}).get("run") and r["result"]["run"] != run)}
                                 for r in data.get("rows") or []], "run": run}
    ev = grading.run_evidence(d, run)
    opp = grading.dossier_opportunity(d, meta)
    picked, marked = _picked(data, run)
    rows_for_map = picked if marked else [r for r in data.get("rows") or [] if grading.active(r)]
    snap = _snapshot(d)
    made = [f"task.json, the Studio snapshot ({_clock(snap.get('at'))})" if snap else "task.json"]
    if data.get("updated_at"):
        made.append(f"grading.json ({_clock(data['updated_at'])})")
    if ev.get("transcript"):
        made.append(f"{run}'s transcript")
    head = [f"# Writing kit · {meta.get('task_name') or d.name}" + (f" · {run}" if run else ""), "",
            "From " + ", ".join(made) + "."]
    parts = [head, _boundary(d, meta, run, ev), _repo_map(d, data, rows_for_map, ev, opp),
             _selected(data, picked, marked, d.name), _evidence(d, ev), _limits(picked), _upstream(d, meta, opp),
             _open(d, data), _rules()]
    return "\n\n".join("\n".join(p) for p in parts if p).rstrip() + "\n"


def write(task_dir: Path, run: str | None = None) -> Path:
    """Refresh the grading table (local sources only, no Claude call), then save tasks/<t>/kit.md."""
    d = Path(task_dir)
    grading.refresh(d, log=lambda *a: None)
    text = render(d, run)
    p = d / FILE
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, p)
    stages.log(d, "kit_written", run=run or grading.load(d).get("run"), path=str(p))
    return p


# --- CLI -----------------------------------------------------------------------------------------------

def cmd_kit(a) -> None:
    try:
        d = workspace.resolve(a.task)
    except FileNotFoundError as e:
        raise SystemExit(str(e)) from None
    if a.run and not (d / "runs" / a.run).is_dir():
        raise SystemExit(f"No run {a.run} in {d}")
    if a.no_write:
        print(render(d, a.run), end="")
        return
    p = write(d, a.run)
    print(p if a.quiet else p.read_text(), end="\n" if a.quiet else "")


def register(sub) -> None:
    s = sub.add_parser("kit", help="the writing kit: facts from the grading table and the run, beside your writing")
    s.add_argument("task")
    s.add_argument("run", nargs="?", help="e.g. validate-1 (default: the submission run)")
    s.add_argument("--quiet", action="store_true", help="print only the kit's path")
    s.add_argument("--no-write", action="store_true", help="print the kit without saving kit.md or refreshing")
    s.set_defaults(func=cmd_kit)
