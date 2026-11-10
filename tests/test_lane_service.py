"""LaneService without Docker (monitor calls stubbed): setup failure, refusals (foreign lane, busy lane, typing
in the wrong state, a draft in the box), the preparing run name, the lane queue order, auto-prepare off by
default, and the async lock flag."""
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from johto import config
from johto.web.lane import monitor, service

PANES = Path(__file__).parent / "fixtures" / "panes"


def task(root: Path, short: str, mode: str = "Interactive/pairing", ready: dict | None = None) -> Path:
    d = root / "tasks" / short
    (d / "runs").mkdir(parents=True)
    meta = {"task_id": f"task_{short}", "task_name": f"Task {short}", "label": f"repo · {short}",
            "slot": {"interaction_mode": mode}, "repo": {"full_name": "o/repo"}, "runs": []}
    if ready:
        meta["ready"] = ready
    (d / "task.json").write_text(json.dumps(meta))
    (d / "submission.md").write_text("## Opening Prompt\nthe prompt\n")
    return d


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="johto-lane-svc-"))
        self.saved = {k: getattr(config, k) for k in ("ROOT", "DATA", "TASKS", "PREWORK_HOME")}
        config.ROOT, config.DATA, config.TASKS = self.tmp, self.tmp / "data", self.tmp / "tasks"
        config.PREWORK_HOME = self.tmp / "prework"
        config.DATA.mkdir(parents=True)
        self.saved_prefix, monitor.PREFIX = monitor.PREFIX, "johtotest-"
        self.containers: list[str] = []
        self.looks: dict[str, dict] = {}
        self.patches = [mock.patch.object(monitor, "running_containers", lambda: list(self.containers)),
                        mock.patch.object(monitor, "look", lambda c: self.looks.get(c, {"ok": True, "alive": False,
                                                                                        "session": False, "pane": None})),
                        mock.patch.object(monitor, "session_alive", lambda c: False),
                        mock.patch.object(monitor, "stop", lambda c: True)]
        for p in self.patches:
            p.start()
        self.svc = service.LaneService()
        self.svc._ensure_loop()
        self.svc._auto_override = False          # tests turn auto-prepare on themselves

        def no_real_setup(d, kind, log):
            raise AssertionError("a test reached the real lane setup")
        self.svc.setup = no_real_setup
        self.svc.gate = lambda d, kind: None     # the prompt checks have their own tests (test_gates)

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        for k, v in self.saved.items():
            setattr(config, k, v)
        monitor.PREFIX = self.saved_prefix
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lane_run(self, d: Path, run: str = "calib-1", transcript: list | None = None) -> str:
        key = f"PREWORK_{d.name}-abc"
        c = f"johtotest-{key}-model_a"
        rd = d / "runs" / run
        rd.mkdir(parents=True)
        (rd / "prework-manifest.json").write_text(json.dumps({"lane": {"container": c, "workspace": "/w"}}))
        meta = json.loads((d / "task.json").read_text())
        meta["runs"].append({"kind": run.split("-")[0], "dir": f"runs/{run}", "container": c,
                             "created_at": "2026-10-30T20:00:00-0700"})
        (d / "task.json").write_text(json.dumps(meta))
        cc = config.PREWORK_HOME / "cc-config" / f"{key}_model_a"
        (cc / "projects" / "-w").mkdir(parents=True)
        if transcript is not None:
            (cc / "projects" / "-w" / "s.jsonl").write_text("".join(json.dumps(e) + "\n" for e in transcript))
        self.containers.append(c)
        return c


HUMAN = {"type": "user", "timestamp": "2026-10-30T20:00:00.000Z", "message": {"role": "user", "content": "go"}}
END = {"type": "assistant", "timestamp": "2026-10-30T20:01:00.000Z",
       "message": {"model": "claude-opus-5-5", "stop_reason": "end_turn", "content": [{"type": "text", "text": "Ok?"}]}}


