import json
import os
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from johto.dossier import generate, prompt, schema, store
from test_dossier_fixtures import DID, REPO, SHA, make_root, valid_doc

SPEC = Path(__file__).resolve().parent.parent / "docs" / "dossier-spec.md"
ENTRY = {"full_name": REPO, "commit": SHA, "pinned_url": f"https://github.com/{REPO}/tree/{SHA}",
         "language": "Typescript", "promise": 0.9, "reasons": ["TypeScript"],
         "fits": [{"action": "Modify", "domain": "Backend / API", "codebase_type": "Enterprise-grade", "open": 45}]}

# A stand-in for ~/.local/bin/claude that speaks stream-json. Its behaviour per pass comes from
# FAKE_PLAN (a JSON list, one entry per invocation): "invalid", "valid", "nomarkers", "ratelimit", "sleep",
# "sleep429" (says "HTTP 429" in an assistant message, then hangs).
FAKE = textwrap.dedent(r'''
    #!{python}
    import json, os, sys, time
    from pathlib import Path
    args = sys.argv[1:]
    state = Path(os.environ["FAKE_STATE"])
    n = int(state.read_text()) if state.exists() else 0
    state.write_text(str(n + 1))
    Path(os.environ["FAKE_ARGS"] + f".{{n}}").write_text(json.dumps(args))
    plan = json.loads(os.environ["FAKE_PLAN"])[n]
    sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
    proj = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "-x-data-cache-dossier-clones-acme--widget"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{{sid}}.jsonl").write_text("{{}}\n")
    todo = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "todos"
    todo.mkdir(parents=True, exist_ok=True)
    (todo / f"{{sid}}-agent-{{sid}}.json").write_text("[]")
    def emit(ev): print(json.dumps(ev), flush=True)
    emit({{"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": sid}})
    emit({{"type": "assistant", "message": {{"content": [
        {{"type": "tool_use", "name": "Read", "input": {{"file_path": os.getcwd() + "/src/server.ts"}}}},
        {{"type": "tool_use", "name": "Bash", "input": {{"command": "gh issue list --limit 50"}}}}]}}}})
    doc = json.loads(os.environ["FAKE_DOC"])
    if plan == "sleep429":
        emit({{"type": "assistant", "message": {{"content": [
            {{"type": "text", "text": "The client backs off on HTTP 429 and rate limit headers."}}]}}}})
    if plan in ("sleep", "sleep429"):
        time.sleep(60)
    if plan == "invalid":
        doc["headline"] = " ".join(["word"] * 20)
    text = "Done.\n<<<DOSSIER\n" + json.dumps(doc) + "\nDOSSIER>>>\n"
    if plan == "nomarkers":
        text = "Here is my summary without the markers."
    if plan == "ratelimit":
        emit({{"type": "result", "subtype": "success", "is_error": True, "result": "You've hit your usage limit",
               "total_cost_usd": 0, "session_id": sid}})
        sys.exit(1)
    emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": text}}]}}}})
    sys.stderr.write("some stderr line\n")
    emit({{"type": "result", "subtype": "success", "is_error": False, "result": text,
           "total_cost_usd": 1.25, "session_id": sid}})
''').lstrip().format(python=sys.executable)


