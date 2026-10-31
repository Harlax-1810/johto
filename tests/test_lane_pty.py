"""The PTY bridge without Docker: echo, replay for a new socket, several clients, resize, a socket closing
never kills the PTY, the async Enter lock, and no spawn when the tmux session is gone."""
import asyncio
import json
import unittest
from unittest import mock

from johto.web.lane import monitor, service
from johto.web.lane.pty_bridge import Client, Pty


class Sink:
    def __init__(self):
        self.bytes, self.texts = bytearray(), []

    async def b(self, data):
        self.bytes += data

    async def t(self, data):
        self.texts.append(json.loads(data))

    def client(self) -> Client:
        return Client(self.b, self.t)


async def until(cond, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


class PtyTests(unittest.IsolatedAsyncioTestCase):
    async def test_echo_replay_and_several_clients(self):
        p = Pty(["/bin/cat"], cols=80, rows=24)
        a = Sink()
        ca = a.client()
        p.attach(ca)
        p.write(b"hello\r")
        self.assertTrue(await until(lambda: b"hello" in a.bytes))
        b = Sink()
        cb = b.client()
        p.attach(cb)                                # a new socket gets the replay first
        self.assertTrue(await until(lambda: b"hello" in b.bytes))
        p.write(b"again\r")
        self.assertTrue(await until(lambda: b"again" in a.bytes and b"again" in b.bytes))
        p.detach(ca)
        ca.close()                                  # a socket closing never kills the PTY
        await asyncio.sleep(0.1)
        self.assertTrue(p.alive)
        p.write(b"third\r")
        self.assertTrue(await until(lambda: b"third" in b.bytes))
        self.assertNotIn(b"third", a.bytes)
        p.kill()
        self.assertTrue(await until(lambda: not p.alive))
        self.assertTrue(await until(lambda: any(t.get("type") == "exit" for t in b.texts)))
        cb.close()

    async def test_replay_starts_at_last_clear(self):
        p = Pty(["/bin/sh", "-c", r"printf 'old screen\033[H\033[2Jnew screen'; sleep 5"])
        self.assertTrue(await until(lambda: b"new screen" in p.ring))
        self.assertTrue(p.replay().startswith(b"\x1b[H\x1b[2J"))
        self.assertNotIn(b"old screen", p.replay())
        p.kill()
        await until(lambda: not p.alive)

    async def test_resize_reaches_the_child(self):
        p = Pty(["/bin/sh", "-c", "stty size; trap 'stty size' WINCH; while :; do sleep 0.1; done"], cols=90, rows=30)
        self.assertTrue(await until(lambda: b"30 90" in p.ring))
        p.resize(132, 41)
        self.assertTrue(await until(lambda: b"41 132" in p.ring), bytes(p.ring))
        p.kill()
        await until(lambda: not p.alive)

    async def test_large_write_is_not_lost(self):
        p = Pty(["/bin/sh", "-c", "stty raw -echo; head -c 20000 | wc -c; sleep 5"])
        try:
            await asyncio.sleep(0.3)
            p.write(b"x" * 20000)
            self.assertTrue(await until(lambda: b"20000" in p.ring, 8), bytes(p.ring[-200:]))
        finally:
            p.kill()
            await until(lambda: not p.alive)

    async def test_kill_works_when_sighup_is_ignored(self):
        p = Pty(["/bin/sh", "-c", "trap '' HUP; sleep 30"])
        await asyncio.sleep(0.3)
        p.kill()
        self.assertTrue(await until(lambda: not p.alive, 6))


class ServiceSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.svc = service.LaneService()
        self.svc._auto_override = False
        self.svc.setup = lambda d, kind, log: (_ for _ in ()).throw(AssertionError("real setup"))
        self.svc._ensure_loop()

    async def test_async_lock_swallows_enter_unless_permission(self):
        s = self.svc
        s.pty = Pty(["/bin/cat"])
        sink = Sink()
        c = sink.client()
        s.clients.add(c)
        s.pty.attach(c)
        s._state = {**s._blank(), "input_locked": True}
        s._probe = {"look": {"pane": None}}
        s.lane_input(c, b"one more thing\r")
        self.assertTrue(await until(lambda: any(t.get("type") == "swallowed" for t in sink.texts)))
        self.assertTrue(await until(lambda: b"one more thing" in sink.bytes))
        s.pty.write(b"\x04")
        await asyncio.sleep(0.2)
        # a permission dialog on screen lets Enter through
        s._probe = {"look": {"pane": (monitor.Path(__file__).parent / "fixtures" / "panes" / "permission.txt").read_text()}}
        n = len([t for t in sink.texts if t.get("type") == "swallowed"])
        s.lane_input(c, b"\r")
        await asyncio.sleep(0.2)
        self.assertEqual(len([t for t in sink.texts if t.get("type") == "swallowed"]), n)
        s.pty.kill()
        await until(lambda: s.pty is None or not s.pty.alive)
        c.close()

    async def test_never_spawns_without_a_live_session(self):
        s = self.svc
        info = {"short": "zz01", "run": "calib-1", "dir": monitor.Path("/nonexistent")}
        s._probe = {"info": info, "cur": "johtotest-x-model_a", "paths": {"workspace": "/w"}}
        with mock.patch.object(monitor, "session_alive", return_value=False), \
                mock.patch.object(service, "Pty") as P:
            self.assertIsNone(await s.ensure_pty())
            P.assert_not_called()

    async def test_attach_argv_cannot_start_claude(self):
        argv = service.attach_argv("johtotest-x-model_a", "/exp/w")
        self.assertIn("attach-session", argv)
        self.assertNotIn("new-session", argv)
        self.assertNotIn("claude", " ".join(argv[argv.index("johtotest-x-model_a") + 1:]))

    async def test_idle_socket_gets_hello_then_idle(self):
        s = self.svc
        sink = Sink()

        async def no_refresh():
            s._probe = {}
            return s.state()
        with mock.patch.object(s, "refresh", no_refresh):
            c = await s.lane_client(sink.b, sink.t)
        self.assertTrue(await until(lambda: len(sink.texts) >= 2))
        self.assertEqual(sink.texts[0], {"type": "hello", "run": None, "locked": False})
        self.assertEqual(sink.texts[1], {"type": "idle"})
        s.lane_closed(c)


if __name__ == "__main__":
    unittest.main()
