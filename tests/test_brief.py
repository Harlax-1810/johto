"""The repo brief (johto/dossier/brief.py): validation, read-time facts, the fallback, the queue's states and one
generation with a stub claude (never a real model call)."""
import copy
import re
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

from johto.dossier import brief, brief_prompt, generate, store
from test_dossier_fixtures import _opp, valid_doc

DID = "acme__vidlabel__5ad0c0de"
REPO = "acme/vidlabel"
SEEN = ["https://docs.example.com/northbin/barcode-history",
        "https://docs.example.com/tapewright/live-counts",
        "https://docs.example.com/reelkeep/handover-records", "https://docs.example.com/archivora/partner-requests"]


def vidlabel_dossier() -> dict:
    """A dossier with the Vidlabel example's id, repo and opportunities o1 to o5 (enough for its seeds and places)."""
    d = valid_doc()
    d["id"], d["repo"] = DID, REPO
    d["commit"] = "5ad0c0de1234567890abcdef1234567890abcdef"
    d["blocks"] = [b for b in d["blocks"] if b["type"] != "opportunity"] + [_opp(i) for i in range(1, 6)]
    o4 = next(b for b in d["blocks"] if b.get("id") == "o4")
    o4["slot_fit"] = [{"action": "Operate", "domain": "DevOps / cloud", "codebase_type": "Enterprise-grade"}]
    o4["one_shot"] = {"likelihood": "possible", "why": "x"}
    o4["bounded_task"]["size"] = "S"
    d["verdict"]["best_fits"].append({"action": "Understand / Assess", "domain": "Backend / API",
                                      "codebase_type": "Enterprise-grade", "why": "x"})
    return d


def example() -> dict:
    return copy.deepcopy(brief_prompt.EXAMPLE)


SLOTS = {"synced_at": "2026-11-01T00:30:30-0700", "groups": [
    {"slot": {"action": "Modify", "domain": "Backend / API", "codebase_type": "Enterprise-grade",
              "prompt_style": "Casual prose", "interaction_mode": "Interactive/pairing"}, "count": 19},
    {"slot": {"action": "Modify", "domain": "Backend / API", "codebase_type": "Enterprise-grade",
              "prompt_style": "Explicit spec", "interaction_mode": "Async/autonomous"}, "count": 5},
    {"slot": {"action": "Operate", "domain": "DevOps / cloud", "codebase_type": "Enterprise-grade",
              "prompt_style": "Casual prose", "interaction_mode": "Interactive/pairing"}, "count": 24},
    {"slot": {"action": "Understand / Assess", "domain": "Backend / API", "codebase_type": "Enterprise-grade",
              "prompt_style": "Casual prose", "interaction_mode": "Interactive/pairing"}, "count": 53},
    {"slot": {"action": "Create", "domain": "Web frontend", "codebase_type": "Personal-scale",
              "prompt_style": "Error / artifact brief", "interaction_mode": "Interactive/pairing"}, "count": 3},
]}


