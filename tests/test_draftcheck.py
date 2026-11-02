"""johto.draftcheck: the saved-draft check (deterministic part, delta, the critique-only AI route) and its CLI.

The deep check runs through a fake runner, so no model is called. Task folders come from test_actions' Base: a
claimed fixture task with a fake validation run; Studio's saved text is written straight into
app/studio-fields.json, which is all the check reads.

Run: PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_draftcheck -v
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import test_actions_support as fx  # noqa: E402
from test_actions import Base  # noqa: E402

from johto import actions, draftcheck as dc, lint, stages, sync, transcript, workspace  # noqa: E402
from johto.fakes import lanes as fake_lanes  # noqa: E402

CONTRACT_KEYS = {"id", "field", "quote", "category", "evidence", "consequence", "source"}
GOAL = fx.FIXTURE_TEXT["High-Level Task Goal"]
NOTES = fx.FIXTURE_TEXT["Validation Notes"]


def quiet(*_a, **_k):
    pass


class FakeRunner:
    """Stands in for the headless Claude run: returns queued answers and records each call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def __call__(self, system, prompt, schema):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        a = self.answers.pop(0) if self.answers else {"issues": [], "resolved_ids": [], "nothing_material": True}
        if isinstance(a, Exception):
            return {"data": None, "error": str(a), "cost_usd": None}
        return {"data": a, "error": None, "cost_usd": 0.42}


def ai_issue(field, category, consequence, evidence="turn 1", quote=None) -> dict:
    return {"id": "", "field": field, "quote": quote, "category": category, "evidence": evidence,
            "consequence": consequence, "source": "ai"}


def answer(*issues, resolved=()) -> dict:
    return {"issues": list(issues), "resolved_ids": list(resolved), "nothing_material": not issues}


class CheckBase(Base):
    """A claimed task with Austin's fixture text, a collected validation run, and Studio holding the same text,
    the run's minutes and its transcript in Trajectory Logs."""

    def setUp(self):
        super().setUp()
        self.nokit = mock.patch.dict(sys.modules, {"johto.kit": None, "johto.grading": None})
        self.nokit.start()
        fx.write_fixture_text(self.d, ratings=("No", "3", "3"))
        self.run = self.run_("validate")
        rec = actions.run_record(self.d, self.run)
        self.tpath = Path(rec["transcript"])
        self.working = transcript.summarize(self.tpath)["working_minutes"]
        self.studio = actions.fields(self.d)
        self.studio["field_trajectory_log_files"] = [
            {"file_s3_url": f"s3://bucket/files/file_0123abcd_{self.tpath.stem}.jsonl"}]
        self.save_studio()
        sync.mark_pushed(self.d, actions.fields(self.d), actions.update_meta)

    def tearDown(self):
        self.nokit.stop()
        super().tearDown()

    def save_studio(self, **changes):
        self.studio.update(changes)
        for k in [k for k, v in self.studio.items() if v is None]:
            del self.studio[k]
        p = self.d / "app" / "studio-fields.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps({"at": "2026-10-31T22:30:35-0700", "fields": self.studio}))

    def set_local(self, heading: str, text: str):
        sync.write_section_file(self.d, heading, text)

    def check(self, **kw) -> dict:
        kw.setdefault("log", quiet)
        return dc.run(self.d, **kw)

    def ids(self, res, group, **match) -> list[dict]:
        return [i for i in res[group] if all(i.get(k) == v for k, v in match.items())]


