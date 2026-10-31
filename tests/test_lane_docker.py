"""The lane routes end to end against a throwaway container (never a real lane).

The container comes from the lane image (same tmux, `lane` user and tmux config as a real lane) and runs a
fake Claude TUI under the tmux session "lane": the Claude Code 2.1.311 layout, /login, a bracketed paste, a
working spinner, an end_turn question, /exit, and transcript lines in the real format on the host config dir.

The server is the lane routes on 127.0.0.1:8767 (uvicorn in a thread). Containers are named johtotest-*, and
the lane prefix is switched to that for the test, so a real prework-* lane is never seen, typed into or
stopped. Skipped when Docker or the lane image is missing. Everything made here is removed afterwards.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from johto import config, workspace

PORT = int(os.environ.get("JOHTO_LANE_TEST_PORT", "8767"))
IMAGE = os.environ.get("JOHTO_LANE_TEST_IMAGE", "mercor-prework-lane:2.1.311")
PREFIX = "johtotest-"
SCRATCH = Path(os.environ.get("JOHTO_LANE_TEST_DIR") or tempfile.gettempdir())

FAKE_CLAUDE = r'''
import json, os, select, sys, termios, time, tty, uuid
CC = os.environ.get("CLAUDE_CONFIG_DIR", "/cc")
WORK_S = float(os.environ.get("FAKE_WORK_S", "1.5"))
SID = str(uuid.uuid4())
TPATH = os.path.join(CC, "projects", "-work", SID + ".jsonl")
RULE, NB = "─" * 100, " "
S = {"state": "main", "inp": "", "busy": None, "turn": 0, "tid": None}
hist = []
QUESTION = ("I added the lock endpoint. Should locking a room that isn't loaded yet load it first, "
            "or return 404 like the user endpoints do?")

def creds(): return os.path.join(CC, ".credentials.json")
def logged(): return os.path.exists(creds())
def now(): return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"
def write(e):
    os.makedirs(os.path.dirname(TPATH), exist_ok=True)
    e = {"parentUuid": None, "isSidechain": False, "uuid": str(uuid.uuid4()), "timestamp": now(),
         "sessionId": SID, "cwd": "/work", "version": "2.1.311", **e}
    with open(TPATH, "a") as f:
        f.write(json.dumps(e) + "\n")
def user(content, **kw): write({"type": "user", "message": {"role": "user", "content": content}, **kw})
def asst(content, stop): write({"type": "assistant", "message": {"id": "msg_" + uuid.uuid4().hex[:8],
    "model": "claude-opus-5-5", "role": "assistant", "content": content, "stop_reason": stop}})
def local(cmd, out):
    user("<local-command-caveat>The command below was run directly in Claude Code</local-command-caveat>", isMeta=True)
    user(f"<command-name>{cmd}</command-name>\n<command-message>{cmd[1:]}</command-message>")
    user(f"<local-command-stdout>{out}</local-command-stdout>")

def render():
    lines = [" ▐▛███▜▌   Claude Code v2.1.311",
             "▝▜█████▛▘  Opus 5.5 with high effort", " /work", ""]
    lines += hist[-12:]
    st = S["state"]
    if st == "login_select":
        lines += ["", RULE, "  Login", "", "  Select login method:", "",
                  "  ❯ 1. Claude account with subscription · Pro, Max, Team, or Enterprise",
                  "    2. Anthropic Console account · API usage billing", "", "  Esc to cancel"]
    elif st == "login_code":
        lines += ["", RULE, "  Browser didn't open? Use the url below to sign in", "",
                  "  https://claude.ai/oauth/authorize?fake=1", "", "  Paste code here if prompted > " + S["inp"]]
    else:
        if S["busy"]:
            lines += ["", "✻ Working… (3s · esc to interrupt)"]
        box = S["inp"].split("\n")
        lines += ["", RULE, "❯" + NB + box[0]] + ["  " + b for b in box[1:]] + [RULE,
                  "  ⏵⏵ auto mode on (shift+tab to cycle)" + ("" if logged() else "      Not logged in · Run /login")]
    sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(lines))
    sys.stdout.flush()

def submit():
    text = S["inp"].strip()
    S["inp"] = ""
    st = S["state"]
    if st == "login_select":
        S["state"] = "login_code"; return
    if st == "login_code":
        with open(creds(), "w") as f:
            f.write('{"fake": true}')
        S["state"] = "main"; hist.append("  Login successful"); return
    if S["busy"] or not text:
        return
    if text == "/login":
        local("/login", "Login interrupted"); S["state"] = "login_select"; return
    if text == "/exit":
        local("/exit", "Bye!"); raise SystemExit(0)
    if not logged():
        hist.append("  ⎿  Not logged in · Please run /login"); return
    S["turn"] += 1
    hist.append("❯ " + text.split("\n")[0])
    user(text)
    write({"type": "api-request-blob", "message": {"output_config": {"effort": "high"}}})
    S["tid"] = "toolu_" + uuid.uuid4().hex[:8]
    asst([{"type": "tool_use", "id": S["tid"], "name": "Bash", "input": {"command": "npm test"}}], "tool_use")
    S["busy"] = time.time() + WORK_S

def finish():
    user([{"type": "tool_result", "tool_use_id": S["tid"], "content": "ok", "is_error": False}])
    text = QUESTION if S["turn"] == 1 else "Done. Unloaded groups return 404 now and the tests pass."
    asst([{"type": "text", "text": text}], "end_turn")
    hist.append("⏺ " + text)
    S["busy"] = None

def esc():
    if S["state"] != "main":
        S["state"] = "main"; return
    if S["busy"]:
        user([{"type": "text", "text": "[Request interrupted by user]"}]); S["busy"] = None

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
sys.stdout.write("\x1b[?2004h")
buf, pasting = b"", False
try:
    render()
    while True:
        wait = max(0.0, S["busy"] - time.time()) if S["busy"] else 1.0
        r, _, _ = select.select([fd], [], [], wait)
        if S["busy"] and time.time() >= S["busy"]:
            finish(); render()
        if not r:
            continue
        data = os.read(fd, 65536)
        if not data:
            break
        buf += data
        while buf:
            if pasting:
                i = buf.find(b"\x1b[201~")
                if i < 0:
                    S["inp"] += buf.decode("utf-8", "replace").replace("\r", "\n"); buf = b""; break
                S["inp"] += buf[:i].decode("utf-8", "replace").replace("\r", "\n"); buf = buf[i + 6:]; pasting = False
                continue
            if buf.startswith(b"\x1b[200~"):
                pasting, buf = True, buf[6:]; continue
            if buf.startswith(b"\x1b["):
                j = 2
                while j < len(buf) and not (0x40 <= buf[j] <= 0x7e):
                    j += 1
                if j >= len(buf):
                    break
                buf = buf[j + 1:]; continue
            ch, buf = buf[:1], buf[1:]
            if ch == b"\x1b":
                esc()
            elif ch in (b"\r", b"\n"):
                submit()
            elif ch == b"\x7f":
                S["inp"] = S["inp"][:-1]
            elif ch >= b" ":
                S["inp"] += ch.decode("utf-8", "replace")
        render()
finally:
    sys.stdout.write("\x1b[?2004l")
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
'''

CLAUDE_DEV = """#!/usr/bin/env bash
exec docker exec -it -u lane -e HOME=/home/lane -e CLAUDE_CONFIG_DIR=/cc -e FAKE_WORK_S=1.5 -w /work {container} \\
  tmux -f /home/lane/.tmux.lane.conf new-session -A -s lane "bash -c 'exec -a claude python3 /work/fake_claude.py'"
