"""Server-side PTYs for the lane: one shared Claude terminal, plus a throwaway shell per socket.

The lane PTY runs a tmux client inside the lane container (through `model_a/claude-dev`, or a plain
`tmux attach` when the session already exists), so the browser, Ghostty and the CLI all see the same tmux
session "lane". Output goes to a 1 MiB ring buffer and to every connected socket. A socket closing never
kills the PTY; killing the PTY only detaches its tmux client, so Claude keeps running.

Everything runs on the server's event loop. Other threads call in with `loop.call_soon_threadsafe`.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path
from typing import Callable

RING_BYTES = 1 << 20
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
CLEARS = (b"\x1b[H\x1b[2J", b"\x1b[2J", b"\x1b[H\x1b[J", b"\x1bc")

# Popen gives the child a new session; this makes the PTY its controlling terminal, so resizes reach
# `docker exec -it` as SIGWINCH and tmux follows them.
_CTTY = ("import fcntl, os, signal, sys, termios\n"
         "for s in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):\n"
         "    signal.signal(s, signal.SIG_DFL)\n"
         "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
         "os.execvp(sys.argv[1], sys.argv[1:])\n")


def _winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Client:
    """One WebSocket (or a test double) reading the PTY. Frames go out in order through one queue."""

    def __init__(self, send_bytes: Callable, send_text: Callable):
        self._send_bytes, self._send_text = send_bytes, send_text
        self.q: asyncio.Queue = asyncio.Queue()
        self.task = asyncio.get_running_loop().create_task(self._pump())
        self.closed = False

    def bytes(self, b: bytes) -> None:
        if not self.closed:
            self.q.put_nowait(("b", b))

    def text(self, obj: dict) -> None:
        if not self.closed:
            self.q.put_nowait(("t", json.dumps(obj)))

    async def _pump(self) -> None:
        try:
            while True:
                kind, data = await self.q.get()
                if kind == "b":
                    await self._send_bytes(data)
                else:
                    await self._send_text(data)
        except asyncio.CancelledError:
            pass
        except Exception:
            self.closed = True

    def close(self) -> None:
        self.closed = True
        self.task.cancel()


class Pty:
    """A child process on a PTY, with a ring buffer and any number of clients."""

    def __init__(self, argv: list[str], *, cwd: str | Path | None = None, cols: int = 100, rows: int = 32,
                 env: dict | None = None, on_exit: Callable[["Pty", int], None] | None = None, tag: dict | None = None):
        self.argv, self.tag = list(argv), dict(tag or {})
        self.cols, self.rows = cols, rows
        self.clients: set[Client] = set()
        self.ring = bytearray()
        self.code: int | None = None
        self.started_at = time.time()
        self.first_output_at: float | None = None
        self._on_exit = on_exit
        self._wbuf = bytearray()
        self._writing = False
        self._loop = asyncio.get_running_loop()
        master, slave = os.openpty()
        _winsize(slave, cols, rows)
        full_env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor", **(env or {})}
        try:
            self.proc = subprocess.Popen([sys.executable, "-I", "-c", _CTTY, *self.argv], stdin=slave, stdout=slave,
                                         stderr=slave, cwd=str(cwd) if cwd else None, env=full_env,
                                         start_new_session=True, close_fds=True)
        finally:
            os.close(slave)
        self.fd = master
        os.set_blocking(master, False)
        self._loop.add_reader(master, self._readable)
        self._waiter = self._loop.create_task(self._wait())

    # -- output
    def _readable(self) -> None:
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._loop.remove_reader(self.fd)
            return
        if self.first_output_at is None:
            self.first_output_at = time.time()
        self.ring += data
        if len(self.ring) > RING_BYTES:
            del self.ring[:len(self.ring) - RING_BYTES]
        for c in list(self.clients):
            c.bytes(data)

    async def _wait(self) -> None:
        code = await asyncio.to_thread(self.proc.wait)
        await asyncio.sleep(0.05)
        try:
            self._readable()             # whatever is left in the buffer
        except Exception:
            pass
        try:
            self._loop.remove_reader(self.fd)
            self._loop.remove_writer(self.fd)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.code = code
        for c in list(self.clients):
            c.text({"type": "exit", "code": code})
        if self._on_exit:
            self._on_exit(self, code)

    @property
    def alive(self) -> bool:
        return self.code is None

    def replay(self) -> bytes:
        """The buffer from its last full-screen clear, so a new terminal starts on a clean screen."""
        buf = bytes(self.ring)
        best = None                                   # (end, -start): the latest clear, the longest form
        for c in CLEARS:
            i = buf.rfind(c)
            if i >= 0 and (best is None or (i + len(c), -i) > best):
                best = (i + len(c), -i)
        return buf[-best[1]:] if best else buf

    def text_tail(self, n: int = 12) -> str:
        from .monitor import strip_ansi
        lines = [ln.rstrip() for ln in strip_ansi(bytes(self.ring[-20000:]).decode("utf-8", "replace"))
                 .replace("\r", "\n").splitlines() if ln.strip()]
        return "\n".join(lines[-n:])

    # -- input
    def write(self, data: bytes) -> None:
        if not self.alive or not data:
            return
        self._wbuf += data
        self._flush()

    def _flush(self) -> None:
        while self._wbuf:
            try:
                n = os.write(self.fd, self._wbuf)
            except BlockingIOError:
                n = 0
            except OSError:
                self._wbuf.clear()
                break
            if n <= 0:
                break
            del self._wbuf[:n]
        if self._wbuf and not self._writing:
            self._writing = True
            self._loop.add_writer(self.fd, self._writable)
        elif not self._wbuf and self._writing:
            self._writing = False
            self._loop.remove_writer(self.fd)

    def _writable(self) -> None:
        self._flush()

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(20, min(int(cols), 500)), max(5, min(int(rows), 200))
        if not self.alive or (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        try:
            _winsize(self.fd, cols, rows)
        except OSError:
            pass

    # -- clients
    def attach(self, client: Client, replay: bool = True) -> None:
        self.clients.add(client)
        if replay:
            r = self.replay()
            if r:
                client.bytes(r)

    def detach(self, client: Client) -> None:
        self.clients.discard(client)

    def kill(self) -> None:
        """End the PTY's process (for the lane, that only detaches its tmux client)."""
        if not self.alive:
            return

        def sig(n):
            if self.alive and self.proc.poll() is None:
                try:
                    os.killpg(self.proc.pid, n)
                except (ProcessLookupError, PermissionError):
                    pass
        sig(signal.SIGHUP)                    # like a closed terminal
        self._loop.call_later(1, sig, signal.SIGTERM)
        self._loop.call_later(3, sig, signal.SIGKILL)


def strip_enters(data: bytes) -> tuple[bytes, bool]:
    """Drop Enter keys from typed input, keeping bracketed pastes whole. Returns (bytes, dropped)."""
    if PASTE_START in data:
        return data, False
    out = data.replace(b"\r", b"").replace(b"\n", b"")
    return out, len(out) != len(data)
