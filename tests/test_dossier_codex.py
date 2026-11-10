"""The Codex CLI dossier path (johto/dossier/codex.py): the command line, the prompt, the event reader, and one
generation driven through a stand-in `codex` binary that speaks `codex exec --json`."""
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from johto import settings
from johto.dossier import codex, generate, prompt, store, worker
from test_dossier_fixtures import DID, REPO, SHA, make_root, valid_doc

ENTRY = {"full_name": REPO, "commit": SHA, "pinned_url": f"https://github.com/{REPO}/tree/{SHA}",
         "language": "Typescript", "promise": 0.9, "reasons": ["TypeScript"],
         "fits": [{"action": "Modify", "domain": "Backend / API", "codebase_type": "Enterprise-grade", "open": 45}]}

# A stand-in for the Codex CLI. FAKE_PLAN is a JSON list, one entry per run: "valid", "invalid", "nomarkers",
# "ratelimit" (exit 1, a usage-limit line on stderr), "sleep" (hangs), "oldformat" (valid, in the msg-event style).
FAKE = textwrap.dedent(r'''
    #!{python}
    import json, os, sys, time
    from pathlib import Path
    args = sys.argv[1:]
    state = Path(os.environ["FAKE_STATE"])
    n = int(state.read_text()) if state.exists() else 0
    state.write_text(str(n + 1))
    Path(os.environ["FAKE_ARGS"] + f".{{n}}").write_text(json.dumps(args))
    prompt = sys.stdin.read()
    Path(os.environ["FAKE_ARGS"] + f".{{n}}.prompt").write_text(prompt)
    plan = json.loads(os.environ["FAKE_PLAN"])[n]
    out = Path(args[args.index("--output-last-message") + 1])
    def emit(ev): print(json.dumps(ev), flush=True)
    if plan == "ratelimit":
        sys.stderr.write("ERROR: You've hit your usage limit. Try again later.\n")
        sys.exit(1)
    if plan == "sleep":
        time.sleep(60)
    doc = json.loads(os.environ["FAKE_DOC"])
    if plan == "invalid":
        doc["headline"] = " ".join(["word"] * 20)
    text = "Done.\n<<<DOSSIER\n" + json.dumps(doc) + "\nDOSSIER>>>\n"
    if plan == "nomarkers":
        text = "Here is my summary without the markers."
    if plan == "oldformat":
        emit({{"id": "0", "msg": {{"type": "session_configured", "model": "gpt-5.4"}}}})
        emit({{"id": "1", "msg": {{"type": "exec_command_begin", "command": ["bash", "-lc", "rg -n queue src"]}}}})
        emit({{"id": "2", "msg": {{"type": "agent_message", "message": text}}}})
        emit({{"id": "3", "msg": {{"type": "task_complete", "last_agent_message": text}}}})
    else:
        emit({{"type": "thread.started", "thread_id": "0199-abc"}})
        emit({{"type": "item.started", "item": {{"id": "i1", "type": "command_execution", "command": "git log --oneline -n 20"}}}})
        emit({{"type": "item.completed", "item": {{"id": "i1", "type": "command_execution", "command": "git log --oneline -n 20", "exit_code": 0}}}})
        emit({{"type": "item.completed", "item": {{"id": "i2", "type": "agent_message", "text": text}}}})
        emit({{"type": "turn.completed", "usage": {{"input_tokens": 1000, "output_tokens": 500}}}})
    out.write_text(text)
    sys.stderr.write("some stderr line\n")
''').lstrip().format(python=sys.executable)


