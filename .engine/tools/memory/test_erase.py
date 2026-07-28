"""Unit tests for erase.py — the operator-asked erasure verb.

The load-bearing half is what it REFUSES. Erasure is the one act in the system with no undo, so every test
below is about a door staying shut: shut to a target that is not already withheld, shut to a target that does
not exist, and — the one that matters most — shut to any caller without a controlling terminal, which is every
automated path there is.
"""

from __future__ import annotations

import inspect
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import erase, forget, ledger, records  # noqa: E402


class _Tty(io.StringIO):
    """A stream that claims to be a terminal — what the real gate checks, and the only thing a test can fake."""

    def isatty(self) -> bool:
        return True


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        self._tmp.cleanup()

    def _turn(self, session="s-1", seq=0, text="the thing that should not have been said"):
        rid = records.new_record_id()
        ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, records.RECORD_ID_KEY: rid,
                       "session_id": session, "seq": seq, "speaker": "user", "ts": 1785000000 + seq,
                       "text": text})
        return rid

    def _on_a_terminal(self, typed="erase", out=None):
        """Stand in for a real terminal: both streams claim to be one, and `typed` is what the operator types."""
        return mock.patch.multiple(erase.sys, stdin=_Tty(typed + "\n"), stdout=out or _Tty(""))


class TerminalGateTests(_Base):
    def test_without_a_terminal_it_refuses_and_writes_nothing(self):
        # THE BARRIER. An automated caller — every AI path there is — has no controlling terminal, so it never
        # reaches the confirmation. This is what makes a command-line verb safe in a repository that carries a
        # blanket permission grant for running commands.
        rid = self._turn()
        forget.withhold(record_id=rid)
        opened = []
        with self.assertRaises(erase.EraseRefused) as caught:
            erase.request(rid, opener=lambda *a, **k: opened.append(a) or 1)
        self.assertIn("real terminal", str(caught.exception))
        self.assertEqual(opened, [])                       # nothing was opened
        self.assertFalse(os.path.exists(".engine/erasures/proposal.json"))

    def test_a_terminal_that_types_anything_else_declines_and_writes_nothing(self):
        rid = self._turn()
        forget.withhold(record_id=rid)
        opened = []
        with self._on_a_terminal(typed="yes"):
            report = erase.request(rid, opener=lambda *a, **k: opened.append(a) or 1)
        self.assertEqual(report["status"], "declined")
        self.assertEqual(opened, [])

    def test_the_preview_shows_the_operator_their_own_words_before_they_confirm(self):
        # The committed proposal is content-free by design, so the pull request they merge cannot show them
        # what they are erasing. This is the only surface that can, which is why it is not optional.
        rid = self._turn(text="the passphrase is hunter2 and I regret typing it")
        forget.withhold(record_id=rid)
        out = _Tty("")
        with self._on_a_terminal(typed="no", out=out):
            erase.request(rid, stream=out)
        self.assertIn("hunter2", out.getvalue())
        self.assertIn("no undo", out.getvalue())


class RefusalTests(_Base):
    def test_a_target_that_is_not_withheld_is_refused(self):
        # The reversible act comes first: withhold it, live without it, and only then erase.
        rid = self._turn()
        with self._on_a_terminal():
            with self.assertRaises(erase.EraseRefused) as caught:
                erase.request(rid)
        self.assertIn("withhold it first", str(caught.exception))

    def test_a_target_that_does_not_exist_is_refused_rather_than_confirmed(self):
        with self._on_a_terminal():
            with self.assertRaises(erase.EraseRefused) as caught:
                erase.request("0" * 32)
        self.assertIn("nothing to erase", str(caught.exception))

    def test_a_whole_conversation_resolves_to_all_of_its_turns(self):
        ids = [self._turn(seq=i) for i in range(4)]
        forget.withhold(session_id="s-1")
        found = erase._targets_for("s-1")
        self.assertEqual({r[records.RECORD_ID_KEY] for r in found}, set(ids))

    def test_a_partly_withheld_conversation_is_refused_whole(self):
        # Naming a session erases every turn in it, so every turn must already be withheld. Refusing the whole
        # batch is the honest answer — a partial erase of a conversation is not what anyone asked for.
        self._turn(seq=0)
        loose = self._turn(seq=1)
        forget.withhold(record_id=self._targets_first())
        with self._on_a_terminal():
            with self.assertRaises(erase.EraseRefused):
                erase.request("s-1")
        self.assertTrue(loose)

    def _targets_first(self):
        return erase._targets_for("s-1")[0][records.RECORD_ID_KEY]


