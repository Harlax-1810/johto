"""Domain logic for real tasks: run facts, stage derivation, the Home queue and the Studio checklist.

Pure functions over files plus lane and job snapshots (build-plan section 9). `johto.web.data` reads the
files and calls these; nothing here writes.
"""
from . import checklist, derive, facts, queue  # noqa: F401
