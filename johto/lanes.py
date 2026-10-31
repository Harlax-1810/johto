"""Pre-work lanes: one new folder per run under tasks/<short>/runs/, via the vendor prework.py."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path

from . import config, transcript, workspace

PREWORK = "mercor-prework"
HOME = config.PREWORK_HOME            # JOHTO_PREWORK_HOME; ~/.mercor-prework by default
KINDS = ("calib", "validate")
# Lane containers are prework-<key>-model_a. Tests run throwaway lanes under their own prefix.
PREFIX = os.environ.get("JOHTO_LANE_PREFIX", "prework-")
# A Claude Code process: argv[0] is `claude`. Not tmux's `... claude-dev` argument, not a zombie `[claude]`.
CLAUDE_PROC_RE = re.compile(r"^(?:\S*/)?claude(?:\s|$)")


def _run(cmd, cwd=None, check=False, capture=True, timeout=None):
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture, check=check, timeout=timeout)


# Two lanes can run on one machine. prework.py gives every lane the same host ports, so the second one goes
# through johto/prework_slot.py (the mercor-prework wrapper runs it when PREWORK_PORT_SLOT is set), which
# moves its host ports up by 2000 and puts it on the next CPUs. Each lane may use 45% of Docker's memory, so
# two fit and a third doesn't. The slot is recorded on the run as port_slot (missing means 0).
SLOTS = 2
SLOT_PORT_SHIFT = 2000
HOST_PORT_RANGES = ((34000, 34019), (35000, 35009))  # slot 0's, as in prework.py's LANE_PORT_RANGES


def slot_ports(slot: int) -> list[int]:
    return [p + SLOT_PORT_SHIFT * slot for first, last in HOST_PORT_RANGES for p in range(first, last + 1)]


def slot_label(slot: int) -> str:
    return " and ".join(f"{a + SLOT_PORT_SHIFT * slot}-{b + SLOT_PORT_SHIFT * slot}" for a, b in HOST_PORT_RANGES)


def _port_free(port: int) -> bool:
    """The check prework's own setup makes: can this machine bind 127.0.0.1:port right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_slot() -> int | None:
    """The first slot whose host ports are all free (a running lane holds its slot's ports), or None."""
    for slot in range(SLOTS):
        if all(_port_free(p) for p in slot_ports(slot)):
            return slot
    return None


def container_slot(container: str) -> int | None:
    """The slot a lane container was created on, from its port bindings; None when Docker can't say."""
    r = _run(["docker", "inspect", "-f", '{{(index (index .HostConfig.PortBindings "3000/tcp") 0).HostPort}}',
              container], timeout=20)
    try:
        offset = int(r.stdout.strip()) - HOST_PORT_RANGES[0][0]
    except ValueError:
        return None
    slot, rest = divmod(offset, SLOT_PORT_SHIFT)
    return slot if rest == 0 and 0 <= slot < SLOTS else None


def run_slot(run: dict | None) -> int:
    return int((run or {}).get("port_slot") or 0)


def prework_env(slot: int) -> dict:
    env = dict(os.environ)
    env.pop("PREWORK_PORT_SLOT", None)
    if slot:
        env["PREWORK_PORT_SLOT"] = str(slot)
    return env


def running_lanes() -> list[str]:
    r = _run(["docker", "ps", "--filter", f"name=^{PREFIX}", "--format", "{{.Names}}"])
    return sorted(n for n in r.stdout.split() if n.startswith(PREFIX)) if r.returncode == 0 else []