class PromptTest(unittest.TestCase):
    """The prompt is the spec's text plus prompt.py's listed revisions, nothing else."""

    def test_prompt_is_verbatim_from_spec(self):
        spec = SPEC.read_text()
        block = spec.split("### The prompt (embed verbatim, then append the schema from section 3 and the repo facts)\n",
                           1)[1].split("\n## 5.", 1)[0].strip("\n")
        quoted = "\n".join(l[2:] if l.startswith("> ") else l[1:] for l in block.splitlines())
        self.assertEqual(prompt.revise(quoted, prompt.PROMPT_REVISIONS), prompt.PROMPT)

    def test_schema_is_section_3_verbatim(self):
        spec = SPEC.read_text()
        s3 = spec.split("## 3. The dossier document (`johto.dossier/1`)\n", 1)[1].split("\n## 4.", 1)[0].strip("\n")
        self.assertEqual(prompt.revise(s3, prompt.SCHEMA_REVISIONS), prompt.SCHEMA)

    def test_revisions_apply(self):
        for old, new in prompt.PROMPT_REVISIONS:
            self.assertIn(new, prompt.PROMPT)
        for old, new in prompt.SCHEMA_REVISIONS:
            self.assertIn(new, prompt.SCHEMA)
        self.assertNotIn("wrong-but-plausible fix", prompt.SCHEMA)
        self.assertEqual(prompt.revise(prompt.PROMPT, prompt.PROMPT_REVISIONS), prompt.PROMPT)  # idempotent
        with self.assertRaises(ValueError):
            prompt.revise("unrelated text", [("missing", "replacement")])

    def test_output_rules_name_the_text_checks_and_slot_limits(self):
        rules = generate.output_rules(ENTRY, DID, {"counts": {"source_files": 120},
                                                    "verdicts": {"Personal-scale": {"overall": "ok"},
                                                                 "Enterprise-grade": {"overall": "fail"}}})
        self.assertIn("Understand / Assess is not available: the repository has 120 source files", rules)
        self.assertIn("codebase_type is only Personal-scale", rules)
        self.assertIn("ending in '?'", rules)
        self.assertNotIn("not available", generate.output_rules(ENTRY, DID, {"counts": {"source_files": 450}}))

    def test_build_prompt_parts(self):
        p = generate.build_prompt(ENTRY, {"counts": {"source_files": 440, "test_files": 58}, "docker": ["Dockerfile"]},
                                  DID)
        self.assertTrue(p.startswith(prompt.PROMPT))
        self.assertIn(prompt.SCHEMA, p)
        self.assertIn(f"Dossier id: {DID}", p)
        self.assertIn("440 source files, 58 test files", p)
        self.assertIn("Modify | Backend / API | Enterprise-grade: 45", p)
        self.assertIn("`<<<DOSSIER`", p)
        self.assertIn("`DOSSIER>>>`", p)

    def test_argv_is_read_only(self):
        a = generate.argv("hi", ["--session-id", "x"])
        self.assertEqual(a[0], str(Path.home() / ".local/bin/claude"))
        self.assertIn("-p", a)
        for flag, val in (("--model", "opus"), ("--effort", "high"), ("--tools", "Read,Grep,Glob,Bash"),
                          ("--permission-mode", "dontAsk"), ("--setting-sources", "project"),
                          ("--output-format", "stream-json")):
            self.assertEqual(a[a.index(flag) + 1], val)
        self.assertIn("--strict-mcp-config", a)
        allowed = a[a.index("--allowedTools") + 1].split(",")
        self.assertNotIn("Edit", allowed)
        self.assertNotIn("Write", allowed)
        self.assertTrue(all(x in ("Read", "Grep", "Glob") or x.startswith("Bash(") for x in allowed))
        denied = a[a.index("--disallowedTools") + 1].split(",")
        for t in ("Edit", "Write", "WebFetch", "Bash(find * -delete*)", "Bash(rg * --pre*)"):
            self.assertIn(t, denied)

    def test_env_drops_parent_session(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_EFFORT": "xhigh",
                                          "CLAUDE_CONFIG_DIR": "/c", "PATH": "/bin"}):
            env = generate._env()
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertNotIn("CLAUDE_EFFORT", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/c")
        self.assertEqual(env["PATH"], "/bin")

    def test_env_drops_api_billing_and_model_overrides(self):
        extra = {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "u",
                 "ANTHROPIC_MODEL": "m", "ANTHROPIC_DEFAULT_OPUS_MODEL": "o", "PATH": "/bin"}
        with mock.patch.dict(os.environ, extra):
            env = generate._env()
        self.assertFalse([k for k in env if k.startswith("ANTHROPIC_")])
        with mock.patch.dict(os.environ, {**extra, generate.KEEP_ENV_VAR: "ANTHROPIC_BASE_URL"}):
            env = generate._env()
        self.assertEqual([k for k in env if k.startswith("ANTHROPIC_")], ["ANTHROPIC_BASE_URL"])


class PassTest(unittest.TestCase):
    def test_timeout_is_never_a_usage_limit(self):
        self.assertFalse(generate._Pass(result=None, text="backs off on HTTP 429", timed_out=True).rate_limited)
        self.assertFalse(generate._Pass(result={"subtype": "success", "is_error": True, "result": "usage limit"},
                                        timed_out=True).rate_limited)

    def test_only_error_results_count(self):
        P = generate._Pass
        self.assertFalse(P(result={"subtype": "error_during_execution", "is_error": True},
                           text="rate limit 429").rate_limited)  # last_text is the model's, not an error
        self.assertFalse(P(result={"subtype": "success", "is_error": False, "result": "handles 429"}).rate_limited)
        self.assertFalse(P(result=None, text="usage limit").rate_limited)
        self.assertTrue(P(result={"subtype": "success", "is_error": True,
                                  "result": "You've hit your usage limit"}).rate_limited)
        self.assertTrue(P(result={"subtype": "success", "is_error": True, "api_error_status": 429}).rate_limited)
        self.assertTrue(P(result={"is_error": True, "result": "x"}, api_error="rate_limit").rate_limited)
        self.assertTrue(P(result=None, api_error="rate_limit").rate_limited)


class StaleSessionTest(unittest.TestCase):
    def test_removes_only_this_stores_clone_projects(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            clones = t / "data" / "cache" / "dossier-clones"
            proj = t / "cfg" / "projects"
            mine = proj / (generate._project_dir_name(clones) + "-acme--widget--9f1e2d3c")
            other = proj / "-Users-x-elsewhere-data-cache-dossier-clones-acme--widget--9f1e2d3c"
            keep = proj / "-Users-austin-dev-mercor"
            for d in (mine, other, keep):
                d.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(t / "cfg")}):
                gone = generate.clean_stale_sessions(clones)
            self.assertEqual(gone, [mine])
            self.assertTrue(other.exists() and keep.exists())


