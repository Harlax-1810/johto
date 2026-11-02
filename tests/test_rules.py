"""docs/rules.md parses, newer rules supersede older ones, and reviewer lessons come out of real feedback.

Run: .venv/bin/python -m unittest tests/test_rules.py
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from johto import config, rules  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RUBRIC = config.CHECKOUT / "docs" / "reference" / "qc_rubrics.md"
GROUPS = ["Picking a repo and slot", "Opening prompt by style", "Lanes and runs", "Interaction modes",
          "High-level goal", "Decision points", "Difficulty & codebase", "Ratings and pass matrix",
          "Validation notes", "Submitting", "LLM policy", "Reviewer lessons"]


def feedback() -> dict:
    return json.loads((FIXTURES / "reviewer_feedback.json").read_text())


class RulesFile(unittest.TestCase):
    """The real docs/rules.md."""

    @classmethod
    def setUpClass(cls):
        cls.rules = rules.load()
        cls.by_id = {r["id"]: r for r in cls.rules}

    def test_parses_with_every_group_in_order(self):
        self.assertGreater(len(self.rules), 80)
        seen = list(dict.fromkeys(r["group"] for r in self.rules))
        self.assertEqual(seen, GROUPS)

    def test_entries_are_complete(self):
        for r in self.rules:
            with self.subTest(r["id"]):
                self.assertRegex(r["id"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertTrue(r["text"] and r["source"])
                self.assertRegex(r["date"], r"^2026-\d{2}-\d{2}$")
                self.assertIn(r["status"], ("active", "superseded"))
                self.assertTrue(set(r["fields"]) <= set(rules.NAMES))
                self.assertEqual(r["applies_to"], r["fields"][0])
                self.assertNotIn("—", r["text"] + r["source"])

    def test_superseded_rules_point_at_newer_active_ones(self):
        old = [r for r in self.rules if r["status"] == "superseded"]
        self.assertTrue(old)
        for r in old:
            new = self.by_id[r["superseded_by"]]
            self.assertEqual(new["status"], "active", r["id"])
            self.assertGreaterEqual(new["date"], r["date"], r["id"])

    def test_austin_task_order_wins_over_the_doc(self):
        self.assertEqual(self.by_id["calib-first"]["superseded_by"], "no-calib")
        self.assertEqual(self.by_id["goal-before-run"]["superseded_by"], "goal-after-start")
        for rid in ("no-calib", "prompt-before-run", "goal-after-start", "fields-after-run"):
            self.assertEqual(self.by_id[rid]["status"], "active", rid)
            self.assertEqual(self.by_id[rid]["date"], "2026-10-31", rid)
        # The goal and the other fields may come after the run; the prompt may not.
        self.assertNotIn("fields-after-run", [r["id"] for r in rules.for_field("opening_prompt")])
        self.assertIn("fields-after-run", [r["id"] for r in rules.for_field("Validation Notes")])

    def test_transcript_upload_rule_churn(self):
        for rid in ("submit-no-transcript", "submit-links"):
            self.assertEqual(self.by_id[rid]["superseded_by"], "submit-trajectory-logs")
        self.assertIn("Trajectory Logs", self.by_id["submit-trajectory-logs"]["text"])

    def test_reviewer_feedback_is_in_the_file(self):
        self.assertIn("500 failed rows", self.by_id["lesson-goal-threshold"]["text"])
        self.assertIn("it fo", self.by_id["lesson-saved-whole"]["text"])

    @unittest.skipUnless(RUBRIC.exists(), "Mercor's rubric isn't in this copy")
    def test_prompt_qc_dimensions_match_the_rubric(self):
        text = RUBRIC.read_text()
        autoqc = text.split("\n# ", 2)[0] if text.startswith("# ") else text.split("\n# ")[1]
        part = autoqc.split("## Opening Prompt", 1)[1].split("\n## ", 1)[0]
        names = tuple(re.findall(r"^### (.+?) \(", part, re.M))
        self.assertEqual(names, rules.PROMPT_QC_DIMENSIONS[:-1])
        self.assertIn("### AI Detection (", autoqc)
        self.assertEqual(rules.PROMPT_QC_DIMENSIONS[-1], "AI Detection")


class Parse(unittest.TestCase):
    GOOD = """# Title

