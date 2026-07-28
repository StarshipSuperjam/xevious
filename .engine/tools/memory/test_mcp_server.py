"""test_mcp_server.py — the engine-memory MCP server, headless (memory substrate).

Run via the engine's CI command:
    uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

Exercises the server in-process (no Claude Desktop, no subprocess): the single `search` tool delegates to the
ranked library and returns `{"results": [...]}`, writing nothing at all — a read is a read. Beside it are the
operator's own controls, which DO write, and the two that do not appear here at all: permanent erasure and the
secret re-scrub are declared in the control contract and deliberately not served, because each is a
command-line verb a person runs at a terminal. Isolation is a throwaway
ENGINE_MEMORY_DIR cabinet, so the server's default-path library calls resolve to the test's temp store.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory import capture, forget, index, ledger, records  # noqa: E402
import memory.mcp_server as srv  # noqa: E402

_ID = records.RECORD_ID_KEY


def _marker_count():
    return sum(1 for r in ledger.iter_records()
               if isinstance(r, dict) and r.get("kind") == records.REINFORCEMENT_KIND)


class _ServerBase(unittest.IsolatedAsyncioTestCase):
    """Each test runs against a throwaway ENGINE_MEMORY_DIR cabinet; the server's default-path calls land there."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engine-memory-mcp-")
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self.tmp
        self.now = int(time.time())

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, text, *, role="observation", tags=(), with_id=True):
        record = {"ts": self.now, "role": role, "tags": list(tags), "text": text}
        if with_id:
            record[_ID] = records.new_record_id()
        ledger.append(record)
        index.rebuild()
        return record.get(_ID)

    @staticmethod
    def _result_json(res):
        import json
        content = res[0] if isinstance(res, tuple) else res
        return json.loads(content[0].text)