"""

PROMPT = "locking a room from the api doesn't work when the room isn't loaded yet\nit just hangs"


def docker_ready() -> bool:
    try:
        r = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def make_task(root: Path, short: str, mode: str = "Interactive/pairing") -> Path:
    d = root / "tasks" / short
    (d / "runs").mkdir(parents=True)
    meta = {"task_id": f"task_{short}", "task_name": f"Task {short}", "label": f"chatd · Lock {short}",
            "slot": {"action": "Modify", "domain": "Backend / API", "codebase_type": "Personal-scale",
                     "prompt_style": "Casual prose", "interaction_mode": mode},
            "repo": {"full_name": "acme/chatd", "commit": "0" * 40, "pinned_url": "https://github.com/acme/chatd",
                     "language": "Go"}, "lead_id": None, "created_at": "2026-10-30T20:00:00-0700", "runs": [],
            "gists": {}}
    (d / "task.json").write_text(json.dumps(meta, indent=1))
    (d / "submission.md").write_text(f"## Opening Prompt\n{PROMPT}\n\n## High-Level Task Goal\n\n")
    return d


class FakeSetup:
    """Stands in for lanes.new_lane: a new runs/<kind>-N folder, its manifest, claude-dev and a container."""

    def __init__(self, root: Path):
        self.root, self.containers = root, []

    def __call__(self, d: Path, kind: str, log) -> Path:
        n = 1
        while (d / "runs" / f"{kind}-{n}").exists():
            n += 1
        run_dir = d / "runs" / f"{kind}-{n}"
        (run_dir / "model_a").mkdir(parents=True)
        key = f"PREWORK_{d.name}-{uuid.uuid4().hex[:10]}"
        container = f"{PREFIX}{key}-model_a"
        cc = config.PREWORK_HOME / "cc-config" / f"{key}_model_a"
        work = self.root / "work" / key
        cc.mkdir(parents=True)
        work.mkdir(parents=True)
        (work / "fake_claude.py").write_text(FAKE_CLAUDE)
        os.chmod(cc, 0o777)
        os.chmod(work, 0o777)
        (run_dir / "prework-manifest.json").write_text(json.dumps(
            {"lane": {"container": container, "workspace": "/work"}, "base_commit": "0" * 40}))
        cd = run_dir / "model_a" / "claude-dev"
        cd.write_text(CLAUDE_DEV.format(container=container))
        cd.chmod(0o755)
        log(f"docker run {container}")
        r = subprocess.run(["docker", "run", "-d", "--rm", "--init", "--name", container, "--entrypoint", "sleep",
                            "-v", f"{cc}:/cc", "-v", f"{work}:/work", "-w", "/work", IMAGE, "infinity"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"[ERROR] docker run failed: {r.stderr.strip()}")
        self.containers.append(container)
        meta = workspace.load(d)
        meta["runs"].append({"kind": kind, "dir": f"runs/{run_dir.name}", "container": container,
                             "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        workspace.save(d, meta)
        return run_dir


class FakeLanes:
    """lane_watch's collect: mark the run collected (the vendor collect would copy files out)."""

    calls = []

    def collect(self, d: Path, run: str) -> dict:
        self.calls.append((d.name, run))
        meta = workspace.load(d)
        for rec in meta["runs"]:
            if rec["dir"] == f"runs/{run}":
                rec["collected_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        workspace.save(d, meta)
        return {"summary": {"follow_ups": [], "active_minutes": 0.1}}


class WS:
    """A /ws/lane client that records every frame in a thread."""

    def __init__(self, path: str, token: str, origin: bool = True):
        from websockets.sync.client import connect
        headers = {"Cookie": f"johto={token}"}
        self.ws = connect(f"ws://127.0.0.1:{PORT}{path}", additional_headers=headers,
                          origin=f"http://127.0.0.1:{PORT}" if origin else None, open_timeout=10,
                          legacy=True)
        self.texts, self.data = [], bytearray()
        self.closed = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for m in self.ws:
                if isinstance(m, str):
                    self.texts.append(json.loads(m))
                else:
                    self.data += m
        except Exception:
            pass
        self.closed.set()

    def send(self, b: bytes):
        self.ws.send(b)

    def text(self, obj: dict):
        self.ws.send(json.dumps(obj))

    def has(self, typ: str) -> bool:
        return any(t.get("type") == typ for t in self.texts)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def wait(cond, timeout=25.0, every=0.25, what=""):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = cond()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what or cond}: last={last!r}")