Intro text with - a dash and `code` that is not a rule.

## Group one

- First rule, with text that wraps
  onto a second line.
  `first · goal · Onboarding doc, X · 2026-10-30 · superseded by second`
- Second rule.
  `second · High-Level Task Goal, field_self_check · Austin, Slack, 2026-10-31 · 2026-10-31 · active`

## Group two

- Third rule.
  `third · fields · Studio form · 2026-10-30 · active`
"""

    def test_format(self):
        rs = rules.parse(self.GOOD)
        self.assertEqual([r["id"] for r in rs], ["first", "second", "third"])
        a, b, c = rs
        self.assertEqual(a["text"], "First rule, with text that wraps onto a second line.")
        self.assertEqual((a["status"], a["superseded_by"], a["group"]), ("superseded", "second", "Group one"))
        self.assertEqual(b["fields"], ["goal", "validation_notes"])
        self.assertEqual(b["source"], "Austin, Slack, 2026-10-31")
        self.assertEqual((c["applies_to"], c["group"], c["superseded_by"]), ("fields", "Group two", None))

    def test_for_field(self):
        rs = rules.parse(self.GOOD)
        ids = lambda name, **kw: [r["id"] for r in rules.for_field(name, rules=rs, **kw)]  # noqa: E731
        self.assertEqual(ids("goal"), ["second", "third"])
        self.assertEqual(ids("field_high_level_goal"), ids("High-Level Task Goal"))
        self.assertEqual(ids("goal", include_superseded=True), ["second", "third", "first"])
        self.assertEqual(ids("Validation Notes"), ["second", "third"])
        self.assertEqual(ids("submit"), [])  # a stage, not a written field

    def test_errors(self):
        bad = {
            "no tail": "## G\n\n- A rule.\n",
            "four parts": "## G\n\n- A rule.\n  `a · goal · src · active`\n",
            "unknown field": "## G\n\n- A rule.\n  `a · gaol · src · 2026-10-30 · active`\n",
            "bad date": "## G\n\n- A rule.\n  `a · goal · src · Oct 30 · active`\n",
            "bad status": "## G\n\n- A rule.\n  `a · goal · src · 2026-10-30 · old`\n",
            "dangling": "## G\n\n- A rule.\n  `a · goal · src · 2026-10-30 · superseded by b`\n",
            "duplicate": "## G\n\n- A.\n  `a · goal · s · 2026-10-30 · active`\n- B.\n  `a · goal · s · 2026-10-30 · active`\n",
        }
        for name, text in bad.items():
            with self.subTest(name), self.assertRaises(rules.RulesError):
                rules.parse(text)

    def test_key_aliases(self):
        for name, want in (("Opening Prompt", "opening_prompt"), ("field_rationale_difficulty", "decision_points"),
                           ("Difficulty & Codebase", "codebase"), ("Difficulty & Codebase Understanding", "codebase"),
                           ("Notes for the Reviewer", "reviewer_notes"), ("field_steering", "ratings"),
                           ("Runs", "run"), ("Initial State / Setup", "initial_state")):
            self.assertEqual(rules.key(name), want, name)


class Lessons(unittest.TestCase):
    def test_real_feedback(self):
        fb = feedback()
        got = rules.extract_lessons(fb["t4sk0001"]["feedback"])
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].startswith("The goal should state the row limit."), got[0])
        self.assertIn("500-row cutoff", got[0])
        self.assertEqual(rules.lesson_field(got[0]), "goal")
        got = rules.extract_lessons(fb["t4sk0003"]["feedback"])
        self.assertEqual(len(got), 1)
        self.assertIn('cut off mid-word at the end ("when I ship the second half it fo")', got[0])
        self.assertNotIn("**", got[0])
        self.assertEqual(rules.lesson_field(got[0]), "validation_notes")

    def test_praise_only_gives_nothing(self):
        fb = feedback()["t4sk0003"]["feedback"].split("**What to fix")[0]
        self.assertEqual(rules.extract_lessons(fb), [])
        self.assertEqual(rules.extract_lessons(""), [])
        self.assertEqual(rules.extract_lessons("Accepted, nice work.\n\n* The goal is gradable."), [])

    def test_other_shapes(self):
        self.assertEqual(rules.extract_lessons("Good task.\nWhat to improve: name the threshold in the goal."),
                         ["name the threshold in the goal."])
        self.assertEqual(rules.extract_lessons("Good.\n\nNext time, keep the decision points to three.\n"),
                         ["Next time, keep the decision points to three."])
        two = ("Review 1\nSent back.\nThings to fix:\n- The goal is vague.\n- Steering looks too high.\n\n"
               "Review 2\nAccepted.\n* Clear goal now.\n")
        self.assertEqual(rules.extract_lessons(two), ["The goal is vague.", "Steering looks too high."])
        # A praise bullet that mentions "next time" does not start the improve part.
        self.assertEqual(rules.extract_lessons("Accepted.\n* Keep doing this next time.\n"), [])

    def test_lessons_reads_task_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for short, st in feedback().items():
                (root / short).mkdir()
                (root / short / "task.json").write_text(json.dumps({"studio": st}))
            (root / "nofeedback").mkdir()
            (root / "nofeedback" / "task.json").write_text(json.dumps({"studio": {"feedback": None}}))
            (root / "broken").mkdir()
            (root / "broken" / "task.json").write_text("{not json")
            got = rules.lessons(root)
        self.assertEqual(sorted(x["task"] for x in got), ["t4sk0001", "t4sk0003"])
        self.assertTrue(all(x["date"] == "2026-03-10" for x in got))
        self.assertEqual({x["task"]: x["field"] for x in got}, {"t4sk0001": "goal", "t4sk0003": "validation_notes"})


class Command(unittest.TestCase):
    def run_cli(self, *argv) -> str:
        p = argparse.ArgumentParser(prog="johto")
        rules.register(p.add_subparsers(dest="cmd"))
        a = p.parse_args(["rules", *argv])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            a.func(a)
        return buf.getvalue()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for short, st in feedback().items():
            (root / short).mkdir()
            (root / short / "task.json").write_text(json.dumps({"studio": st}))
        self.patch = mock.patch.object(config, "TASKS", root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_all_rules_active_first(self):
        out = self.run_cli()
        self.assertTrue(out.startswith("Picking a repo and slot\n"))
        self.assertIn("\nSuperseded\n", out)
        active, old = out.split("\nSuperseded\n")
        self.assertIn("no-calib", active)
        self.assertIn("calib-first", old)
        self.assertIn("replaced by no-calib", old)
        self.assertNotIn("—", out)

    def test_one_field(self):
        out = self.run_cli("--field", "High-Level Task Goal")
        self.assertIn("goal-specific", out)
        self.assertIn("llm-protected", out)
        self.assertNotIn("dp-count", out)
        self.assertIn("From your reviews", out)
        self.assertIn("t4sk0001 (2026-03-10): The goal should state the row limit", out)
        self.assertNotIn("t4sk0003", out.split("From your reviews")[1])

    def test_lessons(self):
        out = self.run_cli("--lessons")
        self.assertIn("t4sk0001 (2026-03-10)", out)
        self.assertIn("t4sk0003 (2026-03-10)", out)

    def test_unknown_field(self):
        with self.assertRaises(SystemExit):
            self.run_cli("--field", "vibes")


if __name__ == "__main__":
    unittest.main()
