"""Unit tests for export.py — writing saved conversation to a file, and refusing to write it somewhere unsafe.

The destination guard is the load-bearing half, so most of what is here is about where an export may land
rather than what it says. Its failure mode is not a crash: it is a transcript sitting in a working tree until
something sweeps it into a commit, which no later fix retracts.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import export, forget, ledger, records  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self._tmp.name
        self.out = tempfile.TemporaryDirectory()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        self._tmp.cleanup()
        self.out.cleanup()

    def _session(self, session_id="s-1", turns=3, word="quokka"):
        for i in range(turns):
            ledger.append({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND,
                           records.RECORD_ID_KEY: records.new_record_id(), "session_id": session_id,
                           "seq": i, "speaker": "user" if i % 2 == 0 else "assistant", "ts": 1000 + i,
                           "text": f"message {i} about {word}"})


class DestinationGuardTests(_Base):
    def _repo(self) -> str:
        """A real throwaway git working tree, with an ignore rule, so the guard is exercised against git rather
        than against a stand-in that could agree with a wrong implementation."""
        root = tempfile.mkdtemp(dir=self.out.name)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("scratch/\n")
        return root

    def test_a_destination_inside_a_working_tree_is_refused(self):
        root = self._repo()
        with self.assertRaises(export.ExportRefused) as caught:
            export.write("conversation", os.path.join(root, "leaked.md"))
        self.assertIn("committed", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(root, "leaked.md")))   # and nothing was written

    def test_a_destination_the_project_already_ignores_is_allowed(self):
        root = self._repo()
        os.makedirs(os.path.join(root, "scratch"))
        written = export.write("conversation", os.path.join(root, "scratch", "ok.md"))
        self.assertTrue(os.path.exists(written))

    def test_a_destination_whose_folder_does_not_exist_yet_is_still_judged(self):
        # THE BYPASS THAT SHIPPED. `git rev-parse` was asked from the destination's own directory, so a folder
        # that did not exist yet made the question raise — and the failure was read as "not in a git project",
        # i.e. as permission. `exports/tuesday.md` is the shape an operator actually types, so the guard was
        # inverted on its friendliest path: writing beside the repo root refused, writing into a new folder
        # inside it succeeded, and `write` then created the folder and put the transcript there.
        root = self._repo()
        for missing in ("new-folder/leak.md", "a/b/c/leak.md"):
            with self.subTest(missing=missing):
                with self.assertRaises(export.ExportRefused):
                    export.write("conversation", os.path.join(root, missing))
                self.assertFalse(os.path.exists(os.path.join(root, missing.split("/")[0])))

    def test_a_symlink_pointing_into_a_working_tree_is_refused(self):
        # The guard judged the path it was handed, not the place the bytes land: a destination that is itself
        # a symlink into a repository was judged by its parent — outside the tree, therefore allowed — and the
        # write then followed the link inside.
        root = self._repo()
        link = os.path.join(self.out.name, "looks-harmless.md")
        os.symlink(os.path.join(root, "leaked.md"), link)
        with self.assertRaises(export.ExportRefused):
            export.write("conversation", link)
        self.assertFalse(os.path.exists(os.path.join(root, "leaked.md")))

    def test_an_unanswerable_question_refuses_rather_than_permitting(self):
        # The stated failure direction, asserted rather than trusted: if git cannot be consulted at all there
        # is no way to know whether the path would be committed, and "don't know" must never read as "safe".
        root = self._repo()
        with mock.patch.object(export.subprocess, "run", side_effect=OSError("git is not here")):
            with self.assertRaises(export.ExportRefused) as caught:
                export.write("conversation", os.path.join(root, "anywhere.md"))
        self.assertIn("could not be consulted", str(caught.exception))

    def test_a_destination_outside_any_working_tree_is_allowed(self):
        written = export.write("conversation", os.path.join(self.out.name, "anywhere.md"))
        self.assertTrue(os.path.exists(written))

    def test_a_blank_destination_is_refused(self):
        for bad in ("", "   ", None):
            with self.assertRaises(export.ExportRefused):
                export.write("conversation", bad)


class ContentTests(_Base):
    def test_a_session_export_carries_the_conversation_and_the_caveats(self):
        self._session(turns=4)
        text = export.session_markdown("s-1")
        self.assertIn("message 0 about quokka", text)
        self.assertIn("message 3 about quokka", text)
        self.assertIn("**Operator**", text)
        self.assertIn("**Assistant**", text)
        # A file outlives the session that made it and will be read by someone with none of its context.
        self.assertIn("as it was captured", text)
        self.assertIn("masked on the way in", text)

    def test_a_withheld_conversation_exports_nothing(self):
        # The control has to mean the same thing on every path, and an export is the path where being wrong
        # produces a durable artefact rather than a transient answer.
        self._session()
        forget.withhold(session_id="s-1")
        text = export.session_markdown("s-1")
        self.assertNotIn("about quokka", text)
        self.assertIn("withheld", text)

    def test_a_search_export_reads_through_the_same_seam_as_recall(self):
        self._session(word="marzipan")
        from memory import index

        index.rebuild()
        text = export.search_markdown("marzipan")
        self.assertIn("message 0 about marzipan", text)
        self.assertIn("Saved memory", text)

    def test_a_search_export_can_be_scoped_to_one_conversation(self):
        self._session(session_id="s-A", word="shared")
        self._session(session_id="s-B", word="shared")
        from memory import index

        index.rebuild()
        scoped = export.search_markdown("shared", session="s-B", limit=50)
        self.assertIn("s-B", scoped)
        self.assertNotIn("s-A", scoped)

    def test_an_empty_search_says_so_rather_than_writing_a_blank_file(self):
        self._session()
        from memory import index

        index.rebuild()
        self.assertIn("No saved memory matched", export.search_markdown("zzzqqx"))

    def test_a_session_export_is_not_capped_at_the_window_readers_ceiling(self):
        # The window reader stops at 200 turns so a huge session cannot flood a live session's context. A file
        # has no context to flood, and a quarter of real sessions are longer than that — so borrowing the
        # ceiling here would hand back a fraction of a conversation and look complete.
        from memory import recall

        self._session(session_id="s-long", turns=recall.MAX_TURNS_CEILING + 25)
        text = export.session_markdown("s-long")
        self.assertIn(f"message {recall.MAX_TURNS_CEILING + 24} about", text)


if __name__ == "__main__":
    unittest.main()
