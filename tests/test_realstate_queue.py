"""The Home queue for real tasks: tiers, order, surfaces, one row per task, lane rows, pick and hides."""
import json
import unittest

from test_realstate_derive import RealRoot
from johto.web import data
from johto.web.domain import derive, queue

SURFACES = {"write_prompt", "details", "decide", "rate", "studio", "lane", "blocker", "log_discard", "feedback", "pick"}


FULL = {"opening_prompt": "lorem prompt", "goal": "lorem goal", "decision_points": "lorem dps", "codebase": "lorem code"}


def _task(short, stage, run=None, since="2026-10-30T10:00:00-0700", texts=None, monitor=None, slot=None, **st):
    return {"short": short, "label": f"repo · {short}", "slot": slot or {"prompt_style": "Casual prose"},
            "stage": {"stage": stage, "run": run, "since": since, **st}, "claimed_at": since,
            "texts": dict(texts) if texts is not None else {}, "changed_since_push": None, "monitor": monitor}


def _mon(run="validate-1", state="waiting", cls=None, host=None, **kw):
    """A run monitor value (lane/runmon.compute's shape) for queue tests."""
    return {"short": "x", "run": run, "run_word": queue.run_word(run), "host": {"build-box": "PC"}.get(host, "Mac"),
            "host_name": host, "up": state in ("working", "waiting", "background", "ready"), "state": state,
            "collected": state == "collected", "turns": kw.pop("turns", 2),
            "turn": {"index": 2, "ended": "end_turn", "end": "2026-10-30T10:05:00-0700"},
            "working_minutes": 12.0, "task_minutes": 12.0, "budget": 90, "budget_tone": "",
            "background": {"minutes": 0.0, "running": 0}, "turn_class": cls, "review": kw.pop("review", {"state": "none"}),
            **kw}


