"""`johto dossier brief`, `brief-show` and `johto grading plain` (the argv rewrite), with the stub claude."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from test_brief import DID, FakeClaude  # noqa: E402

from johto import cli as top  # noqa: E402
from johto.dossier import brief  # noqa: E402
from johto.dossier import cli  # noqa: E402


def ns(**kw):
    base = dict(target="all", force=False, verbose=False, json=False)
    return argparse.Namespace(**{**base, **kw})


class BriefCli(FakeClaude):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_PLAN"] = json.dumps(["valid"])

    def run_cmd(self, fn, **kw) -> tuple[str, int]:
        out, code = io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                fn(ns(**kw))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                if not isinstance(e.code, int):
                    out.write(str(e.code))
        return out.getvalue(), code

    def test_targets(self):
        self.assertEqual(cli._brief_targets("all"), [DID])
        self.assertEqual(cli._brief_targets(DID), [DID])
        self.assertEqual(cli._brief_targets("acme/vidlabel"), [DID])
        self.assertEqual(cli._brief_targets("ACME/Vidlabel/"), [DID])
        with self.assertRaises(SystemExit):
            cli._brief_targets("nobody/here")

    def test_all_writes_then_skips_ready(self):
        out, code = self.run_cmd(cli.cmd_brief)
        self.assertEqual(code, 0, out)
        self.assertIn(f"{DID}: ready  (", out)
        self.assertIn("briefs: 1 written, 0 failed", out)
        self.assertEqual(brief.state(DID), "ready")
        out, _ = self.run_cmd(cli.cmd_brief)
        self.assertIn(f"{DID}: ready, skipped", out)
        self.assertIn("briefs: 0 written", out)
        self.assertFalse((self.tmp / "args.1").exists(), "a ready brief is not written again")

    def test_force_writes_again_and_keeps_history(self):
        self.install_example()
        out, code = self.run_cmd(cli.cmd_brief, target=DID, force=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(brief.history(DID)), 1)

    def test_a_failure_exits_1_and_writes_nothing(self):
        os.environ["FAKE_PLAN"] = json.dumps(["invalid", "invalid"])
        out, code = self.run_cmd(cli.cmd_brief)
        self.assertEqual(code, 1, out)
        self.assertIn("FAILED", out)
        self.assertIn("1 failed", out)
        self.assertFalse(brief.path(DID).exists())

    def test_show(self):
        out, _ = self.run_cmd(cli.cmd_brief_show, target=DID)
        self.assertIn("(no brief; the dossier says)", out)
        self.install_example()
        out, _ = self.run_cmd(cli.cmd_brief_show, target=DID)
        self.assertIn("Start from one of these:", out)
        self.assertIn("Rival:", out)
        out, _ = self.run_cmd(cli.cmd_brief_show, target=DID, json=True)
        self.assertEqual(json.loads(out)["state"], "ready")


class GradingPlainArgv(unittest.TestCase):
    def test_grading_plain_is_rewritten(self):
        seen = {}
        with mock.patch("johto.plain.cmd_plain", lambda a: seen.update(task=a.task, force=a.force)):
            try:
                top.main(["grading", "plain", "abcd1234", "--force"])
            except SystemExit as e:
                self.assertIn(e.code, (0, None))
        self.assertEqual(seen, {"task": "abcd1234", "force": True})


if __name__ == "__main__":
    unittest.main()
