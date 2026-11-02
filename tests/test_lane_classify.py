"""Lane state rules: pane parsing (real Claude Code 2.1.311 captures plus layouts copied from them), the
transcript reducer, the classifier's rule order and debounce, the process check, and the async Enter lock."""
import json
import tempfile
import unittest
from pathlib import Path

from johto import transcript
from johto.web.lane import monitor
from johto.web.lane.monitor import Reducer, classify, screen
from johto.web.lane.pty_bridge import strip_enters

PANES = Path(__file__).parent / "fixtures" / "panes"


def pane(name: str) -> dict:
    return screen((PANES / f"{name}.txt").read_text())


def ts(i: int) -> str:
    return f"2026-10-30T20:{i // 60:02d}:{i % 60:02d}.000Z"


def human(text: str, i: int = 0) -> dict:
    return {"type": "user", "isSidechain": False, "timestamp": ts(i), "message": {"role": "user", "content": text}}


def claude(text: str, i: int = 1, stop: str = "end_turn", model: str = "claude-opus-5-5") -> dict:
    return {"type": "assistant", "isSidechain": False, "timestamp": ts(i),
            "message": {"model": model, "role": "assistant", "stop_reason": stop,
                        "content": [{"type": "text", "text": text}]}}


def tool_use(tid: str, name: str, inp: dict, i: int = 1) -> dict:
    return {"type": "assistant", "isSidechain": False, "timestamp": ts(i),
            "message": {"model": "claude-opus-5-5", "role": "assistant", "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def tool_result(tid: str, is_error: bool = False, i: int = 2) -> dict:
    return {"type": "user", "isSidechain": False, "timestamp": ts(i),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "out",
                                                     "is_error": is_error}]}}


def local_cmd(cmd: str, i: int = 0) -> list[dict]:
    return [{"type": "user", "isMeta": True, "timestamp": ts(i),
             "message": {"role": "user", "content": "<local-command-caveat>The command below was run</local-command-caveat>"}},
            {"type": "user", "timestamp": ts(i), "message": {"role": "user", "content":
             f"<command-name>{cmd}</command-name>\n<command-message>{cmd[1:]}</command-message>"}},
            {"type": "user", "timestamp": ts(i), "message": {"role": "user", "content":
             "<local-command-stdout>Login interrupted</local-command-stdout>"}}]


def red_of(*entries) -> Reducer:
    r = Reducer()
    r.feed([e for x in entries for e in (x if isinstance(x, list) else [x])])
    return r


def run(**kw):
    base = dict(container_running=True, claude_alive=True, scr=None, red=None, has_credentials=True,
                collect_phase=None, launched_at=None, pty_alive=False, pty_failed=None, held_since=None, now=1000.0)
    base.update(kw)
    return classify(**base)


class ScreenTests(unittest.TestCase):
    def test_real_not_logged_in_screen(self):
        s = pane("not_logged_in")
        self.assertTrue(s["input_visible"])
        self.assertEqual(s["input_text"], "")
        self.assertTrue(s["login"])
        self.assertFalse(s["login_screen"])
        self.assertFalse(s["permission"])

    def test_real_login_method_screen(self):
        s = pane("login_method")
        self.assertFalse(s["input_visible"])
        self.assertTrue(s["login_screen"])
        self.assertFalse(s["permission"], "❯ 1. Claude account is not a permission dialog")

    def test_real_slash_menu_is_not_the_input_line(self):
        s = pane("slash_menu")
        self.assertTrue(s["input_visible"])
        self.assertEqual(s["input_text"], "/log")

    def test_real_paste_shows_in_the_box(self):
        s = pane("pasted_not_logged_in")
        self.assertEqual(s["input_text"], "hello there\nsecond line of my prompt\nthird")

    def test_working_long_test_is_not_permission(self):
        s = pane("working_long_test")
        self.assertTrue(s["working"])
        self.assertFalse(s["permission"])
        self.assertTrue(s["input_visible"])
        self.assertIsNone(s["rate"])

    def test_permission_dialog(self):
        s = pane("permission")
        self.assertTrue(s["permission"])
        self.assertFalse(s["input_visible"])

    def test_typed_reply_counts_as_text_in_the_box(self):
        self.assertEqual(pane("waiting_typed_reply")["input_text"], "return 404, same as the users endpoint")
        self.assertEqual(pane("waiting_question")["input_text"], "")

    def test_rate_limit_line_and_reset_time(self):
        self.assertEqual(pane("rate_limited")["rate"], {"until": "3pm"})
        self.assertIsNone(pane("waiting_mentions_rate_limit")["rate"], "Claude's prose about rate limits is not one")

    def test_no_pane(self):
        self.assertIsNone(screen(None))