class BuildQueue(unittest.TestCase):
    def build(self, tasks, lane=None, **kw):
        snap = {"tasks": tasks, "lane": lane or {"state": "free"}, "blockers": [], "prefs": {"hidden": set()},
                "settings": {"wip_before_validation": 2, "wip_max": 4}, "leads": {"usable": 3, "open": 1004},
                "now": derive.ts("2026-10-30T12:00:00-0700"), **kw}
        return queue.build_queue(snap)

    def test_tiers_and_order(self):
        q, running = self.build([
            _task("p1", "prompt"), _task("d1", "details", run="calib-1"), _task("c1", "decide", run="calib-1"),
            _task("r1", "rate", run="validate-1", texts=FULL), _task("s1", "ready_to_save", run="validate-1"),
            _task("f1", "fix_qc", run="validate-1", qc={"audit_id": "a", "counts": {"pass": 11, "fail": 1}}),
            _task("u1", "ready_to_submit", run="validate-1", qc={"audit_id": "b"}),
            _task("b1", "sent_back", feedback="lorem"), _task("x1", "dropped"), _task("i1", "in_review"),
        ])
        kinds = [i["kind"] for i in q]
        # Finish what is close (nearest to submitted first), then work under way, then new work.
        self.assertEqual(kinds, ["submit", "save", "fix_qc", "log_discard", "decide", "rate", "feedback",
                                 "write_details", "write_prompt"])
        self.assertEqual([i["priority"] for i in q], [2, 2, 2, 2, 2, 2, 2, 3, 4])
        self.assertEqual([r["kind"] for r in running], ["in_review"])
        for i in q:
            self.assertIn(i["surface"], SURFACES)
            self.assertFalse(i["attention"])
        self.assertEqual([i["id"].split(":")[2] for i in q if i["kind"] == "decide"], ["calib-1"])  # run from the id
        self.assertEqual([i["id"].split(":")[2] for i in q if i["kind"] == "rate"], ["validate-1"])

    def test_each_task_once_with_its_human_step_and_machine_state(self):
        # Claude is running and the goal isn't written: one row says both.
        lane = {"state": "working", "since": "2026-10-30T10:00:00-0700",
                "run": {"short": "v1", "run": "validate-1", "kind": "validate", "mode": "interactive"}}
        q, running = self.build([_task("v1", "validating", run="validate-1", texts={"opening_prompt": "x"}),
                                 _task("p1", "prompt")], lane=lane)
        row = q[0]
        self.assertEqual((row["kind"], row["title"], row["surface"], row["priority"]), ("write_details", "Write the goal", "details", 3))
        self.assertEqual(row["machine"]["text"], "Claude working · validation 1 · Mac")
        self.assertEqual(row["machine"]["dot"], "work")
        self.assertFalse(row["attention"])
        shorts = [i["short"] for i in q + running if i["short"]]
        self.assertEqual(len(shorts), len(set(shorts)))
        self.assertNotIn("v1", [r["short"] for r in running])

    def test_a_question_takes_the_attention_signal_and_keeps_the_surface(self):
        lane = {"state": "waiting", "since": "2026-10-30T10:05:00-0700", "question": "Should I also cover the API?",
                "run": {"short": "v1", "run": "validate-1", "kind": "validate", "mode": "interactive"}}
        writing = _task("v1", "validating", run="validate-1", texts={"opening_prompt": "x"})
        q, _ = self.build([writing, _task("u1", "ready_to_submit", run="validate-1", qc={})], lane=lane)
        row = q[0]
        self.assertEqual(row["short"], "v1")
        self.assertEqual((row["title"], row["tier"], row["priority"], row["attention"], row["lane"]),
                         ("Answer Claude's question", 1, 1, True, True))
        # The writing step keeps the row's kind and surface, so the caret in the goal stays put.
        self.assertEqual((row["kind"], row["surface"], row["also"]), ("write_details", "details", "Write the goal"))
        self.assertEqual(row["lane_need"]["kind"], "lane_waiting")
        # With nothing to write, the lane step is the row.
        q, _ = self.build([_task("v1", "validating", run="validate-1", texts=FULL)], lane=lane)
        self.assertEqual((q[0]["kind"], q[0]["surface"], q[0]["tier"]), ("lane_waiting", "lane", 1))

    def test_an_ended_turn_alone_owes_nothing(self):
        lane = {"state": "waiting", "since": "2026-10-30T10:05:00-0700", "question": "Lorem summary of turn 2.",
                "run": {"short": "v1", "run": "validate-1", "kind": "validate", "mode": "interactive"}}
        t = _task("v1", "validating", run="validate-1", texts=FULL)
        q, running = self.build([t], lane=lane)
        self.assertEqual((q[0]["kind"], q[0]["title"], q[0]["priority"], q[0]["attention"]),
                         ("lane_answer", "Read Claude's answer", 3, False))
        self.assertEqual(q[0]["tier"], 5)                       # not counted in the tab title
        for cls, text in (("progress", "Claude posted progress"), ("background", "Background work running")):
            t = _task("v1", "validating", run="validate-1", texts=FULL, monitor=_mon(cls=cls))
            q, running = self.build([t], lane=lane)
            self.assertFalse([i for i in q if i["short"] == "v1"], cls)
            self.assertTrue(running[0]["machine"]["text"].startswith(text), running[0]["machine"])
        t = _task("v1", "validating", run="validate-1", texts=FULL, monitor=_mon(cls="permission"))
        self.assertEqual(self.build([t], lane=lane)[0][0]["kind"], "lane_permission")

    def test_async_turn_end_and_login_and_paste(self):
        lane = {"state": "waiting", "facts": {"turns": 1},
                "run": {"short": "v1", "run": "validate-1", "kind": "validate", "mode": "async"}}
        q, _ = self.build([_task("v1", "validating", texts=FULL)], lane=lane)
        self.assertEqual((q[0]["kind"], q[0]["priority"]), ("lane_async_done", 2))
        lane = {"state": "needs_login", "run": {"short": "v1", "run": "calib-1", "kind": "calib"}}
        self.assertEqual(self.build([_task("v1", "calibrating", texts=FULL)], lane=lane)[0][0]["kind"], "lane_login")
        lane = {"state": "ready_for_prompt", "run": {"short": "v1", "run": "calib-1", "kind": "calib"}}
        q, _ = self.build([_task("v1", "calibrating", texts=FULL)], lane=lane)
        self.assertEqual((q[0]["kind"], q[0]["attention"]), ("lane_paste", True))

    def test_working_lane_is_a_running_row(self):
        lane = {"state": "working", "run": {"short": "v1", "run": "calib-1", "kind": "calib"}}
        q, running = self.build([_task("v1", "calibrating", run="calib-1", texts=FULL)], lane=lane)
        self.assertEqual(running[0]["title"], "Claude working · calibration 1")
        self.assertTrue(running[0]["lane"])
        self.assertEqual(running[0]["machine"]["text"], "Claude working · calibration 1 · Mac")

    def test_lane_start_only_without_a_lane_service(self):
        t = _task("w1", "waiting_validate", kind="validate")
        q, running = self.build([t], lane={"state": "free"}, lane_service=False)
        start = [i for i in q if i["kind"] == "lane_start"]
        self.assertEqual(len(start), 1)
        self.assertEqual(start[0]["tier"], 2)
        self.assertEqual(start[0]["primary"]["args"], {"short": "w1", "kind": "validate"})
        self.assertEqual(start[0]["command"], "johto lane w1 validate")
        self.assertEqual([i["short"] for i in q].count("w1"), 1)
        self.assertFalse(running)
        q, running = self.build([t], lane={"state": "free", "auto_prepare": True}, lane_service=True)
        self.assertFalse([i for i in q if i["kind"] == "lane_start"])
        # The lane starts it on its own; meanwhile the goal can be written.
        self.assertEqual([(i["kind"], i["title"], i["machine"]["text"]) for i in q if i["short"] == "w1"],
                         [("write_details", "Write the goal", "Waiting for the lane")])
        t["texts"] = dict(FULL)
        q, running = self.build([t], lane={"state": "free", "auto_prepare": True}, lane_service=True)
        self.assertEqual(running[0]["kind"], "waiting_validate")

    def test_prompt_alone_is_enough_to_start_validation(self):
        t = _task("s1", "details", skipped=True, texts={"opening_prompt": "lorem"})
        q, _ = self.build([t])
        row = [i for i in q if i["short"] == "s1"][0]
        self.assertEqual((row["kind"], row["surface"], row["primary"]["action"]), ("start_validation", "lane", "lane_start"))
        self.assertEqual(row["primary"]["args"], {"short": "s1", "kind": "validate"})
        # A chosen calibration run or a kept one still asks for the details first.
        q, _ = self.build([_task("s1", "details", run="calib-1", chosen=True)])
        self.assertEqual(q[0]["kind"], "write_details")

    def test_the_prompt_leads_to_validation_unless_calibration_is_on(self):
        """Austin skips calibration: the prompt's Done names the validation run, not a calibration."""
        q, _ = self.build([_task("s1", "prompt")])
        self.assertEqual(q[0]["primary"]["label"], "Done, go to validation")
        q, _ = self.build([_task("s1", "prompt")], settings={"wip_before_validation": 2, "wip_max": 4, "calibrate_first": True})
        self.assertEqual(q[0]["primary"]["label"], "Done, queue calibration")

    def test_after_the_run_the_details_come_before_the_ratings(self):
        t = _task("r1", "rate", run="validate-1", texts={"opening_prompt": "x", "goal": "y"})
        q, _ = self.build([t])
        self.assertEqual((q[0]["kind"], q[0]["title"], q[0]["surface"], q[0]["priority"]),
                         ("write_details", "Write decision points and codebase notes", "details", 2))
        self.assertEqual(q[0]["primary"]["label"], "Done, rate validation 1")
        t["texts"] = dict(FULL)
        self.assertEqual(self.build([t])[0][0]["kind"], "rate")

    def test_a_pc_run_that_asks_is_a_row_with_its_command(self):
        mon = _mon(run="validate-2", cls="question", host="build-box", question="Which index should it use?")
        t = _task("g1", "validating", run="validate-2", texts=FULL, monitor=mon)
        q, _ = self.build([t])
        row = q[0]
        self.assertEqual((row["kind"], row["title"], row["lane"], row["attention"], row["host"]),
                         ("lane_waiting", "Answer Claude's question · PC", False, True, "PC"))
        self.assertEqual(row["command"], "johto resume g1 validate-2")   # no record of where it runs
        self.assertEqual(row["machine"]["text"], "Claude asked a question at 10:05 · PC")
        # With the run's PC folder on record, the command reaches it over ssh (`johto resume` would stop a Mac lane).
        t["meta"] = {"runs": [{"dir": "runs/validate-2", "host": "build-box",
                               "remote_dir": "/home/u/johto-lanes/g1/validate-2"}]}
        cmd = self.build([t])[0][0]["command"]
        self.assertTrue(cmd.startswith("ssh -t build-box 'cd /home/u/johto-lanes/g1/validate-2/model_a && "
                                       "./claude-dev"), cmd)
        del t["meta"]
        # Working on the PC: the machine line says where.
        t["monitor"] = _mon(run="validate-2", state="working", host="build-box")
        q, running = self.build([t])
        self.assertEqual(running[0]["machine"]["text"], "Claude working · validation 2 · PC")
        t["monitor"] = _mon(run="validate-2", state="paused", host="build-box")
        q, running = self.build([t])
        self.assertIn("Docker isn't running", running[0]["machine"]["text"])
        # The PC answered and the container is stopped: not collected yet, and nothing asked of him.
        t["monitor"] = _mon(run="validate-2", state="stopped", host="build-box")
        q, running = self.build([t])
        self.assertEqual(q, [i for i in q if i["kind"] == "pick"])
        self.assertEqual(running[0]["machine"]["text"], "Validation 2 stopped on the PC, not collected")

    def test_a_substantive_turn_that_asks_is_a_question(self):
        mon = _mon(run="validate-2", cls="substantive", host="build-box", question="Should I also migrate?")
        q, _ = self.build([_task("g1", "validating", run="validate-2", texts=FULL, monitor=mon)])
        self.assertEqual((q[0]["kind"], q[0]["attention"]), ("lane_waiting", True))
        mon = _mon(run="validate-2", cls="substantive", host="build-box")
        q, _ = self.build([_task("g1", "validating", run="validate-2", texts=FULL, monitor=mon)])
        self.assertEqual((q[0]["kind"], q[0]["attention"]), ("lane_answer", False))

    def test_priorities_question_then_close_then_under_way_then_new(self):
        lane = {"state": "waiting", "question": "Ok to drop the old table?",
                "run": {"short": "v1", "run": "validate-1", "kind": "validate", "mode": "interactive"}}
        q, _ = self.build([_task("p1", "prompt"), _task("w1", "validating", run="validate-1", texts={"opening_prompt": "x"}),
                           _task("u1", "ready_to_submit", run="validate-1", qc={}),
                           _task("v1", "validating", run="validate-1", texts=FULL)], lane=lane)
        self.assertEqual([(i["short"], i["priority"]) for i in q], [("v1", 1), ("u1", 2), ("w1", 3), ("p1", 4)])

    def test_studio_signed_out_holds_back_only_studio_rows(self):
        blocker = queue._item("blocker_studio", 0, "blocker", "Studio tab is signed out", facts=("signed_out",))
        lane = {"state": "waiting", "question": "Which one?",
                "run": {"short": "v1", "run": "validate-1", "kind": "validate", "mode": "interactive"}}
        tasks = [_task("v1", "validating", run="validate-1", texts=FULL), _task("s1", "ready_to_save", run="validate-1")]
        q, _ = self.build(tasks, lane=lane, blockers=[dict(blocker)], studio={"state": "signed_out"})
        self.assertEqual([i["kind"] for i in q][:3], ["lane_waiting", "blocker_studio", "save"])
        self.assertEqual(q[2]["blocked"], "Studio is signed out")
        self.assertEqual(q[1]["blocks"], ["s1"])
        # Nothing needs Studio: its row is quiet and last.
        q, _ = self.build([_task("p1", "prompt", texts={"opening_prompt": "x"})], blockers=[dict(blocker)],
                          studio={"state": "signed_out"}, settings={"wip_before_validation": 0, "wip_max": 0})
        self.assertEqual([(i["kind"], i["attention"]) for i in q], [("write_prompt", False), ("blocker_studio", False)])

    def test_review_badges_are_quiet(self):
        rv = {"state": "ready", "turn": 3, "text": "Review ready for turn 3", "at": "2026-10-30T11:00:00-0700"}
        t = _task("r1", "rate", run="validate-1", texts=FULL, monitor=_mon(state="collected", review=rv))
        q, _ = self.build([t])
        self.assertEqual(q[0]["badges"][0]["text"], "Review ready for turn 3")
        self.assertFalse(q[0]["attention"])
        self.assertEqual(q[0]["machine"]["text"], "Validation 1 collected · 12 min · Mac")
        t = _task("i1", "in_review", monitor=_mon(state="collected", review=rv))
        self.assertEqual(self.build([t])[1][0]["badges"], [])

    def test_overview_rows_follow_the_queue(self):
        lane = {"state": "working", "run": {"short": "v1", "run": "validate-1", "kind": "validate"}}
        q, running = self.build([_task("v1", "validating", run="validate-1", texts={"opening_prompt": "x"}),
                                 _task("u1", "ready_to_submit", run="validate-1", qc={}), _task("i1", "in_review")], lane=lane)
        rows = queue.overview_rows(q, running)
        self.assertEqual([r["short"] for r in rows], ["u1", "v1", None, "i1"])     # None: Pick the next problem
        self.assertEqual(set(rows[0]), {"short", "title", "needs_you", "machine", "action", "command", "priority",
                                        "attention", "blocked", "badges"})
        self.assertEqual((rows[1]["needs_you"], rows[1]["machine"]), ("Write the goal", "Claude working · validation 1 · Mac"))
        self.assertEqual((rows[3]["needs_you"], rows[3]["priority"], rows[3]["machine"]), ("", None, "In review since 10:00"))
        # Without a submit time the clock is Studio's last poll, and the row says so.
        q, running = self.build([_task("i1", "in_review", checked=True)])
        self.assertEqual(queue.overview_rows(q, running)[-1]["machine"], "In review (Studio checked 10:00)")

    def test_pick_follows_work_in_progress(self):
        q, _ = self.build([_task("p1", "prompt")])
        pick = [i for i in q if i["kind"] == "pick"]
        self.assertEqual(pick[0]["id"], "pick")
        self.assertEqual(pick[0]["tier"], 6)
        self.assertEqual(pick[0]["label"], "3 leads fit · 1,004 open")
        q, _ = self.build([_task("p1", "prompt"), _task("p2", "details")])
        self.assertFalse([i for i in q if i["kind"] == "pick"])      # two tasks before validation
        q, _ = self.build([_task("i1", "in_review")])
        self.assertEqual([i for i in q if i["kind"] == "pick"][0]["tier"], 2)   # nothing else to do

    def test_hidden_rows_come_back_when_state_changes(self):
        t = _task("c1", "decide", run="calib-1")
        q, _ = self.build([t])
        hid = q[0]["id"]
        q, _ = self.build([t], prefs={"hidden": {hid}})
        self.assertNotIn(hid, [i["id"] for i in q])
        q, _ = self.build([_task("c1", "decide", run="calib-2")], prefs={"hidden": {hid}})
        self.assertEqual(q[0]["kind"], "decide")

    def test_reasons_are_facts_with_clock_times(self):
        q, _ = self.build([_task("c1", "decide", run="calib-1", since="2026-10-30T14:31:00-0700")])
        self.assertRegex(q[0]["reason"], r"^Calibration 1 was collected (at \d\d:\d\d|yesterday at \d\d:\d\d|on \w{3} \d+ at \d\d:\d\d)$")
        self.assertNotIn("ago", q[0]["reason"])

    def test_writing_order_least_work_first(self):
        a = _task("a1", "prompt", texts={"opening_prompt": "lorem draft"})
        b = _task("b1", "details", run="calib-1")
        b["texts"] = {"opening_prompt": "lorem"}
        q, _ = self.build([a, b])
        self.assertEqual([i["short"] for i in q if i["priority"] == 3], ["b1", "a1"])
        self.assertEqual(self.build([_task("a1", "prompt")])[0][0]["priority"], 4)   # an empty prompt is new work

    def test_collect_failed_is_a_blocker(self):
        q, _ = self.build([_task("x1", "collect_failed", run="validate-1")])
        self.assertEqual((q[0]["tier"], q[0]["surface"], q[0]["kind"]), (0, "blocker", "collect_failed"))