class Deterministic(CheckBase):
    def test_a_clean_saved_draft_needs_nothing(self):
        res = self.check()
        self.assertEqual(res["attention"], [], res["attention"])
        self.assertEqual(res["decide"], [])
        self.assertEqual(res["run"], self.run)
        self.assertFalse(res["deep"])
        self.assertEqual(set(res["fields_sha"]) >= {"goal", "decision_points", "validation_notes", "ratings"}, True)
        for group in ("attention", "decide", "optional"):
            for i in res[group]:
                self.assertEqual(set(i), CONTRACT_KEYS)
                self.assertLessEqual(len(i["consequence"]), 200)
                self.assertIn(i["category"], dc.CATEGORIES)

    def test_reads_studio_text_when_the_local_copy_is_blank(self):
        for h in ("High-Level Task Goal", "Decision Points", "Validation Notes"):
            self.set_local(h, "")
        res = self.check()
        self.assertFalse([i for i in res["attention"] if i["category"] == "missing_required"], res["attention"])
        self.assertEqual(res["sources"]["goal"], "studio")

    def test_empty_everywhere_is_missing(self):
        self.set_local("Validation Notes", "")
        self.save_studio(field_self_check=None)
        res = self.check()
        hit = self.ids(res, "attention", field="validation_notes", category="missing_required")
        self.assertEqual(len(hit), 1)
        self.assertIn("empty in Studio and your local copy", hit[0]["consequence"])

    def test_no_trajectory_log_is_a_missing_upload(self):
        self.save_studio(field_trajectory_log_files=None)
        res = self.check()
        self.assertEqual(len(self.ids(res, "attention", field="trajectory_logs", category="missing_upload")), 1)

    def test_trajectory_log_from_another_run_is_the_wrong_run(self):
        other = self.run
        self.run = self.run_("validate")   # a second validation run becomes the submission run
        res = self.check()
        hit = self.ids(res, "attention", field="trajectory_logs", category="wrong_run")
        self.assertEqual(len(hit), 1, res["attention"])
        self.assertIn(f"holds {other}'s transcript", hit[0]["consequence"])
        self.assertEqual(res["run"], self.run)

    def test_unknown_trajectory_log_is_for_austin_to_decide(self):
        self.save_studio(field_trajectory_log_files=[
            {"file_s3_url": "s3://b/file_x_11111111-2222-3333-4444-555555555555.jsonl"}])
        res = self.check()
        self.assertEqual(len(self.ids(res, "decide", field="trajectory_logs", category="wrong_run")), 1)

    def test_claude_minutes_against_working_minutes(self):
        self.save_studio(field_claude_minutes=round(self.working))
        self.assertFalse(self.ids(self.check(), "attention", field="claude_minutes"))
        self.save_studio(field_claude_minutes=337)
        hit = self.ids(self.check(), "attention", field="claude_minutes", category="contradiction")
        self.assertEqual(len(hit), 1)
        self.assertIn("Studio says 337 Claude minutes", hit[0]["consequence"])
        self.save_studio(field_claude_minutes=None)
        self.assertEqual(len(self.ids(self.check(), "attention", field="claude_minutes",
                                      category="missing_required")), 1)

    def test_unsaved_local_edit_needs_attention_and_older_local_copy_is_optional(self):
        self.set_local("High-Level Task Goal", GOAL.replace("a burst of failures", "a burst of 500 failures"))
        res = self.check()
        hit = self.ids(res, "attention", field="goal", category="save_mismatch")
        self.assertEqual(len(hit), 1, res["attention"])
        self.assertIn("edits Studio doesn't have", hit[0]["consequence"])
        # Back to the saved text locally; now Studio moves on and the local copy is the stale one.
        self.set_local("High-Level Task Goal", GOAL)
        self.save_studio(field_high_level_goal=GOAL + " Burst tests must use more than 100 failures.")
        res = self.check()
        self.assertFalse(self.ids(res, "attention", field="goal", category="save_mismatch"))
        self.assertEqual(len(self.ids(res, "optional", field="goal", category="save_mismatch")), 1)

    def test_studio_cut_mid_word_is_incomplete_with_the_tail_quoted(self):
        cut = NOTES[: NOTES.index("flood") + 3]
        self.save_studio(field_self_check=cut)
        res = self.check()
        hit = self.ids(res, "attention", field="validation_notes", category="incomplete")
        self.assertEqual(len(hit), 1, res["attention"])
        self.assertTrue(hit[0]["quote"] and cut.endswith(hit[0]["quote"]))
        self.assertEqual(hit[0]["evidence"], "submit-saved-whole")

    def test_decision_point_word_counts_never_need_attention(self):
        dps = ("Good: traces the reset to the requeue path in queue.go and fixes it there. Bad: adds a sleep in "
               "the worker loop. Good: floods the queue with failures in a test. Bad: checks one upload by hand.")
        self.save_studio(field_rationale_difficulty=dps)
        res = self.check()
        self.assertFalse([i for i in res["attention"] + res["optional"]
                          if i["field"] == "decision_points" and i["category"] == "style"])
        self.save_studio(field_rationale_difficulty="Traces the reset to the requeue path and fixes it there; "
                                                    "the other approach adds a sleep in the worker loop instead.")
        self.set_local("Decision Points", self.studio["field_rationale_difficulty"])
        res = self.check()
        self.assertFalse([i for i in res["attention"] if i["field"] == "decision_points"])
        self.assertEqual([i["evidence"] for i in res["optional"] if i["field"] == "decision_points"],
                         ["dp-better-worse"])

    def test_run_prompt_must_match_the_saved_prompt(self):
        self.save_studio(field_opening_prompt=fx.PROMPT + " Also the dashboard is slow.")
        res = self.check()
        self.assertEqual(len(self.ids(res, "attention", field="opening_prompt", category="wrong_run")), 1)

    def test_async_slot_with_two_prompts(self):
        workspace.save(self.d, {**workspace.load(self.d), "slot": {**fx.SLOT, "interaction_mode": "Async/autonomous"}})
        res = self.check()
        hit = self.ids(res, "attention", field="run", category="contradiction")
        self.assertTrue(any("Async slot" in i["consequence"] for i in hit), res["attention"])

    def test_uncollected_runs(self):
        run_dir = fake_lanes.new_lane(self.d, "validate")
        res = self.check()   # the submission stays on the collected run; the newer one is noted
        self.assertEqual(res["run"], self.run)
        note = self.ids(res, "optional", field="run", category="wrong_run")
        self.assertEqual(len(note), 1)
        self.assertIn(f"{run_dir.name} started after {self.run}", note[0]["consequence"])
        res = self.check(run_name=run_dir.name)   # johto check --run validate-2
        hit = self.ids(res, "attention", field="run", category="missing_required")
        self.assertEqual(len(hit), 1)
        self.assertIn("isn't collected", hit[0]["consequence"])

    def test_grading_marks(self):
        rows = [
            {"id": "R1", "finding": "requeue resets the attempt counter", "marks": {"goal": True}, "kind": "defect",
             "scope": None, "result": {"run": self.run, "value": "missed"}},
            # A credit marked for the goal needs no scope call: scope is about what the run missed.
            {"id": "R6", "finding": "found the counter reset", "marks": {"goal": True}, "kind": "credit",
             "scope": None, "result": {"run": self.run, "value": "credit"}},
            {"id": "R2", "finding": "flood test", "marks": {"dp": 2}, "scope": "outside",
             "result": {"run": self.run, "value": "missed"}},
            {"id": "R3", "finding": "backoff", "marks": {"notes": True}, "scope": "explicit",
             "result": {"run": "validate-9", "value": "partial"}},
            {"id": "R4", "finding": "claims tests pass", "marks": {"notes": True}, "scope": "explicit",
             "result": {"run": self.run, "value": "unknown"}, "verify": {"status": "refuted", "path": "verify/x.md"}},
            {"id": "R5", "finding": "unmarked", "marks": {}, "scope": None, "result": {"run": "validate-9"}},
        ]
        (self.d / "grading.json").write_text(json.dumps({"version": 1, "run": self.run, "rows": rows}))
        res = self.check()
        dec = {i["evidence"]: i for i in res["decide"] if i["category"] == "scope_question"}
        self.assertEqual(set(dec), {"R1", "R2"})
        self.assertIn("scope isn't set", dec["R1"]["consequence"])
        self.assertIn("outside the task", dec["R2"]["consequence"])
        wrong = self.ids(res, "attention", category="wrong_run", field="validation_notes")
        self.assertEqual([i["evidence"] for i in wrong], ["R3"])
        self.assertEqual(len(self.ids(res, "attention", category="unsupported", field="validation_notes")), 1)
        self.assertFalse([i for g in ("attention", "decide") for i in res[g] if "R5" in i["evidence"]])

    def test_stores_json_md_and_an_event(self):
        res = self.check()
        p = Path(res["path"])
        self.assertTrue(p.exists() and p.with_suffix(".md").exists())
        self.assertEqual(p.parent.name, "checks")
        stored = json.loads(p.read_text())
        self.assertEqual(stored["texts"]["goal"], GOAL)
        self.assertNotIn("texts", dc.latest(self.d))
        ev = stages.last(self.d, "draft_checked")
        self.assertEqual((ev["deep"], ev["attention"]), (False, 0))
        self.assertIn("No AI issues", p.with_suffix(".md").read_text())


