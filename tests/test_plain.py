"""johto/plain.py: hide_code (every screen's fallback filter), the never-list, and plain findings (storage, the sha
staleness, generation with a fake runner, ensure in fake mode). Never a real model call."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from johto import config, plain


class HideCode(unittest.TestCase):
    def check(self, text, want):
        self.assertEqual(plain.hide_code(text), want)

    def test_backticks_and_the_word_before(self):
        self.check("The sync in `ledger.py` drops scans when batches overlap.",
                   "The sync drops scans when batches overlap.")
        self.check("The `open_loans` list is a copy of every open loan in the batch.",
                   "The list is a copy of every open loan in the batch.")

    def test_fenced_code(self):
        self.check("Reprints leave files behind.\n```\nrm -rf spool/labels\n```\nNothing cleans them up later.",
                   "Reprints leave files behind.\nNothing cleans them up later.")

    def test_paths_and_path_line(self):
        self.check("See vidlabel/server/loans/ledger.py:212-218 and #412.", "")
        self.check("The rule lives in settings.py and the reminder run reads loans before checking dates.",
                   "The rule lives and the reminder run reads loans before checking dates.")
        self.check("Scan ordering (#318) looks like a cause but is fixed by order_by(\"id\") in "
                   "scanner/upload.py:140 and tested in test_ledger.py.",
                   "Scan ordering looks like a cause but is fixed and tested.")

    def test_issue_numbers_and_hashes(self):
        self.check("Reminders stop for loans, renewals and holds (#412).",
                   "Reminders stop for loans, renewals and holds.")
        self.check("Fixed in 9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c last week by the core team.",
                   "Fixed last week by the core team.")
        self.check("Upstream PR 731 touches retired barcodes in reprints only, nothing else.",
                   "Upstream touches retired barcodes in reprints only, nothing else.")
        self.check("Commit b7c41e2 is a refactor of the strings only, by the maintainer.",
                   "Is a refactor of the strings only, by the maintainer.")

    def test_identifiers(self):
        self.check("getLoansByShelf builds a new list holding every open old loan.",
                   "Builds a new list holding every open old loan.")
        self.check("Restricted SMTP_RELAYS cuts the reminder service off from its mail queue after a restart.",
                   "Restricted cuts the reminder service off from its mail queue after a restart.")
        self.check("Calls Foo::bar() on every row of the merged table before export.",
                   "Calls on every row of the merged table before export.")
        self.check("The matched loan is written into a local per-shelf list, not into self.loans.",
                   "The matched loan is written into a local per-shelf list, not.")

    def test_urls_flags_and_line_numbers(self):
        self.check("Details at https://github.com/x/y/issues/12 explain it well enough for now.",
                   "Details explain it well enough for now.")
        self.check("Start the importer with --dry-run to see the failure in the export.",
                   "Start the importer to see the failure in the export.")
        # A sentence that names a command to run is dropped whole (it was test plumbing, never his concern).
        self.check("Run pytest with --with-services to see the failure in the export.", "")
        self.check("Exports drop the returned loan. All testable with go test, no paid services.",
                   "Exports drop the returned loan.")
        self.check("The relay list is read on line 42 when the reminder service starts up.",
                   "The relay list is read when the reminder service starts up.")

    def test_opportunity_ids(self):
        self.check("Open issues on repeat scans overlap o1 and o2 in the dossier research.",
                   "Open issues on repeat scans overlap in the dossier research.")

    def test_product_names_and_plain_text_stay(self):
        for text in ("Runs on iOS and macOS with PostgreSQL, ClickHouse and Node.js today.",
                     "A shelf(s) count stays the same for every loan in the batch.",
                     "The read/write split holds for every user in the organization.",
                     "Exported loan ids are sorted-list indexes, so they shift when loans are added.",
                     '29,184 ledger tests pass, and "the second scan is thrown away" says why.'):
            self.check(text, text)

    def test_too_little_left(self):
        self.check("LoanLedger._close_batches never closes elements.", "")
        self.check("", "")
        self.check(None, "")
        self.assertEqual(plain.hide_code("Jobs stuck", min_words=1), "Jobs stuck")


class Strict(unittest.TestCase):
    def test_strict_drops_the_sentence_that_lost_a_name(self):
        """A title or finding with a hole where a code name was reads wrong; strict mode drops that sentence."""
        text = "Claude edits apiRoomHandler to stop it. The tests then pass on the first try."
        self.assertEqual(plain.hide_code(text, 3, strict=True), "The tests then pass on the first try.")
        self.assertIn("Claude edits to stop it.", plain.hide_code(text, 3))


class Problems(unittest.TestCase):
    def assertFlags(self, text, needle, **kw):
        self.assertTrue(any(needle in p for p in plain.problems(text, **kw)), (text, plain.problems(text, **kw)))

    def test_each_rule(self):
        self.assertFlags("The export lives in exports/loans.py now.", "names a file or path")
        self.assertFlags("The bug sits on line 488 of the sync.", "has a line number")
        self.assertFlags("The sync reads open_loans as a copy.", "has a code identifier")
        self.assertFlags("The getLoans helper drops scans.", "has a code identifier")
        self.assertFlags("LoanLedger.close drops scans.", "has a code identifier")
        self.assertFlags("It needs pip install freezegun first.", "has a command")
        self.assertFlags("Start it with --force.", "has a command-line flag")
        self.assertFlags("It changed in 1.6.0 for reports.", "has a version number")
        self.assertFlags("It shipped in 5ad0c0de last week.", "has a commit hash")
        self.assertFlags("Upstream records it as #412.", "has an issue or PR number")
        self.assertFlags("It ended with exit code 1.", "mentions an exit code")
        self.assertFlags("Opus found it in an hour.", "names a model or effort")
        self.assertFlags("It ran at high effort.", "names a model or effort")
        self.assertFlags("The run cost $3 in all.", "mentions a cost")
        self.assertFlags("Exports drop loans — silently.", "em or en dash")
        self.assertFlags("Fix the sync so loans survive.", "reads as an instruction")
        self.assertFlags("Loans vanish. Make the sync keep them.", "reads as an instruction")
        self.assertFlags("Your exports drop loans.", "addresses the reader")
        self.assertFlags("As a user, I want whole loans.", "goal or user story")
        self.assertFlags("A fix should keep holds as they are.", "better-or-worse")
        self.assertFlags("Holds sync better than renewals.", "better-or-worse")
        self.assertFlags("A robust catalogue server.", "marketing word")
        self.assertFlags("Do exports keep loans?", "is a question")

    def test_allowed(self):
        self.assertEqual(plain.problems("Do exports keep loans whole across batches?", questions=True), [])
        self.assertEqual(plain.problems("The catalogue runs on PostgreSQL and ClickHouse in Docker, with a better-funded "
                                        "rival since 2024."), [])
        self.assertEqual(plain.problems("Claude got the sync right but missed the renewal dates."), [])
        self.assertEqual(plain.problems("It runs under Docker Compose, and the Android app builds with Gradle."), [])
        self.assertFlags("Start it with docker-compose up first.", "has a command")
        self.assertEqual(plain.problems("Daily totals reset at 0:50 for some, in 16:9 charts at 10:30."), [])
        self.assertFlags("The check fails in ledger.py:488 on export.", "has a line number")
        self.assertEqual(plain.problems("Casting 3.141 to a whole-number column keeps 3, and 1.250 keeps 1."), [])


class Storage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name) / "t1"
        self.d.mkdir()
        (self.d / "task.json").write_text(json.dumps({"repo": {"full_name": "acme/vidlabel",
                                                                "description": "Catalogue and labels for tape archives."}}))
        self.rows = [
            {"id": "R1", "finding": "The sync in ledger.py:488 throws the second scan away.", "result": {}},
            {"id": "R2", "finding": "Claude kept holds syncing as before.", "result": {}},
            {"id": "R3", "finding": "Withdrawn: nothing here.", "review_status": "withdrawn"},
            {"id": "R4", "finding": "", "result": {}},
        ]
        self.write_grading()

    def tearDown(self):
        self._tmp.cleanup()

    def write_grading(self):
        (self.d / "grading.json").write_text(json.dumps({"version": 1, "rows": self.rows}))

    def runner(self, answer: dict, cost: float = 0.01):
        calls = []

        def run(prompt):
            calls.append(prompt)
            return {"text": "ok\n" + plain.BEGIN + "\n" + json.dumps(answer) + "\n" + plain.END, "cost_usd": cost,
                    "error": None}
        run.calls = calls
        return run

    def test_missing_skips_withdrawn_and_empty_rows(self):
        self.assertEqual(plain.missing(self.d), ["R1", "R2"])
        self.assertEqual(plain.load(self.d), {})

    def test_generate_keeps_valid_rows_and_drops_bad_ones(self):
        run = self.runner({"R1": "Claude's sync throws away the second scan when the later batch arrives first.",
                           "R2": "See ledger.py for the hold sync, which Claude kept working."})
        res = plain.generate(self.d, runner=run)
        self.assertEqual(res["written"], ["R1"])
        self.assertIn("names a file or path (ledger.py)", res["dropped"]["R2"])
        self.assertEqual(plain.load(self.d), {"R1": "Claude's sync throws away the second scan when the later "
                                                    "batch arrives first."})
        self.assertEqual(plain.missing(self.d), ["R2"])
        data = json.loads((self.d / "plain.json").read_text())
        self.assertEqual(data["schema"], "johto.plain/1")
        self.assertEqual(data["rows"]["R1"]["sha"], plain.sha(self.rows[0]["finding"]))
        prompt = run.calls[0]
        self.assertIn('"R1": "The sync in ledger.py:488 throws the second scan away."', prompt)
        self.assertNotIn("R3", prompt)
        self.assertIn("vidlabel: Catalogue and labels for tape archives.", prompt)

    def test_word_limit(self):
        res = plain.generate(self.d, ["R2"], runner=self.runner({"R2": " ".join(["holds"] * 31)}))
        self.assertEqual(res["dropped"]["R2"], ["has 31 words, limit 30"])

    def test_a_changed_finding_is_stale(self):
        plain.generate(self.d, runner=self.runner({"R1": "Claude's sync throws away the second scan.",
                                                   "R2": "Claude kept holds syncing the way they did before."}))
        self.assertEqual(plain.missing(self.d), [])
        self.rows[1]["finding"] = "Claude kept holds syncing, but expired holds now sync with live ones."
        self.write_grading()
        self.assertEqual(plain.missing(self.d), ["R2"])
        self.assertNotIn("R2", plain.load(self.d))
        self.assertIn("R2", plain.stored_texts(self.d), "a stale row was shown once, so the guard keeps it")
        run = self.runner({"R2": "Claude kept holds syncing, but expired holds now sync with current ones."})
        plain.generate(self.d, runner=run)
        self.assertEqual(plain.missing(self.d), [])
        self.assertNotIn('"R1": "The sync', run.calls[0], "only the stale row is written again")

    def test_failed_call_writes_nothing(self):
        res = plain.generate(self.d, runner=lambda p: {"text": "", "cost_usd": None, "error": "boom"})
        self.assertEqual((res["written"], res["error"]), ([], "boom"))
        self.assertFalse((self.d / "plain.json").exists())

    def test_answer_without_markers_still_parses(self):
        run = lambda p: {"text": '```json\n{"R1": "Claude\'s sync throws away the second scan."}\n```',
                         "cost_usd": 0, "error": None}
        self.assertEqual(plain.generate(self.d, ["R1"], runner=run)["written"], ["R1"])

    def test_ensure_is_a_no_op_in_fake_mode_without_a_stub(self):
        with mock.patch.object(config, "FAKE", True), mock.patch.object(config, "CLAUDE_BIN_SET", False):
            self.assertIsNone(plain.ensure(self.d))
            self.assertFalse(plain.available())

    def test_ensure_starts_one_job(self):
        from johto.web import jobs
        run = self.runner({"R1": "Claude's sync throws away the second scan.",
                           "R2": "Claude kept holds syncing the way they did before."})
        job = plain.ensure(self.d, runner=run)
        self.assertEqual((job["kind"], job["target"]), ("plain", "t:t1"))
        done = jobs.wait(job["id"], 10)
        self.assertEqual(done["state"], "succeeded", done)
        self.assertEqual(done["result"], {"written": 2, "dropped": 0})
        self.assertIsNone(plain.ensure(self.d, runner=run), "nothing missing any more")


if __name__ == "__main__":
    unittest.main()