class ReducerTests(unittest.TestCase):
    def test_login_and_exit_commands_are_not_turns(self):
        r = red_of(local_cmd("/login"), local_cmd("/exit", 5))
        self.assertEqual(r.humans, 0)
        self.assertFalse(r.turn_ended())

    def test_turn_end_and_question(self):
        r = red_of(human("lock and unlock rooms from the API"),
                   claude("I read room.go. Should locking a room that isn't loaded yet load it first, or return "
                          "404 like the user endpoints do?", 3))
        self.assertEqual(r.humans, 1)
        self.assertTrue(r.turn_ended())
        self.assertEqual(monitor.question_of(r.last_claude_text),
                         "Should locking a room that isn't loaded yet load it first, or return 404 like the user "
                         "endpoints do?")

    def test_tool_use_without_result_is_not_a_turn_end(self):
        r = red_of(human("go"), tool_use("t1", "Bash", {"command": "go test ./..."}))
        self.assertFalse(r.turn_ended())
        self.assertEqual(r.test_facts()["runs"], 0)

    def test_tests_paired_with_results_and_last_edit(self):
        r = red_of(human("go"),
                   tool_use("t1", "Bash", {"command": "npm run test"}, 1), tool_result("t1", True, 2),
                   tool_use("e1", "Edit", {"file_path": "/w/src/a.ts"}, 3), tool_result("e1", False, 4),
                   tool_use("t2", "Bash", {"command": "npm run test"}, 5), tool_result("t2", False, 6),
                   claude("Done, tests pass.", 7))
        f = r.test_facts()
        self.assertEqual((f["runs"], f["last_ok"], f["after_last_edit"]), (2, True, True))
        self.assertEqual(r.files, ["/w/src/a.ts"])

    def test_interrupt_counts_as_turn_end(self):
        r = red_of(human("go"), tool_use("t1", "Bash", {"command": "ls"}),
                   {"type": "user", "timestamp": ts(4), "message": {"role": "user", "content": [
                       {"type": "text", "text": "[Request interrupted by user]"}]}})
        self.assertTrue(r.turn_ended())

    def test_sidechain_entries_are_ignored(self):
        side = claude("subagent done", 2)
        side["isSidechain"] = True
        r = red_of(human("go"), tool_use("t1", "Task", {"prompt": "x"}), side)
        self.assertFalse(r.turn_ended())

    def test_usage_limit_message_sets_rate_flag(self):
        err = claude("Claude usage limit reached. Your limit resets 3pm.", 3, model="<synthetic>")
        err["isApiErrorMessage"] = True
        r = red_of(human("go"), err)
        self.assertEqual(r.rate, {"until": "3pm"})
        self.assertTrue(r.turn_ended())

    def test_active_minutes_stop_at_the_last_answer_not_at_exit(self):
        """An /exit typed 20 minutes after the last answer isn't Claude working (t4sk0002 counted 29 for 11)."""
        entries = ([human("one", 0), tool_use("t1", "Bash", {"command": "ls"}, 30), tool_result("t1", i=60),
                    claude("done", 120), human("two", 600), claude("ok", 660)] + local_cmd("/exit", 1800))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
            s = transcript.summarize(p)
        self.assertEqual(s["active_minutes"], 3.0)   # 2 minutes, then 1
        self.assertEqual(s["wall_minutes"], 30.0)
        self.assertEqual(s["human_turns"], 2)

    def test_django_test_runner_counts_as_a_test_command(self):
        for cmd in ("$SP/venv/bin/python manage.py test --settings vidlabel.settings.ci vidlabel.loans",
                    "python3 manage.py test"):
            self.assertTrue(transcript.TEST_CMD.search(cmd), cmd)
        self.assertFalse(transcript.TEST_CMD.search("python manage.py migrate"))

    def test_matches_summarize_human_turns(self):
        entries = [human("one", 0), claude("ok?", 1), human("two", 2), claude("done", 3)] + local_cmd("/exit", 4)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
            r = Reducer()
            tail = monitor.Tail(p)
            r.feed(tail.read())
            self.assertEqual(r.humans, transcript.summarize(p)["human_turns"])
            with open(p, "a") as f:
                f.write(json.dumps(human("three", 5)) + "\n" + json.dumps(claude("x", 6))[:20])   # half a line
            r.feed(tail.read())
            self.assertEqual(r.humans, 3)
            self.assertEqual(r.last["kind"], "human", "the half-written line waits for its newline")