class Summary(CheckBase):
    def test_summary_counts_and_goes_stale_when_the_draft_changes(self):
        self.assertEqual(dc.summary(self.d)["latest_at"], None)
        self.save_studio(field_trajectory_log_files=None)
        res = self.check()
        s = dc.summary(self.d)
        self.assertEqual((s["latest_at"], s["attention"], s["stale"]), (res["at"], 1, False))
        self.save_studio(field_high_level_goal=GOAL + " More.")
        self.assertTrue(dc.summary(self.d)["stale"])
        n = len(list((self.d / "checks").iterdir()))
        dc.summary(self.d)
        self.assertEqual(len(list((self.d / "checks").iterdir())), n)   # never writes


class Delta(CheckBase):
    def test_resolved_since_the_last_check(self):
        self.save_studio(field_trajectory_log_files=None)
        first = self.check()
        gone = self.ids(first, "attention", field="trajectory_logs")
        self.assertEqual(len(gone), 1)
        self.save_studio(field_trajectory_log_files=[
            {"file_s3_url": f"s3://b/file_x_{self.tpath.stem}.jsonl"}])
        second = self.check()
        self.assertEqual([i["id"] for i in second["resolved"]], [gone[0]["id"]])
        self.assertFalse(self.ids(second, "attention", field="trajectory_logs"))
        self.assertEqual(second["since"]["at"], first["at"])
        self.assertIn("trajectory_logs", second["since"]["changed"])
        third = self.check()
        self.assertEqual(third["resolved"], [])   # only relative to the check before
        self.assertEqual(third["since"]["changed"], [])

    def test_the_same_words_from_another_source_are_a_change(self):
        """Studio's goal went blank and the local copy holds the same words: not "nothing changed"."""
        first = self.check()
        self.save_studio(field_high_level_goal=None)
        self.assertTrue(dc.summary(self.d)["stale"])
        second = self.check()
        self.assertEqual(second["sources"]["goal"], "local")
        self.assertIn("text_source", second["since"]["changed"])
        self.assertEqual(second["fields_sha"]["goal"], first["fields_sha"]["goal"])   # the words are the same
        self.assertIn("Where the text comes from changed", dc.render(second, short="t"))

    def test_a_field_that_got_shorter_than_at_the_last_check(self):
        self.check()
        self.save_studio(field_high_level_goal=GOAL[: GOAL.index(" Raising")])
        res = self.check()
        hit = self.ids(res, "decide", field="goal", category="incomplete")
        self.assertEqual(len(hit), 1, res)
        self.assertIn("words shorter than at the last check", hit[0]["consequence"])
        self.assertTrue(GOAL.startswith(hit[0]["quote"]) or hit[0]["quote"] in GOAL)

    def test_a_field_now_cut_mid_word_since_the_last_check(self):
        self.check()
        cut = GOAL[: GOAL.index("configured") + 5]
        self.save_studio(field_high_level_goal=cut)
        self.set_local("High-Level Task Goal", cut)   # equal locally too, so only the delta can see it
        res = self.check()
        hit = [i for i in res["attention"] if i["field"] == "goal" and i["category"] == "incomplete"]
        self.assertEqual(len(hit), 1, res["attention"])
        self.assertIn("partway through a word", hit[0]["consequence"])


