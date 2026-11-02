"""Austin's focused minutes per task: the sessions he starts and stops, else an estimate from the signs he leaves.

`johto focus <t> start` and `stop` log focus_start / focus_stop events, and those spans count as they are. Outside
them the minutes are estimated from signs that he was working on the task at that moment:

- stage events he causes: `johto where` and the helper-chat skill (touch), claiming, starting a lane, saving to
  Studio, submitting, marking the grading table, running the draft check;
- the messages he typed in his helper chats (Claude Code sessions in this checkout, main thread only). A
  message belongs to a task when its text, or the tool calls that answered it, name the task's short id; one
  that names none (or several, evenly) takes the task of the nearest message within 30 minutes that does. A
  pasted review is one message, so it is one sign;
- the prompts he typed in the task's lanes;
- his edits in Studio: johto's pulls of them (app/authorship.jsonl, studio_pulled) and the snapshot history
  (app/studio-history.jsonl);
- the web editor's saves, which carry how long the field had focus.

Signs closer than GAP_MIN apart form one sitting, counted as its span plus PAD_MIN. The estimate misses
reading and thinking between signs and typing in Studio before a pull, so it is a lower bound; summary()
says which method it used. Reads only, apart from the chat cache in data/cache/focus-chats.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, stages, transcript, workspace

GAP_MIN = 10          # signs further apart than this start a new sitting
PAD_MIN = 2           # a sitting counts its span plus this
CARRY_MIN = 30        # a chat message naming no task takes the task of a message this close that does
OPEN_CAP_MIN = 180    # an explicit session left open counts at most this long
SHARE = 0.6           # a message belongs to the task with at least this share of its mentions
CACHE = "focus-chats.json"
CACHE_VERSION = 1

# Stage events that mean Austin was at the task right then (automatic ones, like turn_ended, don't).
HIS_EVENTS = {"touch", "claimed", "lane_started", "studio_pushed", "submitted", "grading_marked", "draft_checked"}
STAGE_KEYS = ("prompt", "run", "write", "submitted")
STAGE_WORDS = {"prompt": "before the first run", "run": "while a run was under way",
               "write": "writing after a run", "submitted": "after submitting"}

_TOOL_RESULT = re.compile(r'"type":\s*"tool_result"')
_SIDECHAIN = re.compile(r'"isSidechain":\s*true')


# --- time helpers ------------------------------------------------------------------------------------------

def _dt(v) -> datetime | None:
    """An aware datetime from the ISO forms the files hold (Z, +00:00, -0700), else None."""
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00")
    if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":" and s[-4:].isdigit():
        s = s[:-2] + ":" + s[-2:]
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


def _iso(t: datetime | None) -> str | None:
    return t.astimezone().isoformat(timespec="seconds") if t else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- explicit sessions -------------------------------------------------------------------------------------

def open_session(task_dir: Path) -> dict | None:
    """The focus_start event with no focus_stop after it, or None."""
    evs = stages.read(task_dir, ("focus_start", "focus_stop"))
    return evs[-1] if evs and evs[-1].get("kind") == "focus_start" else None


def start(task_dir: Path) -> dict:
    """Start an explicit session. One already open is returned as it is (it keeps its start)."""
    cur = open_session(task_dir)
    return cur if cur else stages.log(task_dir, "focus_start")


def stop(task_dir: Path, now: datetime | None = None) -> dict | None:
    """Stop the open session; None when none is open. The event records its minutes."""
    cur = open_session(task_dir)
    if not cur:
        return None
    begin = _dt(cur.get("at"))
    now = now or _now()
    mins = round(max(0.0, (now - begin).total_seconds() / 60), 1) if begin else None
    return stages.log(task_dir, "focus_stop", minutes=mins)


def explicit_spans(task_dir: Path, now: datetime | None = None) -> list[tuple[datetime, datetime]]:
    """(start, end) of each explicit session. An open one runs until now, at most OPEN_CAP_MIN."""
    now = now or _now()
    out, begin = [], None
    for ev in stages.read(task_dir, ("focus_start", "focus_stop")):
        t = _dt(ev.get("at"))
        if t is None:
            continue
        if ev["kind"] == "focus_start":
            begin = begin or t
        elif begin is not None:
            if t > begin:
                out.append((begin, t))
            begin = None
    if begin is not None:
        out.append((begin, min(now, begin + timedelta(minutes=OPEN_CAP_MIN))))
    return out


# --- signs from the task folder ----------------------------------------------------------------------------

def _jsonl(p: Path) -> list[dict]:
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def _sign(t: datetime | None, source: str, end: datetime | None = None) -> dict | None:
    return {"at": t, "end": end or t, "source": source} if t else None


def folder_signs(task_dir: Path) -> list[dict]:
    """Signs from the task folder: his stage events, Studio pulls and snapshots, editor saves, lane prompts."""
    d = Path(task_dir)
    out = []
    for ev in stages.read(d):
        if ev.get("kind") in HIS_EVENTS and ev.get("source") != "web-poll":
            out.append(_sign(_dt(ev.get("at")), "event"))
        elif ev.get("kind") == "studio_pulled":
            out.append(_sign(_dt(ev.get("at")), "studio"))
    for e in _jsonl(d / "app" / "authorship.jsonl"):
        t = _dt(e.get("at"))
        if e.get("event") == "pulled_from_studio":
            out.append(_sign(t, "studio"))
        elif e.get("event") == "save":
            ms = e.get("focus_ms") or 0
            begin = t - timedelta(milliseconds=float(ms)) if t and ms else t
            out.append(_sign(begin, "editor", end=t))
        elif e.get("event"):
            out.append(_sign(t, "editor"))
    for e in _jsonl(d / "app" / "studio-history.jsonl"):
        if not e.get("first"):
            out.append(_sign(_dt(e.get("at")), "studio"))
    out += lane_signs(d)
    return [s for s in out if s]


def _run_transcripts(run_dir: Path) -> list[Path]:
    found = list(transcript.find(run_dir))
    live = run_dir / ".live-transcripts"
    if live.is_dir():
        found += sorted(live.rglob("*.jsonl"))
    return found


def lane_signs(task_dir: Path) -> list[dict]:
    """The prompts Austin typed in the task's lanes (collected transcripts and live copies)."""
    out, seen = [], set()
    for run_dir in sorted((Path(task_dir) / "runs").glob("*-*")):
        for p in _run_transcripts(run_dir):
            for e in _jsonl(p):
                text = transcript._human_text(e)
                t = _dt(e.get("timestamp")) if text is not None else None
                if t is None:
                    continue
                key = (e.get("timestamp"), _sha(text))
                if key not in seen:
                    seen.add(key)
                    out.append(_sign(t, "lane"))
    return out


