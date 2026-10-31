"""Studio edits come back into submission.md (johto.sync), and saving never overwrites them.

Run: .venv/bin/python -m unittest discover -s tests -p "test_sync.py"
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_actions import Base  # noqa: E402

from johto import actions, sync, workspace  # noqa: E402
from johto.actions import ActionError  # noqa: E402
from johto.fakes import studio as fake  # noqa: E402
from johto.web import data  # noqa: E402


def studio_edit(d: Path, **fields) -> None:
    """Austin typing in Studio: change fields there without touching the task folder."""
    fake.patch_fields(workspace.load(d)["task_id"], fields)


def local(d: Path, heading: str) -> str:
    return workspace.parse_submission(d)["sections"].get(heading, "")


class Pull(Base):
    def test_empty_field_takes_studio_text_and_keeps_the_hint(self):
        studio_edit(self.d, field_opening_prompt="Is deleting roles safe here?")
        res = actions.pull_from_studio(self.d)
        self.assertEqual(res["pulled"], ["Opening Prompt"])
        self.assertEqual(local(self.d, "Opening Prompt"), "Is deleting roles safe here?")
        self.assertIn("<!--", (self.d / "submission.md").read_text().split("## Opening Prompt")[1].split("## ")[0])
        self.assertEqual(workspace.load(self.d)["sync"]["pulled"], ["Opening Prompt"])
        self.assertIn("pulled_from_studio", (self.d / "app" / "authorship.jsonl").read_text())
        # Nothing new the second time.
        self.assertEqual(actions.pull_from_studio(self.d)["pulled"], [])

    def test_studio_change_after_agreement_wins_when_local_is_unchanged(self):
        data_write(self.d, "goal", "First goal.")
        actions.push(self.d)
        studio_edit(self.d, field_high_level_goal="Better goal, edited in Studio.")
        self.assertEqual(actions.pull_from_studio(self.d)["pulled"], ["High-Level Task Goal"])
        self.assertEqual(local(self.d, "High-Level Task Goal"), "Better goal, edited in Studio.")

    def test_local_change_alone_is_kept(self):
        data_write(self.d, "goal", "First goal.")
        actions.push(self.d)
        data_write(self.d, "goal", "Edited here.")
        res = actions.pull_from_studio(self.d)
        self.assertEqual((res["pulled"], res["conflicts"]), ([], {}))
        self.assertEqual(local(self.d, "High-Level Task Goal"), "Edited here.")

    def test_both_changed_touches_neither(self):
        data_write(self.d, "goal", "First goal.")
        actions.push(self.d)
        data_write(self.d, "goal", "Edited here.")
        studio_edit(self.d, field_high_level_goal="Edited in Studio.")
        res = actions.pull_from_studio(self.d)
        self.assertEqual(res["pulled"], [])
        self.assertEqual(list(res["conflicts"]), ["High-Level Task Goal"])
        self.assertEqual(local(self.d, "High-Level Task Goal"), "Edited here.")
        with self.assertRaises(ActionError) as e:
            actions.push(self.d)
        self.assertEqual(e.exception.code, "sync_conflict")
        cf = fake.task(workspace.load(self.d)["task_id"])["custom_fields"]
        self.assertEqual(cf["field_high_level_goal"], "Edited in Studio.")

    def test_blank_in_studio_never_wipes_local(self):
        data_write(self.d, "codebase", "Notes here.")
        self.assertEqual(actions.pull_from_studio(self.d)["pulled"], [])
        self.assertEqual(local(self.d, "Difficulty & Codebase Understanding"), "Notes here.")

    def test_ratings_come_back_as_numbers(self):
        studio_edit(self.d, field_one_shot="No", field_task_success="2 - Partial, major issues",
                    field_steering="3 - Occasional input at a few key points")
        res = actions.pull_from_studio(self.d)
        self.assertEqual(sorted(res["pulled"]), ["One-shot", "Steering", "Task Success"])
        r = workspace.parse_submission(self.d)["ratings"]
        self.assertEqual((r["one-shot"], r["task success"], r["steering"]), ("No", "2", "3"))

    def test_prompt_used_by_a_run_is_not_replaced(self):
        data_write(self.d, "opening_prompt", "The prompt the run used.")
        self.run_("calib")
        studio_edit(self.d, field_opening_prompt="A different prompt.")
        res = actions.pull_from_studio(self.d)
        self.assertEqual(res["pulled"], [])
        self.assertIn("Opening Prompt", res["conflicts"])
        self.assertEqual(local(self.d, "Opening Prompt"), "The prompt the run used.")

    def test_prompt_written_only_in_studio_comes_in_even_after_a_run(self):
        self.run_("calib")
        studio_edit(self.d, field_opening_prompt="Written in Studio, pasted into the lane.")
        self.assertEqual(actions.pull_from_studio(self.d)["pulled"], ["Opening Prompt"])
        self.assertEqual(local(self.d, "Opening Prompt"), "Written in Studio, pasted into the lane.")

    def test_push_pulls_studio_edits_first(self):
        data_write(self.d, "goal", "First goal.")
        actions.push(self.d)
        studio_edit(self.d, field_high_level_goal="Studio goal.")
        res = actions.push(self.d)
        self.assertEqual(res["pulled"], ["High-Level Task Goal"])
        self.assertEqual(res["fields"]["field_high_level_goal"], "Studio goal.")

    def test_not_writing_pulls_nothing(self):
        studio_edit(self.d, field_high_level_goal="Studio goal.")
        fake.set_status(workspace.load(self.d)["task_id"], "Awaiting Review")
        self.assertEqual(actions.pull_from_studio(self.d)["pulled"], [])


class PreviousWriter(Base):
    """A slot can carry text a previous writer left in Studio. It never comes into Austin's fields."""

    LEFT = {"field_opening_prompt": "A previous writer's prompt about the importer.",
            "field_high_level_goal": "Their goal.", "field_self_check": "Their validation notes.",
            "field_task_success": "2 - Partial, major issues"}

    def claim_with_leftovers(self) -> Path:
        import test_actions_support as fx
        tid = fx.TASKS[1]["task_id"]
        with fake._LOCK:
            fake._get(tid)["custom_fields"].update(self.LEFT)
            fake._save()
        lead = actions.load_lead(fx.LEAD_ID)
        repo = actions.repo_info(lead["repo"], lead["commit"], lead["primary_language"])
        return actions.claim(tid, repo, lead)

    def test_claim_then_sync_leaves_submission_md_alone(self):
        d = self.claim_with_leftovers()
        before = (d / "submission.md").read_text()
        res = actions.pull_from_studio(d)
        self.assertEqual((res["pulled"], res["conflicts"]), ([], {}))
        self.assertEqual((d / "submission.md").read_text(), before)
        self.assertFalse((d / "app" / "authorship.jsonl").exists())
        self.assertEqual(sorted(workspace.load(d)["sync"]["foreign"]), sorted(self.LEFT))
        row = next(r for r in actions.checklist(d) if r["key"] == "studio_text")
        self.assertEqual((row["status"], row["blocking"]), ("fail", True))
        # Saving sends only Austin's text, never the leftovers as his.
        res = actions.push(d)
        self.assertEqual(res["pulled"], [])
        self.assertNotIn("field_opening_prompt", res["fields"])

    def test_his_text_overwrites_leftovers_and_clears_the_row(self):
        d = self.claim_with_leftovers()
        actions.pull_from_studio(d)
        for key, text in (("opening_prompt", "Mine."), ("goal", "My goal."), ("validation_notes", "My notes.")):
            data_write(d, key, text)
        sync.write_ratings_file(d, {"one_shot": "No", "task_success": 3, "steering": 2})
        self.assertEqual(actions.push(d)["pulled"], [])
        cf = fake.task(workspace.load(d)["task_id"])["custom_fields"]
        self.assertEqual(cf["field_opening_prompt"], "Mine.")
        self.assertNotIn("studio_text", [r["key"] for r in actions.checklist(d)])

    def test_his_studio_edit_after_the_claim_still_comes_in(self):
        d = self.claim_with_leftovers()
        studio_edit(d, field_codebase_overview="Typed in Studio after claiming.")
        self.assertEqual(actions.pull_from_studio(d)["pulled"], ["Difficulty & Codebase Understanding"])
        studio_edit(d, field_high_level_goal="Rewritten in Studio by me.")
        self.assertEqual(actions.pull_from_studio(d)["pulled"], ["High-Level Task Goal"])

    def test_no_claim_record_pulls_nothing_unagreed(self):
        workspace.save(self.d, {k: v for k, v in workspace.load(self.d).items() if k != "studio_at_claim"})
        studio_edit(self.d, field_high_level_goal="Text of unknown origin.")
        self.assertEqual(actions.pull_from_studio(self.d)["pulled"], [])


def data_write(d: Path, key: str, text: str) -> None:
    sync.write_section_file(d, data.SECTION_KEYS[key], text)


if __name__ == "__main__":
    unittest.main()
