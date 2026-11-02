"""johto.where: the task card (run, text version, evidence, since, actions) and the overview in Home's order.

Run: .venv/bin/python -m unittest tests.test_where -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import test_actions_support as fx  # noqa: E402

from johto import actions, cli, config, stages, where, workspace  # noqa: E402
from johto.fakes import gh as fake_gh, lanes as fake_lanes, studio as fake  # noqa: E402
from johto.web import data  # noqa: E402
from johto.web.lane import runmon  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fx.make_root(self.root)
        self.patches = fx.patch_config(self.root) + [
            mock.patch.object(config, "FAKE", True),     # no Docker: the fake lanes answer
            mock.patch.dict(os.environ, {"JOHTO_CHAT_DIRS": ""}),
            mock.patch("johto.scout.rank.rank", return_value=[])]
        for p in self.patches:
            p.start()
        fake.reset()
        fake.QC_DELAY_S = 0
        fake_gh.ACCOUNT = "austeane"
        runmon.clear()
        data._RUNNING["at"] = 0.0
        lead = actions.load_lead(fx.LEAD_ID)
        repo = actions.repo_info(lead["repo"], lead["commit"], lead["primary_language"])
        self.d = actions.claim(fx.TASKS[0]["task_id"], repo, lead)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        fake.reset()
        runmon.clear()
        self.tmp.cleanup()

    def run_(self, kind: str) -> str:
        run_dir = fake_lanes.new_lane(self.d, kind)
        fx.fake_claude(run_dir, fx.PROMPT)
        actions.collect(self.d, run_dir.name)
        runmon.clear()
        return run_dir.name

    def card(self, **kw) -> dict:
        kw.setdefault("live", False)
        return where.card(self.d, **kw)

    def meta(self, change) -> None:
        m = workspace.load(self.d)
        change(m)
        workspace.save(self.d, m)

    def snapshot(self, **fields) -> None:
        (self.d / "app").mkdir(exist_ok=True)
        (self.d / "app" / "studio-fields.json").write_text(json.dumps({"at": "2026-10-31T12:00:00-0700",
                                                                        "fields": fields}))


class Card(Base):
    def test_a_fresh_claim(self):
        c = self.card()
        for k in ("task", "short", "title", "stage", "next", "run", "text", "reviews", "grading", "check", "since",
                  "focus", "actions"):
            self.assertIn(k, c)
        self.assertEqual(c["short"], self.d.name)
        self.assertEqual(c["stage"]["stage"], "prompt")
        self.assertIsNone(c["run"]["name"])
        self.assertEqual(c["text"]["source"], "local")
        self.assertIn("hasn't read Studio", c["text"]["note"])
        self.assertTrue(c["next"]["text"])
        self.assertEqual(c["actions"][0]["label"], "Open Studio")
        self.assertTrue(c["actions"][0]["command"].startswith("https://"))
        self.assertTrue(any("Claimed" in s for s in c["since"]))

    def test_touch_and_since(self):
        self.card()
        self.assertEqual(stages.last(self.d, "touch")["source"], "where")
        c = self.card()
        self.assertEqual(c["since"], [])
        self.assertIsNotNone(c["since_at"])
        self.run_("validate")
        c = self.card()
        self.assertTrue(any("Collected validation 1" in s for s in c["since"]))
        n = len(stages.read(self.d, ("touch",)))
        self.card(touch=False)
        self.assertEqual(len(stages.read(self.d, ("touch",))), n)

    def test_the_collected_validation_run_and_its_commands(self):
        v1 = self.run_("validate")
        c = self.card()
        r = c["run"]
        self.assertEqual((r["name"], r["selected_by"], r["submission"]), (v1, "latest", True))
        self.assertEqual(c["submission_run"], v1)
        self.assertGreater(r["working_minutes"], 0)
        self.assertIn("Claude worked", r["line"])
        cmds = [a["command"] for a in c["actions"]]
        self.assertIn(f"johto check {self.d.name}", cmds)
        self.assertIn(f"johto time {self.d.name} {v1}", cmds)
        self.assertNotIn(f"johto collect {self.d.name} {v1}", cmds)
        self.assertEqual(c["trajectory"]["status"], "no_snapshot")

    def test_a_chosen_calibration_run(self):
        self.run_("calib")
        self.meta(lambda m: m.update(submission_run="calib-1"))
        c = self.card()
        self.assertEqual((c["run"]["name"], c["run"]["selected_by"]), ("calib-1", "chosen"))

    def test_a_newer_validation_run_not_collected_yet(self):
        v1 = self.run_("validate")
        fake_lanes.new_lane(self.d, "validate")
        c = self.card()
        self.assertEqual(c["run"]["name"], v1)
        self.assertEqual(c["run"]["newer"], ["validate-2"])
        self.assertIn(f"johto collect {self.d.name} validate-2", [a["command"] for a in c["actions"]])

    def test_a_pc_lane_gives_the_ssh_resume_command_and_stays_offline(self):
        fake_lanes.new_lane(self.d, "validate")
        self.meta(lambda m: m["runs"][-1].update(host="build-box", remote_dir="/home/u/johto-lanes/x/validate-1",
                                                 port_slot=1))
        with mock.patch("johto.turnwatch.probe_host") as probe, mock.patch("johto.budget._reachable") as reach:
            c = self.card(live=False)
        probe.assert_not_called()
        reach.assert_not_called()
        self.assertEqual(c["run"]["name"], "validate-1")
        self.assertEqual(c["run"]["selected_by"], "current")
        self.assertEqual(c["run"]["host"], "PC")
        resume = next(a["command"] for a in c["actions"] if a["label"].startswith("Resume"))
        self.assertEqual(resume, "ssh -t build-box 'cd /home/u/johto-lanes/x/validate-1/model_a && "
                                 "./claude-dev --model claude-opus-5-5 --effort high'")
        self.assertIn(f"johto collect {self.d.name} validate-1", [a["command"] for a in c["actions"]])

    def test_render_card(self):
        self.run_("validate")
        text = where.render_card(self.card())
        self.assertIn("Next: ", text)
        self.assertIn("Run: validation 1 on the Mac (newest collected validation run)", text)
        self.assertIn("Text: the local file.", text)
        self.assertNotIn("—", text)


class Text(Base):
    def test_studio_text_of_an_accepted_task_is_current_and_creates_no_writing_step(self):
        self.run_("validate")
        self.snapshot(field_opening_prompt=fx.PROMPT, field_high_level_goal="Studio's goal text.",
                      field_rationale_difficulty="Studio's decision points.")
        self.meta(lambda m: m.setdefault("studio", {}).update(status="Accepted", mine=False))
        c = self.card()
        self.assertEqual(c["stage"]["stage"], "accepted")
        self.assertEqual(c["text"]["source"], "studio")
        self.assertIn("as accepted", c["text"]["note"])
        self.assertNotIn("goal", c["text"]["missing"])
        self.assertEqual(c["next"]["text"], "Accepted. Nothing left to do.")
        self.assertEqual([a["label"] for a in c["actions"]], ["Open Studio"])

    def test_a_blank_local_goal_with_studio_text_is_not_a_writing_step(self):
        self.run_("validate")
        self.snapshot(field_opening_prompt=fx.PROMPT, field_high_level_goal="Studio has the goal.",
                      field_rationale_difficulty="Studio has decision points.",
                      field_codebase_overview="Studio has notes.")
        t_local = where.task_view(self.d, live=False)[0]
        t_best = where.task_view(self.d, live=False, text=where.text_state(self.d, workspace.load(self.d)))[0]
        self.assertFalse((t_local["texts"].get("goal") or "").strip())
        self.assertEqual(t_best["texts"]["goal"], "Studio has the goal.")
        c = self.card()
        self.assertNotIn("goal", c["text"]["missing"])
        self.assertIn("goal", c["text"]["pull"])
        self.assertIn(f"johto pull {self.d.name}", c["text"]["note"])
        self.assertIn(f"johto pull {self.d.name}", [a["command"] for a in c["actions"]])
        # Claimed before johto recorded Studio's starting text: Studio's copy is current, nothing is pulled.
        self.meta(lambda m: m.pop("studio_at_claim", None))
        c = self.card()
        self.assertIn("goal", c["text"]["unpulled"])
        self.assertNotIn("goal", c["text"]["missing"])
        self.assertIn("is the current copy", c["text"]["note"])

    def test_local_edits_studio_lacks(self):
        fx.write_fixture_text(self.d)
        sub = workspace.parse_submission(self.d)
        goal = sub["sections"].get("High-Level Task Goal")
        self.assertTrue(goal)
        self.snapshot(field_opening_prompt=sub["sections"]["Opening Prompt"], field_high_level_goal="An older goal.")
        self.meta(lambda m: m.update(pushed={"at": "2026-10-31T11:00:00-0700", "fields": {
            "field_high_level_goal": actions.sha12("An older goal.")}}))
        c = self.card()
        self.assertIn("goal", c["text"]["local_differs"])
        self.assertIn("goal", c["text"]["local_only"])
        self.assertIn("Save to Studio", c["text"]["note"])

    def test_a_previous_writers_text(self):
        prev = "A previous writer's goal."
        self.snapshot(field_opening_prompt=fx.PROMPT, field_high_level_goal=prev)
        self.meta(lambda m: m.update(studio_at_claim={"field_high_level_goal": where.sync.h(prev)}))
        c = self.card()
        self.assertIn("goal", c["text"]["previous_writer"])
        self.assertIn("previous writer", c["text"]["note"])
        self.assertIn("goal", c["text"]["missing"])


class Evidence(Base):
    def test_reviews_grading_check_and_focus_blocks(self):
        v1 = self.run_("validate")
        from johto import reviews
        reviews.save(self.d, v1, 1, "## Findings\n\nThe fix misses the requeue path.\n", source="gptpro")
        c = self.card()
        self.assertIn("GPT Pro review of validation 1 turn 1", c["reviews"]["latest"])
        self.assertEqual((c["reviews"]["covers_turn"], c["reviews"]["run_turns"]), (1, 2))
        self.assertIsNone(c["grading"])
        self.assertIsNone(c["check"])
        self.assertEqual(set(c["focus"]), {"task_minutes", "sittings", "method", "method_text", "by_stage", "open"})
        self.assertGreater(c["focus"]["task_minutes"], 0)    # the claim and the earlier touches are signs

    def test_the_since_lines_collapse_repeats(self):
        self.card()
        v1 = self.run_("validate")
        for turn in (1, 2, 3):
            stages.log(self.d, "turn_ended", run=v1, turn=turn, working_minutes=5)
        c = self.card()
        turns = [s for s in c["since"] if "finished turn" in s]
        self.assertEqual(len(turns), 1)
        self.assertIn("(and 2 more like it)", turns[0])


    def test_his_own_marks_and_checks_are_not_news(self):
        """Marking rows and checking the draft are his actions; the since lines keep only what changed without him."""
        self.card()
        v1 = self.run_("validate")
        stages.log(self.d, "grading_marked", rows=["R1", "R2"])
        stages.log(self.d, "draft_checked", deep=False, attention=3, run=v1)
        stages.log(self.d, "turn_ended", run=v1, turn=1, working_minutes=5)
        since = self.card()["since"]
        self.assertFalse([s for s in since if "marked" in s or "Draft checked" in s], since)
        self.assertTrue([s for s in since if "finished turn 1" in s], since)


class Overview(Base):
    def test_one_row_per_task_and_the_deck(self):
        (self.root / "data" / "dossiers" / "index.json").write_text(json.dumps({"target": 10, "paused": False, "items": [
            {"id": fx.DOSSIER_ID, "repo": "acme/widget", "status": "ready", "commit": fx.COMMIT, "rank": 0}]}))
        rows = where.overview()
        mine = [r for r in rows if r.get("short") == self.d.name]
        self.assertEqual(len(mine), 1)
        for k in ("short", "title", "needs_you", "machine", "action", "command", "priority"):
            self.assertIn(k, mine[0])
        picks = [r for r in rows if r.get("kind") == "deck"]
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]["needs_you"], where.DECK_NEEDS)
        self.assertTrue(picks[0]["command"].startswith("johto start --repo acme/widget"))
        self.assertNotIn("Pick the next problem", [r["needs_you"] for r in rows])
        text = where.render_overview(rows)
        self.assertIn(self.d.name, text)
        self.assertIn(where.DECK_NEEDS, text)
        # A task in review is not "running": its group says who it waits on.
        text = where.render_overview([{"short": "i1", "title": "repo · i1", "needs_you": "", "machine": "In review",
                                       "action": None, "command": None, "priority": None}])
        self.assertIn("Waiting on Claude or reviewers", text)
        self.assertNotIn("Running, nothing for you", text)

    def test_cli(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["where", self.d.name, "--offline", "--json"])
            c = json.loads(buf.getvalue())
        self.assertEqual(c["short"], self.d.name)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["where"])
        self.assertIn(self.d.name, buf.getvalue())


class Status(Base):
    def test_local_status_shows_focus_minutes_and_the_total_per_accepted_task(self):
        self.card()            # a touch: a sign of work
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["status", "--local"])
        out = buf.getvalue()
        self.assertIn("Writing (mine): 1", out)
        self.assertIn(f"Task {self.d.name}", out)
        self.assertIn("you ", out)
        self.assertIn("Open tasks so far:", out)
        self.assertEqual(cli.focus_totals({"accepted": [60.0, 120.0], "rejected": [60.0]}).splitlines()[0],
                         "Your focused time per accepted task: 2.0 h (4.0 h over 3 settled tasks, 2 accepted).")


if __name__ == "__main__":
    unittest.main()
