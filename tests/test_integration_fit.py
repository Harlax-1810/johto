"""Integration fixes that join the parallel builders' pieces: the one submission-run rule on the web side, the
details Done after a validation run, the checklist's lint over Studio's text, the queue's time phrases, the
kit event logged once, and the CLI wiring (check -> draftcheck, no command collisions)."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_realstate_app import SAME, call
from test_realstate_derive import RealRoot
from johto import actions, cli, lanes, stages
from johto.web.domain import derive, queue


class SubmissionRunOnTheWeb(unittest.TestCase):
    def runs(self):
        return [{"name": "validate-1", "kind": "validate", "collected_at": "2026-10-31T10:00:00-0700"},
                {"name": "validate-2", "kind": "validate", "live": True}]

    def test_the_resolved_run_wins(self):
        t = {"meta": {}, "runs": self.runs(), "submission_run": "validate-1"}
        self.assertEqual(derive.submission_run(t)["name"], "validate-1")

    def test_without_one_the_old_rule_still_names_the_run_under_way(self):
        t = {"meta": {}, "runs": self.runs()}
        self.assertEqual(derive.submission_run(t)["name"], "validate-2")
        t = {"meta": {}, "runs": self.runs()[1:], "submission_run": None}
        self.assertEqual(derive.submission_run(t)["name"], "validate-2")   # so the checklist can say "not collected"


class DetailsDoneAfterValidation(RealRoot):
    def test_details_done_after_a_validation_run_queues_no_second_run(self):
        short = "drate001"
        self.assertTrue(any(Path(r["dir"]).name.startswith("validate-") for r in self.meta(short)["runs"]))
        for key, text in (("goal", "Each failed upload is retried once and the queue is empty after one pass."),
                          ("decision_points", "Better runs read the retry loop first; worse ones patch the UI."),
                          ("codebase", "The retry loop lives in the upload client and the queue is in memory.")):
            call("PUT", f"/api/tasks/{short}/sections/{key}", {"text": text}, headers=SAME)
        self.put(short, ready=None, details_ready=None)
        st, out = call("POST", f"/api/tasks/{short}/done", {"surface": "details"}, headers=SAME)
        if st == 409:
            st, out = call("POST", f"/api/tasks/{short}/done",
                           {"surface": "details", "confirm": out["error"]["details"]["nonce"]}, headers=SAME)
        self.assertEqual(st, 200, out)
        m = self.meta(short)
        self.assertIn("details_ready", m)
        self.assertNotIn("ready", m)


class ChecklistLintReadsStudio(unittest.TestCase):
    def test_a_blank_local_section_studio_holds_is_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "app").mkdir()
            (d / "app" / "studio-fields.json").write_text(json.dumps(
                {"at": "2026-10-31T10:00:00-0700", "fields": {"field_high_level_goal": "Studio's goal text."}}))
            sub = {"sections": {"High-Level Task Goal": "", "Decision Points": "Mine."}, "ratings": {}}
            view = actions._lint_view(d, {}, sub)
            self.assertEqual(view["sections"]["High-Level Task Goal"], "Studio's goal text.")
            self.assertEqual(view["sections"]["Decision Points"], "Mine.")   # local text still wins
            sub2 = {"sections": {"High-Level Task Goal": "My newer local goal."}, "ratings": {}}
            self.assertEqual(actions._lint_view(d, {}, sub2)["sections"]["High-Level Task Goal"], "My newer local goal.")


class QueueTimePhrases(unittest.TestCase):
    def test_at_phrase(self):
        self.assertEqual(queue.at_phrase("13:24"), "at 13:24")
        self.assertEqual(queue.at_phrase("yesterday 13:24"), "yesterday at 13:24")
        self.assertEqual(queue.at_phrase("Oct 28 13:24"), "on Oct 28 at 13:24")


class WebTestFacts(unittest.TestCase):
    def test_a_formatter_on_a_tests_path_is_not_a_test_run(self):
        from johto.web.domain import facts
        self.assertFalse(facts.is_test('uvx "black>=26,<27" vidlabel/server/loans/tests/test_repeat_uploads.py'))
        self.assertTrue(facts.is_test("$SP/venv/bin/python manage.py test --settings vidlabel.settings.ci"))
        self.assertTrue(facts.is_test("npx mocha --reporter spec src/a.test.ts"))

    def test_a_cut_command_names_no_fragment(self):
        from johto.web.domain import facts
        cut = "F=vidlabel/server/loans/tests/test_repeat_uploads.py; uvx black $F 2>&1 | tail -1; git diff"
        self.assertEqual(facts._short_cmd(cut), "tests")


class KitEventOnce(unittest.TestCase):
    def test_write_kit_logs_once_when_the_kit_logs_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)

            class Kit:
                @staticmethod
                def write(task_dir, run):
                    (task_dir / "kit.md").write_text("kit")
                    stages.log(task_dir, "kit_written", run=run, path=str(task_dir / "kit.md"))
                    return task_dir / "kit.md"
            with mock.patch.object(lanes.importlib, "import_module", lambda name: Kit):
                lanes.write_kit(d, "validate-1")
            self.assertEqual(len(stages.read(d, ("kit_written",))), 1)


class CliWiring(unittest.TestCase):
    def test_no_command_collisions_and_every_module_registers(self):
        sub = argparse.ArgumentParser().add_subparsers()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            done = cli.register_modules(sub)
        self.assertEqual(err.getvalue(), "")
        for name in ("where", "kit", "grading", "deck", "focus", "budget", "reviews", "turnwatch", "rules"):
            self.assertIn(name, done)

    def test_check_uses_the_draft_check(self):
        out = io.StringIO()
        with mock.patch("johto.draftcheck.cmd_check", return_value=[("warn", "Goal", "x")]) as m, \
                contextlib.redirect_stdout(out):
            got = cli.cmd_check(argparse.Namespace(task="x"))
        m.assert_called_once()
        self.assertEqual(got, [("warn", "Goal", "x")])

    def test_check_parser_has_the_new_flags(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["check", "--help"])
        p = argparse.ArgumentParser()
        from johto import draftcheck
        s = p.add_subparsers().add_parser("check")
        s.add_argument("task")
        s.add_argument("--run")
        draftcheck.add_check_args(s)
        a = s.parse_args(["t1", "--deep", "--full", "--all", "--refresh", "--run", "validate-1"])
        self.assertEqual((a.deep, a.full, a.all, a.refresh, a.run), (True, True, True, True, "validate-1"))


if __name__ == "__main__":
    unittest.main()
