"""In-memory stand-ins for Mercor Studio and gh, used when JOHTO_FAKE_STUDIO=1 (and in tests).

`studio` mirrors johto/studio.py function for function. `gh` covers the GitHub calls the task
actions make: repo metadata for a claim and secret gists for the Conversation Log and Final Output.

The fakes refuse to run against the real checkout's data (config.ROOT == config.CHECKOUT) unless
JOHTO_FAKE_ALLOW_REAL=1, so a fake claim can never create a folder under the real tasks/.
"""
from __future__ import annotations

import os

from .. import config


class FakeRefused(RuntimeError):
    pass


def guard_paths() -> None:
    if config.ROOT == config.CHECKOUT and os.environ.get("JOHTO_FAKE_ALLOW_REAL") != "1":
        raise FakeRefused(
            "The fake Studio runs only with JOHTO_ROOT pointing at a copy of data/ and tasks/, "
            "never at the real checkout.")
