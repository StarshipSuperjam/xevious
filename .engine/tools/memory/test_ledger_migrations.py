"""test_ledger_migrations.py — the home a restore routes through to carry an older-shaped backup forward.

The registry is empty in this version (no record-shape change has shipped), so every real resolve returns None
and the restore declines honestly. These tests pin that refuse-by-default safety AND prove the routing is a live
mechanism, not a stub: a fixture-registered step (injected in-process, never a public API) is found, ordered into
a chain, and applied. The injection is `mock.patch.dict` on the private registry — auto-restored, so nothing
leaks between tests or into production.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools
from memory import ledger_migrations as lm  # noqa: E402


class ResolveRefusesByDefaultTests(unittest.TestCase):
    def test_empty_registry_bridges_nothing(self):
        # The shipped state: no step exists, so a differing version can't be carried forward.
        self.assertIsNone(lm.resolve_ledger_migration(0, 1))
        self.assertIsNone(lm.resolve_ledger_migration(1, 2))

    def test_a_newer_backup_has_no_downgrade_path(self):
        with mock.patch.dict(lm._REGISTRY, {(0, 1): lambda b: b}, clear=False):
            # steps only go forward; a version-2 backup into a version-1 engine can't be walked down.
            self.assertIsNone(lm.resolve_ledger_migration(2, 1))

    def test_a_malformed_version_declines_and_never_raises(self):
        for bad in (None, "two", {"x": 1}, [1], True, 1.5):
            self.assertIsNone(lm.resolve_ledger_migration(bad, 1))

    def test_the_registry_is_empty_between_tests(self):
        # isolation: a prior test's patch must not leak.
        self.assertEqual(lm._REGISTRY, {})


class ResolveRoutesAndAppliesTests(unittest.TestCase):
    def test_a_registered_single_step_is_found_and_applied(self):
        def _to_v1(b):
            return b.replace(b'"kind":"old"', b'"kind":"new"')
        with mock.patch.dict(lm._REGISTRY, {(0, 1): _to_v1}, clear=False):
            chain = lm.resolve_ledger_migration(0, 1)
            self.assertEqual(chain, [_to_v1])
            out = lm.apply_ledger_migrations(b'{"kind":"old"}\n', chain)
            self.assertEqual(out, b'{"kind":"new"}\n')

    def test_a_multi_step_path_is_ordered_and_chained(self):
        def _a(b):
            return b + b"a"
        def _b(b):
            return b + b"b"
        with mock.patch.dict(lm._REGISTRY, {(0, 1): _a, (1, 2): _b}, clear=False):
            chain = lm.resolve_ledger_migration(0, 2)
            self.assertEqual(chain, [_a, _b])
            self.assertEqual(lm.apply_ledger_migrations(b"x", chain), b"xab")

    def test_apply_is_all_or_nothing_on_a_bad_transform(self):
        # a transform that returns something other than bytes raises, so the restore caller lands nothing.
        with self.assertRaises(TypeError):
            lm.apply_ledger_migrations(b"x", [lambda b: "not-bytes"])

    def test_apply_over_an_empty_chain_is_the_bytes_unchanged(self):
        self.assertEqual(lm.apply_ledger_migrations(b"same", []), b"same")


if __name__ == "__main__":
    unittest.main()
