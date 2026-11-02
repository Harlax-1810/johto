"""Synthetic task folders, one per stage, for the real-state tests and a local test server.

`build(root, prework_home)` writes root/tasks/<short>/ folders (task.json, submission.md, runs with
transcripts and diffs) and lane config dirs under prework_home. Nothing here touches the real checkout,
Docker or Studio. Protected fields hold neutral placeholder text ("Lorem ...") so nothing can be mistaken
for Austin's words.
"""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

SLOT = {"action": "Modify", "domain": "Backend / API", "codebase_type": "Personal-scale",
        "prompt_style": "Casual prose", "interaction_mode": "Interactive/pairing"}
REPO = {"full_name": "acme/playlog", "url": "https://github.com/acme/playlog",
        "commit": "9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c",
        "pinned_url": "https://github.com/acme/playlog/tree/9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c",
        "language": "Typescript"}
PROMPT = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore magna aliqua"
GOAL = "Lorem ipsum dolor sit amet. Consectetur adipiscing elit sed do."
DPS = "Better: lorem ipsum dolor sit amet. Worse: consectetur adipiscing elit.\n\nBetter: sed do eiusmod. Worse: tempor incididunt."
CODEBASE = "Lorem ipsum dolor sit amet. Consectetur adipiscing elit. Sed do eiusmod tempor. Incididunt ut labore."
NOTES = "Lorem ipsum dolor sit amet consectetur. Adipiscing elit sed do eiusmod tempor."


def iso(minutes_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - minutes_ago * 60))


def zulu(minutes_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - minutes_ago * 60))


def submission(prompt="", goal="", dps="", codebase="", notes="", one="", ts="", st="") -> str:
    return f"""<!--
Slot: test
-->

## Opening Prompt
{prompt}
<!-- style hint -->

## High-Level Task Goal
{goal}

## Decision Points
{dps}

## Difficulty & Codebase Understanding
{codebase}

## Validation
One-shot: {one}
Task Success: {ts}
Steering: {st}
Claude minutes: auto

## Validation Notes
{notes}

## Notes for the Reviewer

"""


def transcript(turns: int = 1, minutes: float = 6.0, tests_ok: bool | None = True, edit_after_test: bool = False,
               end_turn: bool = True, start_ago: float = 60.0, model: str = "claude-opus-5-5", effort: str = "high",
               prompt: str = PROMPT, claim: str = "") -> list[dict]:
    """A Claude Code transcript: `turns` human prompts spread over `minutes`, an edit, a test run."""
    out, k = [], 0
    step = minutes / max(1, turns)

    def at(m):
        return zulu(start_ago - m)
    for t in range(turns):
        t0 = t * step
        text = prompt if t == 0 else f"Lorem follow-up {t}"
        out.append({"type": "user", "isSidechain": False, "timestamp": at(t0), "message": {"role": "user", "content": text}})
        out.append({"type": "api-request-blob", "message": {"output_config": {"effort": effort}}, "timestamp": at(t0)})
        k += 1
        eid = f"toolu_edit_{k}"
        out.append({"type": "assistant", "isSidechain": False, "timestamp": at(t0 + step * 0.3),
                    "message": {"model": model, "stop_reason": "tool_use", "content": [
                        {"type": "text", "text": "Reading the upload queue."},
                        {"type": "tool_use", "id": eid, "name": "Edit",
                         "input": {"file_path": f"/exp/PREWORK_x/model_a/src/backend/file{t}.ts"}}]}})
        out.append({"type": "user", "isSidechain": False, "timestamp": at(t0 + step * 0.35),
                    "message": {"content": [{"type": "tool_result", "tool_use_id": eid, "content": "ok", "is_error": False}]}})
        if tests_ok is not None:
            tid = f"toolu_test_{k}"
            out.append({"type": "assistant", "isSidechain": False, "timestamp": at(t0 + step * 0.6),
                        "message": {"model": model, "stop_reason": "tool_use", "content": [
                            {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "npm run test"}}]}})
            out.append({"type": "user", "isSidechain": False, "timestamp": at(t0 + step * 0.7),
                        "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                                 "content": "passing" if tests_ok else "Exit code 1\n2 failing",
                                                 "is_error": not tests_ok}]}})
        if edit_after_test and t == turns - 1:
            eid2 = f"toolu_edit_late_{k}"
            out.append({"type": "assistant", "isSidechain": False, "timestamp": at(t0 + step * 0.8),
                        "message": {"model": model, "stop_reason": "tool_use", "content": [
                            {"type": "tool_use", "id": eid2, "name": "Write",
                             "input": {"file_path": "/exp/PREWORK_x/model_a/src/backend/late.ts"}}]}})
            out.append({"type": "user", "isSidechain": False, "timestamp": at(t0 + step * 0.85),
                        "message": {"content": [{"type": "tool_result", "tool_use_id": eid2, "content": "ok"}]}})
        out.append({"type": "assistant", "isSidechain": False, "timestamp": at(t0 + step * 0.99),
                    "message": {"model": model, "stop_reason": "end_turn" if (end_turn or t < turns - 1) else "tool_use",
                                "content": [{"type": "text", "text": claim or f"Lorem summary of turn {t + 1}."}]}})
    return out