class ClassifyTests(unittest.TestCase):
    def test_container_down(self):
        self.assertEqual(run(container_running=False)[0], "free")
        self.assertEqual(run(container_running=False, collect_phase="done")[0], "collected")

    def test_exited_collecting_collected(self):
        r = red_of(human("go"), claude("done"))
        self.assertEqual(run(claude_alive=False, red=r)[0], "exited")
        self.assertEqual(run(claude_alive=False, red=r, collect_phase="collecting")[0], "collecting")
        self.assertEqual(run(claude_alive=False, red=r, collect_phase="done")[0], "collected")

    def test_not_started(self):
        self.assertEqual(run(claude_alive=False, launched_at=990.0, pty_alive=True)[0], "preparing")
        self.assertEqual(run(claude_alive=False, pty_failed="ERROR: no container")[:2],
                         ("setup_failed", "ERROR: no container"))
        self.assertEqual(run(claude_alive=False)[0], "idle")
        self.assertEqual(run(claude_alive=False, red=red_of(local_cmd("/login")))[0], "idle",
                         "a /login alone is not a run")

    def test_login(self):
        self.assertEqual(run(scr=pane("not_logged_in"), has_credentials=False)[0], "needs_login")
        self.assertEqual(run(scr=pane("login_method"), has_credentials=False)[0], "needs_login")
        self.assertEqual(run(scr=pane("ready_logged_in"), has_credentials=False)[0], "needs_login",
                         "no .credentials.json means no login yet")

    def test_ready_for_prompt(self):
        self.assertEqual(run(scr=pane("ready_logged_in"))[0], "ready_for_prompt")
        self.assertEqual(run(scr=None)[0], "ready_for_prompt")

    def test_working_and_waiting_debounce(self):
        r = red_of(human("go"), claude("Should I keep the old endpoint?", 2))
        self.assertEqual(run(red=r, scr=pane("working_long_test"))[0], "working",
                         "the spinner says Claude is still busy")
        st, _, held = run(red=r, scr=pane("waiting_question"), now=1000.0)
        self.assertEqual((st, held), ("working", 1000.0), "not waiting before 3 s")
        st, _, held = run(red=r, scr=pane("waiting_question"), held_since=held, now=1002.9)
        self.assertEqual(st, "working")
        st, _, held = run(red=r, scr=pane("waiting_question"), held_since=held, now=1003.0)
        self.assertEqual(st, "waiting")

    def test_long_tool_use_is_working_not_permission(self):
        r = red_of(human("go"), tool_use("t1", "Bash", {"command": "go test ./..."}))
        self.assertEqual(run(red=r, scr=pane("working_long_test"), held_since=1.0)[0], "working")

    def test_permission_wins_over_waiting(self):
        r = red_of(human("go"), tool_use("t1", "Bash", {"command": "npm ci"}))
        self.assertEqual(run(red=r, scr=pane("permission"))[0], "permission")


class ProcessAndLockTests(unittest.TestCase):
    def test_claude_process_match(self):
        self.assertTrue(monitor.claude_in(["Ssl+ claude --model claude-opus-5-5 --effort high"]))
        self.assertTrue(monitor.claude_in(["S /opt/claude/.local/bin/claude --continue"]))
        self.assertFalse(monitor.claude_in(["Zs [claude] <defunct>"]), "zombies are not alive")
        self.assertFalse(monitor.claude_in(["Ss tmux -f /home/lane/.tmux.lane.conf new-session -A -s lane "
                                            "/opt/prework-harness/claude-dev --model claude-opus-5-5"]))
        self.assertFalse(monitor.claude_in(["R ps -eo stat=,args=", "S sh -c pgrep -f '[c]laude'"]))
        self.assertFalse(monitor.claude_in(["S cat /home/lane/.claude/settings.json"]))

    def test_strip_enters_keeps_pastes(self):
        self.assertEqual(strip_enters(b"more\r"), (b"more", True))
        self.assertEqual(strip_enters(b"abc"), (b"abc", False))
        paste = b"\x1b[200~line one\rline two\x1b[201~"
        self.assertEqual(strip_enters(paste), (paste, False))

    def test_lane_key(self):
        self.assertEqual(monitor.lane_key("prework-PREWORK_t4sk0001-ac90-model_a"), "PREWORK_t4sk0001-ac90")
        self.assertIsNone(monitor.lane_key("postgres"))


if __name__ == "__main__":
    unittest.main()
