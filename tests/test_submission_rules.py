"""The submission rules in johto.actions: one submission-run rule, gists bound to the files they hold, the
Trajectory Logs upload, the Studio history and the resume command.

Run: .venv/bin/python -m unittest tests.test_submission_rules -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_actions_support as fx  # noqa: E402
from test_actions import Base  # noqa: E402

from johto import actions, lint, workspace  # noqa: E402
from johto.fakes import lanes as fake_lanes  # noqa: E402


class SubmissionRun(Base):
    def info(self) -> dict:
        return actions.submission_run_info(workspace.load(self.d), self.d)

    def test_no_run_then_uncollected_then_collected(self):
        self.assertEqual(self.info(), {"run": None, "by": None, "collected": False, "newer": []})
        fake_lanes.new_lane(self.d, "validate")
        self.assertEqual(self.info()["run"], None)
        self.assertEqual(self.info()["newer"], ["validate-1"])
        errs = [m for lvl, _, m in actions.run_problems(self.d) if lvl == lint.ERROR]
        self.assertIn("validate-1 isn't collected yet (collect validate-1)", errs)

    def test_the_newest_collected_validation_run_until_a_newer_one_is_collected(self):
        v1 = self.run_("validate")
        fake_lanes.new_lane(self.d, "validate")
        info = self.info()
        self.assertEqual((info["run"], info["by"], info["collected"], info["newer"]), (v1, "latest", True, ["validate-2"]))
        warns = [m for lvl, _, m in actions.run_problems(self.d) if lvl == lint.WARN]
        self.assertTrue(any("validate-2 is newer but not collected" in w for w in warns))
        self.assertEqual(actions.validation_run(self.d)[0], v1)

    def test_a_chosen_run_is_named_as_chosen(self):
        self.run_("calib")
        self.run_("validate")
        actions.set_submission_run(self.d, "calib-1")
        self.assertEqual((self.info()["run"], self.info()["by"]), ("calib-1", "chosen"))

    def test_a_chosen_run_whose_folder_is_gone_falls_back(self):
        v1 = self.run_("validate")
        m = workspace.load(self.d)
        m["submission_run"] = "calib-7"
        workspace.save(self.d, m)
        self.assertEqual(self.info()["run"], v1)


class Gists(Base):
    def test_links_are_bound_to_the_files_they_hold(self):
        v1 = self.run_("validate")
        g = actions.upload_gists(self.d)
        self.assertEqual(sorted(g["uploaded"]), ["conversation_log", "final_output"])
        stored = workspace.load(self.d)["gists"]
        self.assertEqual(set(stored["sha"]), {"conversation_log", "final_output"})
        self.assertTrue(stored.get("uploaded_at"))
        self.assertEqual(actions.gist_state(self.d)["need"], [])
        rec = actions.run_record(self.d, v1)
        Path(rec["transcript"]).write_text(Path(rec["transcript"]).read_text() + "\n")   # resumed and collected again
        st = actions.gist_state(self.d)
        self.assertEqual(st["need"], ["conversation_log"])
        self.assertIn("transcript changed since the upload", st["why"]["conversation_log"])
        again = actions.upload_gists(self.d)
        self.assertEqual(again["uploaded"], ["conversation_log"])
        self.assertEqual(actions.gist_state(self.d)["need"], [])

    def test_links_from_before_the_sha_record_are_uploaded_again_once(self):
        self.run_("validate")
        actions.upload_gists(self.d)
        m = workspace.load(self.d)
        m["gists"].pop("sha")
        workspace.save(self.d, m)
        st = actions.gist_state(self.d)
        self.assertEqual(sorted(st["need"]), ["conversation_log", "final_output"])
        self.assertIn("before johto recorded", st["why"]["final_output"])

    def test_out_of_date_gists_are_not_a_blocking_checklist_row(self):
        v1 = self.run_("validate")
        actions.upload_gists(self.d)
        row = next(r for r in actions.checklist(self.d) if r["key"] == "gists")
        self.assertEqual(row["status"], "pass")
        rec = actions.run_record(self.d, v1)
        Path(rec["diff"]).write_text(Path(rec["diff"]).read_text() + "\n")
        row = next(r for r in actions.checklist(self.d) if r["key"] == "gists")
        self.assertEqual((row["status"], row["blocking"], row["title"]), ("pending", False, "Gists out of date"))
        self.assertEqual(row["need"], ["final_output"])


class Trajectory(Base):
    def snapshot(self, files: list[str] | None) -> None:
        cf = {} if files is None else {actions.TRAJECTORY_FIELD: [{"file_s3_url": f"s3://b/file_{'a' * 32}_{n}"}
                                                                  for n in files]}
        actions.save_studio_snapshot(self.d, cf)

    def state(self) -> str:
        return actions.trajectory_state(self.d)["status"]

    def test_each_status_and_its_row(self):
        c1 = self.run_("calib")
        v1 = self.run_("validate")
        mine = Path(actions.run_record(self.d, v1)["transcript"]).name
        other = Path(actions.run_record(self.d, c1)["transcript"]).name
        self.assertEqual(self.state(), "no_snapshot")
        self.assertEqual(actions.trajectory_row(self.d)["status"], "pending")
        self.snapshot([])
        self.assertEqual(self.state(), "missing")
        row = actions.trajectory_row(self.d)
        self.assertEqual((row["status"], row["blocking"], row["fix"]["action"]), ("fail", True, "open_studio"))
        self.snapshot([other])
        st = actions.trajectory_state(self.d)
        self.assertEqual((st["status"], st["other"]), ("other_run", [c1]))
        self.snapshot(["notes.txt"])
        row = actions.trajectory_row(self.d)
        self.assertEqual((row["status"], row["blocking"]), ("warn", False))
        self.snapshot([mine])
        self.assertEqual(self.state(), "uploaded")
        self.assertEqual(actions.trajectory_row(self.d)["status"], "pass")
        self.assertIn("trajectory_logs", {r["key"] for r in actions.checklist(self.d)})


class StudioHistory(Base):
    def lines(self) -> list[dict]:
        p = self.d / "app" / "studio-history.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_a_line_only_when_his_fields_change(self):
        actions.save_studio_snapshot(self.d, {"field_high_level_goal": "Goal one."})
        actions.save_studio_snapshot(self.d, {"field_high_level_goal": "Goal one."})
        actions.save_studio_snapshot(self.d, {"field_high_level_goal": "Goal one.", "field_review_writer_feedback": "x"})
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(self.lines()[0]["first"])
        actions.save_studio_snapshot(self.d, {"field_high_level_goal": "Goal two."})
        last = self.lines()[-1]
        self.assertEqual(last["changed"], ["field_high_level_goal"])
        self.assertNotIn("first", last)


class Resume(Base):
    def test_local_and_pc_commands(self):
        fake_lanes.new_lane(self.d, "validate")
        self.assertEqual(actions.resume_command(self.d, "validate-1"), f"johto resume {self.d.name} validate-1")
        m = workspace.load(self.d)
        m["runs"][-1].update(host="build-box", remote_dir="/home/u/johto-lanes/t/validate-1")
        workspace.save(self.d, m)
        self.assertEqual(actions.resume_command(self.d, "validate-1"),
                         "ssh -t build-box 'cd /home/u/johto-lanes/t/validate-1/model_a && "
                         "./claude-dev --model claude-opus-5-5 --effort high'")


if __name__ == "__main__":
    unittest.main()