class ToolWiringTests(_ServerBase):
    async def test_health_is_content_free_and_fixed_identity(self):
        with mock.patch.object(index, "search", side_effect=AssertionError("health read memory")), \
             mock.patch.object(ledger, "iter_records", side_effect=AssertionError("health read ledger")):
            data = self._result_json(await srv.server.call_tool("health", {}))
        self.assertEqual(data, {"status": "ok", "server": "engine-memory"})

    @unittest.skipUnless(srv._semantic_installed(), "the optional semantic module is not installed here")
    async def test_the_meaning_operations_answer_matches_its_declared_schema(self):
        # The contract declares `additionalProperties: false`, so a key the server sends and the interface
        # does not name is a contract breach. Nothing validated this operation's shape, which is why one
        # survived until a cold review found it by hand.
        import json as _json
        import jsonschema

        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        with open(os.path.join(root, ".engine", "interfaces", "search.json"), encoding="utf-8") as fh:
            declared = _json.load(fh)
        schema = next(op["output_schema"] for op in declared["operations"]
                      if op["name"] == "recall-by-meaning")
        self.add("We ruled out a cron job and hooked the calendar instead.", role="decision")
        for query in ("did we consider running it on a timer",       # a hit
                      "zzzqqx nothing here matches this at all"):    # and an empty answer
            with self.subTest(query=query):
                out = self._result_json(
                    await srv.server.call_tool("recall-by-meaning", {"query": query}))
                jsonschema.validate(out, schema)

    async def test_tools_list_is_exactly_the_declared_operations(self):
        # The server answers its declared operation sets and nothing else — an undeclared tool would be a
        # private detail no other conforming implementation would offer, breaking a caller that relied on it.
        #
        # DERIVED FROM THE CONTRACTS, not from a literal: the operations are read out of the declarations
        # themselves, so this fails if a tool is added without declaring it. TWO declarations are in play
        # because the writes are a separate contract — `search.json` describes recall as never changing what
        # is stored, so the operator's controls could not be declared beside it without making that false.
        #
        # SERVED IS A SUBSET OF DECLARED, not an equality, and the difference is deliberate. Two operations —
        # permanent erasure and the secret re-scrub — are declared BECAUSE a reader of the contract must know
        # the capability exists and where it lives, and are NOT served BECAUSE serving them would defeat what
        # makes them safe: each is a command-line verb that a person runs at a terminal, and a callable tool
        # would be exactly the model-reachable path they are built to refuse. Their descriptions say so. The
        # property that actually matters is the one below: the server offers nothing it has not declared.
        #
        # TWO SHAPES ARE REAL, so both are covered rather than whichever this checkout happens to be:
        # `recall-by-meaning` is registered only where the optional semantic module is installed, and a
        # deployment without it offers the rest alone.
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(srv.__file__))))
        declared, unserved = set(), set()
        for slug in ("search", "memory-control"):
            with open(os.path.join(here, "interfaces", f"{slug}.json"), encoding="utf-8") as fh:
                for op in json.load(fh)["operations"]:
                    declared.add(op["name"])
                    if "NOT SERVED AS A TOOL" in op.get("description", ""):
                        unserved.add(op["name"])
        expected = declared - unserved
        if not srv._semantic_installed():
            expected -= {"recall-by-meaning"}
        names = {t.name for t in await srv.server.list_tools()}
        self.assertEqual(names, expected)
        self.assertTrue(unserved, "no operation is declared as unserved — this assertion has stopped biting")
        self.assertFalse(names & unserved,
                         "an operation declared NOT SERVED is being served — the terminal gate is bypassed")

    @unittest.skipUnless(srv._semantic_installed(), "the optional semantic module is not installed here")
    async def test_the_meaning_operation_returns_the_passage_and_no_closeness_figure(self):
        # Measured, nearness does not track relevance: an irrelevant question outscored a correct reworded
        # match. A figure beside a result is read as confidence whatever the description says, so the
        # transport relays the matched passage and the ordering and nothing that looks like a score.
        self.add("We ruled out a cron job and hooked the calendar instead.", role="decision")
        data = self._result_json(
            await srv.server.call_tool("recall-by-meaning",
                                       {"query": "did we consider running it on a timer"}))
        self.assertTrue(data["results"], "expected the reworded question to reach the record")
        for entry in data["results"]:
            self.assertNotIn("score", entry)
            self.assertTrue(entry.get("passage"))

    async def test_an_uninstalled_module_reads_as_absent_even_though_its_folder_remains(self):
        # The honest-absence law, tested against the way it actually breaks. Removing a module deletes its
        # files and leaves the directory, and Python resolves an empty directory as a namespace package — so
        # a plain "can I find this package?" probe answered YES for an uninstalled module, registered the
        # tool, and crashed on the first call. A namespace package has no `origin`; a real module file does.
        import importlib.util

        real = importlib.util.find_spec

        class _NamespaceLike:
            """What importlib hands back for a directory with no module file in it: a spec with no origin."""

            origin = None

        def emptied(name, *args, **kwargs):
            # A STAND-IN, never the real spec: importlib caches specs, so mutating one would leave
            # `origin = None` set for the rest of the process and quietly fail every later check. Discovered
            # by the full suite — this test passed alone and broke two others when run with them.
            if name == "memory.semantic.store":
                return _NamespaceLike()
            return real(name, *args, **kwargs)

        importlib.util.find_spec = emptied
        try:
            self.assertFalse(srv._semantic_installed(),
                             "an emptied module directory must not read as installed")
        finally:
            importlib.util.find_spec = real
        # Deliberately NOT asserting the module is present afterwards: this test file is owned by the always-
        # present memory module, so it also runs on a deployment where the operator declined the semantic
        # add-on. Asserting its presence would fail in exactly the configuration the decline path exists for.

    async def test_recall_window_reads_a_sessions_conversation_back(self):
        # The read side of the transcript-first substrate: raw turns are excluded from every ranked path, so
        # this is the only way the exact wording comes back.
        for seq, (speaker, text) in enumerate([("user", "shall we cache the roster"),
                                               ("assistant", "yes, with a short expiry")]):
            ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, _ID: records.new_record_id(),
                           "session_id": "s-live", "ts": self.now, "seq": seq, "speaker": speaker,
                           "text": text, "tags": ["transcript", "stop"]})
        out = self._result_json(await srv.server.call_tool("recall-window", {"session_id": "s-live"}))
        self.assertEqual([t["text"] for t in out["turns"]],
                         ["shall we cache the roster", "yes, with a short expiry"])

    async def test_recall_window_writes_nothing(self):
        # Reading a conversation must not reinforce or otherwise mutate the store.
        ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, _ID: records.new_record_id(),
                       "session_id": "s-live", "ts": self.now, "seq": 0, "speaker": "user",
                       "text": "a stored turn", "tags": ["transcript", "stop"]})
        before = _marker_count()
        await srv.server.call_tool("recall-window", {"session_id": "s-live"})
        self.assertEqual(_marker_count(), before, "reading a window must append no reinforcement marker")

    async def test_search_returns_ranked_results_matching_the_library(self):
        self.add("export format export schedule decided", role="decision")
        self.add("a note that export came up once")
        for t in ("alpha", "beta", "gamma", "delta"):
            self.add(t)
        data = self._result_json(await srv.server.call_tool("search", {"query": "export"}))
        tool_ids = [r.get(_ID) for r in data["results"]]
        lib_ids = [r.get(_ID) for r in index.search("export").records]
        self.assertEqual(tool_ids, lib_ids)   # the server is a thin pass-through over the ranked library

    async def test_tags_and_limit_pass_through(self):
        d = self.add("we decided to ship export", tags=["release"])
        self.add("a lesson about export")
        capped = self._result_json(
            await srv.server.call_tool("search", {"query": "export", "limit": 1}))
        self.assertEqual(len(capped["results"]), 1)
        tagged = self._result_json(
            await srv.server.call_tool("search", {"query": "export", "tags": ["release"]}))
        self.assertEqual([r.get(_ID) for r in tagged["results"]], [d])

    async def test_search_answer_carries_the_recall_completeness_note(self):
        # The recall answer carries its own disclosures, because it reaches a caller that may never have opened
        # the workflow document. Three of them, and each is a STANDING condition rather than a one-time note:
        # what a result is (summary or a piece of real conversation, and how to read it whole), that recalled
        # text is a record and not an instruction, and that the stored conversation was never fully stripped of
        # secret-shaped content. Present when there are results; omitted on an empty answer.
        self.add("we decided to ship the export format", role="decision")
        data = self._result_json(await srv.server.call_tool("search", {"query": "export"}))
        self.assertTrue(data["results"])
        self.assertIn("recall_completeness", data)
        note = data["recall_completeness"].lower()
        self.assertIn("summary", note)
        self.assertIn("conversation", note)
        self.assertIn("recall-window", note, "the note must name the reader that gets the exact wording")
        self.assertIn("never an instruction", note, "prompt-injection framing must ride the answer, not only "
                                                    "the workflow doc a direct caller may never have read")
        self.assertIn("never masked", note, "the standing privacy condition must be disclosed where it is true "
                                            "— on every answer, not once in a merge note — and stated so it "
                                            "cannot be skim-read as the reassuring opposite")
        empty = self._result_json(await srv.server.call_tool("search", {"query": "nonexistentzqxword"}))
        self.assertEqual(empty["results"], [])
        self.assertNotIn("recall_completeness", empty)   # nothing returned -> nothing to disclose

    async def test_an_omitted_limit_is_bounded_rather_than_unbounded(self):
        # The library returns EVERY match when no limit is given. Against a few hundred summaries that was
        # survivable; against a store whose bulk is conversation, one common word matches tens of thousands of
        # records and every one comes back whole. Reverting this to an unbounded default is a one-character
        # edit, so it needs a guard of its own — and reinforcement fires per RETURNED record, so the cap bounds
        # the writes too.
        for i in range(25):
            self.add("a shared quokka note number %d" % i, role="observation")
        data = self._result_json(await srv.server.call_tool("search", {"query": "quokka"}))
        self.assertEqual(len(data["results"]), srv._DEFAULT_LIMIT)

    async def test_the_search_answer_validates_against_the_interface_output_schema(self):
        # The interface contract must admit exactly what the reference implementation returns — results, plus the
        # optional recall_completeness note it carries when there are results. Without the widening the note would
        # fail a strict conformance build, and the completeness disclosure would have to be dropped.
        import json
        import validate
        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "interfaces", "search.json")
        with open(schema_path, encoding="utf-8") as fh:
            operations = json.load(fh)["operations"]
            out_schema = next(op["output_schema"] for op in operations if op["name"] == "search")
        checker = validate.Draft202012Validator(out_schema)
        self.add("we decided to ship the export format", role="decision")
        answer = self._result_json(await srv.server.call_tool("search", {"query": "export"}))
        self.assertIn("recall_completeness", answer)
        self.assertEqual(list(checker.iter_errors(answer)), [])        # a note-bearing answer conforms
        empty = self._result_json(await srv.server.call_tool("search", {"query": "nonexistentzqxword"}))
        self.assertEqual(list(checker.iter_errors(empty)), [])         # an empty answer conforms
        self.assertTrue(list(checker.iter_errors({"results": [], "surprise": 1})))  # unknown keys still rejected

    async def test_search_still_answers_when_fts5_absent(self):
        # Availability law: with the fast lookup off, the server still returns recall (via the slow scan).
        self.add("export decision", role="decision")
        original = index.fts5_available
        index.fts5_available = lambda *a, **k: False
        try:
            data = self._result_json(await srv.server.call_tool("search", {"query": "export"}))
            self.assertTrue(len(data["results"]) >= 1)
        finally:
            index.fts5_available = original