class StartTests(Base):
    async def test_setup_failure_becomes_setup_failed_with_the_error(self):
        d = task(self.tmp, "zz10")

        def broken(d, kind, log):
            (d / "runs" / "calib-1").mkdir()
            log("[ERROR] Lane ports 34000-34019 are in use")
            raise RuntimeError("prework setup failed (exit 1). Folder: runs/calib-1")
        self.svc.setup = broken
        job = await self.svc.start_lane("zz10", "calib")
        self.assertEqual((job["kind"], job["target"], job["params"]["run"]), ("lane_prepare", "t:zz10", "calib-1"))
        st = await self.svc.refresh()
        for _ in range(40):
            if st["state"] == "setup_failed":
                break
            await asyncio.sleep(0.1)
            st = await self.svc.refresh()
        self.assertEqual(st["state"], "setup_failed")
        self.assertIn("prework setup failed", st["evidence"])
        self.assertEqual((st["run"]["short"], st["run"]["run"]), ("zz10", "calib-1"))

    async def test_prompt_checks_refuse_before_any_setup(self):
        from johto import actions
        task(self.tmp, "zz15")
        seen = []

        def gate(d, kind):
            seen.append(kind)
            if kind == "validate":
                raise actions.ActionError("same_prompt", "validate-1 already ran this prompt.", {"run": "validate-1"})
        self.svc.gate = gate
        self.svc.setup = lambda *a: self.fail("setup ran after a refusal")
        with self.assertRaises(service.LaneError) as e:
            await self.svc.start_lane("zz15", "validate")
        self.assertEqual((e.exception.status, e.exception.code), (409, "same_prompt"))
        self.assertEqual(e.exception.details, {"run": "validate-1"})
        self.assertFalse((self.tmp / "tasks" / "zz15" / "runs" / "validate-1").exists())
        self.assertEqual(seen, ["validate"])

    async def test_refuses_a_foreign_lane(self):
        task(self.tmp, "zz11")
        self.containers.append("johtotest-someone-else-model_a")
        with self.assertRaises(service.LaneError) as e:
            await self.svc.start_lane("zz11", "calib")
        self.assertEqual((e.exception.status, e.exception.code), (409, "lane_busy"))
        self.assertEqual(e.exception.details["foreign"]["container"], "johtotest-someone-else-model_a")

    async def test_refuses_while_another_claude_is_alive(self):
        d = task(self.tmp, "zz12")
        task(self.tmp, "zz13")
        c = self.lane_run(d, transcript=[HUMAN])
        self.looks[c] = {"ok": True, "alive": True, "session": True, "pane": (PANES / "working_long_test.txt").read_text()}
        with self.assertRaises(service.LaneError) as e:
            await self.svc.start_lane("zz13", "validate")
        self.assertEqual(e.exception.code, "lane_busy")
        self.assertEqual(e.exception.details, {"short": "zz12", "run": "calib-1"})

    async def test_bad_requests(self):
        with self.assertRaises(service.LaneError) as e:
            await self.svc.start_lane("zz99", "calib")
        self.assertEqual(e.exception.status, 404)
        task(self.tmp, "zz14")
        with self.assertRaises(service.LaneError) as e:
            await self.svc.start_lane("zz14", "other")
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(service.LaneError) as e:
            await self.svc.start_lane("../zz14", "calib")
        self.assertEqual(e.exception.status, 404)

    async def test_collected_lane_is_stopped_and_unstarted_one_reused(self):
        d = task(self.tmp, "zz15")
        old = self.lane_run(d, "calib-1", transcript=[HUMAN, END])
        meta = json.loads((d / "task.json").read_text())
        meta["runs"][0]["collected_at"] = "2099-01-01T00:00:00-0700"
        (d / "task.json").write_text(json.dumps(meta))
        p = await asyncio.to_thread(self.svc._probe_now)
        reuse, uncollected, idle = self.svc._plan(p, "zz15", "calib")
        self.assertEqual((reuse, uncollected, idle), (None, [], [old]))
        d2 = task(self.tmp, "zz16")
        fresh = self.lane_run(d2, "validate-1", transcript=None)
        p = await asyncio.to_thread(self.svc._probe_now)
        reuse, uncollected, idle = self.svc._plan(p, "zz16", "validate")
        self.assertEqual(reuse[0], fresh, "a lane set up but never prompted is used as is")
        self.assertEqual(idle, [old])