class QueueFromFiles(RealRoot):
    def test_fixture_queue(self):
        snap = data.real_snapshot()
        kinds = [i["kind"] for i in snap["queue"]]
        self.assertEqual(kinds[0], "submit")
        # The live run ended its turn with a summary: a quiet row, not the attention signal.
        live = [i for i in snap["queue"] if i["short"] == "llive001"]
        self.assertEqual([(i["kind"], i["attention"]) for i in live], [("lane_answer", False)])
        self.assertEqual(live[0]["machine"]["text"].split(" · ")[0][:14], "Claude answere")
        for k in ("decide", "rate", "log_discard", "feedback", "fix_qc", "submit", "save", "write_details", "write_prompt"):
            self.assertIn(k, kinds)
        self.assertEqual([r["kind"] for r in snap["running"]], ["in_review"])
        ids = [i["id"] for i in snap["queue"]]
        self.assertEqual(ids, [i["id"] for i in data.real_snapshot()["queue"]])   # stable between passes

    def test_fixture_question_is_next(self):
        tx = next((self.home / "cc-config").glob("*/projects/*/*.jsonl"))
        with open(tx, "a") as f:
            f.write(json.dumps({"type": "assistant", "isSidechain": False, "timestamp": "2099-01-01T00:00:00Z",
                                "message": {"model": "claude-opus-5-5", "stop_reason": "end_turn",
                                            "content": [{"type": "text", "text": "Done. Should I also migrate the old rows?"}]}}) + "\n")
        q = data.real_snapshot()["queue"]
        self.assertEqual((q[0]["short"], q[0]["kind"], q[0]["attention"]), ("llive001", "lane_waiting", True))

    def test_tasks_carry_the_submission_run(self):
        s = {t["short"]: t for t in data.real_snapshot()["summaries"]}
        self.assertEqual(s["drate001"]["submission_run"], "validate-1")
        self.assertIsNone(s["drate001"]["submission_run_chosen"])
        self.assertEqual((s["echosen1"]["submission_run"], s["echosen1"]["submission_run_chosen"]), ("calib-1", "calib-1"))
        self.assertIsNone(s["aprompt1"]["submission_run"])
        # A validation run that isn't collected yet isn't the submission run.
        self.assertEqual(s["llive001"]["submission_run"], None)
        d = data.real_task(self.root / "tasks" / "drate001")
        self.assertEqual(d["submission_run"], "validate-1")
        self.assertEqual(d["summary"]["submission_run"], "validate-1")

    def test_overview_matches_home(self):
        snap = data.real_snapshot()
        rows = data.overview()
        self.assertEqual([r["short"] for r in rows if r["priority"]], [i["short"] for i in snap["queue"]])

    def test_hidden_from_prefs_file(self):
        first = data.real_snapshot()["queue"][1]["id"]
        (self.root / "data" / "web-item-prefs.json").write_text(json.dumps({"hidden": {first: "now"}}))
        self.assertNotIn(first, [i["id"] for i in data.real_snapshot()["queue"]])

    def test_blockers_docker_and_studio(self):
        self.put("aprompt1", ready={"kind": "calib", "at": "2099-01-01T00:00:00-0700"})
        with unittest.mock.patch.object(data, "docker_ok", lambda: False), \
                unittest.mock.patch.object(data, "studio_state", lambda: {"state": "signed_out", "pending": 2}):
            q = data.real_snapshot()["queue"]
        kinds = [i["kind"] for i in q if i["tier"] == 0]
        self.assertIn("blocker_docker", kinds)
        self.assertIn("blocker_studio", kinds)


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
