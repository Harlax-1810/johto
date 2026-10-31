"""The Home queue for real tasks: tiers, order, surfaces, one row per task, lane rows, pick and hides."""
import json
import unittest

from test_realstate_derive import RealRoot
from johto.web import data
from johto.web.domain import queue

SURFACES = {"write_prompt", "details", "decide", "rate", "studio", "lane", "blocker", "log_discard", "feedback", "pick"}


def _task(short, stage, run=None, since="2026-10-30T10:00:00-0700", **st):
    return {"short": short, "label": f"repo · {short}", "slot": {"prompt_style": "Casual prose"},
            "stage": {"stage": stage, "run": run, "since": since, **st}, "claimed_at": since,
            "texts": {}, "changed_since_push": None}


class BuildQueue(unittest.TestCase):
    def build(self, tasks, lane=None, **kw):
        snap = {"tasks": tasks, "lane": lane or {"state": "free"}, "blockers": [], "prefs": {"hidden": set()},
                "settings": {"wip_before_validation": 2, "wip_max": 4}, "leads": {"usable": 3, "open": 1004}, **kw}
        return queue.build_queue(snap)

    def test_tiers_and_order(self):
        q, running = self.build([
            _task("p1", "prompt"), _task("d1", "details", run="calib-1"), _task("c1", "decide", run="calib-1"),
            _task("r1", "rate", run="validate-1"), _task("s1", "ready_to_save", run="validate-1"),
            _task("f1", "fix_qc", run="validate-1", qc={"audit_id": "a", "counts": {"pass": 11, "fail": 1}}),
            _task("u1", "ready_to_submit", run="validate-1", qc={"audit_id": "b"}),
            _task("b1", "sent_back", feedback="lorem"), _task("x1", "dropped"), _task("i1", "in_review"),
        ])
        kinds = [i["kind"] for i in q]
        self.assertEqual(kinds, ["decide", "rate", "log_discard", "feedback", "fix_qc", "submit", "save",
                                 "write_details", "write_prompt"])
        self.assertEqual([r["kind"] for r in running], ["in_review"])
        for i in q:
            self.assertIn(i["surface"], SURFACES)
        self.assertEqual(q[0]["id"].split(":")[2], "calib-1")     # the front end reads the run from the id
        self.assertEqual([i["id"].split(":")[2] for i in q if i["kind"] == "rate"], ["validate-1"])

    def test_each_task_once_and_lane_rows_first(self):
        lane = {"state": "waiting", "since": "2026-10-30T10:00:00-0700",
                "run": {"short": "v1", "run": "validate-1", "kind": "validate", "mode": "interactive"}}
        q, running = self.build([_task("v1", "validating", run="validate-1"), _task("p1", "prompt")], lane=lane)
        self.assertEqual(q[0]["kind"], "lane_waiting")
        self.assertEqual(q[0]["tier"], 1)
        self.assertEqual(q[0]["surface"], "lane")
        shorts = [i["short"] for i in q + running if i["short"]]
        self.assertEqual(len(shorts), len(set(shorts)))
        self.assertNotIn("v1", [r["short"] for r in running])

    def test_async_turn_end_and_login_and_paste(self):
        lane = {"state": "waiting", "facts": {"turns": 1},
                "run": {"short": "v1", "run": "validate-1", "kind": "validate", "mode": "async"}}
        self.assertEqual(self.build([_task("v1", "validating")], lane=lane)[0][0]["kind"], "lane_async_done")
        lane = {"state": "needs_login", "run": {"short": "v1", "run": "calib-1", "kind": "calib"}}
        self.assertEqual(self.build([_task("v1", "calibrating")], lane=lane)[0][0]["kind"], "lane_login")
        lane = {"state": "ready_for_prompt", "run": {"short": "v1", "run": "calib-1", "kind": "calib"}}
        self.assertEqual(self.build([_task("v1", "calibrating")], lane=lane)[0][0]["kind"], "lane_paste")

    def test_working_lane_is_a_running_row(self):
        lane = {"state": "working", "run": {"short": "v1", "run": "calib-1", "kind": "calib"}}
        q, running = self.build([_task("v1", "calibrating", run="calib-1")], lane=lane)
        self.assertEqual(running[0]["title"], "Claude working · calibration 1")
        self.assertTrue(running[0]["lane"])

    def test_lane_start_only_without_a_lane_service(self):
        t = _task("w1", "waiting_validate", kind="validate")
        q, running = self.build([t], lane={"state": "free"}, lane_service=False)
        start = [i for i in q if i["kind"] == "lane_start"]
        self.assertEqual(len(start), 1)
        self.assertEqual(start[0]["tier"], 2)
        self.assertEqual(start[0]["primary"]["args"], {"short": "w1", "kind": "validate"})
        self.assertFalse(running)
        q, running = self.build([t], lane={"state": "free", "auto_prepare": True}, lane_service=True)
        self.assertFalse([i for i in q if i["kind"] == "lane_start"])
        self.assertEqual(running[0]["kind"], "waiting_validate")

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
        self.assertRegex(q[0]["reason"], r"^Calibration 1 was collected at (\d\d:\d\d|yesterday \d\d:\d\d|\w{3} \d+ \d\d:\d\d)$")
        self.assertNotIn("ago", q[0]["reason"])

    def test_writing_order_least_work_first(self):
        a = _task("a1", "prompt")
        b = _task("b1", "details")
        b["texts"] = {"opening_prompt": "lorem"}
        q, _ = self.build([a, b])
        self.assertEqual([i["short"] for i in q if i["tier"] == 5], ["b1", "a1"])

    def test_collect_failed_is_a_blocker(self):
        q, _ = self.build([_task("x1", "collect_failed", run="validate-1")])
        self.assertEqual((q[0]["tier"], q[0]["surface"], q[0]["kind"]), (0, "blocker", "collect_failed"))


class QueueFromFiles(RealRoot):
    def test_fixture_queue(self):
        snap = data.real_snapshot()
        kinds = [i["kind"] for i in snap["queue"]]
        self.assertEqual(kinds[0], "lane_waiting")
        for k in ("decide", "rate", "log_discard", "feedback", "fix_qc", "submit", "save", "write_details", "write_prompt"):
            self.assertIn(k, kinds)
        self.assertEqual([r["kind"] for r in snap["running"]], ["in_review"])
        ids = [i["id"] for i in snap["queue"]]
        self.assertEqual(ids, [i["id"] for i in data.real_snapshot()["queue"]])   # stable between passes

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
