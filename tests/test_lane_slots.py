"""A second lane on one machine: johto/prework_slot.py moves its host ports and CPUs, and johto picks the slot.

Run: .venv/bin/python -m unittest tests/test_lane_slots.py
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from johto import cli, lanes, prework_slot, workspace  # noqa: E402

REAL_PREWORK = Path.home() / ".local/share/mercor-prework/prework-v2/prework.py"

# The parts of prework.py that prework_slot touches, in the same shape.
FAKE_PREWORK = textwrap.dedent('''
    import sys
    LANE_PORT_RANGES = (("3000-3019", "34000-34019"), ("8000-8009", "35000-35009"))
    SMALL_ENGINE_GIB = 8
    WRAPPER_SH = """    if [ "$LIMIT" -lt 2 ]; then
          CPUSET="0-$(( NCPU - 1 ))"
        else
          N=3
          CPUSET="0-$(( N - 1 ))"
        fi
    """
    GIB = 15.0
    CALLS = []
    def lane_cpu_args(engine_gib, ncpu):
        n = ncpu if engine_gib < 8 else 3
        return ["--cpuset-cpus", f"0-{n - 1}"]
    def engine_resources():
        return GIB, 16
    def small_engine(gib):
        return int(gib + 0.5) < SMALL_ENGINE_GIB
    def error(message):
        print("[ERROR] " + message, file=sys.stderr)
        sys.exit(1)
    def main(argv=None):
        CALLS.append((list(argv), LANE_PORT_RANGES, lane_cpu_args(15.0, 16)))
''')


def cpu_block(slot: int, n: int, ncpu: int) -> str:
    """Run the patched CPUSET lines in bash, as claude-dev does, and return the cpuset."""
    lines = prework_slot.apply(_FakeMod(), slot).WRAPPER_SH
    snippet = "".join(ln + "\n" for ln in lines.splitlines() if "OFF" in ln)
    r = subprocess.run(["bash", "-c", f"set -euo pipefail\nN={n}; NCPU={ncpu}\n{snippet}echo \"$CPUSET\""],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


class _FakeMod:
    LANE_PORT_RANGES = (("3000-3019", "34000-34019"), ("8000-8009", "35000-35009"))
    WRAPPER_SH = '      N=3\n      CPUSET="0-$(( N - 1 ))"\n'

    @staticmethod
    def lane_cpu_args(engine_gib, ncpu):
        return ["--cpuset-cpus", "0-2"]


class Launcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "prework.py"
        self.path.write_text(FAKE_PREWORK)

    def tearDown(self):
        self.tmp.cleanup()
        sys.modules.pop("prework", None)

    def test_slot_1_moves_host_ports_and_cpus_and_runs_main(self):
        prework_slot.main([str(self.path), "1", "setup", "--task", "Task x"])
        mod = sys.modules["prework"]
        argv, ports, cpus = mod.CALLS[0]
        self.assertEqual(argv, ["setup", "--task", "Task x"])
        self.assertEqual(ports, (("3000-3019", "36000-36019"), ("8000-8009", "37000-37009")))
        self.assertEqual(cpus, ["--cpuset-cpus", "3-5"])
        self.assertIn('CPUSET="$OFF-$(( OFF + N - 1 ))"', mod.WRAPPER_SH)
        self.assertIn('CPUSET="0-$(( NCPU - 1 ))"', mod.WRAPPER_SH)  # the all-CPUs branch is left alone

    def test_slot_0_changes_nothing(self):
        prework_slot.main([str(self.path), "0", "collect"])
        mod = sys.modules["prework"]
        self.assertEqual(mod.CALLS[0][1], (("3000-3019", "34000-34019"), ("8000-8009", "35000-35009")))
        self.assertEqual(mod.CALLS[0][2], ["--cpuset-cpus", "0-2"])
        self.assertNotIn("OFF", mod.WRAPPER_SH)

    def test_cpus_fall_back_to_0_when_the_next_ones_do_not_exist(self):
        mod = prework_slot.apply(_FakeMod(), 1)
        self.assertEqual(mod.lane_cpu_args(15, 16), ["--cpuset-cpus", "3-5"])
        self.assertEqual(mod.lane_cpu_args(15, 4), ["--cpuset-cpus", "0-2"])
        self.assertEqual(cpu_block(1, 3, 16), "3-5")
        self.assertEqual(cpu_block(1, 2, 8), "2-3")
        self.assertEqual(cpu_block(1, 3, 4), "0-2")

    def test_refuses_a_bad_slot_a_small_engine_and_a_changed_template(self):
        with self.assertRaisesRegex(SystemExit, "0 or 1"):
            prework_slot.main([str(self.path), "2", "setup"])
        self.path.write_text(FAKE_PREWORK.replace("GIB = 15.0", "GIB = 5.8"))
        with self.assertRaises(SystemExit):
            prework_slot.main([str(self.path), "1", "setup"])
        self.assertEqual(sys.modules["prework"].CALLS, [])
        self.path.write_text(FAKE_PREWORK.replace('CPUSET="0-$(( N - 1 ))"', 'CPUSET="0-$N"'))
        with self.assertRaisesRegex(SystemExit, "template has changed"):
            prework_slot.main([str(self.path), "1", "setup"])

    @unittest.skipUnless(REAL_PREWORK.is_file(), "prework.py isn't installed")
    def test_the_installed_prework_still_matches(self):
        """Fails when Mercor ships a prework.py this launcher no longer fits."""
        mod = prework_slot.apply(prework_slot.load(str(REAL_PREWORK)), 1)
        self.assertEqual(mod.LANE_PORT_RANGES, (("3000-3019", "36000-36019"), ("8000-8009", "37000-37009")))
        self.assertEqual([a for a, b in lanes.HOST_PORT_RANGES], [34000, 35000])
        self.assertEqual(mod.WRAPPER_SH.count("OFF=$(( N * 1 ))"), 1)
        self.assertEqual(mod.lane_cpu_args(14.86, 16), ["--cpuset-cpus", "3-5"])


class Slots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name) / "abc"
        self.d.mkdir()
        workspace.save(self.d, {"task_name": "Task abc", "runs": [],
                                "repo": {"pinned_url": "https://github.com/o/r/tree/" + "a" * 40, "commit": "a" * 40}})
        self.patches = [mock.patch.object(lanes, "_guard_real", lambda *a: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def held(self, *slots):
        taken = {p for s in slots for p in lanes.slot_ports(s)}
        return mock.patch.object(lanes, "_port_free", lambda p: p not in taken)

    def fake_setup(self, calls):
        def run(cmd, **kw):
            calls.append((cmd, kw))
            (Path(kw["cwd"]) / "prework-manifest.json").write_text(
                json.dumps({"lane": {"container": "prework-PREWORK_abc-2-model_a"}}))
            return subprocess.CompletedProcess(cmd, 0, "[OK] done\n", "")
        return run

    def test_slot_ports(self):
        self.assertEqual(lanes.slot_ports(1)[:2], [36000, 36001])
        self.assertEqual(lanes.slot_ports(1)[-1], 37009)
        self.assertEqual(lanes.slot_label(1), "36000-36019 and 37000-37009")
        with self.held(0):
            self.assertEqual(lanes.free_slot(), 1)
        with self.held(0, 1):
            self.assertIsNone(lanes.free_slot())
        with self.held():
            self.assertEqual(lanes.free_slot(), 0)

    def test_second_lane_leaves_the_running_one_alone(self):
        calls, stops = [], []
        with self.held(0), mock.patch.object(lanes, "running_lanes", lambda: ["prework-one-model_a"]), \
                mock.patch.object(lanes, "_run", lambda cmd, **kw: stops.append(cmd)), \
                mock.patch.object(lanes.subprocess, "run", self.fake_setup(calls)):
            run_dir = lanes.new_lane(self.d, "validate")
        self.assertEqual(stops, [])
        self.assertEqual(calls[0][1]["env"]["PREWORK_PORT_SLOT"], "1")
        rec = workspace.load(self.d)["runs"][0]
        self.assertEqual((rec["dir"], rec["port_slot"]), ("runs/validate-1", 1))
        self.assertEqual(run_dir, self.d / "runs" / "validate-1")

    def test_first_lane_runs_prework_as_before(self):
        calls = []
        with self.held(), mock.patch.object(lanes, "running_lanes", lambda: []), \
                mock.patch.dict(os.environ, {"PREWORK_PORT_SLOT": "1"}), \
                mock.patch.object(lanes.subprocess, "run", self.fake_setup(calls)):
            lanes.new_lane(self.d, "validate")
        self.assertNotIn("PREWORK_PORT_SLOT", calls[0][1]["env"])
        self.assertNotIn("port_slot", workspace.load(self.d)["runs"][0])

    def test_both_slots_taken_stops_only_idle_lanes(self):
        held = {0, 1}
        stops = []

        def stop(cmd, **kw):
            stops.append(cmd[-1])
            held.discard(1)  # the idle lane was on slot 1
        with mock.patch.object(lanes, "_port_free", lambda p: not any(p in lanes.slot_ports(s) for s in held)), \
                mock.patch.object(lanes, "running_lanes", lambda: ["live", "idle"]), \
                mock.patch.object(lanes, "claude_active", lambda c: c == "live"), \
                mock.patch.object(lanes, "_run", stop), \
                mock.patch.object(lanes.subprocess, "run", self.fake_setup([])):
            lanes.new_lane(self.d, "validate")
        self.assertEqual(stops, ["idle"])
        self.assertEqual(workspace.load(self.d)["runs"][0]["port_slot"], 1)

    def test_both_slots_live_is_refused(self):
        with self.held(0, 1), mock.patch.object(lanes, "running_lanes", lambda: ["a", "b"]), \
                mock.patch.object(lanes, "claude_active", lambda c: True), \
                mock.patch.object(lanes, "_run", mock.Mock(side_effect=AssertionError("stopped a live lane"))):
            with self.assertRaisesRegex(RuntimeError, "Both lane slots are taken"):
                lanes.new_lane(self.d, "validate")
        self.assertFalse((self.d / "runs").exists())

    def test_container_slot(self):
        for out, want in (("34000\n", 0), ("36000\n", 1), ("38000\n", None), ("", None), ("34001", None)):
            with mock.patch.object(lanes, "_run", lambda cmd, o=out, **kw: subprocess.CompletedProcess(cmd, 0, o, "")):
                self.assertEqual(lanes.container_slot("c"), want, out)

    def test_collect_passes_the_slot(self):
        (self.d / "runs" / "validate-1").mkdir(parents=True)
        for rec, want in (({}, None), ({"port_slot": 1}, "1")):
            workspace.save(self.d, {**workspace.load(self.d), "runs": [{"dir": "runs/validate-1", **rec}]})
            calls = []
            with mock.patch.object(lanes.subprocess, "run",
                                   lambda cmd, **kw: calls.append(kw) or subprocess.CompletedProcess(cmd, 0)):
                lanes._prework_collect(self.d, "validate-1", self.d / "runs" / "validate-1")
            self.assertEqual(calls[0]["env"].get("PREWORK_PORT_SLOT"), want)
        workspace.save(self.d, {**workspace.load(self.d), "runs": [
            {"dir": "runs/validate-1", "port_slot": 1, "host": "pc", "remote_dir": "/r"}]})
        calls = []
        with mock.patch.object(lanes.subprocess, "run",
                               lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0)):
            lanes._prework_collect(self.d, "validate-1", self.d / "runs" / "validate-1")
        self.assertEqual(calls[0][2], "cd /r && PREWORK_PORT_SLOT=1 ~/.local/bin/mercor-prework collect")


class Resume(unittest.TestCase):
    """johto resume stops only a lane on the same slot, and nothing when its own lane is already up."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name) / "abc"
        self.run_dir = self.d / "runs" / "validate-1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "prework-manifest.json").write_text(json.dumps({"lane": {"container": "me"}}))
        workspace.save(self.d, {"task_name": "Task abc", "runs": [{"dir": "runs/validate-1", "port_slot": 1}]})

    def tearDown(self):
        self.tmp.cleanup()

    def resume(self, running, slots):
        stopped = []
        with mock.patch.object(cli, "task_dir_arg", lambda t: self.d), \
                mock.patch.object(lanes, "running_lanes", lambda: running), \
                mock.patch.object(lanes, "container_slot", lambda c: slots.get(c)), \
                mock.patch.object(cli, "confirm", lambda *a: True), \
                mock.patch.object(cli.subprocess, "run", lambda cmd, **kw: stopped.append(cmd[-1])), \
                mock.patch.object(cli, "_exec_lane", lambda run_dir: None), \
                mock.patch("builtins.print"):
            cli.cmd_resume(argparse.Namespace(task="abc", run="validate-1"))
        return stopped

    def test_other_slot_keeps_running(self):
        self.assertEqual(self.resume(["first"], {"first": 0}), [])

    def test_same_slot_or_unknown_is_stopped(self):
        self.assertEqual(self.resume(["old", "odd", "first"], {"old": 1, "first": 0}), ["old", "odd"])

    def test_own_lane_already_up_stops_nothing(self):
        self.assertEqual(self.resume(["me", "old"], {"old": 1}), [])


