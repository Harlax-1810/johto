"""The writing kit (johto/kit.py): its sections in order, the saved prompt from the Studio snapshot, rows grouped
by Austin's marks (none when he marked none), run evidence, limits, upstream links, open decisions,
rules and lessons; facts only, the same page for the same inputs, and write() after a collect.

Run: .venv/bin/python -m unittest tests.test_kit -v
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_delta import DIFF1, OPENING, TURN1, claude, result, user  # noqa: E402
from test_grading import DOSSIER, REPLY1, Base, bash, block, finding  # noqa: E402

from johto import grading, kit, lanes, stages  # noqa: E402

SECTIONS = ["## Task boundary", "## Repository map", "## Selected rows", "## Run evidence", "## Verification limits",
            "## Upstream context", "## Open decisions", "## Rules and lessons"]


class Kit(Base):
    def setUp(self):
        super().setUp()
        (self.root / "data" / "dossiers").mkdir(parents=True)
        (self.root / "data" / "dossiers" / f"{DOSSIER['id']}.json").write_text(json.dumps(DOSSIER))
        self.meta(dossier={"id": DOSSIER["id"], "opportunity_id": "o2"},
                  runs=[{"kind": "validate", "dir": "runs/validate-1", "host": "build-box",
                         "collected_at": "2026-10-30T11:00:00-0700"}])
        (self.d / "app").mkdir()
        (self.d / "app" / "studio-fields.json").write_text(json.dumps(
            {"at": "2026-10-30T12:00:00-0700", "fields": {"field_opening_prompt": OPENING}}))
        self.write_run(TURN1 + [user("2026-10-30T10:30:00Z", "Can you add a test for that?\nSecond line."),
                                bash("t9", "2026-10-30T10:31:00Z", "go test ./queue/ | tail -2"),
                                result("2026-10-30T10:31:20Z", "t9", "Exit code 1\nFAIL"),
                                claude("2026-10-30T10:32:00Z", "All tests pass now.", stop="end_turn")], DIFF1)
        self.review(REPLY1)
        self.refresh()

    def select_all(self):
        grading.mark(self.d, [r["id"] for r in grading.load(self.d)["rows"]], selected=True)

    def test_sections_in_order(self):
        self.select_all()   # Verification limits lists only selected rows
        text = kit.render(self.d)
        at = [text.find(s) for s in SECTIONS]
        self.assertTrue(all(i >= 0 for i in at), dict(zip(SECTIONS, at, strict=True)))
        self.assertEqual(at, sorted(at))
        self.assertTrue(text.startswith("# Writing kit · Task t1 · validate-1\n"))

    def test_task_boundary(self):
        (self.d / "submission.md").write_text("## Opening Prompt\n\nAn older local draft of the prompt.\n")
        text = kit.render(self.d)
        sec = text.split("## Task boundary")[1].split("\n## ")[0]
        self.assertIn("as saved in Studio (snapshot 2026-10-30", sec)
        self.assertIn("> " + OPENING, sec)
        self.assertIn("The local copy in submission.md differs from Studio's.", sec)
        self.assertIn("validate-1's first prompt matches this text.", sec)
        self.assertIn("- Slot: Modify · Backend / API · Personal-scale · Casual prose · Interactive/pairing", sec)
        self.assertIn(f"- Pinned commit: acme/widget @ {self.commit}", sec)
        self.assertIn("- Run: validate-1 · build-box · collected", sec)

    def test_no_marks_lists_no_rows(self):
        """Unvetted dossier hypotheses and review findings never stand in for Austin's selection."""
        text = kit.render(self.d)
        sec = text.split("## Selected rows")[1].split("\n## ")[0]
        self.assertIn("None marked yet: `johto grading t1`.", sec)
        self.assertEqual([ln for ln in sec.splitlines() if ln.startswith("- R")], [])
        self.assertNotIn("## Verification limits", text)

    def test_marked_rows_grouped(self):
        grading.mark(self.d, ["R1"], goal=True, dp=1)
        grading.mark(self.d, ["R2"], dp=2)
        grading.mark(self.d, ["R3"], notes=True)
        grading.mark(self.d, ["R4"], selected=True)
        sec = kit.render(self.d).split("## Selected rows")[1].split("\n## ")[0]
        heads = [ln for ln in sec.splitlines() if ln.startswith("### ")]
        self.assertEqual(heads, ["### Marked G", "### Marked 1", "### Marked 2", "### Marked N",
                                 "### Selected, no mark yet"])
        goal = sec.split("### Marked G")[1].split("###")[0]
        self.assertIn("- R1 · Requeue resets the attempts counter", goal)
        self.assertIn("result missed (validate-1 turn 1) · known: traced · `queue/queue.go:4`", goal)
        self.assertNotIn("None marked", sec)

    def test_run_evidence(self):
        sec = kit.render(self.d).split("## Run evidence")[1].split("\n## ")[0]
        self.assertRegex(sec, r"- Claude worked \d+\.\d min over 2 turns \(validate-1, transcript `runs/validate-1/")
        self.assertIn("  - 2. ", sec)
        self.assertIn("Can you add a test for that?", sec)
        self.assertNotIn("Second line", sec)                              # first line of each turn only
        self.assertIn("- Files edited: `queue/queue.go`", sec)
        self.assertIn("- Diff (`runs/validate-1/prework_output/final.diff`): 1 file, +1 -1", sec)
        self.assertIn("- Test runs detected in Claude's commands: 1 (0 with exit 0)", sec)
        self.assertIn("  - turn 2 · `go test ./queue/` · exit 1, piped", sec)
        self.assertIn('Claude said "All tests pass" (turn 2); the last test command detected before it', sec)

    def test_piped_test_runs_are_not_counted_as_passing(self):
        """`| tail` and `| grep` exit 0 whatever the tests did; the output decides when it shows a failure."""
        self.write_run(TURN1 + [user("2026-10-30T10:30:00Z", "Can you add tests?"),
                                bash("t7", "2026-10-30T10:31:00Z", "python -m pytest tests 2>&1 | tail -20"),
                                result("2026-10-30T10:31:10Z", "t7", "ModuleNotFoundError: No module named 'barcode'"),
                                bash("t8", "2026-10-30T10:31:30Z", "python -m pytest tests 2>&1 | grep -E 'ok|FAIL'"),
                                result("2026-10-30T10:31:40Z", "t8", "test_a ok\ntest_b ok"),
                                bash("t9", "2026-10-30T10:32:00Z", "go test ./queue/"),
                                result("2026-10-30T10:32:20Z", "t9", "ok queue 0.2s"),
                                claude("2026-10-30T10:33:00Z", "Done.", stop="end_turn")], DIFF1)
        sec = kit.render(self.d).split("## Run evidence")[1].split("\n## ")[0]
        self.assertIn("- Test runs detected in Claude's commands: 3 (1 with exit 0, 1 failed in their output, "
                      "1 hidden by a pipe)", sec)
        self.assertIn("failed (from output: ModuleNotFoundError: No module named 'barcode')", sec)
        self.assertIn("result hidden by a pipe", sec)

    def test_limits_are_never_cut(self):
        long = "Ran the batch upload in isolation with a stand-in clock; " * 6 + "the Vidlabel import path was not run."
        self.review("## Digest\n\nSecond.\n\n" + block([finding("F4", "The due date is lost at a batch boundary.",
                                                                     basis="isolated", limits=long)]), turn=2)
        rows = self.refresh()["rows"]
        r = next(r for r in rows if r["finding"].startswith("The due date is lost"))
        grading.mark(self.d, [r["id"]], selected=True)
        lim = kit.render(self.d).split("## Verification limits")[1].split("\n## ")[0]
        self.assertIn(" ".join(long.split()), lim)
        self.assertNotIn("…", lim)

    def test_limits_upstream_open_rules(self):
        self.select_all()
        text = kit.render(self.d)
        lim = text.split("## Verification limits")[1].split("\n## ")[0]
        self.assertIn(grading.DOSSIER_LIMITS, lim)
        up = text.split("## Upstream context")[1].split("\n## ")[0]
        self.assertIn("- [open] Backoff never grows https://github.com/acme/widget/issues/7 (dossier, as of 2026-10-30)", up)
        opened = text.split("## Open decisions")[1].split("\n## ")[0]
        self.assertIn("- R1 · no scope set, high severity, new in the latest review · Requeue resets", opened)
        rules = text.split("## Rules and lessons")[1]
        for head in ("### High-level goal", "### Decision points", "### Validation notes"):
            self.assertIn(head, rules)
        self.assertIn("- dp-count: ", rules)
        repo = text.split("## Repository map")[1].split("\n## ")[0]
        self.assertIn("- `queue/queue.go:3-5`: Requeue sets attempts to zero. (dossier acme__widget__abc12345 o2)", repo)

    def test_facts_only_and_deterministic(self):
        a, b = kit.render(self.d), kit.render(self.d)
        self.assertEqual(a, b)
        low = a.lower()
        for phrase in ("in your own words", "must cover", "should say", "suggested wording", "draft:"):
            self.assertNotIn(phrase, low)

    def test_write_refreshes_saves_and_logs(self):
        self.review(block([finding("F4", "The retry test was never run green.", severity="high")]), turn=2)
        p = kit.write(self.d)
        self.assertEqual(p, self.d / "kit.md")
        self.assertIn("The retry test was never run green.", p.read_text())   # write refreshed the table first
        ev = stages.last(self.d, "kit_written")
        self.assertEqual((ev["run"], ev["path"]), ("validate-1", str(p)))

    def test_lanes_write_kit_hook(self):
        out = lanes.write_kit(self.d, "validate-1")
        self.assertEqual(out, str(self.d / "kit.md"))
        self.assertTrue((self.d / "kit.md").exists())

    def test_collecting_a_stray_run_keeps_the_submission_runs_kit(self):
        """Austin chose validate-1; collecting an accidental validate-2 must not swap the kit's evidence."""
        (self.d / "runs" / "validate-2" / "prework_output").mkdir(parents=True)
        m = json.loads((self.d / "task.json").read_text())
        m["runs"].append({"kind": "validate", "dir": "runs/validate-2", "collected_at": "2026-10-31T11:00:00-0700"})
        m["submission_run"] = "validate-1"
        (self.d / "task.json").write_text(json.dumps(m))
        lanes.write_kit(self.d, "validate-2")
        self.assertTrue((self.d / "kit.md").read_text().startswith("# Writing kit · Task t1 · validate-1\n"))

    def test_empty_task(self):
        (self.d / "reviews").rename(self.d / "reviews-old")
        self.meta(dossier=None)
        (self.d / "grading.json").unlink()
        self.write_run(TURN1, DIFF1)
        text = kit.render(self.d)
        self.assertIn("No rows in the grading table yet.", text)
        self.assertNotIn("## Upstream context", text)   # empty sections are left out
        self.assertNotIn("## Open decisions", text)

    def test_cli(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers()
        kit.register(sub)
        out = io.StringIO()
        a = p.parse_args(["kit", "t1", "--quiet"])
        with contextlib.redirect_stdout(out):
            a.func(a)
        self.assertEqual(out.getvalue().strip(), str(self.d / "kit.md"))
        out = io.StringIO()
        a = p.parse_args(["kit", "t1", "--no-write"])
        with contextlib.redirect_stdout(out):
            a.func(a)
        self.assertTrue(out.getvalue().startswith("# Writing kit"))


if __name__ == "__main__":
    unittest.main()