@unittest.skipUnless(docker_ready(), f"needs Docker and the {IMAGE} image")
class LaneDockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from johto.web import api_lane, api_pack, lane_watch
        from johto.web import app as webapp
        from johto.web.lane import monitor, service
        cls.tmp = Path(tempfile.mkdtemp(prefix="johto-lane-test-", dir=SCRATCH))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        saved = {k: getattr(config, k) for k in ("ROOT", "DATA", "TASKS", "PREWORK_HOME")}
        cls.addClassCleanup(lambda: [setattr(config, k, v) for k, v in saved.items()])
        config.ROOT, config.DATA, config.TASKS = cls.tmp, cls.tmp / "data", cls.tmp / "tasks"
        config.PREWORK_HOME = cls.tmp / "prework"
        config.DATA.mkdir(parents=True)
        saved_prefix, monitor.PREFIX = monitor.PREFIX, PREFIX
        cls.addClassCleanup(setattr, monitor, "PREFIX", saved_prefix)
        cls.addClassCleanup(cls.remove_containers)      # runs first (cleanups run last-in, first-out)
        os.environ["JOHTO_NO_NOTIFY"] = "1"
        cls.d1 = make_task(cls.tmp, "zz01")
        cls.d2 = make_task(cls.tmp, "zz02")
        cls.setup_fn = FakeSetup(cls.tmp)
        cls.svc = svc = service.svc
        saved_svc = (svc.setup, svc.open_ghostty, svc._auto_override)
        cls.addClassCleanup(lambda: (setattr(svc, "setup", saved_svc[0]), setattr(svc, "open_ghostty", saved_svc[1]),
                                     setattr(svc, "_auto_override", saved_svc[2])))
        svc.setup, svc._auto_override = cls.setup_fn, False
        cls.ghostty_calls = []
        svc.open_ghostty = lambda argv: (cls.ghostty_calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
        saved_hooks = (lane_watch._lanes, api_pack._start)
        cls.addClassCleanup(lambda: (setattr(lane_watch, "_lanes", saved_hooks[0]),
                                     setattr(api_pack, "_start", saved_hooks[1])))
        lane_watch._lanes = lambda: FakeLanes()
        api_pack._start = lambda short, run, ai=True: {"state": "done", "out": str(cls.tmp / "pack.xml"), "tokens": 1}
        cls.token = webapp.TOKEN
        import uvicorn
        from contextlib import asynccontextmanager
        from starlette.applications import Starlette

        @asynccontextmanager
        async def lifespan(app):
            api_lane.start()
            yield
            api_lane.stop()
        app = Starlette(routes=api_lane.routes, lifespan=lifespan)
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.stop_server)
        wait(lambda: cls.server.started or not cls.thread.is_alive(), 15, what="server start")
        if not cls.server.started:
            raise unittest.SkipTest(f"port {PORT} is taken")

    @classmethod
    def stop_server(cls):
        cls.server.should_exit = True
        cls.thread.join(10)

    @classmethod
    def remove_containers(cls):
        r = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^{PREFIX}"], capture_output=True, text=True)
        ids = r.stdout.split()
        if ids:
            subprocess.run(["docker", "rm", "-f", "-v", *ids], capture_output=True)

    # -- helpers
    def call(self, method: str, path: str, body=None, cookie=True, origin=None):
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = f"johto={self.token}"
        headers["Origin"] = origin or f"http://127.0.0.1:{PORT}"
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            with e:
                raw = e.read()
            return e.code, (json.loads(raw) if raw else None)

    def lane(self) -> dict:
        return self.call("GET", "/api/lane")[1]

    def until_state(self, *states, timeout=25.0) -> dict:
        return wait(lambda: (lambda s: s if s["state"] in states else None)(self.lane()), timeout,
                    what=f"lane state {states} (now {self.lane()['state']})")

    def pane(self, container: str) -> str:
        r = subprocess.run(["docker", "exec", "-u", "lane", container, "tmux", "capture-pane", "-p", "-t", "lane"],
                           capture_output=True, text=True)
        return r.stdout

    # -- the whole loop
    def test_full_lane_cycle(self):
        # auth and origin
        self.assertEqual(self.call("GET", "/api/lane", cookie=False, origin="http://evil.example")[0], 401)
        self.assertEqual(self.call("POST", "/api/lane/end", origin="http://evil.example")[0], 403)
        self.assertEqual(self.lane()["state"], "free")
        s, b = self.call("POST", "/api/lane/start", {"short": "nope", "kind": "calib"})
        self.assertEqual((s, b["error"]["code"]), (404, "not_found"))

        idle = WS("/ws/lane?cols=100&rows=30", self.token)
        wait(lambda: idle.has("idle"), 10, what="idle frame")
        self.assertEqual(idle.texts[0], {"type": "hello", "run": None, "locked": False})

        # start: setup in a job, then claude-dev in the server's PTY
        s, b = self.call("POST", "/api/lane/start", {"short": "zz01", "kind": "calib"})
        self.assertEqual(s, 202, b)
        self.assertEqual(b["job"]["kind"], "lane_prepare")
        self.assertEqual(b["job"]["target"], "t:zz01")
        st = self.until_state("needs_login")
        container = st["run"]["container"]
        self.assertTrue(container.startswith(PREFIX))
        self.assertEqual((st["run"]["short"], st["run"]["run"], st["run"]["kind"]), ("zz01", "calib-1", "calib"))
        self.assertTrue(st["pty"]["alive"])
        wait(lambda: idle.has("run"), 10, what="run frame")
        wait(lambda: "Not logged in" in idle.data.decode("utf-8", "replace"), 10, what="screen bytes")

        # one lane at a time
        s, b = self.call("POST", "/api/lane/start", {"short": "zz02", "kind": "calib"})
        self.assertEqual((s, b["error"]["code"]), (409, "lane_busy"))
        self.assertEqual(b["error"]["details"], {"short": "zz01", "run": "calib-1"})

        # typing only in the right states
        s, b = self.call("POST", "/api/lane/type", {"what": "prompt"})
        self.assertEqual((s, b["error"]["code"]), (409, "lane_state"))
        s, b = self.call("POST", "/api/lane/end")
        self.assertEqual((s, b["error"]["code"]), (409, "lane_state"))
        self.assertEqual(self.call("POST", "/api/lane/type", {"what": "login"})[0], 204)
        wait(lambda: "Select login method" in self.pane(container), 10, what="login screen")
        s, b = self.call("POST", "/api/lane/type", {"what": "login"})
        self.assertEqual((s, b["error"]["code"]), (409, "lane_state"), "the login screen is already open")
        idle.send(b"\r")
        wait(lambda: "Paste code here" in self.pane(container), 10, what="paste code screen")
        idle.send(b"fake-code\r")
        st = self.until_state("ready_for_prompt")
        self.assertIsNone(st["run"]["pasted_at"])

        # the saved prompt goes in as a bracketed paste, with no Enter
        self.assertEqual(self.call("POST", "/api/lane/type", {"what": "prompt"})[0], 204)
        wait(lambda: "it just hangs" in self.pane(container), 10, what="pasted prompt in the box")
        time.sleep(2.5)
        st = self.lane()
        self.assertEqual(st["state"], "ready_for_prompt", "nothing is sent until Austin presses Enter")
        self.assertEqual(st["input"], {"visible": True, "empty": False})
        rec = workspace.load(self.d1)["runs"][0]
        self.assertEqual(len(rec["prompt_sha"]), 12)
        self.assertTrue(rec["pasted_at"])
        s, b = self.call("POST", "/api/lane/type", {"what": "prompt"})
        self.assertEqual((s, b["error"]["code"]), (409, "lane_state"), "no second paste over the first")
        s, b = self.call("POST", "/api/lane/end")
        self.assertEqual((s, b["error"]["code"]), (409, "lane_state"), "no /exit on top of a draft")

        # Austin presses Enter: Claude works, then asks
        idle.send(b"\r")
        self.until_state("working", "waiting")
        st = self.until_state("waiting")
        self.assertEqual(st["question"], "Should locking a room that isn't loaded yet load it first, or return 404 "
                                         "like the user endpoints do?")
        self.assertEqual(st["facts"]["turns"], 1)
        self.assertEqual(st["facts"]["tests"]["runs"], 1)
        self.assertTrue(st["facts"]["tests"]["last_ok"])
        self.assertTrue(st["facts"]["model_ok"])
        self.assertTrue(st["facts"]["effort_ok"])
        s, b = self.call("POST", "/api/lane/type", {"what": "prompt"})
        self.assertEqual((s, b["error"]["code"]), (409, "lane_state"), "the prompt only goes in before turn 1")

        # resize: the last client's size reaches tmux
        idle.text({"type": "resize", "cols": 120, "rows": 40})
        wait(lambda: "120x40" in subprocess.run(
            ["docker", "exec", "-u", "lane", container, "tmux", "list-clients", "-F", "#{client_width}x#{client_height}"],
            capture_output=True, text=True).stdout, 10, what="tmux client size")

        # the server's terminal goes away; a new socket attaches to the same tmux session
        import asyncio
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), self.svc.loop).result(5)
        self.svc.loop.call_soon_threadsafe(self.svc.pty.kill)
        wait(lambda: idle.has("exit"), 10, what="exit frame for the old PTY")
        self.assertEqual(subprocess.run(["docker", "exec", "-u", "lane", container, "tmux", "has-session", "-t",
                                         "lane"]).returncode, 0, "Claude keeps running in tmux")
        second = WS("/ws/lane?cols=100&rows=30", self.token)
        wait(lambda: second.texts, 10, what="hello")
        self.assertEqual(second.texts[0]["run"], {"short": "zz01", "run": "calib-1"})
        wait(lambda: "load it first" in second.data.decode("utf-8", "replace"), 10, what="attached screen")
        second.text({"type": "hello", "caps": ["navigate"]})

        # a reply, then End run
        second.send(b"return 404\r")
        self.until_state("working", "waiting")
        wait(lambda: "Done. Unloaded" in self.pane(container), 15, what="second answer")
        self.until_state("waiting")
        self.assertEqual(self.call("POST", "/api/lane/ghostty")[0], 204)
        argv = self.ghostty_calls[-1]
        self.assertEqual(argv[:4], ["open", "-na", "Ghostty.app", "--args"])
        self.assertTrue(argv[5] == "-e" and argv[6].endswith("calib-1/model_a/claude-dev"))
        self.assertEqual(argv[7:], ["--model", "claude-opus-5-5", "--effort", "high"])
        self.assertEqual(self.call("POST", "/api/lane/end")[0], 204)
        st = self.until_state("exited")
        self.assertEqual(st["run"]["run"], "calib-1")
        wait(lambda: second.has("idle"), 10, what="idle after exit")
        third = WS("/ws/lane", self.token)
        wait(lambda: third.has("idle"), 10, what="no attach once the session is gone")
        self.assertIsNone(third.texts[0]["run"])

        # collect (lane_watch does it on its own in the app; here the retry route)
        s, b = self.call("POST", "/api/lane/collect")
        self.assertEqual(s, 202, b)
        self.assertEqual(b["job"]["kind"], "lane_collect")
        st = self.until_state("collected")
        self.assertIn(("zz01", "calib-1"), FakeLanes.calls)
        wait(lambda: second.has("navigate"), 10, what="navigate frame")
        nav = [t for t in second.texts if t.get("type") == "navigate"][0]
        self.assertEqual(nav["href"], "/task/zz01#field=goal")
        self.assertFalse(idle.has("navigate"), "only tabs that said they can navigate")

        # the next lane stops the idle, collected one first
        s, b = self.call("POST", "/api/lane/start", {"short": "zz02", "kind": "calib"})
        self.assertEqual(s, 202, b)
        st = self.until_state("needs_login")
        self.assertEqual(st["run"]["short"], "zz02")
        wait(lambda: container not in subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True,
                                                     text=True).stdout, 20, what="old lane stopped")
        for w in (idle, second, third):
            w.close()


if __name__ == "__main__":
    unittest.main()