if __name__ == "__main__":
    unittest.main()


WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "mercor-prework"
# Prints what main() saw, whether run as a script (slot 0) or loaded by prework_slot (slot 1).
WRAPPED_PREWORK = FAKE_PREWORK + textwrap.dedent('''
    import json
    def main(argv=None):
        print(json.dumps({"argv": list(argv), "ports": [list(r) for r in LANE_PORT_RANGES]}))
    if __name__ == "__main__":
        main(sys.argv[1:])
''')


class Wrapper(unittest.TestCase):
    """bin/mercor-prework: prework.py as-is for the first lane, through prework_slot.py for the second."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prework = Path(self.tmp.name) / "prework.py"
        self.prework.write_text(WRAPPED_PREWORK)

    def tearDown(self):
        self.tmp.cleanup()

    def run_wrapper(self, *args, **env):
        e = {k: v for k, v in os.environ.items() if k not in ("PREWORK_PORT_SLOT", "JOHTO_PREWORK_SLOT")}
        e.update({"PREWORK_PY": str(self.prework), **env})
        return subprocess.run([str(WRAPPER), *args], env=e, capture_output=True, text=True)

    def test_first_lane_runs_prework_unchanged(self):
        r = self.run_wrapper("collect")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"argv": ["collect"],
                                                "ports": [["3000-3019", "34000-34019"], ["8000-8009", "35000-35009"]]})

    def test_second_lane_goes_through_prework_slot(self):
        r = self.run_wrapper("setup", "--task", "Task x", PREWORK_PORT_SLOT="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"argv": ["setup", "--task", "Task x"],
                                                "ports": [["3000-3019", "36000-36019"], ["8000-8009", "37000-37009"]]})

    def test_missing_files_are_refused_in_plain_words(self):
        r = self.run_wrapper("collect", PREWORK_PORT_SLOT="1", JOHTO_PREWORK_SLOT="/nonexistent/prework_slot.py")
        self.assertEqual(r.returncode, 2)
        self.assertIn("needs prework_slot.py", r.stderr)
        r = self.run_wrapper("collect", PREWORK_PY="/nonexistent/prework.py")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no prework.py", r.stderr)
