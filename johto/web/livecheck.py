"""Is Claude actually running in a lane? A lane's container stays up after /exit, so check the process.

Cached for a few seconds so page loads don't each run `docker exec`.
"""
from __future__ import annotations

import subprocess
import time

_CACHE: dict[str, tuple[float, bool]] = {}
TTL_S = 4.0


def claude_alive(container: str) -> bool:
    now = time.time()
    hit = _CACHE.get(container)
    if hit and now - hit[0] < TTL_S:
        return hit[1]
    try:
        # The lane monitor's rule: a live process whose argv[0] is claude (not tmux's claude-dev argument,
        # not a zombie [claude]).
        from .lane import monitor
        alive = monitor.claude_alive(container)
    except Exception:
        alive = False
    _CACHE[container] = (now, alive)
    return alive