DIFF = """diff --git a/src/backend/file0.ts b/src/backend/file0.ts
index 1..2 100644
--- a/src/backend/file0.ts
+++ b/src/backend/file0.ts
@@ -1,3 +1,4 @@
-old
+new
+more
diff --git a/src/backend/tests/file0.test.ts b/src/backend/tests/file0.test.ts
new file mode 100644
--- /dev/null
+++ b/src/backend/tests/file0.test.ts
@@ -0,0 +1,2 @@
+a
+b
"""


def _write_jsonl(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def make_task(root: Path, short: str, *, sub: str, runs: list[dict] | None = None, extra: dict | None = None,
              prework_home: Path | None = None, slot: dict | None = None, lead_id: str | None = None) -> Path:
    """runs: [{kind, n, collected: bool, tx: [entries] | None, live_tx: [entries] | None, container: str|None,
    created_ago: minutes, collected_ago: minutes}]"""
    d = root / "tasks" / short
    (d / "runs").mkdir(parents=True, exist_ok=True)
    meta = {"task_id": f"task_fake_{short}", "task_name": f"Task {short}", "slot": dict(slot or SLOT), "repo": dict(REPO),
            "lead_id": lead_id, "created_at": iso(240), "runs": [], "gists": {}}
    for r in runs or []:
        name = f"{r['kind']}-{r.get('n', 1)}"
        rd = d / "runs" / name
        (rd / "model_a").mkdir(parents=True, exist_ok=True)
        container = r.get("container") or f"prework-PREWORK_{short}-{name.replace('-', '')}fake-model_a"
        (rd / "prework-manifest.json").write_text(json.dumps(
            {"lane": {"container": container, "workspace": f"/exp/PREWORK_{short}/model_a"}}))
        rec = {"kind": r["kind"], "dir": f"runs/{name}", "created_at": iso(r.get("created_ago", 120)), "container": container}
        if r.get("tx") is not None and r.get("collected", True):
            tp = rd / "prework_output" / "aaaa-bbbb.jsonl"
            _write_jsonl(tp, r["tx"])
            dp = rd / "prework_output" / "final.diff"
            dp.write_text(r.get("diff", DIFF))
            rec.update(collected_at=iso(r.get("collected_ago", 30)), transcript=str(tp), diff=str(dp))
            from johto import transcript as tx
            sm = tx.summarize(tp)
            rec.update(active_minutes=sm["active_minutes"], human_turns=sm["human_turns"])
        if r.get("live_tx") is not None and prework_home is not None:
            key = container[len("prework-"):].replace("-model_a", "_model_a")
            _write_jsonl(prework_home / "cc-config" / key / "projects" / "-exp-x" / "live.jsonl", r["live_tx"])
            if r.get("logged_in", True):
                (prework_home / "cc-config" / key / ".credentials.json").write_text("{}")
        rec.update(r.get("rec") or {})
        meta["runs"].append(rec)
    meta.update(extra or {})
    (d / "task.json").write_text(json.dumps(meta, indent=1))
    (d / "submission.md").write_text(sub)
    (d / "brief.md").write_text("# brief\n")
    return d


def build(root: Path, prework_home: Path | None = None) -> dict:
    """One task per stage. Returns {short: expected stage}."""
    root = Path(root)
    exp = {}
    full = dict(prompt=PROMPT, goal=GOAL, dps=DPS, codebase=CODEBASE)
    rated = dict(full, notes=NOTES, one="No", ts="3", st="3")
    calib_drop = transcript(turns=1, minutes=6.0, tests_ok=True)
    calib_keep = transcript(turns=4, minutes=34.0, tests_ok=False)
    val = transcript(turns=5, minutes=47.0, tests_ok=True, start_ago=200)

    make_task(root, "aprompt1", sub=submission())
    exp["aprompt1"] = "prompt"
    make_task(root, "bdecide1", sub=submission(prompt=PROMPT),
              runs=[{"kind": "calib", "tx": calib_drop, "collected_ago": 3}])
    exp["bdecide1"] = "decide"
    make_task(root, "cdetail1", sub=submission(prompt=PROMPT, goal=GOAL),
              runs=[{"kind": "calib", "tx": calib_keep, "collected_ago": 50}],
              extra={"decisions": {"calib-1": {"decision": "keep", "at": iso(45)}}})
    exp["cdetail1"] = "details"
    make_task(root, "drate001", sub=submission(**full),
              runs=[{"kind": "calib", "tx": calib_keep, "created_ago": 300, "collected_ago": 250},
                    {"kind": "validate", "tx": val, "created_ago": 100, "collected_ago": 5}],
              extra={"decisions": {"calib-1": {"decision": "keep", "at": iso(240)}}})
    exp["drate001"] = "rate"
    make_task(root, "echosen1", sub=submission(**full),
              runs=[{"kind": "calib", "tx": calib_keep, "collected_ago": 20}],
              extra={"submission_run": "calib-1", "details_ready": {"at": iso(10), "sha": {}}})
    exp["echosen1"] = "rate"
    make_task(root, "fsave001", sub=submission(**rated),
              runs=[{"kind": "validate", "tx": val, "collected_ago": 40}], extra={"rated": {"at": iso(30)}})
    exp["fsave001"] = "ready_to_save"
    # Pushed with the current hashes, then AutoQC: one failure (fix) or all pass (submit).
    for short, fails, stage in (("gfixqc01", 1, "fix_qc"), ("hsubmit1", 0, "ready_to_submit")):
        d = make_task(root, short, sub=submission(**rated), runs=[{"kind": "validate", "tx": val, "collected_ago": 90}])
        pushed = pushed_now(d)
        dims = [{"section": "Task & Rationale", "dimension": "High-Level Task Goal", "status": "fail" if fails else "pass",
                 "justification": "Lorem justification.", "field_key": "goal"}]
        qc = [{"audit_id": f"qca_{short}", "spec": "AutoQC", "started_at": iso(20), "finished_at": iso(18), "status": "completed",
               "counts": {"pass": 11, "fail": fails, "neutral": 0}, "dims": dims}]
        _update(d, pushed=pushed, qc=qc)
        exp[short] = stage
    make_task(root, "isentbk1", sub=submission(**rated), runs=[{"kind": "validate", "tx": val, "collected_ago": 900}],
              extra={"submitted_at": iso(800), "studio": {"status": "Writing", "feedback": "Lorem reviewer feedback.",
                                                            "polled_at": iso(60), "times_submitted": 1}})
    exp["isentbk1"] = "sent_back"
    make_task(root, "jdrop001", sub=submission(prompt=PROMPT), runs=[{"kind": "calib", "tx": calib_drop, "collected_ago": 30}],
              extra={"discarded": True, "discard": {"at": iso(12), "url": "https://docs.google.com/forms/x", "reason": "easy",
                                                    "logged_at": None, "run": "calib-1"},
                     "decisions": {"calib-1": {"decision": "drop", "at": iso(12)}}})
    exp["jdrop001"] = "dropped"
    make_task(root, "kreview1", sub=submission(**rated), runs=[{"kind": "validate", "tx": val, "collected_ago": 600}],
              extra={"submitted_at": iso(500), "studio": {"status": "Awaiting Review", "polled_at": iso(5), "times_submitted": 1}})
    exp["kreview1"] = "in_review"
    if prework_home is not None:
        live = transcript(turns=2, minutes=9.0, tests_ok=True, start_ago=10, end_turn=True)
        make_task(root, "llive001", sub=submission(**full), prework_home=prework_home,
                  runs=[{"kind": "calib", "tx": calib_keep, "created_ago": 300, "collected_ago": 250},
                        {"kind": "validate", "tx": None, "live_tx": live, "created_ago": 12,
                         "container": "prework-PREWORK_llive001-livefake-model_a", "collected": False}],
                  extra={"decisions": {"calib-1": {"decision": "keep", "at": iso(240)}}})
        exp["llive001"] = "validating"
    return exp


def _update(d: Path, **kw) -> None:
    meta = json.loads((d / "task.json").read_text())
    meta.update(kw)
    (d / "task.json").write_text(json.dumps(meta, indent=1))


def pushed_now(d: Path) -> dict:
    """A "pushed" record whose hashes match the folder as it is now (what actions.push would record)."""
    from johto import workspace
    from johto.web import data
    meta = json.loads((d / "task.json").read_text())
    sub = workspace.parse_submission(d)
    rec = meta["runs"][-1]
    f = workspace.to_fields(meta, sub, rec.get("active_minutes"))
    return {"at": iso(25), "fields": {k: data.sha12(v) for k, v in f.items()}}


class FixturesBuild(unittest.TestCase):
    def test_build_writes_every_folder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            exp = build(Path(tmp), Path(tmp) / "prework")
            for short in exp:
                self.assertTrue((Path(tmp) / "tasks" / short / "task.json").exists(), short)


if __name__ == "__main__":
    unittest.main()
