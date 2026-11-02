"""Claude minutes (transcript.minutes / summarize) and the live budget (budget.py).

Synthetic transcripts cover the cases that stretched the old count: a compaction after Claude ended its turn,
task notices while it was idle, long tool calls and background jobs, interrupted turns and a turn still open.
One real collected transcript (d3m0aaa9 validate-1, from the data copy) checks the numbers against what Austin
entered and Claude Code's own turn markers. status()["live"] follows the lane container (docker ps is faked),
and classify_turn / turn_facts sort turns into question, permission, substantive, progress, background and
problem.
"""
import argparse
import contextlib
import glob
import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from johto import budget, config, pack, settings, transcript

T0 = datetime(2026, 10, 30, 20, 0, 0, tzinfo=timezone.utc)


def at(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def ts(s: float) -> str:
    return at(s).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def human(text: str, s: float) -> dict:
    return {"type": "user", "timestamp": ts(s), "message": {"role": "user", "content": text}}


def reply(text: str, s: float, stop: str = "end_turn", mid: str | None = None, model: str = "claude-opus-5-5") -> dict:
    return {"type": "assistant", "timestamp": ts(s),
            "message": {"id": mid or f"msg_{s}", "model": model, "role": "assistant", "stop_reason": stop,
                        "content": [{"type": "text", "text": text}]}}


def call(tid: str, s: float, name: str = "Bash", bg: bool = False, mid: str | None = None, **inp) -> dict:
    inp = {"command": "make test", **inp}
    if bg:
        inp["run_in_background"] = True
    return {"type": "assistant", "timestamp": ts(s),
            "message": {"id": mid or f"msg_{s}", "model": "claude-opus-5-5", "role": "assistant",
                        "stop_reason": "tool_use", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def result(tid: str, s: float, task: str | None = None, content: str = "out") -> dict:
    e = {"type": "user", "timestamp": ts(s),
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]}}
    if task:
        e["toolUseResult"] = {"backgroundTaskId": task}
    return e


def notice(task: str, tid: str, s: float) -> list[dict]:
    """A background job reporting back while Claude is idle: the queue entry, then the user entry."""
    text = (f"<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>{tid}</tool-use-id>\n"
            f"<status>completed</status>\n</task-notification>")
    return [{"type": "queue-operation", "operation": "enqueue", "timestamp": ts(s), "content": text},
            {"type": "queue-operation", "operation": "dequeue", "timestamp": ts(s)},
            {"type": "user", "timestamp": ts(s), "message": {"role": "user", "content": text}}]


def compaction(s: float) -> list[dict]:
    return [{"type": "system", "subtype": "compact_boundary", "timestamp": ts(s), "content": "Conversation compacted"},
            {"type": "user", "isCompactSummary": True, "timestamp": ts(s), "message": {"role": "user", "content":
             "This session is being continued from a previous conversation that ran out of context."}}]


def exit_cmd(s: float) -> list[dict]:
    return [{"type": "user", "isMeta": True, "timestamp": ts(s),
             "message": {"role": "user", "content": "<local-command-caveat>The command below was run</local-command-caveat>"}},
            {"type": "user", "timestamp": ts(s), "message": {"role": "user", "content":
             "<command-name>/exit</command-name>\n<command-message>exit</command-message>"}}]


def flat(*rows) -> list[dict]:
    return [e for r in rows for e in (r if isinstance(r, list) else [r])]


def write(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


class MinutesTests(unittest.TestCase):
    def summarize(self, entries, now=None) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return transcript.summarize(write(Path(tmp) / "t.jsonl", entries), now=now)

    def test_compaction_after_end_turn_does_not_stretch_the_turn(self):
        """d3m0aaa9: the first turn ended at 02:08, an idle compaction at 03:03 made it 66 minutes."""
        s = self.summarize(flat(human("build it", 0), call("t1", 30), result("t1", 60), reply("done", 120),
                                compaction(3600), human("how did you test it?", 7200), reply("like this", 7260)))
        self.assertEqual(s["working_minutes"], 3.0)
        self.assertEqual(s["active_minutes"], 3.0)
        self.assertEqual(s["human_turns"], 2)      # the compaction summary is not Austin
        self.assertEqual(s["background_minutes"], 0.0)
        self.assertEqual(s["turns"][0]["working_minutes"], 2.0)
        self.assertEqual(s["turns"][0]["idle_minutes"], 118.0)
        self.assertEqual(s["turns"][0]["end"], at(120).isoformat(timespec="seconds"))

    def test_compaction_inside_a_running_turn_counts_as_working(self):
        s = self.summarize(flat(human("go", 0), call("t1", 30), result("t1", 60), compaction(90),
                                reply("done", 150)))
        self.assertEqual(s["working_minutes"], 2.5)
        self.assertEqual(s["human_turns"], 1)

    def test_task_notice_nobody_answers_is_idle(self):
        s = self.summarize(flat(human("go", 0), reply("done", 60), notice("b1", "t9", 1200),
                                human("next", 3000), reply("ok", 3060)))
        self.assertEqual(s["working_minutes"], 2.0)
        self.assertEqual(s["background_minutes"], 0.0)

    def test_background_job_while_idle_is_background_and_the_answer_is_working(self):
        """t4sk0001: two background shells ran 8 minutes after Claude ended its turn."""
        s = self.summarize(flat(human("reproduce it", 0), call("t1", 30, bg=True), result("t1", 31, task="b1"),
                                reply("started, will report", 60), notice("b1", "t1", 1860),
                                call("t2", 1870), result("t2", 1880), reply("it fails at 101 rows", 1890)))
        self.assertEqual(s["working_minutes"], 1.5)
        self.assertEqual(s["background_minutes"], 30.0)
        self.assertEqual(s["idle_minutes"], 0.0)
        self.assertEqual(s["turns"][0]["ended"], "end_turn")
        self.assertEqual(s["turns"][0]["end"], at(1890).isoformat(timespec="seconds"))

    def test_background_job_still_running_at_the_next_prompt(self):
        """d3m0aaa9: a 3h50m app test kept running after the turn; the next turn starts at Austin's prompt."""
        s = self.summarize(flat(human("load test it", 0), call("t1", 30, bg=True), result("t1", 31, task="b1"),
                                reply("running", 60), human("how far is it?", 3660), reply("40%", 3690),
                                notice("b1", "t1", 4260), exit_cmd(4300)))
        t1, t2 = s["turns"]
        self.assertEqual(t1["background_minutes"], 60.0)
        self.assertEqual(t2["working_minutes"], 0.5)
        self.assertEqual(t2["background_minutes"], 9.5)    # until the job reported back
        self.assertAlmostEqual(t2["idle_minutes"], 0.7, places=1)
        self.assertEqual(s["working_minutes"], 1.5)

    def test_async_subagent_counts_as_background(self):
        agent = result("a1", 21)
        agent["toolUseResult"] = {"isAsync": True, "status": "async_launched", "agentId": "ag1"}
        s = self.summarize(flat(human("plan it", 0), call("a1", 20, name="Agent"), agent, reply("waiting", 30),
                                notice("ag1", "a1", 330), reply("plan ready", 390)))
        self.assertEqual(s["working_minutes"], 1.5)
        self.assertEqual(s["background_minutes"], 5.0)

    def test_killed_background_job_stops_counting(self):
        s = self.summarize(flat(human("go", 0), call("t1", 10, bg=True), result("t1", 11, task="b1"),
                                reply("started", 20), human("stop it", 620),
                                call("k1", 630, name="KillShell", shell_id="b1"), result("k1", 631),
                                reply("stopped", 640), exit_cmd(1240)))
        self.assertEqual(s["turns"][0]["background_minutes"], 10.0)
        self.assertEqual(s["turns"][1]["background_minutes"], 0.0)   # killed while Claude was working
        self.assertEqual(s["turns"][1]["idle_minutes"], 10.0)        # not background: the job is gone

    def test_long_foreground_tool_call_past_the_cap_is_background(self):
        s = self.summarize(flat(human("build", 0), call("t1", 30), result("t1", 30 + 25 * 60),
                                reply("built", 30 + 25 * 60 + 30)))
        self.assertEqual(s["working_minutes"], 0.5 + transcript.TOOL_CAP_MIN + 0.5)
        self.assertEqual(s["background_minutes"], 25.0 - transcript.TOOL_CAP_MIN)

    def test_stalled_model_wait_past_the_cap_is_idle(self):
        s = self.summarize(flat(human("go", 0), call("t1", 30), result("t1", 60), reply("done", 60 + 45 * 60)))
        self.assertEqual(s["working_minutes"], 1.0 + transcript.MODEL_STALL_MIN)
        self.assertEqual(s["idle_minutes"], 45.0 - transcript.MODEL_STALL_MIN)

    def test_a_live_prompt_with_no_reply_counts_only_a_short_grace(self):
        """A hung API request after "hello": not 16 min of work, and the turn says there's no reply."""
        entries = flat(human("go", 0), reply("ok", 60), human("hello", 600))
        s = transcript.minutes(entries, now=at(600 + 16 * 60))
        self.assertEqual(s["working_minutes"], 1.0 + transcript.NO_REPLY_GRACE_MIN)
        self.assertEqual(s["turns"][-1]["ended"], "open")
        self.assertEqual(s["turns"][-1]["no_reply"], 16.0)
        # Within the grace it is plain work, with no flag.
        s = transcript.minutes(entries, now=at(600 + 60))
        self.assertEqual(s["working_minutes"], 2.0)
        self.assertNotIn("no_reply", s["turns"][-1])
        # When the reply finally comes, the whole wait counts as before (up to MODEL_STALL_MIN).
        s = transcript.minutes(entries + flat(reply("hi", 600 + 5 * 60)), now=at(600 + 6 * 60))
        self.assertEqual(s["working_minutes"], 1.0 + 5.0)
        self.assertNotIn("no_reply", s["turns"][-1])

    def test_interrupted_turn(self):
        interrupt = {"type": "user", "timestamp": ts(90), "message": {"role": "user", "content": [
            {"type": "text", "text": "[Request interrupted by user]"}]}}
        s = self.summarize(flat(human("fix everything", 0), call("t1", 30), result("t1", 60), interrupt,
                                human("only the gaps", 600), reply("ok", 660)))
        self.assertEqual([t["ended"] for t in s["turns"]], ["interrupted", "end_turn"])
        self.assertEqual(s["turns"][0]["working_minutes"], 1.5)
        self.assertEqual(s["working_minutes"], 2.5)

    def test_prompt_interrupted_before_any_reply_is_not_running(self):
        interrupt = {"type": "user", "timestamp": ts(70), "message": {"role": "user", "content": [
            {"type": "text", "text": "[Request interrupted by user]"}]}}
        s = self.summarize(flat(human("one", 0), reply("ok", 60), human("two", 65), interrupt), now=at(600))
        self.assertEqual(s["working_minutes"], 1.0)
        self.assertNotEqual(s["turns"][-1]["ended"], "open")

    def test_reply_blocks_after_end_turn_count(self):
        """Claude Code writes thinking and text as separate entries of one reply, both with end_turn."""
        s = self.summarize(flat(human("go", 0), reply("thinking", 60, mid="m1"), reply("answer", 66, mid="m1")))
        self.assertEqual(s["working_minutes"], 1.1)

    def test_api_error_ends_the_turn(self):
        err = reply("API Error: overloaded", 30, stop="stop_sequence", model="<synthetic>")
        err["isApiErrorMessage"] = True
        s = self.summarize(flat(human("go", 0), call("t1", 10), result("t1", 20), err, exit_cmd(1800)))
        self.assertEqual(s["working_minutes"], 0.5)
        self.assertEqual(s["turns"][0]["ended"], "end_turn")

    def test_open_turn_counts_up_to_now(self):
        entries = flat(human("go", 0), call("t1", 30))
        s = self.summarize(entries, now=at(300))
        self.assertEqual(s["working_minutes"], 5.0)
        self.assertEqual(s["turns"][-1]["ended"], "open")
        done = self.summarize(entries)            # collected: the session stopped mid-turn
        self.assertEqual(done["working_minutes"], 0.5)
        self.assertEqual(done["turns"][-1]["ended"], "interrupted")

    def test_prompt_waiting_for_its_first_reply_is_open(self):
        s = self.summarize(flat(human("one", 0), reply("ok", 60), human("two", 600)), now=at(660))
        self.assertEqual(len(s["turns"]), 2)
        self.assertEqual(s["turns"][1]["ended"], "open")
        self.assertEqual(s["turns"][1]["working_minutes"], 1.0)

    def test_running_background_job_is_reported_when_live(self):
        s = self.summarize(flat(human("go", 0), call("t1", 10, bg=True), result("t1", 11, task="b1"),
                                reply("started", 20)), now=at(620))
        self.assertEqual(s["background_running"], 1)
        self.assertEqual(s["background_minutes"], 10.0)
        self.assertEqual(s["turns"][0]["ended"], "end_turn")

    def test_turns_carry_index_start_and_prompt(self):
        s = self.summarize(flat(human("x" * 300, 0), reply("ok", 60), human("more", 120), reply("ok", 180)))
        self.assertEqual([t["index"] for t in s["turns"]], [1, 2])
        self.assertEqual(s["turns"][0]["start"], at(0).isoformat(timespec="seconds"))
        self.assertEqual(len(s["turns"][0]["prompt"]), 200)

    def test_render_shows_working_background_and_idle_on_one_line(self):
        s = self.summarize(flat(human("go", 0), call("t1", 30, bg=True), result("t1", 31, task="b1"),
                                reply("started", 60), notice("b1", "t1", 660), reply("done", 690), exit_cmd(1290)))
        line = next(ln for ln in transcript.render(s).splitlines() if "Claude minutes" in ln)
        self.assertEqual(line, "- Claude minutes: 1.5 working, 10.0 waiting on background jobs, 10.0 idle "
                               "(wall clock 21.5)")

    def test_no_prompt(self):
        s = self.summarize([reply("hello", 10)])
        self.assertEqual((s["working_minutes"], s["turns"]), (0.0, []))


def _real(short: str, run: str) -> Path | None:
    for root in (config.TASKS,):
        found = sorted(glob.glob(str(root / short / "runs" / run / "prework_output" / "*.jsonl")))
        if found:
            return Path(found[-1])
    return None


class RunningNames(unittest.TestCase):
    """budget._running_names: one read-only docker ps, here or over ssh."""

    def setUp(self):
        budget._running.clear()
        self.addCleanup(budget._running.clear)

    def run_with(self, out: str, rc: int = 0, host=None, fake=False):
        r = mock.Mock(returncode=rc, stdout=out)
        with mock.patch.object(budget.subprocess, "run", return_value=r) as run, \
                mock.patch.object(budget.monitor, "docker_free", return_value=fake):
            got = budget._running_names(host)
        return got, run

    def test_local(self):
        got, run = self.run_with("prework-a\nprework-b\n")
        self.assertEqual(got, {"prework-a", "prework-b"})
        self.assertEqual(run.call_args.args[0][:2], ["docker", "ps"])

    def test_remote_with_docker_stopped_runs_nothing(self):
        got, run = self.run_with("@@rc 1\n", host="pc")
        self.assertEqual(got, set())
        self.assertEqual(run.call_args.args[0][0], "ssh")
        self.assertTrue(budget._reach["pc"][1])

    def test_unknown_when_it_cannot_be_told(self):
        self.assertIsNone(self.run_with("", rc=255, host="pc2")[0])
        self.assertFalse(budget._reach["pc2"][1])
        budget._running.clear()
        got, run = self.run_with("x", fake=True)
        self.assertIsNone(got)
        run.assert_not_called()


class ClassifyTests(unittest.TestCase):
    """budget.turn_facts / classify_turn on small transcripts."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(settings, "get", lambda k: settings.DEFAULTS.get(k))
        p.start()
        self.addCleanup(p.stop)

    def path(self, *rows) -> Path:
        return write(self.tmp / "t.jsonl", flat(*rows))

    def kind(self, *rows, **kw) -> str:
        return budget.classify_turn(self.path(*rows), **kw)

    def test_an_edit_is_substantive(self):
        f = budget.turn_facts(self.path(human("fix it", 0), call("e1", 30, name="Edit", file_path="/w/app/x.py"),
                                        result("e1", 31), reply("Done.", 90)))
        self.assertEqual((f["kind"], f["edits"], f["edited_files"]), ("substantive", 1, ["/w/app/x.py"]))

    def test_scratch_edits_and_reads_are_progress(self):
        rows = [human("look", 0), call("e1", 30, name="Write", file_path="/tmp/probe.py"), result("e1", 31),
                call("b1", 40, command="sed -i s/a/b/ /tmp/x.txt"), result("b1", 41), reply("Looked around.", 90)]
        self.assertEqual(self.kind(*rows), "progress")
        self.assertEqual(self.kind(*rows, diff_changed=True), "substantive")    # the caller knows the diff moved

    def test_a_long_report_after_an_investigation_is_substantive(self):
        tools = [x for i in range(8) for x in (call(f"r{i}", 10 + i, name="Read", file_path=f"/w/f{i}.py"),
                                              result(f"r{i}", 10.5 + i))]
        f = budget.turn_facts(self.path(human("why?", 0), *tools, reply("Finding. " * 250, 60)))
        self.assertEqual(f["kind"], "substantive")
        self.assertIn("report", f["why"])
        self.assertEqual(self.kind(human("why?", 0), reply("Finding. " * 250, 60)), "progress")   # no investigation

    def test_a_question_at_the_end(self):
        f = budget.turn_facts(self.path(human("go", 0), reply("I looked.\n\nShould I also update the importer?", 60)))
        self.assertEqual((f["kind"], f["asks"]), ("question", "Should I also update the importer?"))
        f = budget.turn_facts(self.path(human("go", 0), reply("Is it `x?` here:\n\n```\nif a?\n```\n\nDone.", 60)))
        self.assertEqual((f["kind"], f["asks"]), ("progress", None))           # code and earlier lines don't count

    def test_ask_user_question_waits_on_austin(self):
        ask = call("q1", 30, name="AskUserQuestion", questions=[{"question": "Keep the old endpoint?", "header": "API",
                                                                 "options": [{"label": "Yes"}, {"label": "No"}]}])
        f = budget.turn_facts(self.path(human("go", 0), ask))
        self.assertEqual((f["kind"], f["asks"]), ("question", "Keep the old endpoint?"))
        self.assertEqual(f["pending_tool"]["name"], "AskUserQuestion")

    def test_a_pending_tool_is_permission_only_on_screen(self):
        rows = [human("go", 0), call("b1", 30, command="rm -rf build")]
        self.assertEqual(self.kind(*rows), "progress")                          # could be a long test run
        f = budget.turn_facts(self.path(*rows), screen={"permission": True, "input_visible": False})
        self.assertEqual((f["kind"], f["permission"]), ("permission", "Bash rm -rf build"))

    def test_problems(self):
        err = {"type": "assistant", "timestamp": ts(60), "isApiErrorMessage": True,
               "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error: 529 overloaded"}]}}
        self.assertEqual(budget.turn_facts(self.path(human("go", 0), err))["problem"], "API Error: 529 overloaded")
        rows = [human("go", 0), reply("ok", 60)]
        self.assertEqual(self.kind(*rows, screen={"rate": {"until": "4pm"}}), "problem")
        self.assertEqual(self.kind(*rows, screen={"login_screen": True}), "problem")

    def test_background_and_open_turns(self):
        st = {"transcript": str(self.path(human("go", 0), call("b1", 30, bg=True), result("b1", 31, task="t1"),
                                          reply("Started the suite.", 40))),
              "background_running": 1}
        st["turns"] = transcript.minutes(budget._entries(st["transcript"]))["turns"]
        self.assertEqual(budget.classify_turn(st), "background")
        self.assertEqual(self.kind(human("go", 0), call("b1", 30)), "progress")
        self.assertEqual(budget.turn_facts(self.path(human("go", 0)), 3)["why"], "no such turn")

    def test_real_transcripts_from_the_data_copy(self):
        """Every turn of every collected run in the data copy gets a kind; the Vidlabel validation's first turn is
        its long assessment report (checked by hand)."""
        root = Path(config.ROOT)
        logs = sorted(glob.glob(str(root / "tasks" / "*" / "runs" / "*" / "prework_output" / "*.jsonl")))
        if not logs:
            self.skipTest("no collected transcripts under JOHTO_ROOT")
        for log in logs:
            for tr in transcript.minutes(budget._entries(log))["turns"]:
                self.assertIn(budget.classify_turn(Path(log), tr["index"]), budget.KINDS, log)
        vidlabel = [x for x in logs if "/t4sk0002/runs/validate-1/" in x]
        if vidlabel:
            f = budget.turn_facts(Path(vidlabel[-1]), 1)
            self.assertEqual(f["kind"], "substantive")
            self.assertGreater(f["report_chars"], budget.REPORT_CHARS)


class RealTranscriptTests(unittest.TestCase):
    def test_d3m0aaa9_validate_1(self):
        """Austin entered 50; Claude Code's own turn markers add up to 50.5; the old count said 337.4."""
        src = _real("d3m0aaa9", "validate-1")
        if not src:
            self.skipTest("d3m0aaa9 validate-1 transcript not in this data copy")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / src.name
            shutil.copy(src, p)
            s = transcript.summarize(p)
            markers = sum(json.loads(ln).get("durationMs", 0) for ln in p.read_text().splitlines()
                          if '"turn_duration"' in ln)
        self.assertEqual(s["human_turns"], 4)
        self.assertTrue(49 <= s["working_minutes"] <= 56, s["working_minutes"])
        self.assertAlmostEqual(s["working_minutes"], markers / 60000, delta=1.0)
        self.assertGreater(s["background_minutes"], 200)     # the long app test after turn 3
        self.assertEqual(s["turns"][0]["working_minutes"], 11.8)  # ended 02:08, not at the 03:03 compaction
        self.assertEqual([t["ended"] for t in s["turns"]], ["end_turn"] * 4)


class RunMinutesTests(unittest.TestCase):
    """workspace.run_minutes: task.json records from before working minutes hold the older, larger number."""

    def test_old_record_is_read_again_from_its_transcript(self):
        from johto import actions, workspace
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            out = d / "runs" / "validate-1" / "prework_output"
            out.mkdir(parents=True)
            # 1.5 working minutes, then 30 minutes of a background shell (the old measure said 31.5).
            write(out / "s.jsonl", flat(human("reproduce it", 0), call("t1", 30, bg=True), result("t1", 31, task="b1"),
                                        reply("started, will report", 60), notice("b1", "t1", 1860),
                                        call("t2", 1870), result("t2", 1880), reply("it fails at 101 rows", 1890)))
            # The stored path is the live tree's; the copy in this task folder is the one read.
            rec = {"dir": "runs/validate-1", "kind": "validate", "active_minutes": 31.5,
                   "transcript": "/nowhere/tasks/t/runs/validate-1/prework_output/s.jsonl"}
            self.assertEqual(workspace.run_minutes(rec, d), 1.5)
            self.assertEqual(workspace.run_minutes(rec), 31.5)               # transcript not found: stored number
            self.assertEqual(workspace.run_minutes({**rec, "working_minutes": 7.0}, d), 7.0)
            self.assertIsNone(workspace.run_minutes({"dir": "runs/validate-2"}, d))
            with mock.patch.object(workspace, "load", lambda _d: {"runs": [rec]}), \
                    mock.patch.object(workspace, "parse_submission", lambda _d: {}), \
                    mock.patch.object(workspace, "to_fields", lambda meta, sub, mins: {"field_claude_minutes": mins}), \
                    mock.patch.object(actions, "validation_run", lambda _d, run=None: ("validate-1", rec)):
                self.assertEqual(actions.runs_overview(d)[0]["active_minutes"], 1.5)
                self.assertEqual(actions.fields(d)["field_claude_minutes"], 1.5)   # Studio's Claude minutes


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "prework"
        self.task = self.tmp / "tasks" / "zz01"
        self.running = None          # docker ps: None (can't tell), or the names running
        for p in (mock.patch.object(config, "TASKS", self.tmp / "tasks"),
                  mock.patch.object(config, "PREWORK_HOME", self.home),
                  mock.patch.object(settings, "get", lambda k: settings.DEFAULTS.get(k)),
                  mock.patch.object(budget, "_running_names", lambda host: self.running)):
            p.start()
            self.addCleanup(p.stop)

    def make_run(self, name: str, rec: dict | None = None, collected: list | None = None,
                 lane: list | None = None) -> Path:
        run_dir = self.task / "runs" / name
        run_dir.mkdir(parents=True, exist_ok=True)
        container = f"prework-PREWORK_zz01-{name.replace('-', '')}x-model_a"
        (run_dir / "prework-manifest.json").write_text(json.dumps({"lane": {"container": container}}))
        meta = json.loads((self.task / "task.json").read_text()) if (self.task / "task.json").exists() else {
            "task_name": "Task zz01", "runs": []}
        meta["runs"].append({"kind": name.split("-")[0], "dir": f"runs/{name}", "container": container, **(rec or {})})
        (self.task / "task.json").write_text(json.dumps(meta))
        if collected is not None:
            write(run_dir / "prework_output" / "aaaa.jsonl", collected)
        if lane is not None:
            key = container[len("prework-"):-len("-model_a")]
            write(self.home / "cc-config" / f"{key}_model_a" / "projects" / "-exp-x" / "live.jsonl", lane)
        return run_dir

    def test_collected_run_status_and_line(self):
        self.make_run("validate-1", {"torn_down_at": "x"}, collected=flat(
            human("go", 0), call("t1", 30, bg=True), result("t1", 31, task="b1"), reply("started", 60),
            notice("b1", "t1", 780), call("t2", 800), result("t2", 1340), call("t3", 1400), result("t3", 1940),
            call("t4", 2000), result("t4", 2540), reply("done", 780 + 37 * 60)))
        st = budget.status(self.task, now=at(4000))
        self.assertFalse(st["live"])
        self.assertEqual(st["run"], "validate-1")
        self.assertEqual(st["working_minutes"], 38.0)
        self.assertEqual(st["background_minutes"], 12.0)
        self.assertEqual(st["budget"], 90)
        self.assertFalse(st["turn_open"])
        self.assertEqual(st["last_turn"]["index"], 1)
        self.assertEqual(st["line"], "validate-1: Claude worked 38 min (12 min more waiting on background jobs). "
                                     "1 turn, collected. 38 of 90 min used.")

    def test_live_local_lane_beats_the_collected_copy(self):
        self.make_run("validate-1", collected=flat(human("go", 0), reply("ok", 60)),
                      lane=flat(human("go", 0), reply("ok", 60), human("tests?", 600), call("t1", 630),
                                result("t1", 660), reply("added", 720), human("and the edge case?", 900),
                                call("t2", 960)))
        st = budget.status(self.task, "validate-1", now=at(1200))
        self.assertTrue(st["live"])
        self.assertTrue(st["turn_open"])
        self.assertEqual(len(st["turns"]), 3)
        self.assertEqual(st["working_minutes"], 1 + 2 + 5)
        self.assertEqual(st["line"], "validate-1: Claude worked 8 min. Turn 3 is running. 8 of 90 min used.")

    def test_live_line_with_no_reply_says_it_may_be_stuck(self):
        self.make_run("validate-1", lane=flat(human("go", 0), reply("ok", 60), human("hello", 600)))
        st = budget.status(self.task, "validate-1", now=at(600 + 16 * 60))
        self.assertEqual(st["line"], f"validate-1: Claude worked {1 + transcript.NO_REPLY_GRACE_MIN} min. Turn 2 has had "
                                     "no reply from Claude for 16 min; the lane or the API may be stuck. "
                                     f"{1 + transcript.NO_REPLY_GRACE_MIN} of 90 min used.")

    def test_live_line_after_the_turn_ended(self):
        self.make_run("validate-1", lane=flat(human("go", 0), call("t1", 30, bg=True), result("t1", 31, task="b1"),
                                               reply("started", 60)))
        st = budget.status(self.task, now=at(60 + 240))
        self.assertEqual(st["line"], "validate-1: Claude worked 1 min (4 min more waiting on background jobs). "
                                     "Turn 1 ended 4 min ago. 1 background job still running. 1 of 90 min used.")

    def test_no_transcript_and_no_runs(self):
        self.task.mkdir(parents=True)
        (self.task / "task.json").write_text(json.dumps({"runs": []}))
        self.assertEqual(budget.status(self.task)["line"], "No runs yet.")
        self.make_run("validate-1")
        st = budget.status(self.task)
        self.assertEqual(st["line"], "validate-1: no transcript yet.")
        self.assertIsNone(st["last_turn"])

    def test_latest_run_is_the_last_started(self):
        self.make_run("validate-1", collected=[])
        self.make_run("validate-2", collected=[])
        self.assertEqual(budget.latest_run(self.task), "validate-2")

    def test_pc_lane_is_not_read_in_fake_mode_or_when_the_pc_is_away(self):
        self.make_run("validate-1", {"host": "build-box", "remote_dir": "/home/x/run"},
                      collected=flat(human("go", 0), reply("ok", 60)))
        boom = mock.Mock(side_effect=AssertionError("no ssh in tests"))
        with mock.patch.object(pack, "_live_transcript", boom), mock.patch.object(budget.subprocess, "run", boom):
            with mock.patch.object(config, "FAKE", True):
                self.assertIsNone(budget.live_transcript(self.task, "validate-1"))
            with mock.patch.object(config, "FAKE", False), mock.patch.object(budget, "_reachable", lambda h: False):
                st = budget.status(self.task)
        self.assertFalse(st["live"])
        self.assertEqual(st["working_minutes"], 1.0)
        self.assertEqual(st["unreached"], "build-box")
        self.assertTrue(st["line"].endswith("build-box didn't answer, so this is the collected copy."), st["line"])

    def test_an_uncollected_pc_run_says_the_pc_did_not_answer(self):
        """Not "no transcript yet": the transcript is on the PC, which is away."""
        self.make_run("validate-2", {"host": "build-box", "remote_dir": "/home/x/run"})
        with mock.patch.object(config, "FAKE", False), mock.patch.object(budget, "_reachable", lambda h: False), \
                mock.patch.object(budget.subprocess, "run", mock.Mock(side_effect=AssertionError("no ssh in tests"))):
            st = budget.status(self.task, "validate-2")
        self.assertEqual(st["line"], "validate-2: its transcript is on build-box, which didn't answer.")

    def test_pc_lane_copy_is_used_when_the_pc_answers(self):
        run_dir = self.make_run("validate-1", {"host": "build-box", "remote_dir": "/home/x/run"})
        copy = write(run_dir / ".live-transcripts" / "-exp-x" / "live.jsonl", flat(human("go", 0), call("t1", 30)))
        with mock.patch.object(config, "FAKE", False), mock.patch.object(budget, "_reachable", lambda h: True), \
                mock.patch.object(pack, "_live_transcript", lambda d: copy):
            st = budget.status(self.task, now=at(120))
        self.assertTrue(st["live"])
        self.assertEqual(st["working_minutes"], 2.0)

    def test_live_means_the_container_runs(self):
        run_dir = self.make_run("validate-1", lane=flat(human("go", 0), reply("ok", 60)))
        container = json.loads((run_dir / "prework-manifest.json").read_text())["lane"]["container"]
        self.running = {container}
        st = budget.status(self.task, now=at(120))
        self.assertEqual((st["live"], st["lane"]), (True, "running"))
        self.running = set()
        st = budget.status(self.task, now=at(120))
        self.assertEqual((st["live"], st["lane"], st["working_minutes"]), (False, "stopped", 1.0))
        self.assertEqual(st["line"], "validate-1: Claude worked 1 min. 1 turn; the lane is stopped. 1 of 90 min used.")
        write(run_dir / "prework_output" / "aaaa.jsonl", flat(human("go", 0), reply("ok", 60)))
        self.assertIn("1 turn, collected.", budget.status(self.task, now=at(120))["line"])

    def test_a_torn_down_lane_is_gone(self):
        self.make_run("validate-1", {"torn_down_at": "x"}, collected=flat(human("go", 0), reply("ok", 60)))
        self.running = {"anything"}
        self.assertEqual(budget.status(self.task)["lane"], "gone")

    def test_nudge_due(self):
        due = budget.nudge_due
        self.assertIsNone(due({"working_minutes": 30}, []))
        self.assertEqual(due({"working_minutes": 45}, []), 45)
        self.assertIsNone(due({"working_minutes": 50}, [45]))
        self.assertEqual(due({"working_minutes": 80}, [45]), 75)
        self.assertEqual(due({"working_minutes": 80}, []), 75)       # two crossed at once: one nudge, the higher
        self.assertIsNone(due({"working_minutes": 80}, [75]))
        self.assertEqual(due({"working_minutes": 95}, [45, 75]), 90)
        self.assertIsNone(due({"working_minutes": 95}, [90]))

    def test_johto_time_command(self):
        self.make_run("validate-1", collected=flat(human("Import my old export file", 0), reply("ok", 120),
                                                   human("How did you test it?", 600), reply("like this", 630)))
        p = argparse.ArgumentParser()
        budget.register(p.add_subparsers())
        a = p.parse_args(["time", "zz01"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            a.func(a)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "validate-1: Claude worked 2 min. 2 turns, collected. 2 of 90 min used.")
        self.assertTrue(lines[2].startswith("Turn  Started"))
        self.assertIn("2.0", lines[3])
        self.assertIn("finished", lines[4])
        self.assertTrue(lines[4].endswith("How did you test it?"))
        with self.assertRaises(SystemExit):
            a = p.parse_args(["time", "zz01", "calib-9"])
            a.func(a)


if __name__ == "__main__":
    unittest.main()