class ExtractTest(unittest.TestCase):
    def test_markers(self):
        doc, e = generate.extract('notes\n<<<DOSSIER\n{"a": 1}\nDOSSIER>>>\n')
        self.assertEqual((doc, e), ({"a": 1}, None))

    def test_last_block_wins_and_fences(self):
        t = '<<<DOSSIER\n{"a": 1}\nDOSSIER>>>\nagain\n<<<DOSSIER\n```json\n{"a": 2}\n```\nDOSSIER>>>'
        self.assertEqual(generate.extract(t)[0], {"a": 2})

    def test_missing_end_marker(self):
        self.assertEqual(generate.extract('<<<DOSSIER\n{"a": 3}\n')[0], {"a": 3})

    def test_errors(self):
        self.assertIn("no `<<<DOSSIER` line", generate.extract("just text")[1])
        self.assertIn("does not parse", generate.extract("<<<DOSSIER\n{nope\nDOSSIER>>>")[1])
        self.assertIn("must be a JSON object", generate.extract("<<<DOSSIER\n[1]\nDOSSIER>>>")[1])
        self.assertIn("empty", generate.extract("")[1])

    def test_finalize_stamps_known_fields(self):
        d = generate.finalize({"id": "wrong", "headline": "h", "blocks": []}, ENTRY, DID, "claude-opus-5-5", 12, 1.23456)
        self.assertEqual(list(d)[:10], ["schema", "id", "repo", "commit", "pinned_url", "generated_at", "model",
                                        "effort", "elapsed_s", "cost_usd"])
        self.assertEqual((d["id"], d["repo"], d["effort"], d["cost_usd"]), (DID, REPO, "high", 1.2346))

    def test_describe(self):
        root = Path("/r")
        ev = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/r/src/a.ts", "offset": 40}},
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "lease", "path": "/r/src"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "git log --oneline -5"}},
            {"type": "text", "text": "<<<DOSSIER\n{}"}]}}
        self.assertEqual(generate.describe(ev, root), [
            ("reading the code", "Read src/a.ts:40"), ("searching the code", "Grep lease in src"),
            ("reading history", "$ git log --oneline -5"), ("writing the dossier", "dossier written")])


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        store.use_data_dir(self.tmp / "data")
        self.cfg = self.tmp / "claude-config"
        fake = self.tmp / "claude"
        fake.write_text(FAKE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.patches = [mock.patch.object(generate, "CLAUDE_BIN", fake),
                        mock.patch.object(generate, "clone_repo", self.fake_clone),
                        mock.patch.object(generate.candidates, "record", lambda name: {"counts": {}})]
        for p in self.patches:
            p.start()
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg),
                                                "FAKE_STATE": str(self.tmp / "state"),
                                                "FAKE_ARGS": str(self.tmp / "args"),
                                                "FAKE_DOC": json.dumps(valid_doc())})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for p in self.patches:
            p.stop()
        store.use_data_dir(generate.store.config.DATA)
        self._tmp.cleanup()

    def fake_clone(self, name, commit, dest, deadline=None, cancel=None):
        self.clone_deadline = deadline
        dest.mkdir(parents=True)
        make_root(dest)
        return dest

    def run_plan(self, plan, **kw):
        os.environ["FAKE_PLAN"] = json.dumps(plan)
        snaps = []
        res = generate.generate(ENTRY, DID, on_progress=snaps.append, **kw)
        return res, snaps

    def args(self, n):
        return json.loads((self.tmp / f"args.{n}").read_text())

    def assertCleaned(self):
        self.assertFalse((store.P.clones / DID).exists(), "clone left behind")
        self.assertEqual(list((self.cfg / "projects").glob("*/*.jsonl")), [], "session transcript left behind")
        self.assertEqual(list((self.cfg / "todos").glob("*")), [], "session todos left behind")

    def test_valid_first_pass(self):
        res, snaps = self.run_plan(["valid"])
        self.assertTrue(res.ok, res.error)
        self.assertEqual((res.passes, res.cost_usd, res.model), (1, 1.25, "claude-opus-5-5"))
        self.assertEqual(schema.validate(res.doc), [])
        self.assertEqual(res.doc["cost_usd"], 1.25)
        self.assertEqual(res.doc["elapsed_s"], res.elapsed_s)
        self.assertIn("--session-id", self.args(0))
        steps = [s["step"] for s in snaps]
        self.assertIn("cloning", steps)
        self.assertEqual(steps[-1], "done")
        log = store.run_log(DID).read_text()
        self.assertIn("Read src/server.ts", log)
        self.assertIn("stderr: some stderr line", log)
        self.assertCleaned()

    def test_repair_pass_uses_same_session(self):
        res, _ = self.run_plan(["invalid", "valid"])
        self.assertTrue(res.ok, res.error)
        self.assertEqual((res.passes, res.cost_usd), (2, 2.5))
        a0, a1 = self.args(0), self.args(1)
        sid = a0[a0.index("--session-id") + 1]
        self.assertEqual(a1[a1.index("--resume") + 1], sid)
        repair = a1[a1.index("-p") + 1]
        self.assertIn("headline: 20 words, limit 16", repair)
        self.assertCleaned()

    def test_missing_markers_get_a_repair_pass(self):
        res, _ = self.run_plan(["nomarkers", "valid"])
        self.assertTrue(res.ok, res.error)
        self.assertIn("no `<<<DOSSIER` line", self.args(1)[self.args(1).index("-p") + 1])

    def test_second_failure_fails_with_errors(self):
        res, snaps = self.run_plan(["invalid", "invalid"])
        self.assertFalse(res.ok)
        self.assertIn("after the repair pass", res.error)
        self.assertTrue(any("headline: 20 words" in e for e in res.errors))
        self.assertEqual(snaps[-1]["step"], "failed")
        self.assertCleaned()

    def test_rate_limit_is_retryable(self):
        res, _ = self.run_plan(["ratelimit"])
        self.assertFalse(res.ok)
        self.assertTrue(res.retryable)
        self.assertCleaned()

    def test_hard_timeout(self):
        res, _ = self.run_plan(["sleep"], timeout=2)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "timed out")
        self.assertLess(res.elapsed_s, 30)
        self.assertCleaned()

    def test_timeout_mentioning_429_is_not_retryable(self):
        res, _ = self.run_plan(["sleep429"], timeout=2)
        self.assertEqual((res.ok, res.retryable, res.error), (False, False, "timed out"))
        self.assertCleaned()

    def test_one_deadline_for_the_whole_run(self):
        t0 = time.time()
        res, _ = self.run_plan(["invalid"], timeout=30)  # under MIN_REPAIR_S left after the first pass
        self.assertFalse(res.ok)
        self.assertIn("no time left for the repair pass", res.error)
        self.assertEqual(res.passes, 1)
        self.assertTrue(res.errors)
        self.assertAlmostEqual(self.clone_deadline, t0 + 30, delta=5)
        self.assertCleaned()

    def test_cancel_stops_claude_and_cleans(self):
        os.environ["FAKE_PLAN"] = json.dumps(["sleep"])
        cancel = generate.Cancel()
        out = {}
        th = threading.Thread(target=lambda: out.update(res=generate.generate(ENTRY, DID, cancel=cancel)))
        th.start()
        deadline = time.time() + 20
        while not (self.tmp / "args.0").exists() and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        cancel.cancel("stopped by pause")
        th.join(30)
        res = out["res"]
        self.assertEqual((res.ok, res.cancelled, res.retryable, res.error), (False, True, False, "stopped by pause"))
        self.assertLess(res.elapsed_s, 25)
        self.assertCleaned()

    def test_cancel_before_start_runs_nothing(self):
        cancel = generate.Cancel()
        cancel.cancel("stopped when the server shut down")
        res = generate.generate(ENTRY, DID, cancel=cancel)
        self.assertTrue(res.cancelled)
        self.assertFalse((self.tmp / "args.0").exists())

    def test_clone_failure_still_cleans(self):
        def boom(name, commit, dest, **kw):
            dest.mkdir(parents=True)
            raise RuntimeError("git clone failed: nope")
        with mock.patch.object(generate, "clone_repo", boom):
            res, _ = self.run_plan(["valid"])
        self.assertFalse(res.ok)
        self.assertIn("git clone failed", res.error)
        self.assertFalse((store.P.clones / DID).exists())