def claude_active(container: str) -> bool:
    """True when a Claude Code process is running inside the lane container.

    Reads `ps` rather than `pgrep -f claude`, which also matched its own `sh -c` line (so it always said yes),
    tmux's `claude-dev` argument and zombies. When the probe fails or times out it answers True: the callers
    use it to refuse a new lane, skip a teardown or keep a lane running."""
    try:
        r = _run(["docker", "exec", container, "ps", "-eo", "stat=,args="], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return True   # can't tell: every caller refuses or skips on True, so unknown counts as alive
    if r.returncode != 0:
        # A stopped container has no Claude; any other failure (Docker busy, exec refused) is unknown.
        try:
            st = _run(["docker", "inspect", "-f", "{{.State.Running}}", container], timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return True
        return not (st.returncode == 0 and st.stdout.strip() == "false")
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and not parts[0].startswith("Z") and CLAUDE_PROC_RE.match(parts[1]):
            return True
    return False


def _guard_real(container: str | None, what: str) -> None:
    """Austin's real lanes are prework-*: a server on a scratch copy (or in fake mode) never touches one.
    Copied task folders carry the real container names."""
    if (container or PREFIX).startswith("prework-"):
        config.require_real(f"{what} {container or ''}".strip())


def lane_key(container: str | None) -> str | None:
    """prework-PREWORK_<task>-<id>-model_a -> PREWORK_<task>-<id> (the lane's config dirs are <key>_model_a)."""
    if not container or not container.endswith("-model_a"):
        return None
    for p in (PREFIX, "prework-"):
        if container.startswith(p):
            return container[len(p):-len("-model_a")]
    return None


def manifest(run_dir: Path) -> dict:
    p = Path(run_dir) / "prework-manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def next_run_dir(task_dir: Path, kind: str) -> Path:
    n = 1
    while (task_dir / "runs" / f"{kind}-{n}").exists():
        n += 1
    return task_dir / "runs" / f"{kind}-{n}"


def _pick_slot(stop_others: bool) -> int:
    """A free slot for a new lane. Running lanes are left alone while a slot is free. With none free, idle
    lanes (no Claude) are stopped; with --stop-others every running lane is. Stopped lanes keep their files."""
    busy = running_lanes()
    if stop_others:
        for c in busy:
            _run(["docker", "stop", c])
        busy = []
    slot = free_slot()
    if slot is not None:
        return slot
    active = [c for c in busy if claude_active(c)]
    idle = [c for c in busy if c not in active]
    for c in idle:
        _run(["docker", "stop", c])
    slot = free_slot()
    if slot is None:
        raise RuntimeError(
            f"Both lane slots are taken (host ports {slot_label(0)}, and {slot_label(1)})"
            + (": Claude is running in " + ", ".join(active) if active else "")
            + ".\nFinish one (/exit) or pass --stop-others to stop them (their files are kept).")
    return slot


def new_lane(task_dir: Path, kind: str, stop_others: bool = False) -> Path:
    """Set up a lane in a brand-new folder, on a free slot. Returns the run folder."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    _guard_real(None, "prework setup")
    meta = workspace.load(task_dir)
    slot = _pick_slot(stop_others)
    run_dir = next_run_dir(task_dir, kind)
    run_dir.mkdir(parents=True)
    cmd = [PREWORK, "setup", "--task", meta["task_name"], "--repo", meta["repo"]["pinned_url"]]
    print(f"Setting up {run_dir.name} for {meta['task_name']} at {meta['repo']['commit'][:8]}...")
    if slot:
        print(f"Another lane is running, so this one is the second lane: its dev servers open at host ports "
              f"{slot_label(slot)}, not {slot_label(0)}.")
    r = subprocess.run(cmd, cwd=run_dir, stdin=subprocess.DEVNULL, text=True, capture_output=True,
                       env=prework_env(slot))
    log = (r.stdout or "") + (r.stderr or "")
    (run_dir.parent / f".{run_dir.name}-setup.log").write_text(log)
    keep = [ln for ln in log.splitlines() if re.match(r"\s*\[(OK|INFO|WARN|ERROR)\]|ERROR", ln)]
    print("\n".join("  " + ln.strip() for ln in keep))
    if r.returncode != 0:
        print(log[-3000:])
        raise RuntimeError(f"prework setup failed (exit {r.returncode}). Folder: {run_dir}")
    rec = {
        "kind": kind, "dir": str(run_dir.relative_to(task_dir)),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "container": manifest(run_dir).get("lane", {}).get("container"),
    }
    if slot:
        rec["port_slot"] = slot
    meta = workspace.load(task_dir)  # setup took minutes; don't write back a task.json read before it
    meta.setdefault("runs", []).append(rec)
    workspace.save(task_dir, meta)
    return run_dir


def remote_of(run: dict) -> tuple[str, str] | None:
    """(ssh host, run folder there) for a run whose lane lives on another machine, such as the PC."""
    if run.get("host") and run.get("remote_dir"):
        return run["host"], run["remote_dir"]
    return None


def _prework_collect(task_dir: Path, run_name: str, run_dir: Path) -> None:
    rec = next((r for r in workspace.load(task_dir).get("runs", []) if r["dir"] == f"runs/{run_name}"), {})
    remote, slot = remote_of(rec), run_slot(rec)
    if not remote:
        # The slot matters only if collect has to restart a stopped lane: it then pins the lane's CPUs again.
        r = subprocess.run([PREWORK, "collect"], cwd=run_dir, stdin=subprocess.DEVNULL, text=True,
                           env=prework_env(slot))
        if r.returncode != 0:
            raise RuntimeError(f"prework collect failed in {run_dir}")
        return
    # The lane's container is on the other machine: collect there, then copy the outputs back.
    host, rdir = remote
    env = f"PREWORK_PORT_SLOT={slot} " if slot else ""
    r = subprocess.run(["ssh", host, f"cd {shlex.quote(rdir)} && {env}~/.local/bin/mercor-prework collect"],
                       stdin=subprocess.DEVNULL, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"prework collect failed on {host} in {rdir}")
    r = subprocess.run(["rsync", "-a", f"{host}:{rdir}/prework_output/", f"{run_dir}/prework_output/"],
                       stdin=subprocess.DEVNULL, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"copying {host}:{rdir}/prework_output back failed")


def collect(task_dir: Path, run_name: str) -> dict:
    run_dir = task_dir / "runs" / run_name
    _prework_collect(task_dir, run_name, run_dir)
    diff = run_dir / "prework_output" / "final.diff"
    logs = transcript.find(run_dir)
    summary = transcript.summarize(logs[-1]) if logs else None
    if len(logs) > 1:
        print(f"note: {len(logs)} transcripts in {run_dir}/prework_output; using the newest. "
              "More than one usually means the lane was reused.")
    info = {"diff": str(diff) if diff.exists() else None,
            "diff_bytes": diff.stat().st_size if diff.exists() else 0,
            "transcript": str(logs[-1]) if logs else None, "summary": summary}
    meta = workspace.load(task_dir)
    for run in meta.get("runs", []):
        if run["dir"] == f"runs/{run_name}":
            run["collected_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            run["diff"], run["transcript"] = info["diff"], info["transcript"]
            if summary:
                run["active_minutes"] = summary["active_minutes"]
                run["human_turns"] = summary["human_turns"]
    workspace.save(task_dir, meta)
    if summary:
        (run_dir / "summary.md").write_text(f"# {run_name} summary\n\n" + transcript.render(summary) + "\n")
    return info


def latest_run(task_dir: Path, kind: str) -> str | None:
    runs = sorted((task_dir / "runs").glob(f"{kind}-*"), key=lambda p: int(p.name.split("-")[1]))
    return runs[-1].name if runs else None


def diff_stat(diff_path: str | None) -> str:
    if not diff_path or not Path(diff_path).exists():
        return "(no diff)"
    files = [ln[len("+++ b/"):] for ln in Path(diff_path).read_text(errors="ignore").splitlines()
             if ln.startswith("+++ b/")]
    return f"{len(files)} files changed: " + ", ".join(files[:12]) + (" ..." if len(files) > 12 else "")


def teardown(task_dir: Path, run_name: str) -> list[str]:
    """Remove one run's container, volumes, network and lane config (it holds the /login)."""
    run_dir = task_dir / "runs" / run_name
    m = manifest(run_dir)
    container = (m.get("lane") or {}).get("container")
    done = []
    if not container:
        return done
    _guard_real(container, "lane teardown")
    _run(["docker", "rm", "-f", "-v", container])
    for v in (container, f"{container}-dind", f"{container}-ctr"):
        if _run(["docker", "volume", "rm", "-f", v]).returncode == 0:
            done.append(v)
    key = lane_key(container)
    if key:
        _run(["docker", "network", "rm", f"prework-{key}"])
        for sub in ("cc-config", "lane-harness"):
            p = Path(config.PREWORK_HOME) / sub / f"{key}_model_a"
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
                done.append(str(p))
    staging = run_dir / ".prework-clone"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    return done


LAUNCH_ARGS = "--model claude-opus-5-5 --effort high"


def launch_cmd(run_dir: Path) -> str:
    return f"cd {run_dir}/model_a && ./claude-dev {LAUNCH_ARGS}"


def launch_hint(run_dir: Path) -> str:
    return (f"  {launch_cmd(run_dir)}\n"
            "  First launch: /login with your Claude account. Check the header says Opus 5.5 / high,\n"
            "  then paste the prompt (it's on your clipboard).")


def copy_to_clipboard(text: str) -> bool:
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False