# --- signs from the helper chats ---------------------------------------------------------------------------

def chat_dirs() -> list[Path]:
    """Folders of helper-chat transcripts: JOHTO_CHAT_DIRS (colon-separated; empty turns chats off), else
    Claude Code's project folder for this checkout."""
    env = os.environ.get("JOHTO_CHAT_DIRS")
    if env is not None:
        return [Path(p).expanduser() for p in env.split(":") if p.strip()]
    name = "-" + str(config.CHECKOUT).strip("/").replace("/", "-").replace(".", "-")
    return [Path.home() / ".claude" / "projects" / name]


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def known_shorts() -> list[str]:
    return sorted(p.parent.name for p in config.TASKS.glob("*/task.json"))


def _count(rx: re.Pattern, text: str, into: dict) -> None:
    for m in set(rx.findall(text or "")):
        into[m] = into.get(m, 0) + 1


def _parse_lines(lines: list[str], rx: re.Pattern, msgs: list[dict]) -> None:
    """Add the messages Austin typed in these transcript lines to `msgs`, and count the task ids in what
    answered each one (tool calls and Claude's text) onto the latest message."""
    for line in lines:
        if not line or _SIDECHAIN.search(line[:400]):
            continue
        # Cheap tests first: most lines are tool results or replies that name no task.
        if '"type":"assistant"' in line or '"type": "assistant"' in line:
            if not msgs or not rx.search(line):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") != "assistant":
                continue
            for c in (e.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    _count(rx, json.dumps(c.get("input"), ensure_ascii=False), msgs[-1]["tools"])
                elif c.get("type") == "text":
                    _count(rx, c.get("text") or "", msgs[-1]["said"])
        elif ('"type":"user"' in line or '"type": "user"' in line) and not _TOOL_RESULT.search(line):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            text = transcript._human_text(e)
            if text is None or not e.get("timestamp"):
                continue
            ids: dict = {}
            _count(rx, text, ids)
            msgs.append({"ts": e["timestamp"], "sha": _sha(text), "text": ids, "tools": {}, "said": {}})


def _load_cache() -> dict:
    p = config.DATA / "cache" / CACHE
    try:
        v = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) and v.get("version") == CACHE_VERSION else {}