class Deep(CheckBase):
    def test_schema_has_exactly_the_issue_keys_and_no_replacement_field(self):
        s = dc.schema()
        item = s["properties"]["issues"]["items"]
        self.assertEqual(set(item["properties"]), CONTRACT_KEYS)
        self.assertFalse(item["additionalProperties"])
        self.assertFalse(any(w in k for k in item["properties"] for w in ("replace", "suggest", "rewrite", "wording")))
        self.assertEqual(set(item["properties"]["category"]["enum"]), set(dc.AI_CATEGORIES))
        self.assertNotIn("style", item["properties"]["category"]["enum"])

    def test_wording_leaks_are_dropped_and_counted(self):
        quote = "stop after the configured limit"
        runner = FakeRunner(answer(
            ai_issue("goal", "contradiction", "The goal says uploads stop after the limit, but turn 2 shows the "
                                              "worker requeues forever.", "turn 2", quote),
            ai_issue("goal", "terminology", "Consider writing 'retry budget' instead of limit.", "goal"),
            ai_issue("validation_notes", "unsupported", 'The notes should say "Claude never ran the flood test".',
                     "turn 2"),
            ai_issue("validation_notes", "contradiction", 'The notes claim "the test passed on a full queue", '
                                                          "which never happened.", "turn 2"),
            ai_issue("validation_notes", "contradiction", "With these notes you could lower Task Success to 2.",
                     "turn 2"),
            ai_issue("validation_notes", "contradiction", "A 2 would fit better than the Task Success given.",
                     "turn 2"),
            ai_issue("goal", "selected_missing", "R9 is not in the goal.", "R9"),
            ai_issue("goal", "style", "The goal is long.", "goal"),
            ai_issue("goal", "unsupported", "The goal names a burst test that no run executed.", "turn 1",
                     "this text is not in any field"),
        ))
        res = self.check(deep=True, runner=runner)
        self.assertEqual(len(runner.calls), 1)
        ai = [i for i in res["attention"] + res["decide"] if i["source"] == "ai"]
        self.assertEqual(len(ai), 2, ai)
        kept = {i["category"]: i for i in ai}
        self.assertEqual(kept["contradiction"]["quote"], quote)
        self.assertIsNone(kept["unsupported"]["quote"])
        d = res["ai"]["dropped"]
        self.assertEqual(d.get("wording"), 3)
        self.assertEqual(d.get("rating_advice"), 2)
        self.assertEqual(d.get("unmarked_row"), 1)
        self.assertEqual(d.get("bad_category"), 1)
        self.assertEqual(d.get("quote_cleared"), 1)
        self.assertEqual(res["ai"]["dropped_total"], 7)
        for i in ai:
            self.assertEqual(set(i), CONTRACT_KEYS)
            self.assertEqual(i["source"], "ai")

    def test_a_quote_from_austins_text_or_the_run_record_is_not_a_leak(self):
        runner = FakeRunner(answer(
            ai_issue("validation_notes", "contradiction",
                     'The notes say "It needed one nudge" but turn 2 shows Claude added the test unprompted.', "turn 2"),
            ai_issue("validation_notes", "unsupported",
                     'Claude said "Fixed the requeue path too." without running a test.', "turn 2"),
        ))
        res = self.check(deep=True, runner=runner)
        self.assertEqual(len([i for i in res["attention"] if i["source"] == "ai"]), 2, res["ai"])

    def test_scope_questions_go_to_needs_your_decision(self):
        runner = FakeRunner(answer(ai_issue("goal", "scope_question",
                                            "The goal needs the queue size, which the prompt never gives.", "turn 1")))
        res = self.check(deep=True, runner=runner)
        self.assertEqual([i["category"] for i in res["decide"] if i["source"] == "ai"], ["scope_question"])

    def test_selected_missing_needs_a_marked_row(self):
        (self.d / "grading.json").write_text(json.dumps({"version": 1, "rows": [
            {"id": "R2", "finding": "flood test", "marks": {"goal": True}, "scope": "explicit",
             "result": {"run": self.run, "value": "missed"}}]}))
        runner = FakeRunner(answer(ai_issue("goal", "selected_missing", "R2, marked for the goal, is not in it.", "R2")))
        res = self.check(deep=True, runner=runner)
        self.assertEqual(len(self.ids(res, "attention", category="selected_missing")), 1)
        self.assertIn('"id": "R2"', runner.calls[0]["prompt"])

    def test_the_prompt_carries_the_route_rules_and_inputs(self):
        runner = FakeRunner(answer())
        self.check(deep=True, runner=runner)
        c = runner.calls[0]
        self.assertIn("You may not", c["system"])
        self.assertIn("advise a rating", c["system"])
        self.assertIn(GOAL, c["prompt"])
        self.assertIn("=== RUN FACTS", c["prompt"])
        self.assertIn("goal-must-nots", c["prompt"])
        self.assertIn("Check every field", c["prompt"])
        self.assertIn("None. The writer has not marked grading rows", c["prompt"])

    def test_unchanged_fields_reuse_their_ai_issues(self):
        g = ai_issue("goal", "terminology", "The goal says limit and the notes say cap for the same setting.",
                     "goal; notes", "configured limit")
        n = ai_issue("validation_notes", "contradiction", "The notes put the counter fix on turn 2; turn 1 has it.",
                     "turn 1", "on the second turn")
        runner = FakeRunner(answer(g, n), answer())
        first = self.check(deep=True, runner=runner)
        self.assertEqual(len([i for i in first["attention"] if i["source"] == "ai"]), 2)
        notes_id = next(i["id"] for i in first["attention"] if i["field"] == "validation_notes")
        goal_id = next(i["id"] for i in first["attention"] if i["field"] == "goal" and i["source"] == "ai")

        # Only the notes change: the AI sees only them, with the goal issue as standing.
        self.save_studio(field_self_check="Claude found the counter reset in turn 1. It needed one nudge for the "
                                          "flood test.")
        self.set_local("Validation Notes", self.studio["field_self_check"])
        second = self.check(deep=True, runner=runner)
        self.assertEqual(len(runner.calls), 2)
        p = runner.calls[1]["prompt"]
        self.assertIn("Since the last check only these changed: validation_notes", p)
        self.assertIn("=== STANDING ISSUES", p)
        self.assertIn(goal_id, p)
        self.assertEqual([i["id"] for i in second["attention"] if i["source"] == "ai"], [goal_id])
        self.assertIn(notes_id, [i["id"] for i in second["resolved"]])
        self.assertEqual(second["ai"]["focus"], ["validation_notes"])

        # Nothing changed: no AI call, the goal issue stays.
        third = self.check(deep=True, runner=runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual([i["id"] for i in third["attention"] if i["source"] == "ai"], [goal_id])
        self.assertEqual(third["resolved"], [])

        # full=True checks everything again.
        runner.answers.append(answer(g))
        fourth = self.check(deep=True, full=True, runner=runner)
        self.assertEqual(len(runner.calls), 3)
        self.assertIn("Check every field", runner.calls[2]["prompt"])
        self.assertEqual([i["id"] for i in fourth["attention"] if i["source"] == "ai"], [goal_id])

    def test_the_ai_can_resolve_a_standing_issue(self):
        g = ai_issue("goal", "terminology", "The goal says limit and the notes say cap.", "goal", "configured limit")
        runner = FakeRunner(answer(g))
        first = self.check(deep=True, runner=runner)
        gid = next(i["id"] for i in first["attention"] if i["source"] == "ai")
        self.save_studio(field_self_check=NOTES + " The limit held at 50 failures.")
        self.set_local("Validation Notes", self.studio["field_self_check"])
        runner.answers.append(answer(resolved=[gid]))
        second = self.check(deep=True, runner=runner)
        self.assertFalse([i for i in second["attention"] if i["source"] == "ai"])
        self.assertIn(gid, [i["id"] for i in second["resolved"]])

    def test_a_plain_check_carries_ai_issues_and_marks_changed_ones_stale(self):
        g = ai_issue("goal", "terminology", "The goal says limit and the notes say cap.", "goal", "configured limit")
        n = ai_issue("validation_notes", "contradiction", "The notes put the fix on turn 2; turn 1 has it.", "turn 1",
                     "on the second turn")
        self.check(deep=True, runner=FakeRunner(answer(g, n)))
        self.save_studio(field_self_check="Claude found it in turn 1 after one nudge.")
        self.set_local("Validation Notes", self.studio["field_self_check"])
        res = self.check()
        self.assertEqual([i["field"] for i in res["attention"] if i["source"] == "ai"], ["goal"])
        self.assertEqual([i["field"] for i in res["stale"]], ["validation_notes"])
        self.assertFalse([i for i in res["resolved"] if i["source"] == "ai"])   # not re-checked, so not resolved
        text = dc.render(res, short=self.d.name)
        self.assertIn("1 earlier AI issue on Validation Notes waits for the next", text)
        # The next deep check looks at the notes again and the stale issue is resolved.
        res2 = self.check(deep=True, runner=FakeRunner(answer()))
        self.assertIn("validation_notes", res2["ai"]["focus"])
        self.assertIn("validation_notes", [i["field"] for i in res2["resolved"] if i["source"] == "ai"])
        self.assertEqual(res2["stale"], [])

    def test_carried_issues_pass_todays_filters(self):
        g = ai_issue("goal", "terminology", "The goal says limit and the notes say cap.", "goal", "configured limit")
        first = self.check(deep=True, runner=FakeRunner(answer(g)))
        p = Path(first["path"])
        c = json.loads(p.read_text())
        c["ai_state"]["issues"][0]["consequence"] = "The goal probably meant the retry cap."   # an older filter let it by
        p.write_text(json.dumps(c))
        res = self.check()
        self.assertFalse([i for i in res["attention"] + res["stale"] + res["resolved"] if i["source"] == "ai"])

    def test_a_failed_ai_run_keeps_the_deterministic_result(self):
        self.save_studio(field_trajectory_log_files=None)
        res = self.check(deep=True, runner=FakeRunner(RuntimeError("usage limit")))
        self.assertEqual(res["ai"]["error"], "usage limit")
        self.assertEqual(len(self.ids(res, "attention", field="trajectory_logs")), 1)
        self.assertIn("AI check didn't run: usage limit", dc.render(res, short=self.d.name))

    def test_the_md_holds_only_ai_text_with_austins_words_quoted(self):
        self.save_studio(field_trajectory_log_files=None)   # a rule issue, which must not reach the .md
        echo = "back off and stop after the configured limit"
        runner = FakeRunner(answer(ai_issue("goal", "contradiction",
                                            f"Uploads {echo} per the goal, yet turn 2 shows endless requeues.",
                                            "turn 2", "stop after the configured limit")))
        res = self.check(deep=True, runner=runner)
        md = Path(res["path"]).with_suffix(".md").read_text()
        self.assertIn('"stop after the configured limit" (Austin\'s words)', md)
        self.assertIn(f'"{echo}"', md)   # his words coming back are quoted, so the guard skips them
        self.assertNotIn("Trajectory Logs", md)
        self.assertEqual(stages.last(self.d, "draft_checked")["deep"], True)

    def test_the_overlap_guard_does_not_flag_austins_words_echoed_by_the_check(self):
        from johto.web.guard import ledger, overlap
        echo = "back off and stop after the configured limit, and the queue stays bounded"
        runner = FakeRunner(answer(ai_issue("goal", "contradiction",
                                            f"The goal says uploads {echo}; turn 2 shows the queue grew without end.",
                                            "turn 2")))
        self.check(deep=True, runner=runner)
        ledger._CACHE.clear()
        overlap._INDEX.clear()
        self.assertTrue(any(it["kind"] == "check" for it in ledger.items(self.d)))
        self.assertEqual(overlap.unaccepted_red(self.d, {"goal": GOAL}), {})

    def test_an_empty_required_field_means_no_ai_call(self):
        """The deep check waits for a whole draft: with Decision Points empty it costs nothing and says why."""
        self.set_local("Decision Points", "")
        self.save_studio(field_rationale_difficulty=None)
        self.assertIn("Decision Points is empty", dc.deep_blocker(self.d))
        runner = FakeRunner()
        res = self.check(deep=True, runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertIn("Decision Points is empty", res["ai"]["error"])
        self.assertIn("AI check didn't run: Decision Points is empty", dc.render(res, short="t"))
        self.set_local("Decision Points", fx.FIXTURE_TEXT["Decision Points"])
        self.assertIsNone(dc.deep_blocker(self.d))

    def test_no_saved_text_means_no_ai_call(self):
        for h in fx.FIXTURE_TEXT:
            self.set_local(h, "")
        for f in ("field_opening_prompt", "field_high_level_goal", "field_rationale_difficulty",
                  "field_codebase_overview", "field_self_check"):
            self.studio.pop(f, None)
        self.save_studio()
        runner = FakeRunner()
        res = self.check(deep=True, runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertIn("no saved text", res["ai"]["error"])


class Filter(unittest.TestCase):
    CORPUS = "the worker requeues forever. claude said all tests pass"

    def test_wording_phrases(self):
        for text in ("Consider adding a sentence about the queue.", "You could write that the queue is bounded.",
                     "For example: the queue stays under 100.", "Rephrase the second point.",
                     "End the notes with the flood test.", "Add a sentence on retries.",
                     "The goal should read as a checkable outcome.", 'It needs something like "bounded queue".',
                     "Mention that Claude never ran the tests.",
                     "The point seems to be about new permission entries, so a reviewer may not follow.",
                     "The writer probably meant the role, not the user.",
                     # What a field should say, orders to say it, and ready-made decision-point sentences.
                     "The goal should name the 100-failed-rows threshold so a reviewer can grade it.",
                     "The notes should mention that Claude wrote 429 lines of tests.",
                     "Name the threshold in the goal.",
                     "It would be clearer to call this the admin throughout.",
                     "Missing from the goal: the per-role report must list each affected user.",
                     "The decision point lacks the worse behavior: a weaker model would only hide pages and never "
                     "check chapter permissions.",
                     "It's unclear whether 'freezing' means the export hangs or a shape stops moving."):
            self.assertEqual(dc.wording_leak(text, self.CORPUS), "wording", text)

    def test_rating_advice(self):
        for text in ("Lower the Task Success to 2.", "A 2 would fit the notes better.",
                     "Steering 2 would pass the matrix.", "That makes it eligible.",
                     "With these numbers the ratings make the task ineligible."):
            self.assertEqual(dc.wording_leak(text, self.CORPUS), "rating_advice", text)

    def test_facts_pass(self):
        for text in ("The notes end with a half word, so the last sentence is lost.",
                     "Task Success is rated 4, but the notes describe two silent data-loss bugs.",
                     'Claude said "all tests pass" but the worker requeues forever.',
                     "The goal names `queue.MaxAttempts` but the code calls it `retryLimit`.",
                     "Turn 2 shows the run started from another prompt.",
                     "The goal says the export freezes, but turn 2 shows the export finishing.",
                     "Decision point 2 names a function the diff never touches (R4)."):
            self.assertIsNone(dc.wording_leak(text, self.CORPUS), text)


class Runner(unittest.TestCase):
    def test_claude_runner_flags_and_structured_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = Path(tmp) / "argv.json"
            stub = Path(tmp) / "claude"
            stub.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                            f"open({str(argv)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                            "sys.stdin.read()\n"
                            "print(json.dumps({'result': '', 'total_cost_usd': 0.5, 'structured_output': "
                            "{'issues': [], 'resolved_ids': [], 'nothing_material': True}}))\n")
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            with mock.patch.object(dc, "CLAUDE_BIN", str(stub)):
                res = dc.claude_runner("sys", "prompt", dc.schema())
            self.assertIsNone(res["error"])
            self.assertEqual(res["data"]["nothing_material"], True)
            self.assertEqual(res["cost_usd"], 0.5)
            args = json.loads(argv.read_text())
            self.assertEqual(args[args.index("--tools") + 1], "")
            self.assertIn("--json-schema", args)
            self.assertIn("--no-session-persistence", args)
            self.assertEqual(args[args.index("--model") + 1], "opus")
            self.assertNotIn("Edit", " ".join(args))

    def test_missing_binary_is_an_error_not_a_crash(self):
        with mock.patch.object(dc, "CLAUDE_BIN", "/nonexistent/claude"):
            res = dc.claude_runner("s", "p", dc.schema())
        self.assertIsNone(res["data"])
        self.assertIn("isn't available", res["error"])


class Cli(CheckBase):
    def test_args_fit_the_existing_check_parser(self):
        p = argparse.ArgumentParser()
        p.add_argument("task")
        p.add_argument("--run")
        dc.add_check_args(p)
        a = p.parse_args(["x", "--deep", "--full", "--all", "--refresh"])
        self.assertTrue(a.deep and a.full and a.all and a.refresh)
        fresh = argparse.ArgumentParser()
        dc.add_check_args(fresh)
        self.assertEqual(fresh.parse_args(["x"]).task, "x")

    def test_cmd_check_prints_groups_and_returns_old_style_problems(self):
        self.save_studio(field_trajectory_log_files=None, field_claude_minutes=999,
                         field_rationale_difficulty="It should find the requeue reset rather than add a sleep, and "
                                                    "test a burst of failures instead of one upload.")
        self.set_local("Decision Points", self.studio["field_rationale_difficulty"])
        self.set_local("High-Level Task Goal", GOAL + " Extra local words.")
        self.save_studio(field_self_check=NOTES[: NOTES.index("flood") + 3])
        p = argparse.ArgumentParser()
        dc.add_check_args(p)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            probs = dc.cmd_check(p.parse_args([self.d.name]))
        out = buf.getvalue()
        self.assertIn("Needs attention", out)
        self.assertIn("Trajectory Logs, upload missing", out)
        self.assertNotIn("Optional (", out)
        levels = {(lvl, f) for lvl, f, _ in probs}
        self.assertIn((lint.ERROR, "Validation Notes"), levels)    # cut off: blocks submit
        self.assertIn((lint.WARN, "Trajectory Logs"), levels)      # Studio's own submit refuses it
        self.assertIn((lint.WARN, "Claude minutes"), levels)       # submit saves the minutes
        self.assertIn((lint.WARN, "Goal"), levels)                 # submit saves the local edit
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dc.cmd_check(p.parse_args([self.d.name, "--all"]))
        self.assertIn("Optional (", buf.getvalue())

    def test_refresh_reads_studio_through_pull(self):
        calls = []
        with mock.patch.object(actions, "pull_from_studio", side_effect=lambda d: calls.append(d) or
                               {"status": "Writing", "pulled": [], "conflicts": {}}):
            self.check(refresh=True)
            self.check()
        self.assertEqual(calls, [self.d])

    def test_refresh_is_skipped_in_fake_mode_and_when_refused(self):
        from johto import config
        lines = []
        with mock.patch.object(config, "FAKE", True), mock.patch.object(actions, "pull_from_studio") as pull:
            dc.run(self.d, refresh=True, log=lines.append)
        pull.assert_not_called()
        self.assertTrue(any("fake mode" in x for x in lines))
        lines.clear()
        with mock.patch.object(actions, "pull_from_studio", side_effect=config.RealWriteRefused("nope")):
            res = dc.run(self.d, refresh=True, log=lines.append)
        self.assertTrue(any("wasn't read again" in x for x in lines))
        self.assertEqual(res["attention"], [])


class LintFlags(unittest.TestCase):
    def test_flags_are_still_three_tuples(self):
        f = lint.Flag(lint.WARN, "Goal", "msg", "goal-specific", "length", "q")
        lvl, field, msg = f
        self.assertEqual((lvl, field, msg), (lint.WARN, "Goal", "msg"))
        self.assertEqual(f, (lint.WARN, "Goal", "msg"))
        self.assertEqual((f.rule, f.kind, f.quote), ("goal-specific", "length", "q"))
        self.assertEqual(json.loads(json.dumps(f)), [lint.WARN, "Goal", "msg"])
        import copy
        self.assertEqual(copy.copy(f).rule, "goal-specific")
        self.assertEqual(lint.flag_info((lint.ERROR, "X", "y"))["kind"], "form")

    def test_decision_points_count_good_and_bad_and_never_error(self):
        slot = dict(fx.SLOT)
        sec = {"Decision Points": "A good answer fixes the requeue path in queue.go. A bad answer adds a sleep. A good "
                                  "answer tests a flood of failures; a bad one checks a single upload."}
        self.assertFalse([f for f in lint.check_sections(sec, slot) if f[1] == "Decision Points"])
        sec = {"Decision Points": "It should find the requeue reset rather than add a sleep, and it should test a "
                                  "burst of failures instead of one upload."}
        flags = [f for f in lint.check_sections(sec, slot) if f[1] == "Decision Points"]
        self.assertEqual([(f[0], f.rule, f.kind) for f in flags], [(lint.WARN, "dp-better-worse", "structure")])

    def test_every_flag_has_a_known_kind(self):
        meta = {"slot": {**fx.SLOT, "action": "Operate"}, "repo": {"pinned_url": "x", "full_name": "django/django"}}
        sub = {"sections": {"Opening Prompt": "Hi, could you please do Step 1 — the rubric?"},
               "ratings": {"one-shot": "yes", "task success": "5"}}
        flags = lint.run_all(meta, sub)
        self.assertTrue(flags)
        for f in flags:
            self.assertIn(f.kind, lint.KINDS, f)
        quotes = {f.quote.lower() for f in flags if f.quote}
        self.assertTrue({"hi", "could you", "step 1", "rubric"} <= quotes, quotes)


if __name__ == "__main__":
    unittest.main()
