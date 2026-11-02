"""The sent-back checklist (johto/feedback.py, johto/web/api_feedback.py) and the sent-back rule it hangs on
(derive.sent_back_since). No test runs Claude: every run goes through a stub runner."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from johto import config, feedback
from johto.web import api_feedback, api_now
from johto.web.domain import derive

REVIEW = ("Hello,\n\nThis run is short at 14 minutes; we look for 30+ on enterprise tasks.\n\n"
          "Claude changed test files, so add the .diff for me to read.\n\n"
          "The goal also needs a few things the model must not do (negative cases).")
GOAL = "The explanation covers how large exports are split into batches. It names the gap in batch merging."


def meta_of(**studio):
    st = {"status": "Writing", "feedback": REVIEW, "feedback_at": "2026-11-02T05:36:06-0700",
          "polled_at": "2026-11-02T06:00:00-0700", "times_submitted": 0}
    st.update(studio)
    return {"claimed_at": "2026-10-31T01:30:13-0700", "studio": st}


class SentBackRule(unittest.TestCase):
    def test_submitted_in_the_app(self):
        m = meta_of()
        m["submitted_at"] = "2026-11-01T09:00:00-0700"
        self.assertEqual(derive.sent_back_since(m), "2026-11-02T05:36:06-0700")

    def test_studio_moved_it_back_from_review(self):
        m = meta_of(sent_back_at="2026-11-02T05:37:00-0700")
        m["feedback_at_claim"] = feedback.sync.h(REVIEW)   # even the same words as at the claim
        self.assertEqual(derive.sent_back_since(m), "2026-11-02T05:37:00-0700")

    def test_feedback_at_claim_decides(self):
        m = meta_of()
        m["feedback_at_claim"] = feedback.sync.h(REVIEW)   # a previous writer's review: not his
        self.assertIsNone(derive.sent_back_since(m))
        m["feedback_at_claim"] = ""                         # nothing at the claim: his
        self.assertIsNotNone(derive.sent_back_since(m))

    def test_empty_claim_baseline_means_nothing_was_there(self):
        m = meta_of()
        m["studio_at_claim"] = {}
        self.assertIsNotNone(derive.sent_back_since(m))
        m["studio_at_claim"] = {"field_high_level_goal": "abc"}   # a previous writer's text, and no other sign
        m["claimed_at"] = "2026-11-02T05:00:00-0700"             # feedback seen within the hour of the claim
        self.assertIsNone(derive.sent_back_since(m))

    def test_old_claims_go_by_when_the_feedback_appeared(self):
        m = meta_of()                                   # claimed 2026-10-31, feedback seen 2026-11-02
        self.assertIsNotNone(derive.sent_back_since(m))
        m["claimed_at"] = "2026-11-02T05:20:00-0700"    # seen 16 minutes after the claim: there before
        self.assertIsNone(derive.sent_back_since(m))

    def test_not_after_a_resubmit_and_not_outside_writing(self):
        m = meta_of()
        m["submitted_at"] = "2026-11-02T08:00:00-0700"
        self.assertIsNone(derive.sent_back_since(m))
        self.assertIsNone(derive.sent_back_since(meta_of(status="Accepted")))
        self.assertIsNone(derive.sent_back_since(meta_of(feedback="  ")))

    def test_stage_uses_the_rule(self):
        t = {"short": "x", "meta": meta_of(), "slot": {}, "texts": {}, "runs": [], "lane": {}, "jobs": []}
        s = derive.stage(t)
        self.assertEqual((s["stage"], s["since"]), ("sent_back", "2026-11-02T05:36:06-0700"))


class StudioStatusRecordsTheMove(unittest.TestCase):
    def test_review_to_writing_sets_sent_back_at(self):
        from johto import actions
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "t1"
            (d / "app").mkdir(parents=True)
            (d / "task.json").write_text(json.dumps({"task_id": "task_1", "studio": {"status": "Awaiting Review",
                                                                                      "feedback": None}}))
            fake = mock.Mock()
            fake.task.return_value = {"owned_by": "me", "custom_fields": {"field_review_writer_feedback": REVIEW}}
            fake.status_name.return_value = "Writing"
            with mock.patch.object(actions, "S", return_value=fake), mock.patch.object(actions, "me", return_value="me"):
                actions.studio_status(d)
            st = json.loads((d / "task.json").read_text())["studio"]
            self.assertTrue(st.get("sent_back_at"))
            self.assertTrue(st.get("feedback_at"))
            self.assertEqual(st["status"], "Writing")


def plan_answer(**over):
    items = [
        {"ask": "Run Claude for longer: the reviewer expects 30+ minutes of Claude time on enterprise tasks.",
         "quote": "This run is short at 14 minutes; we look for 30+ on enterprise tasks.",
         "kind": "run", "fields": ["claude_minutes"], "where_field": None, "where": None,
         "facts": ["validate-1: Claude worked 14 minutes over 4 prompts."]},
        {"ask": "Put the diff and the tests Claude wrote where the reviewer can read them.",
         "quote": "Claude changed test files, so add the .diff for me to read.",
         "kind": "attach", "fields": ["final_output"], "where_field": None, "where": None,
         "facts": ["Studio's Final Output field is empty.", "a fact nobody gathered"]},
        {"ask": "Goal: add a few cases of what Claude must not do.",
         "quote": "a few things the model must not do (negative cases)",
         "kind": "write", "fields": ["goal"], "where_field": "goal", "where": "It names the gap in batch merging.",
         "facts": []},
    ]
    a = {"items": items, "summary": "Three things: a longer run, the diff in Studio, and must-not cases in the goal.",
         "drop_question": None}
    a.update(over)
    return a


class Task(unittest.TestCase):
    """A temp task folder with a review, Studio's fields and one collected run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.d = self.root / "tasks" / "t1"
        run = self.d / "runs" / "validate-1" / "prework_output"
        run.mkdir(parents=True)
        (run / "final.diff").write_text("diff --git a/app/tests/test_export.py b/app/tests/test_export.py\n+x\n")
        (self.d / "app").mkdir()
        meta = meta_of()
        meta.update(task_id="task_1", task_name="Task t1", repo={"full_name": "o/r"},
                    runs=[{"kind": "validate", "dir": "runs/validate-1", "collected_at": "2026-10-31T13:00:00-0700",
                           "diff": str(run / "final.diff")}])
        (self.d / "task.json").write_text(json.dumps(meta))
        (self.d / "submission.md").write_text("## Opening Prompt\nExplain large exports.\n\n## High-Level Task Goal\n"
                                              + GOAL + "\n")
        (self.d / "app" / "studio-fields.json").write_text(json.dumps({"at": "2026-11-02T06:00:00-0700", "fields": {
            "field_review_writer_feedback": REVIEW, "field_review_difficulty_ok": "Borderline",
            "field_review_understanding_ok": "Yes", "field_high_level_goal": GOAL, "field_claude_minutes": 14}}))
        self.patches = [mock.patch.object(config, "TASKS", self.root / "tasks"),
                        mock.patch.object(feedback.draftcheck, "_rules_text", return_value="")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def runner(self, answer):
        calls = []

        def run(system, prompt, schema):
            calls.append((system, prompt, schema))
            return {"data": answer, "error": None, "cost_usd": 0.5}
        run.calls = calls
        return run


class Plan(Task):
    def test_plan_keeps_verified_spans_and_gathered_facts(self):
        r = self.runner(plan_answer())
        out = feedback.plan(self.d, runner=r, log=lambda *_: None, gh=lambda a: None)
        items = out["items"]
        self.assertEqual([i["kind"] for i in items], ["run", "attach", "write"])
        self.assertEqual(items[2]["where"], {"field": "goal", "text": "It names the gap in batch merging."})
        self.assertEqual(items[2]["action"], {"type": "field", "field": "goal"})
        self.assertEqual(items[1]["action"]["type"], "attach")
        self.assertEqual(items[1]["action"]["tests"], ["app/tests/test_export.py"])
        self.assertEqual(items[1]["facts"], ["Studio's Final Output field is empty."])   # invented one dropped
        self.assertEqual(items[0]["facts"], [])          # no transcript here, so no minutes were gathered
        self.assertEqual(out["filtered"].get("fact_not_gathered"), 2)
        self.assertEqual(out["review"], {"difficulty_ok": "Borderline", "understanding_ok": "Yes"})
        prompt = r.calls[0][1]
        self.assertIn("# REVIEW", prompt)
        self.assertIn(GOAL, prompt)
        self.assertIn("Final Output field is empty", prompt)
        self.assertNotIn("additionalProperties", r.calls[0][0])

    def test_asks_that_hand_over_wording_fall_back_to_the_reviewer(self):
        a = plan_answer()
        a["items"][2]["ask"] = 'Goal: add a check such as "Claude must not delete the job table".'
        out = feedback.plan(self.d, runner=self.runner(a), log=lambda *_: None, gh=lambda a: None)
        it = out["items"][2]
        self.assertIsNone(it["ask"])
        self.assertTrue(it["quote"].startswith("a few things"))
        self.assertEqual(out["filtered"].get("ask_wording"), 1)

    def test_spans_not_in_the_text_are_dropped(self):
        a = plan_answer()
        a["items"][0]["quote"] = "words the reviewer never wrote"
        a["items"][2]["where"] = "text that isn't in the goal"
        out = feedback.plan(self.d, runner=self.runner(a), log=lambda *_: None, gh=lambda a: None)
        self.assertIsNone(out["items"][0]["quote"])
        self.assertIsNotNone(out["items"][0]["ask"])
        self.assertIsNone(out["items"][2]["where"])
        self.assertEqual(out["items"][2]["action"], {"type": "field", "field": "goal"})

    def test_a_failed_run_is_recorded_and_retried_later(self):
        def broken(*_):
            return {"data": None, "error": "boom", "cost_usd": None}
        out = feedback.plan(self.d, runner=broken, log=lambda *_: None, gh=lambda a: None)
        self.assertEqual(out["error"], "boom")
        meta = json.loads((self.d / "task.json").read_text())
        self.assertFalse(feedback.needs_plan(self.d, meta))                 # waits before trying again
        self.assertTrue(feedback.needs_plan(self.d, meta, retry_s=0))
        v = feedback.view(self.d, meta)
        self.assertEqual(v["state"], "error")

    def test_view_counts_and_manual_ticks(self):
        feedback.plan(self.d, runner=self.runner(plan_answer()), log=lambda *_: None, gh=lambda a: None)
        meta = json.loads((self.d / "task.json").read_text())
        v = feedback.view(self.d, meta)
        self.assertEqual((v["state"], v["counts"]), ("ready", {"todo": 3, "done": 0, "total": 3}))
        iid = v["items"][1]["id"]
        feedback.mark(self.d, iid, True)
        v = feedback.view(self.d, meta)
        self.assertEqual(v["counts"]["done"], 1)
        self.assertTrue(v["items"][1]["manual"])
        b = feedback.brief(self.d, meta)
        self.assertEqual((b["todo"], b["total"]), (2, 3))
        with self.assertRaises(KeyError):
            feedback.mark(self.d, "nope", True)
        # A new review: the old checklist doesn't count, and a plan is due.
        meta["studio"]["feedback"] = REVIEW + " One more thing."
        self.assertEqual(feedback.view(self.d, meta)["state"], "planning")
        self.assertTrue(feedback.needs_plan(self.d, meta))

    def test_ticks_survive_a_new_plan_of_the_same_review(self):
        feedback.plan(self.d, runner=self.runner(plan_answer()), log=lambda *_: None, gh=lambda a: None)
        iid = feedback.load(self.d)["items"][0]["id"]
        feedback.mark(self.d, iid, True)
        feedback.plan(self.d, runner=self.runner(plan_answer()), log=lambda *_: None, gh=lambda a: None)
        self.assertIn(iid, feedback.load(self.d)["done"])


class Check(Task):
    def setUp(self):
        super().setUp()
        feedback.plan(self.d, runner=self.runner(plan_answer()), log=lambda *_: None, gh=lambda a: None)
        self.ids = [i["id"] for i in feedback.load(self.d)["items"]]

    def test_check_marks_items_and_drops_wording(self):
        ans = {"items": [
            {"id": self.ids[0], "state": "open", "why": "The run is still 14 minutes.", "where_field": None, "where": None},
            {"id": self.ids[1], "state": "addressed", "why": "Final Output holds a gist.", "where_field": None, "where": None},
            {"id": self.ids[2], "state": "partly", "why": 'You could write "Claude must not drop rows".',
             "where_field": "goal", "where": "It names the gap in batch merging."},
            {"id": "bogus", "state": "open", "why": "", "where_field": None, "where": None}]}
        out = feedback.check(self.d, runner=self.runner(ans), log=lambda *_: None, gh=lambda a: None)
        got = out["check"]["items"]
        self.assertEqual(set(got), set(self.ids))
        self.assertIsNone(got[self.ids[2]]["why"])                       # wording dropped, state kept
        self.assertEqual(got[self.ids[2]]["state"], "partly")
        self.assertEqual(out["check"]["filtered"], 1)
        meta = json.loads((self.d / "task.json").read_text())
        v = feedback.view(self.d, meta)
        self.assertEqual(v["counts"], {"todo": 2, "done": 1, "total": 3})
        self.assertFalse(v["check"]["stale"])
        self.assertFalse(feedback.needs_check(self.d, meta, gap_s=0))
        # He edits the goal: the check is stale and due again.
        sub = (self.d / "submission.md").read_text().replace("batch merging.", "batch merging. It must not crash.")
        (self.d / "submission.md").write_text(sub)
        self.assertTrue(feedback.view(self.d, meta)["check"]["stale"])
        self.assertTrue(feedback.needs_check(self.d, meta, gap_s=0))
        self.assertFalse(feedback.needs_check(self.d, meta, gap_s=3600))   # not twice in a row


class Words(unittest.TestCase):
    def test_prompt_section_names_become_plain_words(self):
        self.assertEqual(feedback.plain_refs("FACTS show no PRs."), "The record show no PRs.")
        self.assertEqual(feedback.plain_refs("as the FACTS say, and the REVIEW asks"), "As the record say, and the review asks")


class Facts(unittest.TestCase):
    def test_github_refs_follow_a_move_to_codeberg(self):
        def gh(args):
            a = args[0]
            if a.endswith("/issues/4102"):
                return {"number": 4102, "state": "closed", "state_reason": "completed", "closed_at": "2026-02-14T12:46:00Z",
                        "title": "Retry the upload after a timeout"}
            if a.endswith("/timeline?per_page=100"):
                return [{"event": "cross-referenced", "source": {"issue": {"number": 9, "state": "closed",
                         "pull_request": {"merged_at": None}, "repository": {"full_name": "o/r"}, "title": "Try"}}}]
            if "/comments" in a:
                return [{"body": "Moved. You'll find this issue here: https://codeberg.org/o/r/issues/4102"}]
            return None
        refs = feedback.github_refs("o/r", [4102, 77], gh=gh,
                                    codeberg=lambda url: {"url": url, "state": "open", "closed_at": None, "title": "x"})
        self.assertEqual(len(refs), 1)
        r = refs[0]
        self.assertEqual((r["state"], r["moved"]["state"]), ("closed", "open"))
        self.assertEqual(r["prs"][0]["n"], 9)

    def test_gather_lines_name_the_issue_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "t2"
            (d / "app").mkdir(parents=True)
            meta = {"repo": {"full_name": "o/r"}, "runs": [], "studio": {"feedback": "Issue #4102 is closed now."}}
            (d / "task.json").write_text(json.dumps(meta))
            (d / "submission.md").write_text("## Difficulty & Codebase Understanding\nSee issue #4102.\n")

            def gh(args):
                if args[0].endswith("/issues/4102"):
                    return {"number": 4102, "state": "closed", "closed_at": "2026-02-14T12:46:00Z", "title": "t"}
                return [] if "timeline" in args[0] or "comments" in args[0] else None
            with mock.patch.object(feedback.draftcheck, "submission_run", return_value=None):
                f = feedback.gather(d, meta, gh=gh, codeberg=lambda u: None)
        line = next(x for x in f["lines"] if "#4102" in x)
        self.assertIn("closed on GitHub since 2026-02-14", line)
        self.assertIn("no PRs link to it", line)


class AutoStart(Task):
    def test_ensure_only_in_the_running_app_and_only_when_sent_back(self):
        with mock.patch.object(api_feedback, "submit_plan", return_value={"id": "j1"}) as sp, \
                mock.patch.object(api_feedback, "live_job", return_value=None):
            self.assertIsNone(api_feedback.ensure(self.d))                 # AUTO is off outside the app
            sp.assert_not_called()
            with mock.patch.object(api_feedback, "AUTO", True):
                self.assertEqual(api_feedback.ensure(self.d), {"id": "j1"})
                meta = json.loads((self.d / "task.json").read_text())
                meta["studio"]["status"] = "Accepted"
                self.assertIsNone(api_feedback.ensure(self.d, meta))
            self.assertEqual(sp.call_count, 1)


class NowCard(unittest.TestCase):
    def test_feedback_card_counts_the_checklist(self):
        it = {"kind": "feedback", "short": "t1", "title": "Reviewer sent it back"}
        ctx = {"short": "t1", "stage": "sent_back", "texts": {},
               "fb_plan": {"state": "ready", "todo": 2, "total": 3, "summary": "s", "first": "Run Claude for longer."}}
        b = api_now.writing_body(it, ctx)
        self.assertEqual(b["fact"], "2 of 3 things they asked for still to do.")
        self.assertEqual(b["checklist"]["first"], "Run Claude for longer.")
        ctx["fb_plan"] = {"state": "planning"}
        self.assertEqual(api_now.writing_body(it, ctx)["fact"], "Making a checklist of what they asked for.")
        self.assertEqual(api_now.place_for(it, ctx)["href"], "/task/t1#step=write&fb=1")


if __name__ == "__main__":
    unittest.main()
