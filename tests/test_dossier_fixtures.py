"""Shared builders for the dossier tests: a fake repo tree and a valid johto.dossier/1 document."""
from __future__ import annotations

import copy
import unittest
from pathlib import Path

SHA = "9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"
REPO = "acme/widget"
DID = "acme__widget__9f1e2d3c"

FILES = {
    "src/server.ts": 120,
    "src/db/store.ts": 60,
    "src/jobs/queue.ts": 80,
    "web/app.tsx": 40,
    "test/store.test.ts": 30,
    "docker-compose.yml": 12,
}


def make_root(tmp: Path) -> Path:
    for rel, n in FILES.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"line {i}\n" for i in range(1, n + 1)))
    return tmp


def _opp(i: int) -> dict:
    return {
        "type": "opportunity", "id": f"o{i}",
        "title": f"Job queue retries lose state {i}",
        "area": "job state consistency",
        "ambition": "Job state lives in the queue, the database and the API cache, which disagree after a retry, "
                    "so failed jobs show as running.",
        "why_not_done": "Touches every job type; the maintainers lack time to test partial failures.",
        "evidence": [{"pointer": "src/jobs/queue.ts:10-40", "fact": "Retry re-enqueues without clearing the lease."},
                     {"pointer": "src/db/store.ts:5", "fact": "Status is written before the transaction commits."}],
        "where_models_fail": ["The lease in src/jobs/queue.ts outlives a worker crash, so the retry counter "
                              "alone never frees the job."],
        "bounded_task": {"exists": True, "shape": "Lease release and status writes in src/jobs/queue.ts when a "
                                                  "worker dies mid-job; the job stays leased and shows running.",
                         "size": "M", "check_in_session": "test/store.test.ts with a worker killed mid-job; the "
                                                          "leased row before and after."},
        "slot_fit": [{"action": "Modify", "domain": "Backend / API", "codebase_type": "Enterprise-grade"}],
        "one_shot": {"likelihood": "unlikely", "why": "The lease is released three calls away in the store."},
        "upstream": {"related": [{"url": "https://github.com/acme/widget/issues/12", "title": "Jobs stuck",
                                  "state": "open"}], "looks_solved": False},
        "self_contained": {"ok": True, "needs": ["mock mail server"]},
    }


VALID = {
    "schema": "johto.dossier/1", "id": DID, "repo": REPO, "commit": SHA,
    "pinned_url": f"https://github.com/{REPO}/tree/{SHA}",
    "generated_at": "2026-10-30T21:00:00-0700", "model": "claude-opus-5-5", "effort": "high",
    "elapsed_s": 900, "cost_usd": 4.1,
    "headline": "Self-hosted widget tracker for small teams",
    "verdict": {"promise": "high", "why": "Stateful job queue with real partial-failure gaps.",
                "best_fits": [{"action": "Modify", "domain": "Backend / API", "codebase_type": "Enterprise-grade",
                               "why": "Queue and store bugs span layers."}]},
    "blocks": [
        {"type": "overview", "title": "What it is", "what": "A widget tracker with a job queue.",
         "users": "Small teams self-hosting it.", "stack": ["TypeScript", "Postgres", "React"],
         "size": {"source_files": 6, "test_files": 1, "languages": ["TypeScript"]},
         "run": {"setup": ["npm ci"], "test": ["npm test"], "notes": "Needs Postgres from docker-compose."}},
        {"type": "architecture", "summary": "API server, job queue and a Postgres store.",
         "nodes": [{"id": "api", "label": "API server", "kind": "service", "paths": ["src/server.ts"], "note": "HTTP"},
                   {"id": "q", "label": "Job queue", "kind": "worker", "paths": ["src/jobs/"]},
                   {"id": "db", "label": "Store", "kind": "store", "paths": ["src/db/*.ts"]}],
         "edges": [{"from": "api", "to": "q", "label": "enqueues"}, {"from": "q", "to": "db", "label": "writes"}]},
        {"type": "flow", "summary": "A request enqueues a job that writes the store.",
         "steps": [{"label": "Request arrives", "pointer": "src/server.ts:1-20", "note": "Routes are static."},
                   {"label": "Job runs", "pointer": "src/jobs/queue.ts:30"}]},
        {"type": "subsystems", "items": [{"name": "Job queue", "paths": ["src/jobs"], "what": "Leases and retries.",
                                          "health": "fragile", "evidence": ["src/jobs/queue.ts:1-80"]}]},
        _opp(1), _opp(2), _opp(3),
        {"type": "code", "pointer": "src/db/store.ts:1-10", "caption": "Status write before commit."},
        {"type": "facts", "items": [{"label": "License", "value": "MIT"}]},
        {"type": "callout", "tone": "warn", "text": "Tests need Postgres running."},
        {"type": "questions", "items": ["Does the lease table survive a restart?"]},
        {"type": "html", "html": "<svg viewBox='0 0 10 10'><rect width='5' height='5'/></svg>",
         "caption": "Lease timeline."},
    ],
}


def valid_doc() -> dict:
    return copy.deepcopy(VALID)


class FixturesSelfTest(unittest.TestCase):
    def test_files_and_doc_build(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            root = make_root(Path(t))
            self.assertTrue((root / "src/server.ts").is_file())
        self.assertEqual(sum(1 for b in valid_doc()["blocks"] if b["type"] == "opportunity"), 3)