class TypingTests(Base):
    async def lane_in(self, pane: str, transcript: list, alive=True, creds=True, mode="Interactive/pairing"):
        d = task(self.tmp, "zz20", mode=mode)
        c = self.lane_run(d, transcript=transcript)
        if creds:
            cc = config.PREWORK_HOME / "cc-config" / "PREWORK_zz20-abc_model_a"
            (cc / ".credentials.json").write_text("{}")
        self.looks[c] = {"ok": True, "alive": alive, "session": True, "pane": (PANES / f"{pane}.txt").read_text()}
        return d, c

    async def test_prompt_only_when_ready(self):
        await self.lane_in("not_logged_in", [], creds=False)
        with self.assertRaises(service.LaneError) as e:
            await self.svc.type("prompt")
        self.assertEqual((e.exception.code, e.exception.details["state"]), ("lane_state", "needs_login"))

    async def test_prompt_refused_over_a_draft_and_after_turn_one(self):
        await self.lane_in("pasted_not_logged_in", [], creds=True)
        st = await self.svc.refresh()
        self.assertEqual(st["state"], "needs_login", "the footer still says Not logged in")

    async def test_exit_refused_while_working_or_with_a_draft(self):
        await self.lane_in("working_long_test", [HUMAN])
        with self.assertRaises(service.LaneError) as e:
            await self.svc.end()
        self.assertEqual(e.exception.details["state"], "working")

    async def test_exit_refused_with_a_draft(self):
        await self.lane_in("waiting_typed_reply", [HUMAN, END])
        self.svc._held = 1.0          # the 3 s hold has passed
        st = await self.svc.refresh()
        self.assertEqual(st["state"], "waiting")
        self.assertEqual(st["input"], {"visible": True, "empty": False})
        with self.assertRaises(service.LaneError) as e:
            await self.svc.type("exit")
        self.assertIn("text in it", e.exception.message)

    async def test_login_refused_on_the_login_screen(self):
        await self.lane_in("login_method", [], creds=False)
        with self.assertRaises(service.LaneError) as e:
            await self.svc.type("login")
        self.assertIn("already open", e.exception.message)

    async def test_unknown_what(self):
        with self.assertRaises(service.LaneError) as e:
            await self.svc.type("hello")
        self.assertEqual(e.exception.status, 400)

    async def test_async_slot_locks_after_the_first_prompt_and_unlock_logs(self):
        d, c = await self.lane_in("working_long_test", [HUMAN], mode="Async/autonomous")
        st = await self.svc.refresh()
        self.assertTrue(st["input_locked"])
        await self.svc.unlock()
        self.assertFalse(self.svc.state()["input_locked"])
        log = (d / "app" / "authorship.jsonl").read_text()
        self.assertIn('"event": "unlock_input"', log)


