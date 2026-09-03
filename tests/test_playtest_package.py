"""Guards for tools/playtest_package.py — the fidelity-gated handover.

Offline: the checkout, extract, citation, and build steps are stubbed. The one
behaviour under test is the gate — a failing reference check must stop the build,
and a build must run only after every check passes.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import playtest_package as pp  # noqa: E402
import reference_citations as rc  # noqa: E402


def _cit(ok: bool):
    c = rc.Citation("docs/spec/x.md", 1, "src/xevious_main.68k", "lbl", None, 1, 2, False, "`lbl` 1")
    return [], ([] if ok else [c])


class HandoverGate(unittest.TestCase):
    def test_unresolved_citation_blocks_the_build(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "Xevious.sb3"
            with mock.patch.object(pp.checkout, "ensure", return_value=Path(t)), \
                 mock.patch.object(pp, "_run") as run, \
                 mock.patch.object(pp.citations, "check", return_value=_cit(False)):
                with self.assertRaisesRegex(pp.HandoverError, "do not resolve"):
                    pp.package(Path(t), out)
                # extract --verify ran, but the build never did.
                labels = [c.args[0] for c in run.call_args_list]
                self.assertIn("reference_extract --verify", labels)
                self.assertNotIn("scratch_project build", labels)

    def test_missing_checkout_blocks_the_build(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "Xevious.sb3"
            with mock.patch.object(pp.checkout, "ensure",
                                   side_effect=pp.checkout.CheckoutError("upstream down")), \
                 mock.patch.object(pp, "_run") as run:
                with self.assertRaisesRegex(pp.HandoverError, "no verified reference checkout"):
                    pp.package(Path(t), out)
                run.assert_not_called()

    def test_build_runs_after_all_checks_pass(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "Xevious.sb3"
            out.write_bytes(b"PK\x03\x04 fake archive")
            with mock.patch.object(pp.checkout, "ensure", return_value=Path(t)), \
                 mock.patch.object(pp, "_run") as run, \
                 mock.patch.object(pp.citations, "check", return_value=_cit(True)):
                path, digest, _ = pp.package(Path(t), out)
                labels = [c.args[0] for c in run.call_args_list]
                self.assertEqual(labels, ["reference_extract --verify", "scratch_project build"])
                self.assertEqual(path, out)
                self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
