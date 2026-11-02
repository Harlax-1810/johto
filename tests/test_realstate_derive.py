"""Real-mode stage derivation: one synthetic folder per stage, plus edge cases of the pure rules."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_realstate_fixtures as fx
from johto import config, lanes
from johto.web import data
from johto.web.domain import derive

LIVE = "prework-PREWORK_llive001-livefake-model_a"


class RealRoot(unittest.TestCase):
    """A temp JOHTO root with the fixture tasks; Docker, the lane monitor and leads are stubbed."""
    running = {LIVE}
    alive = {LIVE}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "prework"
        (self.root / "data").mkdir()
        (self.root / "data" / "slots.json").write_text(json.dumps({"synced_at": None, "groups": []}))
        self.patches = [
            mock.patch.object(config, "ROOT", self.root), mock.patch.object(config, "DATA", self.root / "data"),
            mock.patch.object(config, "TASKS", self.root / "tasks"), mock.patch.object(lanes, "HOME", self.home),
            mock.patch.object(data, "running_containers", lambda: set(self.running)),
            mock.patch.object(data, "docker_ok", lambda: True),
            mock.patch.object(data, "claude_alive", lambda c: c in self.alive),
            mock.patch.object(data, "service_lane", lambda: None),
            mock.patch.object(data, "ranked_leads", lambda: {"leads": [], "set_aside": [], "claimed": [], "combos": []}),
            mock.patch.object(data, "jobs_snapshot", lambda: list(self.jobs)),
            mock.patch.object(data, "studio_state", lambda: {"state": "ok", "pending": 0, "last_ok_at": None}),
        ]
        self.jobs = []
        for p in self.patches:
            p.start()
        data._LAST.update(at=0.0, lane=None)
        self.expected = fx.build(self.root, self.home)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        data._LAST.update(at=0.0, lane=None)
        self._tmp.cleanup()

    def meta(self, short):
        return json.loads((self.root / "tasks" / short / "task.json").read_text())

    def put(self, short, **kw):
        p = self.root / "tasks" / short / "task.json"
        m = json.loads(p.read_text())
        for k, v in kw.items():
            if v is None:
                m.pop(k, None)
            else:
                m[k] = v
        p.write_text(json.dumps(m, indent=1))

    def stages(self):
        return {s["short"]: s["stage"]["stage"] for s in data.real_snapshot()["states"]}


class StageFromFiles(RealRoot):
    def test_every_fixture_derives_its_stage(self):
        got = self.stages()
        for short, want in self.expected.items():
            self.assertEqual(got.get(short), want, short)

    def test_chosen_calibration_waits_for_details_done(self):
        self.put("echosen1", details_ready=None)
        self.assertEqual(self.stages()["echosen1"], "details")

    def test_rate_waits_for_done_even_when_complete(self):
        self.put("fsave001", rated=None)
        self.assertEqual(self.stages()["fsave001"], "rate")

    def test_decide_then_keep_rework_drop(self):
        self.assertEqual(self.stages()["bdecide1"], "decide")
        self.put("bdecide1", decisions={"calib-1": {"decision": "keep", "at": fx.iso(1)}})
        self.assertEqual(self.stages()["bdecide1"], "details")
        self.put("bdecide1", decisions={"calib-1": {"decision": "rework", "at": fx.iso(1)}})
        self.assertEqual(self.stages()["bdecide1"], "prompt")
        self.put("bdecide1", decisions={"calib-1": {"decision": "drop", "at": fx.iso(1)}})
        self.assertEqual(self.stages()["bdecide1"], "dropped")

    def test_ready_queues_and_is_used_by_the_next_run(self):
        self.put("aprompt1", ready={"kind": "calib", "at": fx.iso(1)})
        self.assertEqual(self.stages()["aprompt1"], "waiting_calib")
        # A calibration created after the ready used it: the task is no longer queued.
        self.put("bdecide1", ready={"kind": "calib", "at": fx.iso(500)})
        self.assertEqual(self.stages()["bdecide1"], "decide")

    def test_lane_states_move_the_live_run(self):
        self.assertEqual(self.stages()["llive001"], "validating")
        self.alive = set()                              # Claude exited, container up: the watcher collects
        self.assertEqual(self.stages()["llive001"], "collecting")
        self.running = set()                            # container gone, never collected
        self.assertEqual(self.stages()["llive001"], "collect_failed")
        self.jobs = [{"id": "j", "kind": "lane_collect", "target": "t:llive001", "state": "running"}]
        self.assertEqual(self.stages()["llive001"], "collecting")

    def test_discard_logged_and_unclaimed(self):
        self.put("jdrop001", discard={**self.meta("jdrop001")["discard"], "logged_at": fx.iso(1)})
        self.assertEqual(self.stages()["jdrop001"], "discarded")
        self.put("aprompt1", unclaimed_at=fx.iso(1))
        self.assertEqual(self.stages()["aprompt1"], "unclaimed")

    def test_studio_statuses(self):
        self.put("kreview1", studio={"status": "Accepted"})
        self.assertEqual(self.stages()["kreview1"], "accepted")
        self.put("kreview1", studio={"status": "Rejected", "feedback": "x"})
        self.assertEqual(self.stages()["kreview1"], "rejected")

    def test_changing_a_field_after_qc_needs_a_new_save(self):
        sub = self.root / "tasks" / "gfixqc01" / "submission.md"
        sub.write_text(sub.read_text().replace(fx.GOAL, fx.GOAL + " Lorem."))
        self.assertEqual(self.stages()["gfixqc01"], "ready_to_save")
        self.assertEqual(self.stages()["hsubmit1"], "ready_to_submit")

    def test_jobs_claim_save_submit(self):
        self.jobs = [{"id": "a", "kind": "save", "target": "t:fsave001", "state": "running"},
                     {"id": "b", "kind": "submit", "target": "t:hsubmit1", "state": "queued"}]
        got = self.stages()
        self.assertEqual(got["fsave001"], "saving")
        self.assertEqual(got["hsubmit1"], "submitting")

    def test_detail_has_runs_facts_moments_notes_and_lock(self):
        notes = self.root / "tasks" / "bdecide1" / "app"
        notes.mkdir()
        (notes / "notes-calib-1.jsonl").write_text(
            json.dumps({"id": "n1", "at": fx.iso(3), "t_run_s": 90, "turn": 1, "text": "lorem note"}) + "\n" +
            json.dumps({"id": "n2", "at": fx.iso(2), "t_run_s": 30, "turn": 1, "text": "gone"}) + "\n" +
            json.dumps({"id": "n2", "deleted": True}) + "\n")
        d = data.real_task(self.root / "tasks" / "bdecide1")
        self.assertEqual(d["summary"]["stage"], "decide")
        run = d["runs"][0]
        self.assertEqual(run["state"], "collected")
        self.assertEqual(run["facts"]["turns"], 1)
        self.assertTrue(run["facts"]["tests"]["last_ok"])
        self.assertTrue(run["facts"]["tests"]["after_last_edit"])
        self.assertEqual([x["path"] for x in run["facts"]["diffstat"]], ["src/backend/file0.ts", "src/backend/tests/file0.test.ts"])
        self.assertEqual(run["recommend"]["decision"], "drop")
        self.assertTrue(any(m["category"] == "speed" and m["level"] == "warn" for m in run["moments"]))
        self.assertEqual([n["text"] for n in run["notes"]], ["lorem note"])
        self.assertEqual(d["sections"]["opening_prompt"]["locked"], {"reason": "sent in calibration 1"})
        self.assertTrue(run["has_diff"])
        self.assertNotIn("_tpath", run)
        self.assertIsInstance(d["checklist"], list)
        self.assertEqual(d["studio"]["status"], "Writing")

    def test_rework_and_unlock_lift_the_prompt_lock(self):
        self.put("bdecide1", decisions={"calib-1": {"decision": "rework", "at": fx.iso(0)}})
        self.assertIsNone(data.real_task(self.root / "tasks" / "bdecide1")["sections"]["opening_prompt"]["locked"])
        self.put("bdecide1", decisions=None, prompt_unlocked_at=fx.iso(0))
        self.assertIsNone(data.real_task(self.root / "tasks" / "bdecide1")["sections"]["opening_prompt"]["locked"])

    def test_studio_view_and_changed_since_save(self):
        d = data.real_task(self.root / "tasks" / "gfixqc01")
        self.assertEqual(d["studio"]["changed_since_save"], [])
        self.assertEqual(d["studio"]["qc"]["counts"]["fail"], 1)
        self.assertEqual(d["studio"]["qc"]["dims"][0]["field_key"], "goal")
        sub = self.root / "tasks" / "gfixqc01" / "submission.md"
        sub.write_text(sub.read_text().replace("Steering: 3", "Steering: 2"))
        self.assertEqual(data.real_task(self.root / "tasks" / "gfixqc01")["studio"]["changed_since_save"], ["ratings"])

    def test_dropped_task_has_the_discard_form_facts(self):
        d = data.real_task(self.root / "tasks" / "jdrop001")
        self.assertEqual(d["discard"]["url"], "https://docs.google.com/forms/x")
        self.assertEqual(d["discard"]["facts"]["Outcome"], "Solved on the first try (one-shot)")
        self.assertEqual(len(d["discard"]["yours"]), 3)

    def test_live_lane_from_files(self):
        lane = data.real_snapshot()["lane"]
        self.assertEqual(lane["state"], "waiting")
        self.assertEqual(lane["run"]["short"], "llive001")
        self.assertEqual(lane["run"]["run"], "validate-1")
        self.assertTrue(lane["question"])
        self.alive = set()
        self.assertEqual(data.real_snapshot()["lane"]["state"], "exited")
        self.running, self.alive = {"prework-someone-else-model_a"}, set()
        lane = data.real_snapshot()["lane"]
        self.assertEqual(lane["state"], "free")
        self.assertEqual(lane["foreign"], {"container": "prework-someone-else-model_a"})

    def test_broken_folder_is_skipped(self):
        bad = self.root / "tasks" / "zbroken1"
        bad.mkdir()
        (bad / "task.json").write_text("{not json")
        self.assertNotIn("zbroken1", self.stages())


class PureRules(unittest.TestCase):
    def test_times_compare_across_offsets(self):
        self.assertTrue(derive.after("2026-10-31T05:00:00Z", "2026-10-30T21:59:00-0700"))
        self.assertFalse(derive.after("2026-10-30T21:59:00-0700", "2026-10-31T05:00:00.000Z"))
        self.assertTrue(derive.after("2026-10-30T21:59:00-0700", None))
        self.assertFalse(derive.after(None, "2026-10-30T21:59:00-0700"))

    def test_sent_back_until_resubmitted(self):
        t = {"short": "x", "meta": {"studio": {"status": "Writing", "feedback": "f", "times_submitted": 1,
                                               "polled_at": "2026-10-30T10:00:00-0700"},
                                    "submitted_at": "2026-10-30T09:00:00-0700"},
             "slot": {}, "texts": {}, "runs": [], "lane": {}, "jobs": []}
        self.assertEqual(derive.stage(t)["stage"], "sent_back")
        t["meta"]["studio"]["status"] = "Awaiting Review"
        self.assertEqual(derive.stage(t)["stage"], "in_review")
        self.assertFalse(derive.stage(t)["checked"])                  # since is the submit time
        del t["meta"]["submitted_at"]
        s = derive.stage(t)
        self.assertEqual((s["since"], s["checked"]), ("2026-10-30T10:00:00-0700", True))   # only Studio's poll

    def test_lane_owner_wins(self):
        t = {"short": "x", "meta": {}, "slot": {}, "texts": {}, "runs": [], "jobs": [],
             "lane": {"state": "needs_login", "run": {"short": "x", "run": "calib-2", "kind": "calib"}}}
        s = derive.stage(t)
        self.assertEqual((s["stage"], s["run"]), ("calibrating", "calib-2"))
        t["lane"]["state"] = "collecting"
        self.assertEqual(derive.stage(t)["stage"], "collecting")

    def test_unused_lane_does_not_hold_the_task(self):
        t = {"short": "x", "meta": {"skipped_calibration": True}, "slot": {}, "texts": {}, "jobs": [], "lane": {},
             "runs": [{"name": "validate-1", "kind": "validate", "n": 1, "created_at": "2026-10-30T10:00:00-0700"}]}
        self.assertEqual(derive.stage(t)["stage"], "details")

    def test_labels_cover_every_stage(self):
        for st in ("prompt", "decide", "details", "rate", "ready_to_save", "fix_qc", "ready_to_submit", "in_review",
                   "sent_back", "dropped", "collect_failed", "waiting_calib"):
            self.assertIn(st, derive.LABELS)


if __name__ == "__main__":
    unittest.main()