class CollectOnExitTests(Base):
    async def test_collects_right_after_claude_exits_when_the_watcher_runs(self):
        from johto.web import lane_watch
        d = task(self.tmp, "zz40")
        c = self.lane_run(d, transcript=[HUMAN, END])
        self.looks[c] = {"ok": True, "alive": True, "session": True,
                         "pane": (PANES / "waiting_question.txt").read_text()}
        await self.svc.refresh()
        calls = []
        alive = mock.Mock(is_alive=lambda: True)
        with mock.patch.object(lane_watch, "_THREAD", alive), \
                mock.patch.object(lane_watch, "collect_run", lambda *a: calls.append(a) or {"phase": "packing"}):
            self.looks[c] = {"ok": True, "alive": False, "session": False, "pane": None}
            st = await self.svc.refresh()
            self.assertEqual(st["state"], "exited")
            await asyncio.sleep(3.6)
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0][0].name, calls[0][1], calls[0][3]), ("zz40", "calib-1", "background"))

    async def test_no_collect_from_the_service_when_the_watcher_is_off(self):
        from johto.web import lane_watch
        d = task(self.tmp, "zz41")
        c = self.lane_run(d, transcript=[HUMAN, END])
        self.looks[c] = {"ok": True, "alive": True, "session": True, "pane": None}
        await self.svc.refresh()
        calls = []
        with mock.patch.object(lane_watch, "_THREAD", None), \
                mock.patch.object(lane_watch, "collect_run", lambda *a: calls.append(a)):
            self.looks[c] = {"ok": True, "alive": False, "session": False, "pane": None}
            self.assertEqual((await self.svc.refresh())["state"], "exited")
            await asyncio.sleep(3.3)
        self.assertEqual(calls, [])


class QueueTests(Base):
    async def test_validation_before_calibration_then_oldest(self):
        task(self.tmp, "zz30", ready={"kind": "calib", "at": "2026-10-30T10:00:00-0700"})
        task(self.tmp, "zz31", ready={"kind": "validate", "at": "2026-10-30T12:00:00-0700"})
        task(self.tmp, "zz32", ready={"kind": "calib", "at": "2026-10-30T09:00:00-0700"})
        st = await self.svc.refresh()
        self.assertEqual([(q["short"], q["kind"]) for q in st["queue"]],
                         [("zz31", "validate"), ("zz32", "calib"), ("zz30", "calib")])
        self.assertFalse(st["auto_prepare"])
        self.assertIsNone(self.svc._prep_job(), "nothing starts while auto-prepare is off")

    async def test_auto_prepare_follows_settings(self):
        self.svc._auto_override = None
        (config.DATA / "web-settings.json").write_text(json.dumps({"auto_prepare_lane": False}))
        self.assertFalse(self.svc.auto_prepare)
        (config.DATA / "web-settings.json").write_text(json.dumps({"auto_prepare_lane": True}))
        self.assertTrue(self.svc.auto_prepare)
        st = await self.svc.set_auto_prepare(False)
        self.assertFalse(st["auto_prepare"], "the route wins for this server's life")

    async def test_auto_prepare_starts_the_head_of_the_queue(self):
        task(self.tmp, "zz33", ready={"kind": "calib", "at": "2026-10-30T09:00:00-0700"})
        task(self.tmp, "zz34", ready={"kind": "validate", "at": "2026-10-30T12:00:00-0700"})
        calls = []

        def fake_setup(d, kind, log):
            calls.append((d.name, kind))
            run_dir = d / "runs" / f"{kind}-1"
            (run_dir / "model_a").mkdir(parents=True)
            cd = run_dir / "model_a" / "claude-dev"
            cd.write_text("#!/bin/sh\nexit 0\n")
            cd.chmod(0o755)
            return run_dir
        self.svc.setup = fake_setup
        self.svc._auto_override = True
        await self.svc.refresh()
        for _ in range(50):
            if calls and not self.svc._prep_job():
                break
            await asyncio.sleep(0.1)
        self.assertEqual(calls, [("zz34", "validate")], "validation runs go first")
        self.assertNotIn("ready", json.loads((self.tmp / "tasks" / "zz34" / "task.json").read_text()))
        if self.svc.pty:
            self.svc.pty.kill()

    async def test_state_shape(self):
        st = await self.svc.refresh()
        for k in ("state", "rate_limited", "since", "run", "pty", "question", "evidence", "facts", "input_locked",
                  "queue", "auto_prepare", "foreign", "login_url"):
            self.assertIn(k, st)
        self.assertEqual(st["state"], "free")


if __name__ == "__main__":
    unittest.main()