class ProposalTests(_Base):
    def test_the_committed_proposal_never_carries_the_wording(self):
        # It is committed to a branch and read on a pull-request page. Neither is a place for the operator's
        # own words — which is exactly why the terminal preview above exists.
        rid = self._turn(text="marzipan quokka sourdough")
        forget.withhold(record_id=rid)
        proposal = erase.build_proposal(erase._targets_for("s-1"))
        blob = str(proposal).lower()
        for word in ("marzipan", "quokka", "sourdough", "s-1"):
            self.assertNotIn(word, blob, f"{word!r} leaked into the committed proposal")
        self.assertEqual(proposal["targets"], [rid])
        self.assertEqual(len(proposal["costs"]), 1)

    def test_the_consent_body_names_a_count_and_says_closing_changes_nothing(self):
        rid = self._turn()
        forget.withhold(record_id=rid)
        body = erase._pr_body(erase.build_proposal(erase._targets_for("s-1")))
        self.assertIn("permanently erases", body)
        self.assertIn("Close", body)
        self.assertIn("fully recoverable", body)

    def test_an_empty_proposal_is_refused_rather_than_rendered(self):
        with self.assertRaises(ValueError):
            erase.build_proposal([])
        with self.assertRaises(ValueError):
            erase._pr_body({"targets": [], "costs": []})


class WallTests(_Base):
    def test_this_module_never_mints_an_erasure_marker(self):
        # The sanctioned minters are compact and erasure_observer, and nothing else — the invariant
        # test_forget.py source-scans for. Asserted here too, at the module that would most plausibly break it.
        import inspect
        self.assertNotIn("enact_erasure(", inspect.getsource(erase))


if __name__ == "__main__":
    unittest.main()


class OpenerTests(_Base):
    """The real pull-request opener, which had no coverage at all — and was broken.

    Both other tests inject `opener=`, so the shipped path ran nowhere. It referenced `base64` without
    importing it, and the opener's blanket `except Exception` turned that into `return None`, which `request`
    reports as "could not be opened just now; try again". Every time, forever, looking exactly like a network
    problem. This drives the real opener over a stub transport where every call succeeds.
    """

    class _Transport:
        def __init__(self):
            self.calls = []

        def __call__(self, method, path, payload=None):
            self.calls.append((method, path))
            if path.endswith("/git/ref/heads/main"):
                return 200, {"object": {"sha": "b" * 40}}
            if path.endswith("/git/refs"):
                return 201, {}
            if "/contents/" in path:
                return 201, {}
            if path.endswith("/pulls"):
                return 201, {"number": 42}
            return 200, {}

    def _gh(self):
        return erase._reader(transport=self._Transport())

    def test_the_real_opener_opens_a_pull_request(self):
        proposal = erase.build_proposal([{records.RECORD_ID_KEY: "a" * 32, "kind": records.AMBIENT_CAPTURE_KIND,
                                          "speaker": "user", "ts": 1785000000, "text": "x"}])
        number = erase._open_erasure_pr(self._gh(), erase._branch_for(proposal["targets"]),
                                        erase._pr_title(1), erase._pr_body(proposal),
                                        json.dumps(proposal))
        self.assertEqual(number, 42, "the real opener returned nothing on an all-success transport")

    def test_every_name_the_module_uses_is_bound(self):
        # The defect class, caught structurally: a ported module referencing a name its imports do not carry.
        # `base64` failed this way and the blanket except hid it; a second, `_MAX_PR_PAGES`, was dead code.
        import ast
        import builtins
        tree = ast.parse(inspect.getsource(erase))
        loaded = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        module_level = set(vars(erase)) | set(dir(builtins))
        local = {a.arg for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) for a in n.args.args}
        local |= {a.arg for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) for a in n.args.kwonlyargs}
        local |= {t.id for n in ast.walk(tree) for t in ast.walk(n)
                  if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store)}
        # names bound inside a function: a lazy `import x`, and an `except ... as exc`
        local |= {(a.asname or a.name).split(".")[0]
                  for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        local |= {n.name for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name}
        unbound = sorted(loaded - module_level - local)
        self.assertEqual(unbound, [], f"erase.py uses names it never binds: {unbound}")
