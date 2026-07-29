"""Tests for the shared optional-module catalog reader.

Verifies the single parse path both readers (the /engine-help index and the first-run walkthrough) share:
a normalized record per entry sorted by command then id; degrade-to-empty on absent / unreadable / malformed /
wrong-shaped input (never raises); a command-less entry (no verb) RELAYED with an empty verb, not dropped
(#254); non-dict items skipped; missing optional fields coerced; and the committed catalog (the default path)
read as the empty array it ships.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_catalog as mc  # noqa: E402


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestEntries(unittest.TestCase):
    def test_absent_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(mc.entries(os.path.join(d, "nope.json")), [])

    def test_malformed_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, "{ not valid json")
            self.assertEqual(mc.entries(p), [], "a damaged catalog narrows to nothing, never raises")

    def test_non_array_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, json.dumps({"modules": []}))
            self.assertEqual(mc.entries(p), [], "the catalog must be a top-level array; an object narrows")

    def test_scalar_top_level_returns_empty(self):
        # A top-level scalar (not even iterable as records) must narrow, never raise.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, "42")
            self.assertEqual(mc.entries(p), [], "a scalar catalog body narrows to nothing")

    def test_valid_entries_normalized_and_sorted_by_verb(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, json.dumps([
                {"id": "z-mod", "verb": "engine-zed", "description": "Z.",
                 "category": "Product Management", "status": "optional"},
                {"id": "a-mod", "verb": "engine-ay", "description": "A.",
                 "category": "Verification & Validation"},
            ]))
            got = mc.entries(p)
            self.assertEqual([e["verb"] for e in got], ["engine-ay", "engine-zed"], "sorted by command")
            self.assertEqual(got[0], {"id": "a-mod", "verb": "engine-ay", "description": "A.",
                                      "category": "Verification & Validation", "status": ""},
                             "missing optional fields coerce to empty string; all fields present")

    def test_entry_without_verb_is_kept(self):
        # A command-less optional module (no verb) is RELAYED with an empty verb — the setup walkthrough
        # offers it by description; /engine-help filters it out at that reader, not here (#254). It sorts
        # first because an empty verb precedes any command.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, json.dumps([{"id": "no-verb", "description": "has no command",
                                   "category": "Verification & Validation"},
                                  {"id": "has-cmd", "verb": "engine-keep", "description": "kept",
                                   "category": "Product Management"}]))
            got = mc.entries(p)
            self.assertEqual([e["verb"] for e in got], ["", "engine-keep"],
                             "a command-less entry is kept (empty verb), not dropped, and sorts first")
            self.assertEqual(got[0], {"id": "no-verb", "verb": "", "description": "has no command",
                                      "category": "Verification & Validation", "status": ""},
                             "the command-less entry is fully relayed, verb coerced to empty string")

    def test_command_less_entries_sort_by_id(self):
        # Multiple command-less entries all share an empty verb; the id secondary key keeps them deterministic.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, json.dumps([{"id": "zeta", "description": "Z.", "category": "Verification & Validation"},
                                  {"id": "alpha", "description": "A.", "category": "Verification & Validation"}]))
            self.assertEqual([e["id"] for e in mc.entries(p)], ["alpha", "zeta"],
                             "command-less entries order by id when verbs tie on empty")

    def test_non_dict_items_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, json.dumps(["a string", 5, {"verb": "engine-ok", "description": "d"}]))
            self.assertEqual([e["verb"] for e in mc.entries(p)], ["engine-ok"])

    def test_committed_catalog_relays_the_shipped_optionals(self):
        # The committed catalog shipped empty until the first optional module was built; it now relays
        # github-projects-sync as a normalized entry (the default path reads the real committed catalog).
        board = [e for e in mc.entries() if e["id"] == "github-projects-sync"]
        self.assertEqual(len(board), 1, "the committed catalog relays the github-projects-sync entry")
        self.assertEqual(board[0]["verb"], "engine-board-setup")
        self.assertEqual(board[0]["category"], "Product Management")


if __name__ == "__main__":
    unittest.main()
