"""Run Mercor's prework.py for a second lane on the same machine. prework.py itself is not changed.

Every lane publishes its dev servers on the same host ports (34000-34019 and 35000-35009) and pins the same
CPUs (0 to N-1), so a second lane can't be set up while one is running. This loads prework.py as a module and,
for slot 1, moves the host ports to 36000-36019 and 37000-37009 and the CPUs to the next N, then runs its
main(). Inside the lane nothing changes: dev servers still use 3000-3019 and 8000-8009.

The CPU change also goes into the claude-dev launcher that setup writes, because claude-dev pins the CPUs
again every time it starts the lane. Only setup creates the container, so the ports are fixed from then on.
Collect runs through here too, so a stopped lane that collect restarts keeps its own CPUs.

Two lanes fit because each may use 45% of Docker's memory. On a Docker engine under 8 GB a lane may use 90%,
so a second lane is refused there.

Usage: python3 -I prework_slot.py <path/to/prework.py> <slot> <prework args...>
Slot 0 runs prework.py unchanged. Standard library only: the Mac and PC wrappers run it with the system python3.
"""
from __future__ import annotations

import importlib.util
import sys

SLOTS = 2
PORT_SHIFT = 2000  # slot 1's host ports are 2000 above slot 0's
# The line in prework.py's claude-dev template that pins the lane to CPUs 0 to N-1.
CPU_LINE = '      CPUSET="0-$(( N - 1 ))"\n'


def _shift(outside: str, by: int) -> str:
    first, _, last = outside.partition("-")
    return f"{int(first) + by}-{int(last or first) + by}"


def load(path: str):
    spec = importlib.util.spec_from_file_location("prework", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"prework_slot: can't load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prework"] = mod
    spec.loader.exec_module(mod)
    return mod


def apply(mod, slot: int):
    """Give `mod` (a loaded prework.py) slot `slot`'s host ports and CPUs. Slot 0 leaves it as it is."""
    if slot == 0:
        return mod
    if mod.WRAPPER_SH.count(CPU_LINE) != 1:
        raise SystemExit("prework_slot: prework.py's claude-dev template has changed, so the second lane's "
                         "CPUs can't be set. Update johto/prework_slot.py for the new prework.py first.")
    mod.LANE_PORT_RANGES = tuple((inside, _shift(outside, PORT_SHIFT * slot))
                                 for inside, outside in mod.LANE_PORT_RANGES)
    orig = mod.lane_cpu_args

    def lane_cpu_args(engine_gib, ncpu):
        args = orig(engine_gib, ncpu)
        if len(args) == 2 and args[0] == "--cpuset-cpus":
            first, _, last = args[1].partition("-")
            n = int(last or first) - int(first) + 1
            off = n * slot
            if int(first) == 0 and off + n <= ncpu:  # a lane that already has every CPU keeps them
                args = ["--cpuset-cpus", f"{off}-{off + n - 1}"]
        return args

    mod.lane_cpu_args = lane_cpu_args
    mod.WRAPPER_SH = mod.WRAPPER_SH.replace(CPU_LINE, (
        f'      OFF=$(( N * {slot} )); [ $(( OFF + N )) -gt "$NCPU" ] && OFF=0  # second lane (johto prework_slot)\n'
        '      CPUSET="$OFF-$(( OFF + N - 1 ))"\n'))
    return mod


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        raise SystemExit("usage: prework_slot.py <path/to/prework.py> <slot> <prework args...>")
    path, slot, rest = argv[0], argv[1], argv[2:]
    if slot not in {str(s) for s in range(SLOTS)}:
        raise SystemExit(f"prework_slot: the slot must be 0 or 1, not {slot!r}")
    mod = apply(load(path), int(slot))
    if int(slot) and rest[:1] == ["setup"]:
        gib, _ = mod.engine_resources()
        if gib and mod.small_engine(gib):
            mod.error(f"Docker has {gib:.1f} GiB, and on an engine under {mod.SMALL_ENGINE_GIB} GB each lane may "
                      "use 90% of it, so a second lane doesn't fit. Give Docker more memory or stop the other lane.")
    mod.main(rest)


if __name__ == "__main__":
    main(sys.argv[1:])
