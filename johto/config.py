"""Fixed ids and paths for Project Johto in Mercor Studio.

Environment overrides (read once, at import, so every module that derives paths from
DATA or TASKS sees the same values):

- JOHTO_ROOT: folder holding data/ and tasks/ (default: this checkout).
- JOHTO_FAKE_STUDIO=1 (or JOHTO_FAKE=1, the same thing): Studio, gh and lanes go to the fakes in johto/fakes/.
- JOHTO_REAL_STUDIO=1: allow real Studio, gists and prework lanes while JOHTO_ROOT points away from this
  checkout. Without it they refuse, so a test server on a scratch copy of data/ can never write to Studio.
- JOHTO_NO_WORKER=1: the web app does not start the dossier worker.
- JOHTO_NO_WATCH=1: the web app does not start the lane watcher.
- JOHTO_PORT: web app port (default 8765, or 8766 with JOHTO_FAKE_STUDIO=1).
- JOHTO_PREWORK_HOME: the prework lanes' home (default ~/.mercor-prework).
- JOHTO_CLAUDE_BIN: the claude binary for headless research runs. In fake mode the default is a binary
  that doesn't exist, so nothing paid runs by accident.
"""
import json
import os
from pathlib import Path


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


CHECKOUT = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ["JOHTO_ROOT"]).expanduser().resolve() if os.environ.get("JOHTO_ROOT") else CHECKOUT
DATA = ROOT / "data"
TASKS = ROOT / "tasks"

# One fake mode: JOHTO_FAKE (build-plan 2.1) and JOHTO_FAKE_STUDIO mean the same thing.
FAKE_STUDIO = _flag("JOHTO_FAKE_STUDIO") or _flag("JOHTO_FAKE")
FAKE = FAKE_STUDIO
REAL_OK = _flag("JOHTO_REAL_STUDIO")
NO_WORKER = _flag("JOHTO_NO_WORKER")
NO_WATCH = _flag("JOHTO_NO_WATCH")
PORT = int(os.environ.get("JOHTO_PORT") or (8766 if FAKE_STUDIO else 8765))
PREWORK_HOME = Path(os.environ.get("JOHTO_PREWORK_HOME") or Path.home() / ".mercor-prework").expanduser()
CLAUDE_BIN_SET = bool(os.environ.get("JOHTO_CLAUDE_BIN"))
CLAUDE_BIN = os.environ.get("JOHTO_CLAUDE_BIN") or (
    "/nonexistent/claude-is-off-in-fake-mode" if FAKE else str(Path.home() / ".local/bin/claude"))
# JOHTO_CODEX_BIN: the Codex CLI for dossiers written by GPT in the clone (dossier_model "codex"); `codex` on PATH.
CODEX_BIN = os.environ.get("JOHTO_CODEX_BIN") or ("/nonexistent/codex-is-off-in-fake-mode" if FAKE else "codex")


class RealWriteRefused(RuntimeError):
    """A real Studio, gh or lane call from a server whose data isn't the real checkout's."""


def require_real(what: str) -> None:
    """Refuse a real Studio / gists / prework-lane call in fake mode, or when ROOT is a scratch copy (unless
    JOHTO_REAL_STUDIO=1). Copies of data/ and tasks/ carry real task ids and container names."""
    if FAKE:
        raise RealWriteRefused(f"{what}: refused in fake mode (the fakes should have handled this).")
    if ROOT != CHECKOUT and not REAL_OK:
        raise RealWriteRefused(f"{what}: refused because JOHTO_ROOT ({ROOT}) isn't this checkout. "
                               "Set JOHTO_REAL_STUDIO=1 to allow it.")

# Mercor-internal ids (campaign, company, account, workflow statuses, QC specs, the Discarded Run Log form)
# and the Chrome profile that holds the Studio login live in studio-ids.json at the checkout root (git-ignored;
# copy studio-ids.example.json). JOHTO_IDS points elsewhere. Without the file, placeholders keep fake mode
# and the tests working; real Studio calls need the real ids.
def _load_ids() -> dict:
    p = Path(os.environ.get("JOHTO_IDS") or CHECKOUT / "studio-ids.json").expanduser()
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return {}


_IDS = _load_ids()
BROWSER_ACCOUNT = os.environ.get("JOHTO_BROWSER_ACCOUNT") or _IDS.get("browser_account", "")
WRITER_NAME = _IDS.get("writer_name", "")  # your name, prefilled in the Discarded Run Log
# The GitHub account gh must be logged in as before the gists upload (the links go into the submission).
GH_ACCOUNT = os.environ.get("JOHTO_GH_ACCOUNT") or _IDS.get("gh_account") or "austeane"

API = "https://api.studio.mercor.com"
STUDIO = "https://studio.mercor.com"
CAMPAIGN_ID = _IDS.get("campaign_id", "camp_placeholder")
COMPANY_ID = _IDS.get("company_id", "comp_placeholder")
ACCOUNT_ID = _IDS.get("account_id", "acct_placeholder")
WORLD_ID = _IDS.get("world_id", "world_placeholder")

_STATUS_KEYS = ("not_ready", "ready", "writing", "awaiting_review", "in_review", "accepted", "rejected")
STATUS = {k: (_IDS.get("status") or {}).get(k, f"status-{k}") for k in _STATUS_KEYS}
STATUS_NAME = {v: k for k, v in STATUS.items()}

# Writer transitions, by name. Edge ids are looked up live from eligible-transitions.
CLAIM = "Claim Task"
SUBMIT = "Submit for Review"
UNCLAIM = "Unclaim Task"

QC_SPECS = {
    "AutoQC": (_IDS.get("qc_specs") or {}).get("AutoQC", "qcspec_autoqc_placeholder"),
    "Codebase Check": (_IDS.get("qc_specs") or {}).get("Codebase Check", "qcspec_codebase_placeholder"),
}

VIEWS = {
    "available": "view_prework_writer_available",
    "mine": "view_prework_writer_my_tasks",
    "submitted": "view_prework_writer_submitted",
    "accepted": "view_prework_writer_approved",
    "rejected": "view_prework_writer_rejected",
}

# The seeded slot fields, in display order.
SLOT_FIELDS = (
    "field_action",
    "field_domain",
    "field_codebase_type",
    "field_prompt_style",
    "field_interaction_mode",
)

LANGUAGES = [
    "Typescript", "JavaScript", "Python", "Go", "Rust", "Java", "Kotlin", "C#", "Swift",
    "PHP", "Ruby", "Dart", "SQL", "Shell", "Haskell", "Other",
]

TASK_SUCCESS = {
    1: "1 - Failed completely",
    2: "2 - Partial, major issues",
    3: "3 - Completed, but highly inefficient or non-optimal, with notable problems",
    4: "4 - Successful, minor or style issues",
    5: "5 - Completely successful",
}
STEERING = {
    1: "1 - Constant input: corrected or redirected the model at most turns",
    2: "2 - Frequent input",
    3: "3 - Occasional input at a few key points",
    4: "4 - One or two light nudges",
    5: "5 - None: the model reached the solution on its own",
}

SWE_BENCH_REPOS = {
    "django/django", "sympy/sympy", "scikit-learn/scikit-learn", "sphinx-doc/sphinx",
    "matplotlib/matplotlib", "pytest-dev/pytest", "pydata/xarray", "astropy/astropy",
    "pylint-dev/pylint", "psf/requests", "mwaskom/seaborn", "pallets/flask",
}

DISCARD_FORM = _IDS.get("discard_form", "https://docs.google.com/forms/d/e/DISCARDED-RUN-LOG/viewform")
