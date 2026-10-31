"""lanes.collect for a run whose lane lives on another machine (the PC): collect there, copy outputs back.

Run: .venv/bin/python -m unittest tests/test_lane_remote.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from johto import lanes, pack, workspace  # noqa: E402


class RemoteCollect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name) / "abc"
        (self.d / "runs" / "validate-1").mkdir(parents=True)
        self.run = {"kind": "validate", "dir": "runs/validate-1", "container": "prework-X-model_a"}
        workspace.save(self.d, {"task_name": "Task abc", "runs": [self.run]})

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_run(self, calls):
        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "rsync":  # the copy back lands the outputs
                out = Path(cmd[-1])
                out.mkdir(parents=True, exist_ok=True)
                (out / "final.diff").write_text("diff --git a/x b/x\n")
            return subprocess.CompletedProcess(cmd, 0)
        return run

    def test_remote_run_collects_over_ssh_and_copies_back(self):
        self.run.update(host="build-box", remote_dir="/home/u/johto-lanes/abc/validate-1")
        workspace.save(self.d, {"task_name": "Task abc", "runs": [self.run]})
        calls = []
        with mock.patch.object(lanes.subprocess, "run", self._fake_run(calls)):
            info = lanes.collect(self.d, "validate-1")
        self.assertEqual(calls[0][:2], ["ssh", "build-box"])
        self.assertIn("cd /home/u/johto-lanes/abc/validate-1 && ~/.local/bin/mercor-prework collect", calls[0][2])
        self.assertEqual(calls[1], ["rsync", "-a", "build-box:/home/u/johto-lanes/abc/validate-1/prework_output/",
                                    f"{self.d}/runs/validate-1/prework_output/"])
        self.assertTrue(info["diff"].endswith("final.diff"))
        self.assertTrue(workspace.load(self.d)["runs"][0].get("collected_at"))

    def test_local_run_still_uses_local_prework(self):
        calls = []
        with mock.patch.object(lanes.subprocess, "run", self._fake_run(calls)):
            lanes.collect(self.d, "validate-1")
        self.assertEqual(calls, [[lanes.PREWORK, "collect"]])

    def test_remote_collect_failure_is_reported(self):
        self.run.update(host="build-box", remote_dir="/r")
        workspace.save(self.d, {"task_name": "Task abc", "runs": [self.run]})
        fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1)  # noqa: E731
        with mock.patch.object(lanes.subprocess, "run", fail):
            with self.assertRaisesRegex(RuntimeError, "on build-box"):
                lanes.collect(self.d, "validate-1")


class RemotePack(unittest.TestCase):
    """johto pack mid-run on a PC lane: transcript copied back, diff read over ssh, both read-only."""
    C = "prework-PREWORK_abc-123-model_a"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name) / "abc"
        self.run_dir = self.d / "runs" / "validate-1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "prework-manifest.json").write_text(json.dumps(
            {"base_commit": "a" * 40, "lane": {"container": self.C, "workspace": "/exp/PREWORK_abc/model_a"}}))
        workspace.save(self.d, {"task_name": "Task abc", "runs": [
            {"kind": "validate", "dir": "runs/validate-1", "container": self.C,
             "host": "build-box", "remote_dir": "/home/u/johto-lanes/abc/validate-1"}]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_transcript_is_copied_back_from_the_pc(self):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            proj = Path(cmd[-1]) / "-exp-PREWORK-abc-model-a"
            proj.mkdir(parents=True, exist_ok=True)
            (proj / "s.jsonl").write_text("{}\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(pack, "_run", run):
            t = pack._live_transcript(self.run_dir)
        self.assertEqual(calls[0][:3], ["rsync", "-a",
                                        "build-box:.mercor-prework/cc-config/PREWORK_abc-123_model_a/projects/"])
        self.assertEqual(t.name, "s.jsonl")

    def test_diff_is_read_inside_the_pc_lane(self):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            out = "abc123\n" if "docker ps" in cmd[-1] else "diff --git a/x b/x\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        with mock.patch.object(pack, "_run", run), mock.patch.object(pack.lanes, "running_lanes", lambda: []):
            diff = pack._live_diff(self.run_dir)
        self.assertEqual(diff, "diff --git a/x b/x\n")
        self.assertEqual(calls[1][:2], ["ssh", "build-box"])
        self.assertTrue(calls[1][2].startswith(f"docker exec -u lane {self.C} bash -c "))

    def test_stopped_pc_lane_gives_no_live_diff(self):
        stopped = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")  # noqa: E731
        with mock.patch.object(pack, "_run", stopped):
            self.assertIsNone(pack._live_diff(self.run_dir))


if __name__ == "__main__":
    unittest.main()