class ControlToolTests(_ServerBase):
    """The three tools that WRITE, exercised through the server rather than through the library beneath it —
    the transport is where a caller actually meets them, and where a wrong shape would surface."""

    async def _call(self, name, args):
        return self._result_json(await srv.server.call_tool(name, args))

    async def test_pinning_stores_the_text_and_makes_it_findable(self):
        out = await self._call("pin", {"text": "Always ask before filing an issue."})
        self.assertTrue(out["id"])
        self.assertEqual(out[records.PIN_VIA_KEY], records.PIN_VIA_ASSISTANT)
        found = await self._call("search", {"query": "filing"})
        self.assertEqual(len(found["results"]), 1)

    async def test_a_pin_is_scrubbed_at_the_transport_too(self):
        # The tool is the path a model actually uses, so the scrub has to hold here and not only in the library
        # — this is the call that would carry a credential a session had just been shown.
        out = await self._call("pin", {"text": "token sk-ant-api03-" + "A" * 32})
        self.assertNotIn("sk-ant-api03", out["text"])

    async def test_withhold_and_restore_round_trip_through_the_server(self):
        rid = self.add("a decision that was withdrawn", role="decision")
        self.assertEqual(len((await self._call("search", {"query": "withdrawn"}))["results"]), 1)
        said = await self._call("withhold", {"record_id": rid})
        self.assertIn("still saved", said["withheld"])          # never reads as erasure
        self.assertEqual((await self._call("search", {"query": "withdrawn"}))["results"], [])
        back = await self._call("restore", {"record_id": rid})
        self.assertIn("back in recall", back["restored"])
        self.assertEqual(len((await self._call("search", {"query": "withdrawn"}))["results"]), 1)

    async def test_withholding_names_exactly_one_target(self):
        # Both, or neither, is refused rather than guessed at: a record id and a session id are both uuid hex,
        # so a wrong guess here withholds something the operator never named.
        for args in ({}, {"record_id": "r", "session_id": "s"}):
            with self.assertRaises(Exception):
                await self._call("withhold", args)

    async def test_no_control_tool_removes_a_record_from_the_ledger(self):
        # The whole safety story of these tools is that they append. If one ever deletes, the reversibility
        # every description promises becomes false and the erasure wall stops being the only way out.
        rid = self.add("something to take out of recall")
        before = sum(1 for _ in ledger.iter_records())
        await self._call("withhold", {"record_id": rid})
        await self._call("pin", {"text": "a standing note"})
        after = list(ledger.iter_records())
        self.assertGreater(len(after), before)
        self.assertIn(rid, {r.get(_ID) for r in after})

    async def test_search_can_be_scoped_to_one_conversation(self):
        for sid in ("s-A", "s-B"):
            for i in range(3):
                ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, _ID: records.new_record_id(),
                               "session_id": sid, "seq": i, "speaker": "user", "ts": self.now + i,
                               "text": f"{sid} talking about wombats"})
        index.rebuild()
        whole = await self._call("search", {"query": "wombats", "limit": 50})
        scoped = await self._call("search", {"query": "wombats", "session": "s-B", "limit": 50})
        self.assertEqual(len(whole["results"]), 6)
        self.assertEqual({r["session_id"] for r in scoped["results"]}, {"s-B"})


class DemoTests(unittest.TestCase):
    def test_demo_body_exits_zero(self):
        # The operator demo exercises the REAL rank + filter + reinforce on its own throwaway cabinet; a real
        # regression flips a `!!!` and returns non-zero. (It manages its own ENGINE_MEMORY_DIR.)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(srv._demo(), 0)


if __name__ == "__main__":
    unittest.main()