def _save_cache(cache: dict) -> None:
    p = config.DATA / "cache" / CACHE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cache))
        os.replace(tmp, p)
    except OSError:
        pass


def _chat_file(p: Path, entry: dict | None, rx: re.Pattern, key: str) -> dict:
    """One chat's messages, parsed again only from where the last parse stopped (chats only grow)."""
    st = p.stat()
    if entry and entry.get("key") == key and entry.get("offset", 0) <= st.st_size and entry.get("mtime") == st.st_mtime_ns:
        return entry
    if not entry or entry.get("key") != key or entry.get("offset", 0) > st.st_size:
        entry = {"key": key, "offset": 0, "msgs": []}
    with open(p, "rb") as fh:
        fh.seek(entry["offset"])
        data = fh.read()
    end = data.rfind(b"\n") + 1     # a line still being written waits for the next read
    lines = data[:end].decode("utf-8", "replace").splitlines()
    msgs = entry["msgs"]
    _parse_lines(lines, rx, msgs)
    return {"key": key, "offset": entry["offset"] + end, "mtime": st.st_mtime_ns, "msgs": msgs}


def chat_messages(shorts: list[str] | None = None) -> dict[str, list[dict]]:
    """{chat file: [{ts, sha, text, tools, said}]} for every top-level helper chat (subagents are skipped)."""
    shorts = known_shorts() if shorts is None else shorts
    dirs = chat_dirs()
    if not shorts or not dirs:
        return {}
    rx = re.compile(r"\b(" + "|".join(re.escape(s) for s in shorts) + r")\b")
    key = _sha(",".join(sorted(shorts)))
    cache = _load_cache()
    files = cache.get("files") or {}
    out, changed = {}, False
    for folder in dirs:
        for p in sorted(folder.glob("*.jsonl")):
            try:
                ent = _chat_file(p, files.get(str(p)), rx, key)
            except OSError:
                continue
            if ent is not files.get(str(p)):
                files[str(p)], changed = ent, True
            out[str(p)] = ent["msgs"]
    if changed:
        _save_cache({"version": CACHE_VERSION, "files": files})
    return out


def _owner(m: dict) -> str | None:
    """The task a message is about: its text counts three times, tool calls once, Claude's words half."""
    w: dict[str, float] = {}
    for part, k in (("text", 3.0), ("tools", 1.0), ("said", 0.5)):
        for s, n in (m.get(part) or {}).items():
            w[s] = w.get(s, 0.0) + k * n
    total = sum(w.values())
    if not total:
        return None
    best = max(w, key=w.get)
    return best if w[best] / total >= SHARE else None


def attribute(msgs: list[dict]) -> list[tuple[datetime, str, str | None]]:
    """(time, text sha, task) for one chat's messages, after carrying owners to nearby messages."""
    rows = [(_dt(m.get("ts")), m.get("sha"), _owner(m)) for m in msgs]
    rows = [r for r in rows if r[0] is not None]
    owned = [(t, s) for t, _, s in rows if s]
    out = []
    for t, sha, s in rows:
        if s is None and owned:
            near = min(owned, key=lambda o: (abs((o[0] - t).total_seconds()), o[0] > t))
            if abs((near[0] - t).total_seconds()) <= CARRY_MIN * 60:
                s = near[1]
        out.append((t, sha, s))
    return out


def chat_signs(short: str, chats: dict[str, list[dict]] | None = None) -> list[dict]:
    """Signs for one task from every helper chat. A message copied into a forked chat counts once."""
    chats = chat_messages() if chats is None else chats
    best: dict[tuple, str | None] = {}
    for msgs in chats.values():
        for t, sha, s in attribute(msgs):
            k = (t, sha)
            if best.get(k) is None:
                best[k] = s
    return [_sign(t, "chat") for (t, _), s in best.items() if s == short]