class Root(unittest.TestCase):
    """A temp data dir with the Vidlabel-shaped dossier, slots and (optionally) the example brief."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        store.use_data_dir(self.tmp / "data")
        store.P.dir.mkdir(parents=True)
        self.dossier = vidlabel_dossier()
        store.save_dossier(self.dossier)
        (store.P.data / "slots.json").write_text(json.dumps(SLOTS))
        with store.edit() as idx:
            idx["items"].append({**store.new_item({"full_name": REPO, "commit": self.dossier["commit"]}, 1),
                                 "status": "ready"})
        self.p_tasks = mock.patch.object(brief, "task_metas", lambda: [])
        self.p_tasks.start()

    def tearDown(self):
        self.p_tasks.stop()
        store.use_data_dir(store.config.DATA)
        self._tmp.cleanup()

    def install_example(self) -> dict:
        doc = example()
        doc["dossier_sha"] = brief.dossier_sha(DID)
        brief._write(doc)
        return doc


class ValidateTest(Root):
    def test_example_passes(self):
        doc = self.install_example()
        self.assertEqual(brief.validate(doc, self.dossier), [])

    def errs(self, change) -> list[str]:
        doc = example()
        change(doc)
        return brief.validate(doc, self.dossier)

    def assertFlags(self, change, *needles):
        e = "\n".join(self.errs(change))
        for n in needles:
            self.assertIn(n, e)

    def test_structure(self):
        self.assertFlags(lambda d: d.pop("seeds"), ": missing seeds")
        self.assertFlags(lambda d: d.update(extra=1), "unknown field extra")
        self.assertFlags(lambda d: d["verdict"].update(level="great"), "verdict.level: 'great' is not one of")
        self.assertFlags(lambda d: d.update(name=7), "name: must be string")
        self.assertFlags(lambda d: d.update(schema="johto.brief/2"), "schema: must be")
        self.assertFlags(lambda d: d["seeds"].pop(), "seeds: 2 items, at least 3")
        self.assertFlags(lambda d: d["places"][0]["questions"].append("Why is it slow on Mondays?"),
                         "places[0].questions: 3 items, at most 2")
        self.assertFlags(lambda d: d["heading"][0].update(when="last year"), "heading[0].when")
        self.assertFlags(lambda d: d["places"][0].update(id="q1"), "places[0].id")
        self.assertFlags(lambda d: d["places"][0].update(actions=["Modify", "Modify"]), "repeated items")
        self.assertFlags(lambda d: d.update(elapsed_s=-1), "elapsed_s: below 0")
        self.assertFlags(lambda d: d.update(dossier_sha="xyz"), "dossier_sha")

    def test_word_limits(self):
        self.assertFlags(lambda d: d.update(one_liner=" ".join(["labels"] * 15)), "one_liner: 15 words, limit 14")
        self.assertFlags(lambda d: d["seeds"][0].update(title=" ".join(["jobs"] * 10)),
                         "seeds[0].title: 10 words, limit 9")
        self.assertFlags(lambda d: d["places"][1]["look_at"].append("Four word product name"),
                         "places[1].look_at[4]: 4 words, limit 3")

    def test_never_list(self):
        cases = {
            "names a file or path": "Reminders read loans under vidlabel/server/loans before sending.",
            "has a line number": "The reminder check sits on line 488 of the server.",
            "has a code identifier": "The open_loans list is a copy of every open loan.",
            "has a commit hash": "The sync has looked like this since 5ad0c0de shipped.",
            "has an issue or PR number": "Upstream has known about it since #412 was filed.",
            "has an em or en dash": "Retired barcodes come back — and nobody notices.",
            "reads as an instruction": "Add a report that lists retired barcodes first.",
            "addresses the reader": "Your reprints leave old labels on the shelf.",
            "is a question": "Do reprints leave old labels on the shelf?",
            "uses a marketing word": "A robust catalogue server for archives of every size.",
            "names a model or effort": "Opus tends to drop repeat scans that are still valid.",
            "has a command": "Teams start it with docker compose up and wait.",
            "has a version number": "Since 1.6.0 condition reports and photos arrive.",
            "makes a better-or-worse judgement": "A sweep should never touch live loans at all.",
        }
        for needle, text in cases.items():
            with self.subTest(needle):
                self.assertFlags(lambda d: d.update(how_it_works=text), f"how_it_works: {needle}")

    def test_one_thought_per_line(self):
        self.assertFlags(lambda d: d["heading"][3].update(what="Client addresses resolve behind proxies; teams go."),
                         "heading[3].what: has a semicolon")
        self.assertFlags(lambda d: d["verdict"].update(lede="Worth diving into: busy; old."), "verdict.lede: has a "
                                                                                               "semicolon")
        self.assertEqual(self.errs(lambda d: d["seeds"][0]["detail"]["today"].append(
            "Holds sync across branches; renewals never do.")), [])

    def test_reproduction_detail_only_in_the_reviewer_fields(self):
        self.assertFlags(lambda d: d["verdict"].update(lede="Worth diving into: each copy is countable at a local "
                                                            "mail sink."),
                         "verdict.lede: says how it is reproduced (mail sink)")
        self.assertFlags(lambda d: d["places"][0].update(today="Reminders run against a mock server first. Then "
                                                               "they reach borrowers."),
                         "places[0].today: says how it is reproduced (mock server)")
        self.assertEqual(self.errs(lambda d: d["seeds"][1]["detail"].update(
            reviewer_check="A reviewer sends one campaign to a local mail sink and counts the copies.")), [])

    def test_first_layer_lines_never_say_where_problems_show_up(self):
        self.assertFlags(lambda d: d.update(list_line="Settings drift from the data they describe. Problems "
                                                      "reproduce in Docker."),
                         "list_line: says where its problems show up (reproduce)")
        self.assertFlags(lambda d: d["verdict"].update(lede="Worth diving into: sync drifts, all locally."),
                         "verdict.lede: says where its problems show up (locally)")
        self.assertEqual(self.errs(lambda d: d["seeds"][0]["detail"]["today"].append(
            "In a reproduced case the second scan opened a new loan.")), [])

    def test_questions_only_where_allowed(self):
        self.assertEqual(self.errs(lambda d: d["seeds"][0]["detail"]["check_by_hand"].append(
            "Does the upgrade path show any symptom at all?")), [])
        self.assertFlags(lambda d: d["seeds"][0]["detail"]["today"].append("Does anyone clean the copies up?"),
                         "seeds[0].detail.today[5]: is a question")

    def test_ids_and_urls_are_not_prose(self):
        # opportunity ids, provenance, release sources and URLs are not checked as prose
        self.assertEqual(self.errs(lambda d: d["places"][0]["from"].append("vidlabel/server/loans/ledger.py")), [])
        self.assertEqual(self.errs(lambda d: d["heading"][0].update(source="release notes v0.9.0")), [])

    def test_ids_must_exist_in_the_dossier(self):
        self.assertFlags(lambda d: d["seeds"][0].update(opportunity_id="o9"),
                         "seeds[0].opportunity_id: 'o9' is not an opportunity in the dossier")
        self.assertFlags(lambda d: d["seeds"][1].update(opportunity_id="o2"), "the same opportunity is used twice")
        self.assertFlags(lambda d: d["places"][0].update(near_seed="o7"), "places[0].near_seed: 'o7'")
        self.assertFlags(lambda d: d.update(repo="acme/other"), "repo: 'acme/other' is not acme/vidlabel")

    def test_a_rival_name_may_look_like_a_domain(self):
        self.assertEqual(self.errs(lambda d: d["places"][0]["rivals"][0].update(who="stats.fm")), [])
        self.assertFlags(lambda d: d["places"][0]["rivals"][0].update(has="keeps every stats.fm export"),
                         "places[0].rivals[0].has: has a code identifier (stats.fm)")

    def test_rival_needs_a_source(self):
        self.assertFlags(lambda d: d["places"][0]["rivals"].append({"who": "Harborline", "has": "keeps every count"}),
                         "places[0].rivals[1]: missing source", "places[0].rivals[1]: no source URL")


class EnrichTest(Root):
    def setUp(self):
        super().setUp()
        self.doc = self.install_example()

    def test_seed_fields(self):
        out = brief.enrich(self.doc, self.dossier, SLOTS, [])
        o4 = next(s for s in out["seeds"] if s["opportunity_id"] == "o4")
        self.assertEqual((o4["one_shot"], o4["size"]), ("possible", "S"))
        self.assertEqual(o4["fits"][0]["open"], 24)
        o2 = next(s for s in out["seeds"] if s["opportunity_id"] == "o2")
        f = o2["fits"][0]
        self.assertEqual((f["action"], f["domain"], f["codebase_type"], f["open"]),
                         ("Modify", "Backend / API", "Enterprise-grade", 24))
        self.assertEqual([(v["prompt_style"], v["interaction_mode"], v["open"]) for v in f["variants"]][:2],
                         [("Casual prose", "Interactive/pairing", 19), ("Explicit spec", "Async/autonomous", 5)])
        self.assertIn(0, [v["open"] for v in f["variants"]], "variants with no open slot are listed, at 0")
        self.assertEqual(o2["upstream"], [{"title": "Jobs stuck", "url": "https://github.com/acme/widget/issues/12",
                                           "state": "open"}])
        self.assertIsNone(o2["used"])
        self.assertFalse(out["stale"])

    def test_place_fits_and_near(self):
        out = brief.enrich(self.doc, self.dossier, SLOTS, [])
        p1 = out["places"][0]   # Create, Modify, Operate
        self.assertTrue(p1["fits"])
        self.assertTrue(all(f["open"] > 0 for f in p1["fits"]))
        self.assertEqual([f["open"] for f in p1["fits"]], sorted((f["open"] for f in p1["fits"]), reverse=True))
        self.assertEqual(p1["near"], {"opportunity_id": "o3", "is_seed": True,
                                      "title": "Retired barcodes come back on new tapes"})
        p2 = out["places"][1]   # near o5, not a seed
        self.assertEqual(p2["near"]["is_seed"], False)
        self.assertTrue(p2["near"]["title"])
        self.assertIsNone(out["places"][4]["near"])
        self.assertEqual([a["opportunity_id"] for a in out["also"]], ["o1", "o5"])
        keys = [(f["action"], f["domain"], f["codebase_type"]) for f in out["fits"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_used(self):
        tasks = [
            {"short": "aaaa1111", "label": "Vidlabel · Repeat scans", "repo": {"full_name": "acme/vidlabel"},
             "studio": {"status": "Awaiting Review"}, "dossier": {"id": DID, "opportunity_id": "o2"}},
            {"short": "bbbb2222", "label": "Vidlabel · Barcodes", "repo": {"full_name": "ACME/vidlabel"},
             "studio": {"status": "Writing"}, "origin": {"kind": "place", "place_id": "p1", "dossier_id": DID}},
            {"short": "cccc3333", "repo": {"full_name": "acme/vidlabel"}, "studio": {"status": "Accepted"}},
            {"short": "dddd4444", "repo": {"full_name": "acme/vidlabel"}, "unclaimed_at": "2026-10-31T01:00:00-0700",
             "dossier": {"id": DID, "opportunity_id": "o3"}},
            {"short": "eeee5555", "repo": {"full_name": "acme/vidlabel"},
             "dossier": {"id": "acme__vidlabel__00000000", "opportunity_id": "o4"}},
            {"short": "ffff6666", "repo": {"full_name": "other/repo"}, "dossier": {"id": DID, "opportunity_id": "o4"}},
        ]
        out = brief.enrich(self.doc, self.dossier, SLOTS, tasks)
        self.assertEqual([(u["kind"], u["id"], u["short"], u["stage_word"]) for u in out["used"]],
                         [("seed", "o2", "aaaa1111", "in review"), ("place", "p1", "bbbb2222", "in flight"),
                          ("repo", None, "cccc3333", "accepted"), ("repo", None, "eeee5555", "in flight")])
        self.assertEqual(out["seeds"][0]["used"]["short"], "aaaa1111")
        self.assertIsNone(out["seeds"][1]["used"], "an unclaimed task does not count")
        self.assertEqual(out["places"][0]["used"]["short"], "bbbb2222")

    def test_facts(self):
        f = brief.facts(self.dossier)
        self.assertEqual((f["needs_paid_service"], f["warnings"]), (False, []))
        self.assertEqual(f["drill"]["needs"], ["mock mail server"])
        d = vidlabel_dossier()
        next(b for b in d["blocks"] if b.get("id") == "o1")["self_contained"] = {"ok": False, "needs": ["a GPU"]}
        d["blocks"].append({"type": "callout", "tone": "warn", "title": "Mostly AI-generated",
                            "text": "The repo is mostly AI-generated code."})
        f = brief.facts(d)
        self.assertEqual(f["warnings"], ["Needs a paid service", "Tests need a GPU", "Much of the code looks AI-written"])
        d["blocks"][-1] = {"type": "callout", "tone": "info", "title": "No AI-authorship signals", "text": "None."}
        self.assertFalse(brief.facts(d)["ai_code"])

    def test_stale_when_the_dossier_changes(self):
        self.assertEqual(brief.state(DID), "ready")
        d = vidlabel_dossier()
        d["headline"] = "A changed headline for the widget"
        store.save_dossier(d)
        self.assertEqual(brief.state(DID), "stale")
        self.assertTrue(brief.enrich(self.doc, d, SLOTS, [])["stale"])

    def test_enrich_leaves_the_stored_doc_alone(self):
        before = json.dumps(self.doc, sort_keys=True)
        brief.enrich(self.doc, self.dossier, SLOTS, [])
        self.assertEqual(json.dumps(self.doc, sort_keys=True), before)


class MechanismTest(unittest.TestCase):
    """funded_team and reviewer_check say what users get, never how the fix is built (the one-shot paradox)."""

    def test_mechanism_words_are_named(self):
        self.assertEqual(brief.names_mechanism("A server-side check stops the bad upload."), "server-side")
        self.assertEqual(brief.names_mechanism("It adds a migration for the new column."), "migration")
        self.assertIsNone(brief.names_mechanism("Admins can turn the new export off for one project."))

    def test_the_prompt_example_names_none(self):
        seeds = brief_prompt.EXAMPLE["seeds"]
        self.assertTrue(seeds)
        for o in seeds:
            for k in ("funded_team", "reviewer_check"):
                self.assertIsNone(brief.names_mechanism((o.get("detail") or {}).get(k)), (o.get("title"), k))


class VerdictWordTest(unittest.TestCase):
    """The verdict shows as a pill beside the lede, so finalize drops a leading verdict word (no repair pass)."""

    def test_finalize_strips_a_leading_verdict_word(self):
        doc = example()
        doc["verdict"]["lede"] = "Worth diving into: every reminder run reads many loans from one ledger."
        out = brief.finalize(doc, "x", "0" * 16, "a/b", model=None, elapsed_s=1, cost_usd=0, rivals_checked=True)
        self.assertEqual(out["verdict"]["lede"], "Every reminder run reads many loans from one ledger.")
        self.assertEqual(brief.strip_verdict_word("Dive: the queue lives in memory."), "The queue lives in memory.")
        self.assertEqual(brief.strip_verdict_word("Divers lose sync."), "Divers lose sync.")

    def test_the_prompt_example_does_not_teach_it(self):
        self.assertFalse(re.match(r"(?i)worth diving|dive\b", brief_prompt.EXAMPLE["verdict"]["lede"]))


class FallbackTest(unittest.TestCase):
    def test_shape(self):
        d = vidlabel_dossier()
        d["verdict"]["why"] = "Sync bugs in ledger.py:488 and #412, reproduced through the exporter twice."
        next(b for b in d["blocks"] if b.get("id") == "o1")["title"] = "Restricted SMTP_RELAYS cuts the reminder queue off"
        fb = brief.fallback(d)
        self.assertEqual(set(fb), {"name", "repo", "one_liner", "verdict", "seeds", "places", "heading"})
        self.assertEqual((fb["name"], fb["places"], fb["heading"], fb["verdict"]["level"]), ("vidlabel", [], [], None))
        self.assertEqual([s["opportunity_id"] for s in fb["seeds"]], ["o1", "o2", "o3"])
        self.assertNotIn("ledger.py", fb["verdict"]["lede"])
        self.assertNotIn("#412", fb["verdict"]["lede"])
        self.assertNotIn("SMTP_RELAYS", fb["seeds"][0]["title"])
        self.assertEqual(fb["seeds"][0]["area"], "Job state consistency")


class StateTest(Root):
    def test_transitions(self):
        self.assertEqual(brief.state(DID), "missing")
        self.assertTrue(brief.queue(DID))
        self.assertFalse(brief.queue(DID), "already queued")
        self.assertEqual(brief.state(DID), "queued")
        self.assertEqual(brief.next_queued(), DID)
        brief._set(DID, status="generating", pid=os.getpid())
        self.assertEqual(brief.state(DID), "queued", "nobody runs it in this process: a dead record")
        brief._current[DID] = generate.Cancel()
        try:
            self.assertEqual(brief.state(DID), "generating")
        finally:
            brief._current.pop(DID)
        brief._set(DID, status="failed", error="timed out")
        self.assertEqual(brief.state(DID), "failed")
        self.install_example()
        brief._set(DID, status="ready")
        self.assertEqual(brief.state(DID), "ready")

    def test_queue_missing_and_requeue_stale(self):
        self.assertEqual(brief.queue_missing(), [DID])
        self.assertEqual(brief.queue_missing(), [])
        brief._set(DID, status="generating", pid=999999)
        self.assertEqual(brief.requeue_stale(), [DID])
        self.assertEqual(brief.status(DID)["status"], "queued")
        # a failure for this same dossier is not queued again on its own
        brief._set(DID, status="failed", dossier_sha=brief.dossier_sha(DID))
        self.assertEqual(brief.queue_missing(), [])
        brief._set(DID, dossier_sha="0" * 16)
        self.assertEqual(brief.queue_missing(), [DID])
        # a ready brief is left alone; a stale one is queued
        brief._set(DID, status="ready")
        self.install_example()
        self.assertEqual(brief.queue_missing(), [])
        d = vidlabel_dossier()
        d["headline"] = "A changed headline for the widget"
        store.save_dossier(d)
        self.assertEqual(brief.queue_missing(), [DID])

    def test_next_queued_prefers_forced(self):
        other = "acme__widget__9f1e2d3c"
        d = valid_doc()
        store.save_dossier(d)
        brief.queue(DID)
        brief.queue(other, force=True)
        self.assertEqual(brief.next_queued(), other)

    def test_items(self):
        self.install_example()
        self.assertEqual(brief.items(), [{"id": DID, "state": "ready", "generated_at": "2026-11-01T03:00:00-07:00",
                                          "verdict": "dive"}])

    def test_view(self):
        v = brief.view(DID)
        self.assertEqual((v["state"], v["brief"], v["progress"]), ("missing", None, None))
        self.assertEqual(len(v["fallback"]["seeds"]), 3)
        self.install_example()
        v = brief.view(DID)
        self.assertEqual(v["state"], "ready")
        self.assertIsNone(v["fallback"])
        self.assertIn("fits", v["brief"]["seeds"][0])
        brief._set(DID, status="generating", pid=os.getpid(), progress={"step": "researching", "elapsed_s": 40})
        brief._current[DID] = generate.Cancel()
        try:
            v = brief.view(DID)
        finally:
            brief._current.pop(DID)
        self.assertEqual(v["progress"], {"step": "Researching rivals", "elapsed_s": 40})
        self.assertIsNotNone(v["brief"], "the old brief stays on screen while a new one is written")
        with self.assertRaises(KeyError):
            brief.view("nobody__here__00000000")


# --- one generation with a stub claude -------------------------------------------------------------------------

# Speaks stream-json. Per pass (FAKE_PLAN, a JSON list): "valid", "invalid" (a 20-word one-liner), "nomarkers",
# "ratelimit", "noweb" (no web tool), "unseen" (a rival whose URL no web result showed), "sleep".
# It writes the session transcript where Claude Code keeps it, with a WebSearch call and results listing FAKE_SEEN.
FAKE = textwrap.dedent(r'''
    #!{python}
    import json, os, re, sys, time
    from pathlib import Path
    args = sys.argv[1:]
    state = Path(os.environ["FAKE_STATE"])
    n = int(state.read_text()) if state.exists() else 0
    state.write_text(str(n + 1))
    Path(os.environ["FAKE_ARGS"] + f".{{n}}").write_text(json.dumps(args))
    plan = json.loads(os.environ["FAKE_PLAN"])[n]
    sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
    proj = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    proj.mkdir(parents=True, exist_ok=True)
    def emit(ev): print(json.dumps(ev), flush=True)
    emit({{"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": sid}})
    with open(proj / f"{{sid}}.jsonl", "a") as t:
        if plan != "noweb":
            use = {{"type": "tool_use", "id": "tu1", "name": "WebSearch", "input": {{"query": "rivals"}}}}
            emit({{"type": "assistant", "message": {{"content": [use]}}}})
            t.write(json.dumps({{"type": "assistant", "message": {{"content": [use]}}}}) + "\n")
            seen = json.loads(os.environ["FAKE_SEEN"])
            t.write(json.dumps({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "tu1",
                    "content": "Results: " + " ".join(seen)}}]}}, "toolUseResult": {{"results": [{{"url": u}} for u in seen]}}}}) + "\n")
    if plan == "sleep":
        time.sleep(60)
    doc = json.loads(os.environ["FAKE_DOC"])
    if plan == "invalid":
        doc["one_liner"] = " ".join(["labels"] * 20)
    if plan == "unseen":
        doc["places"][0]["rivals"].append({{"who": "Harborline", "has": "keeps every shelf count forever.",
                                           "source": "https://harborline.example.com/never-seen"}})
    text = "Done.\n<<<BRIEF\n" + json.dumps(doc) + "\nBRIEF>>>\n"
    if plan == "nomarkers":
        text = "Here is the brief without markers."
    if plan == "ratelimit":
        emit({{"type": "result", "subtype": "success", "is_error": True, "result": "You've hit your usage limit",
               "total_cost_usd": 0, "session_id": sid}})
        sys.exit(1)
    emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": text}}]}}}})
    emit({{"type": "result", "subtype": "success", "is_error": False, "result": text, "total_cost_usd": 0.75,
           "session_id": sid}})
''').lstrip().format(python=sys.executable)

INPUTS = {"readme": "Vidlabel prints shelf labels.", "releases": ["2026-09-01 v1.8.0 Retries https://x/r"], "prs": []}


class FakeClaude(Root):
    def setUp(self):
        super().setUp()
        self.cfg = self.tmp / "claude-config"
        fake = self.tmp / "claude"
        fake.write_text(FAKE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.fetched = []
        self.patches = [mock.patch.object(brief, "CLAUDE_BIN", fake),
                        mock.patch.object(brief, "fetch_inputs", lambda d: self.fetched.append(d["id"]) or INPUTS)]
        for p in self.patches:
            p.start()
        doc = example()
        doc.update(model=None, elapsed_s=None, cost_usd=None)
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg),
                                                "FAKE_STATE": str(self.tmp / "state"),
                                                "FAKE_ARGS": str(self.tmp / "args"),
                                                "FAKE_SEEN": json.dumps(SEEN), "FAKE_DOC": json.dumps(doc)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for p in self.patches:
            p.stop()
        super().tearDown()

    def plan(self, plan, **kw):
        os.environ["FAKE_PLAN"] = json.dumps(plan)
        return brief.generate(DID, **kw)

    def args(self, n):
        return json.loads((self.tmp / f"args.{n}").read_text())

    def assertCleaned(self):
        self.assertFalse((store.P.clones / f"{DID}.brief").exists(), "work folder left behind")
        self.assertEqual(list((self.cfg / "projects").glob("*/*.jsonl")), [], "session transcript left behind")


class GenerateTest(FakeClaude):
    def test_valid_first_pass(self):
        snaps = []
        res = self.plan(["valid"], on_progress=snaps.append)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["passes"], res["cost_usd"]), (1, 0.75))
        doc = brief.load(DID)
        self.assertEqual(brief.validate(doc, self.dossier), [])
        self.assertEqual((doc["dossier_sha"], doc["model"], doc["cost_usd"], doc["rivals_checked"]),
                         (brief.dossier_sha(DID), "claude-opus-5-5", 0.75, True))
        self.assertEqual(list(doc)[:9], list(brief_prompt.SERVER_FIELDS))
        self.assertEqual(sum(len(p["rivals"]) for p in doc["places"]), 4, "every rival's URL was seen")
        self.assertEqual(brief.state(DID), "ready")
        self.assertEqual(brief.status(DID)["status"], "ready")
        self.assertEqual(self.fetched, [DID])
        self.assertIn("brief of " + DID, brief.log_path(DID).read_text())
        self.assertIn("ready in", brief.log_path(DID).read_text())
        self.assertIn("checking", [s["step"] for s in snaps])
        self.assertCleaned()

    def test_argv_is_web_only(self):
        self.plan(["valid"])
        a = self.args(0)
        self.assertEqual(a[a.index("--model") + 1], "opus")
        self.assertEqual(a[a.index("--effort") + 1], "medium")
        self.assertEqual(a[a.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(a[a.index("--allowedTools") + 1], "WebSearch,WebFetch")
        denied = a[a.index("--disallowedTools") + 1].split(",")
        for t in ("Bash", "Edit", "Write", "Read"):
            self.assertIn(t, denied)
        self.assertIn("--strict-mcp-config", a)
        prompt = a[a.index("-p") + 1]
        self.assertIn("one-shot paradox", prompt)
        self.assertIn('"dossier_sha": "' + brief.dossier_sha(DID), prompt)
        self.assertIn("2026-09-01 v1.8.0 Retries", prompt)
        self.assertIn("o2: Job queue retries lose state 2", prompt)

    def test_unseen_rival_is_dropped(self):
        res = self.plan(["unseen"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["dropped_rivals"], ["Harborline (https://harborline.example.com/never-seen)"])
        self.assertNotIn("Harborline", json.dumps(brief.load(DID)))

    def test_no_web_search_means_no_rivals(self):
        res = self.plan(["noweb"])
        self.assertTrue(res["ok"], res)
        doc = brief.load(DID)
        self.assertEqual((doc["rivals_checked"], sum(len(p["rivals"]) for p in doc["places"])), (False, 0))

    def test_repair_pass_uses_same_session(self):
        res = self.plan(["invalid", "valid"])
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["passes"], res["cost_usd"]), (2, 1.5))
        a0, a1 = self.args(0), self.args(1)
        self.assertEqual(a1[a1.index("--resume") + 1], a0[a0.index("--session-id") + 1])
        self.assertIn("one_liner: 20 words, limit 14", a1[a1.index("-p") + 1])
        self.assertCleaned()

    def test_missing_markers_get_a_repair_pass(self):
        res = self.plan(["nomarkers", "valid"])
        self.assertTrue(res["ok"], res)
        self.assertIn("no `<<<BRIEF` line", self.args(1)[self.args(1).index("-p") + 1])

    def test_second_failure_keeps_the_old_brief(self):
        old = self.install_example()
        res = self.plan(["invalid", "invalid"])
        self.assertFalse(res["ok"])
        self.assertIn("after the repair pass", res["error"])
        self.assertEqual(brief.load(DID), old)
        st = brief.status(DID)
        self.assertEqual(st["status"], "failed")
        self.assertTrue(st["errors"])
        self.assertEqual(brief.state(DID), "ready", "the brief that was there is still served")
        self.assertCleaned()

    def test_failure_with_no_brief_is_failed(self):
        res = self.plan(["invalid", "invalid"])
        self.assertFalse(res["ok"])
        self.assertEqual(brief.state(DID), "failed")
        self.assertEqual(brief.view(DID)["error"], "The brief did not pass its checks.")

    def test_rate_limit_is_queued_again(self):
        res = self.plan(["ratelimit"])
        self.assertTrue(res["retryable"])
        self.assertEqual(brief.state(DID), "queued")

    def test_overwrite_keeps_history(self):
        self.install_example()
        self.plan(["valid"])
        self.assertEqual(len(brief.history(DID)), 1)
        self.assertEqual(json.loads(brief.history(DID)[0].read_text())["name"], "Vidlabel")

    def test_cancel_stops_claude(self):
        os.environ["FAKE_PLAN"] = json.dumps(["sleep"])
        c = generate.Cancel()
        threading.Timer(1.5, lambda: c.cancel("stopped by pause")).start()
        t0 = time.time()
        res = brief.generate(DID, cancel=c)
        self.assertLess(time.time() - t0, 20)
        self.assertTrue(res["cancelled"])
        self.assertEqual(brief.state(DID), "queued", "a stopped brief goes back to the queue")
        self.assertCleaned()

    def test_stop_request_from_another_process(self):
        os.environ["FAKE_PLAN"] = json.dumps(["sleep"])
        out = {}
        th = threading.Thread(target=lambda: out.update(brief.generate(DID)))
        th.start()
        for _ in range(100):
            if brief.status(DID).get("status") == "generating":
                break
            time.sleep(0.05)
        with brief._edit_status() as data:   # what request_stop writes, as another process would
            data.setdefault("stop", {})[DID] = "stopped by pause"
        th.join(30)
        self.assertTrue(out.get("cancelled"))
        self.assertNotIn("stop", brief._load_status())

    def test_waits_for_the_generation_lock(self):
        os.environ["FAKE_PLAN"] = json.dumps(["valid"])
        out = {}
        with store.generation_lock() as got:
            self.assertTrue(got)
            th = threading.Thread(target=lambda: out.update(brief.generate(DID)))
            th.start()
            time.sleep(1.0)
            self.assertFalse(out, "it waits while a dossier generates")
        th.join(30)
        self.assertTrue(out["ok"])

    def test_not_before_skips_a_brief_written_meanwhile(self):
        os.environ["FAKE_PLAN"] = json.dumps(["valid"])
        asked = time.time() - 5
        self.install_example()
        res = brief.generate(DID, not_before=asked)
        self.assertTrue(res["skipped"])
        self.assertFalse((self.tmp / "args.0").exists(), "no claude call")


if __name__ == "__main__":
    unittest.main()


class FocusTest(unittest.TestCase):
    """A brief for research started for some slots lists them and asks for a seed for each."""

    def test_focus_section_only_when_given(self):
        base = dict(did="o__r__aaaaaaaa", sha="x", repo="o/r", dossier={})
        self.assertNotIn("Slots this research was for", brief_prompt.build(**base))
        s = brief_prompt.build(**base, focus=["Operate | Games | Personal-scale"])
        self.assertIn("## Slots this research was for (action | domain | codebase type)\n\n- Operate | Games | Personal-scale", s)
        self.assertIn("first take one seed for each such slot", brief_prompt.PROMPT)

    def test_new_item_keeps_focus(self):
        it = store.new_item({"full_name": "o/r", "commit": "a" * 40, "focus": ["Operate|Games|Personal-scale"]}, 1)
        self.assertEqual(it["focus"], ["Operate|Games|Personal-scale"])
        self.assertEqual(store.new_item({"full_name": "o/r", "commit": "a" * 40}, 1)["focus"], [])