class CloneTest(unittest.TestCase):
    """clone_repo against a local repository (file:// URL through a patched _git)."""

    def test_clone_checks_out_commit_and_drops_repo_settings(self):
        import subprocess
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            src = t / "src"
            src.mkdir()
            g = lambda *a: subprocess.run(["git", "-C", str(src), *a], check=True, capture_output=True, text=True)
            g("init", "-q")
            (src / ".claude").mkdir()
            (src / ".claude" / "settings.json").write_text('{"hooks": {}}')
            (src / "a.txt").write_text("one\n")
            g("add", "-A")
            g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one")
            first = g("rev-parse", "HEAD").stdout.strip()
            (src / "a.txt").write_text("two\n")
            g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "two")
            real = generate._git

            def local(*args, cwd=None, timeout=900, cancel=None):
                args = [str(src) if a == "https://github.com/local/src.git" else a for a in args]
                if args and args[0] == "clone":
                    args = [a for a in args if a != "--filter=blob:none"]
                return real(*args, cwd=cwd, timeout=timeout, cancel=cancel)
            with mock.patch.object(generate, "_git", local):
                dest = generate.clone_repo("local/src", first, t / "clones" / "x")
            self.assertEqual((dest / "a.txt").read_text(), "one\n")
            self.assertFalse((dest / ".claude" / "settings.json").exists())
            with mock.patch.object(generate, "_git", local):
                with self.assertRaises(generate.HardTimeout):
                    generate.clone_repo("local/src", first, t / "clones" / "y", deadline=time.time() - 1)

    def test_git_timeout_kills(self):
        with self.assertRaises(generate.HardTimeout):
            generate._git("-c", "alias.hang=!sleep 30", "hang", timeout=1)


