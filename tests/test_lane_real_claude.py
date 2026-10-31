"""The PTY bridge and typing against the real Claude Code TUI in a throwaway container (opt-in).

JOHTO_TEST_REAL_CLAUDE=1 runs it. The container is the lane image with a fresh, empty config dir seeded the way
prework seeds a lane (onboarding and folder trust answered, no login), so Claude shows "Not logged in". The test
launches it through the service, types /login (the login screen opens), presses Esc, pastes text (it lands in
the input box and is not sent), clears it, and types /exit (Claude quits). Nothing reaches Anthropic beyond what
a logged-out Claude Code does at start. Everything is removed afterwards.
"""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from johto import config
from johto.web.lane import monitor, service
from johto.web.lane.pty_bridge import PASTE_END, PASTE_START

IMAGE = os.environ.get("JOHTO_LANE_TEST_IMAGE", "mercor-prework-lane:2.1.311")
SCRATCH = Path(os.environ.get("JOHTO_LANE_TEST_DIR") or tempfile.gettempdir())


def pane(c: str) -> str:
    return subprocess.run(["docker", "exec", "-u", "lane", c, "tmux", "capture-pane", "-p", "-t", "lane"],
                          capture_output=True, text=True).stdout


async def until(cond, timeout=30.0, what=""):
    end = time.time() + timeout
    while time.time() < end:
        v = await asyncio.to_thread(cond)
        if v:
            return v
        await asyncio.sleep(0.4)
    raise AssertionError(f"timed out: {what}")


@unittest.skipUnless(os.environ.get("JOHTO_TEST_REAL_CLAUDE") == "1", "set JOHTO_TEST_REAL_CLAUDE=1")
class RealClaudeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="johto-real-claude-", dir=SCRATCH))
        self.saved = {k: getattr(config, k) for k in ("ROOT", "DATA", "TASKS", "PREWORK_HOME")}
        config.ROOT, config.DATA, config.TASKS = self.tmp, self.tmp / "data", self.tmp / "tasks"
        config.PREWORK_HOME = self.tmp / "prework"
        config.DATA.mkdir(parents=True)
        self.saved_prefix, monitor.PREFIX = monitor.PREFIX, "johtotest-"
        key = f"PREWORK_zz05-{uuid.uuid4().hex[:10]}"
        self.c = f"johtotest-{key}-model_a"
        cc = config.PREWORK_HOME / "cc-config" / f"{key}_model_a"
        cc.mkdir(parents=True)
        (cc / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True, "theme": "dark", "installMethod": "native",
            "projects": {"/work": {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True,
                                   "hasClaudeMdExternalIncludesApproved": True, "allowedTools": []}}}))
        work = self.tmp / "work"
        work.mkdir()
        for p in (cc, cc / ".claude.json", work):
            os.chmod(p, 0o777 if p.is_dir() else 0o666)
        d = config.TASKS / "zz05"
        run_dir = d / "runs" / "calib-1"
        (run_dir / "model_a").mkdir(parents=True)
        (d / "task.json").write_text(json.dumps({"task_id": "t", "task_name": "Task zz05", "slot": {
            "interaction_mode": "Interactive/pairing"}, "repo": {"full_name": "x/y"}, "runs": [
            {"kind": "calib", "dir": "runs/calib-1", "container": self.c}]}))
        (d / "submission.md").write_text("## Opening Prompt\nhello\n")
        (run_dir / "prework-manifest.json").write_text(json.dumps({"lane": {"container": self.c, "workspace": "/work"}}))
        cd = run_dir / "model_a" / "claude-dev"
        cd.write_text("#!/usr/bin/env bash\nexec docker exec -it -u lane -e HOME=/home/lane -e CLAUDE_CONFIG_DIR=/cc "
                      f"-w /work {self.c} tmux -f /home/lane/.tmux.lane.conf new-session -A -s lane claude \"$@\"\n")
        cd.chmod(0o755)
        r = subprocess.run(["docker", "run", "-d", "--rm", "--init", "--name", self.c, "--entrypoint", "sleep",
                            "-e", "CLAUDE_CONFIG_DIR=/cc", "-v", f"{cc}:/cc", "-v", f"{work}:/work", "-w", "/work",
                            IMAGE, "infinity"], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.svc = service.LaneService()
        self.svc._auto_override = False
        self.svc.setup = lambda d, kind, log: (_ for _ in ()).throw(AssertionError("real setup"))

    async def asyncTearDown(self):
        if self.svc.pty:
            self.svc.pty.kill()
        subprocess.run(["docker", "rm", "-f", "-v", self.c], capture_output=True)
        for k, v in self.saved.items():
            setattr(config, k, v)
        monitor.PREFIX = self.saved_prefix
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_login_paste_and_exit_reach_the_real_tui(self):
        s = self.svc
        st = await s.refresh()
        self.assertEqual((st["state"], st["launchable"]), ("free", True))
        await s.launch()
        await until(lambda: "Not logged in" in pane(self.c), what="Claude started")
        st = await s.refresh()
        self.assertEqual(st["state"], "needs_login")
        self.assertEqual(st["run"]["run"], "calib-1")

        await s.type("login")
        await until(lambda: "Select login method" in pane(self.c), what="login screen")
        st = await s.refresh()
        self.assertEqual(st["state"], "needs_login")
        with self.assertRaises(service.LaneError):
            await s.type("login")
        s.pty.write(b"\x1b")
        await until(lambda: "Select login method" not in pane(self.c), what="login screen closed")

        s.pty.write(PASTE_START + b"first line\rsecond line" + PASTE_END)
        await until(lambda: "second line" in pane(self.c), what="paste in the box")
        await asyncio.sleep(1.5)
        scr = monitor.screen(pane(self.c))
        self.assertEqual(scr["input_text"], "first line\nsecond line", "pasted, not sent")
        s.pty.write(b"\x7f" * 30)
        await until(lambda: monitor.screen(pane(self.c))["input_text"] == "", what="box cleared")

        await s._write_slowly(s.pty, b"/exit", b"\r")
        await until(lambda: not monitor.claude_alive(self.c), what="Claude exited")
        await until(lambda: s.pty is None, 10, what="PTY ended")


if __name__ == "__main__":
    unittest.main()