# --- sittings ----------------------------------------------------------------------------------------------

def _union(spans: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[list[datetime]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _inside(t: datetime, spans: list[tuple[datetime, datetime]]) -> bool:
    return any(a <= t <= b for a, b in spans)


def cluster(signs: list[dict], explicit: list[tuple[datetime, datetime]] | None = None) -> list[dict]:
    """Sittings from signs outside the explicit spans: [{start, end, signs, sources}], end padded by PAD_MIN."""
    explicit = explicit or []
    pts = sorted((s for s in signs if s and not _inside(s["at"], explicit)), key=lambda s: s["at"])
    out: list[dict] = []
    gap = timedelta(minutes=GAP_MIN)
    for s in pts:
        if out and s["at"] - out[-1]["last"] <= gap:
            cur = out[-1]
            cur["last"] = max(cur["last"], s["end"])
            cur["signs"] += 1
            cur["sources"][s["source"]] = cur["sources"].get(s["source"], 0) + 1
        else:
            out.append({"start": s["at"], "last": s["end"], "signs": 1, "sources": {s["source"]: 1}})
    for c in out:
        c["end"] = c.pop("last") + timedelta(minutes=PAD_MIN)
    return out


def _cut(span: tuple[datetime, datetime], explicit: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """The parts of an estimated span outside every explicit span."""
    parts = [span]
    for a, b in explicit:
        nxt = []
        for x, y in parts:
            if y <= a or x >= b:
                nxt.append((x, y))
                continue
            if x < a:
                nxt.append((x, a))
            if y > b:
                nxt.append((b, y))
        parts = nxt
    return parts


# --- stages at the time ------------------------------------------------------------------------------------

def stage_marks(task_dir: Path, meta: dict | None = None) -> list[tuple[datetime, str]]:
    """When the task moved between prompt, run, write and submitted: claim, each lane start, each collect,
    each submit. From events, with task.json for what came before events existed."""
    d = Path(task_dir)
    try:
        meta = meta or workspace.load(d)
    except (OSError, ValueError):
        meta = {}
    marks: list[tuple[datetime, str]] = []
    claimed = _dt(meta.get("claimed_at") or meta.get("created_at"))
    if claimed:
        marks.append((claimed, "prompt"))
    for r in meta.get("runs") or []:
        for k, stage in (("created_at", "run"), ("collected_at", "write")):
            t = _dt(r.get(k))
            if t:
                marks.append((t, stage))
    for ev in stages.read(d, ("claimed", "lane_started", "collected", "submitted")):
        t = _dt(ev.get("at"))
        if t:
            marks.append((t, {"claimed": "prompt", "lane_started": "run", "collected": "write",
                              "submitted": "submitted"}[ev["kind"]]))
    t = _dt(meta.get("submitted_at"))
    if t:
        marks.append((t, "submitted"))
    marks.sort(key=lambda m: m[0])
    return marks


def _stage_at(marks: list[tuple[datetime, str]], t: datetime) -> str:
    cur = "prompt"
    for when, stage in marks:
        if when > t:
            break
        cur = stage
    return cur


def by_stage(spans: list[tuple[datetime, datetime]], marks: list[tuple[datetime, str]]) -> dict[str, float]:
    """Minutes per stage, splitting each span at the stage changes inside it."""
    out = {k: 0.0 for k in STAGE_KEYS}
    cuts = [m[0] for m in marks]
    for a, b in spans:
        points = [a] + [c for c in cuts if a < c < b] + [b]
        for x, y in zip(points, points[1:]):
            out[_stage_at(marks, x)] += (y - x).total_seconds() / 60
    return {k: round(v, 1) for k, v in out.items()}


# --- the summary -------------------------------------------------------------------------------------------

def summary(task_dir: Path, now: datetime | None = None, *, chats: dict | None = None) -> dict:
    """{minutes, sittings, method, by_stage, explicit_minutes, estimated_minutes, open, sources, first, last, spans}.

    method is "explicit" (only sessions he started and stopped), "estimated", "explicit + estimated", or
    "none" (no signs yet), with a plain `method_text` saying how the estimate was made."""
    d = Path(task_dir)
    now = now or _now()
    explicit = explicit_spans(d, now)
    signs = folder_signs(d)
    try:
        signs += chat_signs(d.name, chats)
    except Exception:   # an unreadable chat folder leaves the other signs
        pass
    sittings = cluster(signs, explicit)
    est_spans = [p for c in sittings for p in _cut((c["start"], c["end"]), explicit)]
    merged = _union(explicit + est_spans)
    exp_min = sum((b - a).total_seconds() for a, b in explicit) / 60
    est_min = sum((b - a).total_seconds() for a, b in est_spans) / 60
    sources: dict[str, int] = {}
    for c in sittings:
        for k, n in c["sources"].items():
            sources[k] = sources.get(k, 0) + n
    method = ("explicit + estimated" if explicit and est_spans else "explicit" if explicit
              else "estimated" if est_spans else "none")
    return {
        "minutes": round(exp_min + est_min, 1), "sittings": len(merged), "method": method,
        "method_text": method_text(method), "by_stage": by_stage(merged, stage_marks(d)),
        "explicit_minutes": round(exp_min, 1), "estimated_minutes": round(est_min, 1),
        "open": open_session(d) is not None, "sources": sources,
        "first": _iso(merged[0][0]) if merged else None, "last": _iso(merged[-1][1]) if merged else None,
        "spans": [{"start": _iso(a), "end": _iso(b), "minutes": round((b - a).total_seconds() / 60, 1)}
                  for a, b in merged],
    }


def method_text(method: str) -> str:
    est = (f"estimated from your messages, lane prompts, Studio edits and johto commands; signs under "
           f"{GAP_MIN} min apart are one sitting, plus {PAD_MIN} min each")
    return {"explicit": "sessions you started and stopped (johto focus)",
            "estimated": est, "explicit + estimated": "your focus sessions, plus " + est.replace("estimated ", "", 1),
            "none": "no signs of work on this task yet"}.get(method, method)


def fmt_minutes(m: float | None) -> str:
    if m is None:
        return "-"
    if 0 < m < 0.5:
        return "under 1 min"
    if m < 60:
        return f"{round(m)} min"
    return f"{m / 60:.1f} h"


def render(s: dict, short: str | None = None) -> str:
    head = f"{short}: " if short else ""
    if s["method"] == "none":
        return f"{head}no focused time yet ({s['method_text']})."
    lines = [f"{head}{fmt_minutes(s['minutes'])} focused over {s['sittings']} sitting"
             f"{'s' if s['sittings'] != 1 else ''}{' (a session is open now)' if s['open'] else ''}.",
             f"Method: {s['method_text']}."]
    st = [f"{STAGE_WORDS[k]} {fmt_minutes(v)}" for k, v in s["by_stage"].items() if v >= 0.5]
    if st:
        lines.append("By stage: " + "; ".join(st) + ".")
    if s["sources"]:
        lines.append("Signs: " + ", ".join(f"{k} {n}" for k, n in sorted(s["sources"].items(), key=lambda x: -x[1])) + ".")
    for sp in s["spans"][-12:]:
        a = _dt(sp["start"])
        lines.append(f"  {a.astimezone().strftime('%a %H:%M') if a else '?'}  {fmt_minutes(sp['minutes'])}")
    return "\n".join(lines)


# --- CLI ---------------------------------------------------------------------------------------------------

def cmd_focus(a) -> None:
    try:
        d = workspace.resolve(a.task)
    except FileNotFoundError as e:
        raise SystemExit(str(e))
    if a.action == "start":
        cur = open_session(d)
        ev = start(d)
        when = _dt(ev.get("at"))
        clock = when.astimezone().strftime("%H:%M") if when else "now"
        print(f"{d.name}: a focus session was already open since {clock}." if cur
              else f"{d.name}: focus session started at {clock}. `johto focus {d.name} stop` ends it.")
        return
    if a.action == "stop":
        ev = stop(d)
        print(f"{d.name}: no focus session is open." if ev is None
              else f"{d.name}: focus session stopped after {fmt_minutes(ev.get('minutes'))}.")
        return
    print(render(summary(d), d.name))


def register(sub) -> None:
    s = sub.add_parser("focus", help="your focused minutes on a task: start, stop, or show the estimate")
    s.add_argument("task")
    s.add_argument("action", nargs="?", default="show", choices=("start", "stop", "show"))
    s.set_defaults(func=cmd_focus)