if __name__ == "__main__":
    unittest.main()


class FocusTest(unittest.TestCase):
    """Research started for particular open slots asks for opportunities that fit them."""

    def test_no_focus_no_section(self):
        self.assertIsNone(generate.focus_section({"focus": []}))
        self.assertNotIn("Open slots this research is for", generate.build_prompt(
            {"full_name": "o/r", "commit": "a" * 40}, {}, "o__r__aaaaaaaa"))

    def test_focus_lists_each_slot_with_how_it_is_written(self):
        entry = {"full_name": "o/r", "commit": "a" * 40,
                 "focus": ["Understand / Assess|Other software|Enterprise-grade|Casual prose|Async/autonomous",
                           "Operate|Games|Personal-scale", "bad"]}
        s = generate.build_prompt(entry, {}, "o__r__aaaaaaaa")
        self.assertIn("## Open slots this research is for", s)
        self.assertIn("- Understand / Assess | Other software | Enterprise-grade (written as a short casual message", s)
        self.assertIn("works alone on one prompt for about 45 minutes", s)
        self.assertIn("- Operate | Games | Personal-scale\n", s + "\n")
        self.assertNotIn("bad", generate.focus_section(entry))
        # The section sits before the output rules, so the output rules stay last.
        self.assertLess(s.index("Open slots this research is for"), s.index("## Output"))