class PromptTest(unittest.TestCase):
    def test_argv_runs_read_only_in_the_clone_and_reads_the_prompt_from_stdin(self):
        with mock.patch.dict(settings.DEFAULTS, {"codex_model": "", "codex_effort": ""}), \
                mock.patch.object(settings, "load", lambda: dict(settings.DEFAULTS)):
            a = codex.argv(Path("/c"), Path("/o/last.md"))
            self.assertEqual(a[1:], ["exec", "--sandbox", "read-only", "-C", "/c", "--json", "--output-last-message",
                                     "/o/last.md", "-"])
            a = codex.argv(Path("/c"), Path("/o/last.md"), "gpt-5.4", "high")
            self.assertEqual(a[a.index("-m") + 1], "gpt-5.4")
            self.assertIn('model_reasoning_effort="high"', a)
            self.assertNotIn("--full-auto", a)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", a)

    def test_prompt_is_claudes_with_issues_inline_and_no_network(self):
        p = codex.build_prompt(ENTRY, {"counts": {"source_files": 120}}, DID, "#12 Jobs stuck\n#15 Retry loop")
        self.assertTrue(p.startswith(prompt.PROMPT))
        self.assertIn(prompt.SCHEMA, p)
        self.assertIn("## Open issues upstream\n\n```\n#12 Jobs stuck", p)
        self.assertIn("There is no network", p)
        self.assertNotIn("`gh` resolves", p)
        self.assertIn("The sandbox has no network", p)
        self.assertNotIn("gh issue list/view", p)
        self.assertIn("Understand / Assess is not available", p)
        self.assertIn("about 45 minutes", p)   # the agent under test and its budget travel with every prompt

    def test_repair_run_carries_the_task_the_previous_dossier_and_the_errors(self):
        p = codex.repair_run_prompt("TASK", {"headline": "x"}, ["headline: too long"])
        self.assertTrue(p.startswith("TASK\n\n## Your previous attempt\n\n<<<DOSSIER\n{\"headline\": \"x\"}\nDOSSIER>>>"))
        self.assertIn("failed validation with 1 error", p)
        self.assertIn("- headline: too long", p)

    def test_event_reader_understands_both_formats(self):
        step, text, agent, model, err = codex._event({"type": "thread.started", "thread_id": "abcdefgh12", "model": "gpt-5.4"})
        self.assertEqual((step, model), ("reading the code", "gpt-5.4"))
        step, text, *_ = codex._event({"type": "item.started", "item": {"type": "command_execution", "command": "rg -n foo"}})
        self.assertEqual((step, text), ("searching the code", "$ rg -n foo"))
        self.assertEqual(codex._event({"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}}),
                         (None, None, None, None, None))
        step, text, agent, *_ = codex._event({"type": "item.completed", "item": {"type": "agent_message", "text": "<<<DOSSIER\n{}"}})
        self.assertEqual((step, agent), ("writing the dossier", "<<<DOSSIER\n{}"))
        *_, err = codex._event({"type": "turn.failed", "error": {"message": "rate limit reached"}})
        self.assertEqual(err, "rate limit reached")
        step, text, *_ = codex._event({"msg": {"type": "exec_command_begin", "command": ["bash", "-lc", "git log"]}})
        self.assertEqual(text, "$ bash -lc git log")
        *_, agent, model, err = codex._event({"msg": {"type": "task_complete", "last_agent_message": "bye"}})
        self.assertEqual((agent, err), ("bye", None))

    def test_rate_limit_only_from_errors_never_from_a_timeout(self):
        self.assertTrue(codex._Run(error="You've hit your usage limit").rate_limited)
        self.assertTrue(codex._Run(exit=1, stderr_tail="rate limit exceeded").rate_limited)
        self.assertFalse(codex._Run(exit=0, stderr_tail="handles 429 fine").rate_limited)
        self.assertFalse(codex._Run(timed_out=True, error="rate limit").rate_limited)


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        store.use_data_dir(self.tmp / "data")
        fake = self.tmp / "codex"
        fake.write_text(FAKE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.patches = [mock.patch.object(codex.config, "CODEX_BIN", str(fake)),
                        mock.patch.object(codex, "clone_repo", self.fake_clone),
                        mock.patch.object(codex, "issue_list", lambda name: "#12 Jobs stuck"),
                        mock.patch.object(codex.candidates, "record", lambda name: {"counts": {}}),
                        mock.patch.dict(settings.DEFAULTS, {"codex_model": "", "codex_effort": ""}),
                        mock.patch.object(settings, "path", lambda: self.tmp / "settings.json")]
        for p in self.patches:
            p.start()
        (self.tmp / "codex-home").mkdir()
        (self.tmp / "codex-home" / "config.toml").write_text('model = "gpt-6-sol"\nmodel_reasoning_effort = "medium"\n'
                                                            '[projects."/x"]\ntrust_level = "trusted"\n')
        self.env = mock.patch.dict(os.environ, {"FAKE_STATE": str(self.tmp / "state"), "FAKE_ARGS": str(self.tmp / "args"),
                                                "FAKE_DOC": json.dumps(valid_doc()), "CODEX_HOME": str(self.tmp / "codex-home")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for p in self.patches:
            p.stop()
        store.use_data_dir(generate.store.config.DATA)
        self._tmp.cleanup()

    def fake_clone(self, name, commit, dest, deadline=None, cancel=None):
        dest.mkdir(parents=True)
        make_root(dest)
        return dest

    def run_plan(self, plan, **kw):
        os.environ["FAKE_PLAN"] = json.dumps(plan)
        snaps = []
        res = codex.generate(ENTRY, DID, on_progress=snaps.append, **kw)
        return res, snaps

    def args(self, n):
        return json.loads((self.tmp / f"args.{n}").read_text())

    def sent(self, n):
        return (self.tmp / f"args.{n}.prompt").read_text()

    def test_valid_first_run(self):
        res, snaps = self.run_plan(["valid"])
        self.assertTrue(res.ok, res.error)
        self.assertEqual((res.passes, res.model, res.cost_usd), (1, "gpt-6-sol", 0.0))   # Codex's own config
        self.assertEqual((res.doc["model"], res.doc["effort"], res.doc["cost_usd"]), ("gpt-6-sol", "medium", 0.0))
        a = self.args(0)
        self.assertEqual(a[:4], ["exec", "--sandbox", "read-only", "-C"])
        self.assertEqual(a[4], str(store.P.clones / DID))
        self.assertEqual(a[-1], "-")
        self.assertNotIn("-m", a)
        p = self.sent(0)
        self.assertTrue(p.startswith(prompt.PROMPT))
        self.assertIn("#12 Jobs stuck", p)
        steps = [s["step"] for s in snaps]
        self.assertIn("reading history", steps)
        self.assertEqual(steps[-1], "done")
        self.assertFalse((store.P.clones / DID).exists())
        self.assertFalse(list(Path(tempfile.gettempdir()).glob("johto-codex-*")))

    def test_invalid_then_a_repair_run_with_the_previous_dossier(self):
        res, snaps = self.run_plan(["invalid", "valid"])
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.passes, 2)
        p = self.sent(1)
        self.assertIn("## Your previous attempt", p)
        self.assertIn('"headline": "word word', p)
        self.assertIn("failed validation with 1 error", p)
        self.assertIn("repairing", [s["step"] for s in snaps])
        self.assertEqual(res.doc["headline"], valid_doc()["headline"])

    def test_still_invalid_fails_with_errors(self):
        res, _ = self.run_plan(["invalid", "nomarkers"])
        self.assertFalse(res.ok)
        self.assertEqual(res.passes, 2)
        self.assertIn("after the repair run", res.error)
        self.assertTrue(any("no `<<<DOSSIER` line" in e for e in res.errors))

    def test_old_event_format_still_reads(self):
        res, snaps = self.run_plan(["oldformat"])
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.model, "gpt-5.4")   # that format names the model
        self.assertTrue(any("$ bash -lc rg -n queue src" in l for s in snaps for l in s["log_tail"]))

    def test_usage_limit_requeues(self):
        res, _ = self.run_plan(["ratelimit"])
        self.assertFalse(res.ok)
        self.assertTrue(res.retryable)
        self.assertIn("usage limit", res.error)

    def test_timeout_is_not_a_usage_limit(self):
        res, snaps = self.run_plan(["sleep"], timeout=2)
        self.assertFalse(res.ok)
        self.assertFalse(res.retryable)
        self.assertEqual(res.error, "timed out")
        self.assertLess(res.elapsed_s, 20)

    def test_settings_pick_model_and_effort(self):
        settings.save({"codex_model": "gpt-5.4", "codex_effort": "xhigh"})
        res, _ = self.run_plan(["valid"])
        self.assertTrue(res.ok, res.error)
        a = self.args(0)
        self.assertEqual(a[a.index("-m") + 1], "gpt-5.4")
        self.assertEqual(a[a.index("-c") + 1], 'model_reasoning_effort="xhigh"')
        self.assertEqual(res.doc["effort"], "xhigh")

    def test_without_codex_config_the_doc_still_names_a_model(self):
        os.environ["CODEX_HOME"] = str(self.tmp / "nowhere")
        res, _ = self.run_plan(["valid"])
        self.assertTrue(res.ok, res.error)
        self.assertEqual((res.doc["model"], res.doc["effort"]), ("gpt-via-codex", "high"))

    def test_missing_binary_fails_plainly(self):
        with mock.patch.object(codex.config, "CODEX_BIN", "/nonexistent/codex"):
            res, _ = self.run_plan(["valid"])
        self.assertFalse(res.ok)
        self.assertIn("FileNotFoundError", res.error)


class WorkerChoiceTest(unittest.TestCase):
    """The worker runs whichever generator the setting (or the caller) names; the item records the choice."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        store.use_data_dir(Path(self._tmp.name))
        self.patches = [mock.patch.object(worker.WORKER, "kick"),
                        mock.patch.object(settings, "path", lambda: Path(self._tmp.name) / "settings.json")]
        for p in self.patches:
            p.start()
        worker.fill([{**ENTRY, "full_name": "o/a", "commit": "1" * 40}])
        self.did = store.load()["items"][0]["id"]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        store.use_data_dir(generate.store.config.DATA)
        self._tmp.cleanup()

    def gens(self):
        calls = []

        def make(name):
            def gen(entry, did, on_progress=None, echo=None, cancel=None):
                calls.append(name)
                return generate.Result(ok=True, doc={**valid_doc(), "id": did}, model=name, elapsed_s=1)
            return gen
        return calls, mock.patch.object(worker.generate, "generate", make("claude")), mock.patch.object(worker.codex, "generate", make("codex"))

    def test_the_setting_picks_the_generator(self):
        self.assertEqual(worker.model_choice(), "claude")
        self.assertIs(worker.generator(), generate.generate)
        worker.set_model("codex")
        self.assertEqual((worker.model_choice(), settings.get("dossier_model")), ("codex", "codex"))
        self.assertIs(worker.generator(), codex.generate)
        self.assertEqual(worker.status()["model"], "codex")
        worker.set_model("claude")
        self.assertEqual(settings.load().get("dossier_model"), "claude")   # back to the default, not stored
        for bad in ("gemini", "gpt_pro"):
            with self.assertRaises(ValueError):
                worker.set_model(bad)
        self.assertEqual(list(worker.MODELS), ["claude", "codex"])

    def test_run_item_uses_the_setting_or_the_callers_model(self):
        calls, pc, pg = self.gens()
        with pc, pg:
            worker.set_model("codex")
            with mock.patch.object(worker, "queue_brief", lambda *a, **k: True):
                res = worker.run_item(self.did)
                self.assertEqual((calls, res.model), (["codex"], "codex"))
                self.assertEqual(store.get(self.did)["model_choice"], "codex")
                with store.edit() as idx:
                    store.find(idx, self.did).update(status="queued")
                worker.run_item(self.did, model="claude")
                self.assertEqual(calls, ["codex", "claude"])
                self.assertEqual(store.get(self.did)["model_choice"], "claude")


if __name__ == "__main__":
    unittest.main()
