"""johto.focus: explicit sessions, the estimate from signs, chat attribution and its cache, minutes by stage.

Run: .venv/bin/python -m unittest tests.test_focus -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from johto import config, focus, stages  # noqa: E402

T0 = datetime(2026, 10, 31, 17, 0, tzinfo=timezone.utc)


def iso(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def user(minutes: float, text: str, **kw) -> dict:
    return {"type": "user", "isSidechain": False, "timestamp": iso(minutes),
            "message": {"role": "user", "content": text}, **kw}


def reply(minutes: float, text: str = "", tool_input: dict | None = None, **kw) -> dict:
    content = [{"type": "text", "text": text}] if text else []
    if tool_input is not None:
        content.append({"type": "tool_use", "id": f"t{minutes}", "name": "Bash", "input": tool_input})
    return {"type": "assistant", "isSidechain": False, "timestamp": iso(minutes),
            "message": {"role": "assistant", "content": content}, **kw}


def write_jsonl(p: Path, rows: list[dict], mode: str = "w") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, mode) as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        self.chats = self.root / "chats"
        self.chats.mkdir()
        self.patches = [mock.patch.object(config, "ROOT", self.root), mock.patch.object(config, "DATA", self.root / "data"),
                        mock.patch.object(config, "TASKS", self.root / "tasks"),
                        mock.patch.dict(os.environ, {"JOHTO_CHAT_DIRS": str(self.chats)})]
        for p in self.patches:
            p.start()
        self.d = self.task("abcd1234")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def task(self, short: str, **meta) -> Path:
        d = self.root / "tasks" / short
        (d / "app").mkdir(parents=True)
        (d / "task.json").write_text(json.dumps({"task_id": f"task_{short}", "task_name": f"Task {short}",
                                                 "created_at": iso(-60), "runs": [], **meta}))
        (d / "submission.md").write_text("## Opening Prompt\n\n")
        return d

    def event(self, minutes: float, kind: str, **fields) -> None:
        write_jsonl(self.d / stages.FILE, [{"at": iso(minutes), "kind": kind, **fields}], mode="a")

    def summary(self, **kw) -> dict:
        return focus.summary(self.d, now=T0 + timedelta(hours=10), **kw)


class Explicit(Base):
    def test_start_twice_keeps_the_first_and_stop_records_minutes(self):
        first = focus.start(self.d)
        self.assertEqual(focus.start(self.d), first)
        ev = focus.stop(self.d, now=focus._dt(first["at"]) + timedelta(minutes=25))
        self.assertEqual(ev["kind"], "focus_stop")
        self.assertAlmostEqual(ev["minutes"], 25.0, delta=0.1)
        self.assertIsNone(focus.stop(self.d))

    def test_explicit_spans_count_as_they_are_and_win_over_signs(self):
        self.event(0, "focus_start")
        self.event(5, "touch", source="where")      # inside the session: not counted again
        self.event(40, "focus_stop")
        s = self.summary()
        self.assertEqual((s["method"], s["minutes"], s["sittings"]), ("explicit", 40.0, 1))

    def test_an_open_session_runs_until_now_at_most_three_hours(self):
        self.event(0, "focus_start")
        s = focus.summary(self.d, now=T0 + timedelta(minutes=30))
        self.assertEqual((s["minutes"], s["open"]), (30.0, True))
        s = focus.summary(self.d, now=T0 + timedelta(hours=9))
        self.assertEqual(s["minutes"], float(focus.OPEN_CAP_MIN))

    def test_signs_next_to_a_session_merge_and_are_cut_where_they_overlap(self):
        self.event(0, "touch", source="where")        # 0..2 estimated, overlaps the session starting at 1
        self.event(1, "focus_start")
        self.event(11, "focus_stop")
        s = self.summary()
        self.assertEqual(s["method"], "explicit + estimated")
        self.assertEqual(s["sittings"], 1)
        self.assertAlmostEqual(s["minutes"], 11.0, delta=0.01)


class Estimate(Base):
    def test_signs_cluster_with_ten_minute_gaps_plus_two_minutes_each(self):
        for m in (0, 5, 9):
            self.event(m, "touch", source="where")
        self.event(30, "lane_started", run="validate-1")
        s = self.summary()
        self.assertEqual((s["method"], s["sittings"]), ("estimated", 2))
        self.assertAlmostEqual(s["minutes"], (9 + 2) + (0 + 2), delta=0.01)
        self.assertIn("10 min apart", s["method_text"])

    def test_automatic_events_are_not_signs(self):
        self.event(0, "turn_ended", run="validate-1", turn=1)
        self.event(1, "pack_built", run="validate-1")
        self.event(2, "collected", run="validate-1")
        s = self.summary()
        self.assertEqual((s["method"], s["minutes"]), ("none", 0.0))

    def test_studio_pulls_editor_saves_and_studio_history(self):
        write_jsonl(self.d / "app" / "authorship.jsonl", [
            {"at": iso(0), "field": "Goal", "event": "pulled_from_studio", "chars": 90},
            {"at": iso(60), "field": "goal", "event": "save", "typed": 40, "focus_ms": 600_000}])
        write_jsonl(self.d / "app" / "studio-history.jsonl", [
            {"at": iso(-200), "fields": {}, "changed": [], "first": True},
            {"at": iso(120), "fields": {"field_high_level_goal": "x"}, "changed": ["field_high_level_goal"]}])
        s = self.summary()
        self.assertEqual(s["sittings"], 3)
        # pull: 2; editor save that had focus for 10 min: 10 + 2; studio history: 2
        self.assertAlmostEqual(s["minutes"], 2 + 12 + 2, delta=0.01)
        self.assertEqual(s["sources"], {"studio": 2, "editor": 1})

    def test_lane_prompts_count_once(self):
        out = self.d / "runs" / "validate-1" / "prework_output"
        rows = [user(0, "Fix the retry loop."), reply(3, "Looking."), user(6, "<task-notification>done</task-notification>"),
                user(8, "Also the requeue path?")]
        write_jsonl(out / "a.jsonl", rows)
        write_jsonl(self.d / "runs" / "validate-1" / ".live-transcripts" / "a.jsonl", rows)  # the same run, copied
        s = self.summary()
        self.assertEqual(s["sources"], {"lane": 2})
        self.assertAlmostEqual(s["minutes"], 10.0, delta=0.01)

    def test_by_stage_splits_at_claim_lane_start_collect_and_submit(self):
        meta = json.loads((self.d / "task.json").read_text())
        meta.update(claimed_at=iso(-10), runs=[{"dir": "runs/validate-1", "created_at": iso(20),
                                                "collected_at": iso(60)}], submitted_at=iso(100))
        (self.d / "task.json").write_text(json.dumps(meta))
        for m in (0, 30, 70, 110):
            self.event(m, "touch", source="where")
        s = self.summary()
        self.assertEqual(s["by_stage"], {"prompt": 2.0, "run": 2.0, "write": 2.0, "submitted": 2.0})

    def test_a_sitting_across_a_stage_change_splits_its_minutes(self):
        meta = json.loads((self.d / "task.json").read_text())
        meta.update(runs=[{"dir": "runs/validate-1", "created_at": iso(4)}])
        (self.d / "task.json").write_text(json.dumps(meta))
        for m in (0, 8):
            self.event(m, "touch", source="where")
        s = self.summary()
        self.assertEqual(s["by_stage"]["prompt"], 4.0)
        self.assertEqual(s["by_stage"]["run"], 6.0)


class Chats(Base):
    def chat(self, name: str, rows: list[dict], mode: str = "w") -> Path:
        p = self.chats / f"{name}.jsonl"
        write_jsonl(p, rows, mode)
        return p

    def test_messages_belong_to_the_task_their_answers_name(self):
        self.task("zzzz9999")
        self.chat("s1", [
            user(0, "check what I've written"),                       # carried from the next message (2 min later)
            user(2, "and the lane command?"), reply(3, tool_input={"command": "johto where abcd1234"}),
            user(20, "now the other one"), reply(21, tool_input={"command": "johto where zzzz9999"}),
            user(200, "unrelated question about the app"),             # nothing near it names a task
            user(210, "<task-notification>job done</task-notification>"),
            user(220, "sidechain", isSidechain=True)])
        rows = focus.attribute(focus.chat_messages()[str(self.chats / "s1.jsonl")])
        self.assertEqual([r[2] for r in rows], ["abcd1234", "abcd1234", "zzzz9999", None])
        s = self.summary()
        self.assertEqual(s["sources"], {"chat": 2})
        self.assertAlmostEqual(s["minutes"], 4.0, delta=0.01)

    def test_a_message_naming_several_tasks_evenly_is_carried_not_split(self):
        self.task("zzzz9999")
        self.chat("s1", [user(0, "work on abcd1234"),
                         user(5, "clean up"), reply(6, tool_input={"command": "ls tasks/abcd1234 tasks/zzzz9999"})])
        rows = focus.attribute(focus.chat_messages()[str(self.chats / "s1.jsonl")])
        self.assertEqual([r[2] for r in rows], ["abcd1234", "abcd1234"])

    def test_a_forked_chat_copy_counts_once(self):
        rows = [user(0, "look at abcd1234"), user(30, "and abcd1234 again")]
        self.chat("s1", rows)
        self.chat("s2", rows)
        self.assertEqual(self.summary()["sources"], {"chat": 2})

    def test_the_cache_reads_only_what_was_appended(self):
        p = self.chat("s1", [user(0, "look at abcd1234")])
        focus.chat_messages()
        cache = json.loads((config.DATA / "cache" / focus.CACHE).read_text())
        first = cache["files"][str(p)]["offset"]
        self.chat("s1", [user(30, "abcd1234 once more")], mode="a")
        with mock.patch.object(focus, "_parse_lines", wraps=focus._parse_lines) as parse:
            msgs = focus.chat_messages()[str(p)]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(len(parse.call_args.args[0]), 1)     # just the new line
        cache = json.loads((config.DATA / "cache" / focus.CACHE).read_text())
        self.assertGreater(cache["files"][str(p)]["offset"], first)

    def test_a_new_task_reparses_the_chats(self):
        self.chat("s1", [user(0, "look at zzzz9999")])
        self.assertEqual(focus.chat_signs("zzzz9999"), [])
        self.task("zzzz9999")
        self.assertEqual(len(focus.chat_signs("zzzz9999")), 1)

    def test_empty_chat_dirs_turn_chats_off(self):
        self.chat("s1", [user(0, "look at abcd1234")])
        with mock.patch.dict(os.environ, {"JOHTO_CHAT_DIRS": ""}):
            self.assertEqual(focus.chat_dirs(), [])
            self.assertEqual(self.summary()["method"], "none")

    def test_subagent_transcripts_in_subfolders_are_skipped(self):
        write_jsonl(self.chats / "s1" / "subagents" / "a.jsonl", [user(0, "look at abcd1234")])
        self.assertEqual(self.summary()["method"], "none")


class Cli(Base):
    def test_focus_command(self):
        import contextlib
        import io
        from johto import cli
        buf = io.StringIO()
        with mock.patch("johto.workspace.resolve", return_value=self.d), contextlib.redirect_stdout(buf):
            cli.main(["focus", "abcd1234", "start"])
            (self.d / stages.FILE).unlink()
            began = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
            write_jsonl(self.d / stages.FILE, [{"at": began, "kind": "focus_start"}])
            cli.main(["focus", "abcd1234", "start"])
            cli.main(["focus", "abcd1234", "stop"])
            cli.main(["focus", "abcd1234"])
        out = buf.getvalue()
        self.assertIn("focus session started", out)
        self.assertIn("already open", out)
        self.assertIn("focus session stopped after 25 min", out)
        self.assertIn("25 min focused over 1 sitting", out)
        self.assertIn("Method: sessions you started and stopped", out)


if __name__ == "__main__":
    unittest.main()
