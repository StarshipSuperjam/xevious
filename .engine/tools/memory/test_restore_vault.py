"""test_restore_vault.py — memory's backup vault, the RESTORE path.

The REAL restore logic runs fully offline behind the in-module `_FakeVault` (the backup_vault precedent) — only
GitHub is faked. Each test redirects a throwaway ledger cabinet (ENGINE_MEMORY_DIR) AND a throwaway repo root
(validate.ROOT) so neither the real ledger nor the real committed pointer is ever touched. The round-trip pushes
through `backup_vault` and reads back through `restore_vault`, so the two halves are proven against each other.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools
import validate  # noqa: E402
from memory import backup_vault as bv  # noqa: E402
from memory import index  # noqa: E402
from memory import ledger  # noqa: E402
from memory import ledger_migrations as lm  # noqa: E402
from memory import restore_vault as rv  # noqa: E402


def _rb(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class _Base(unittest.TestCase):
    def setUp(self):
        self._root = tempfile.TemporaryDirectory()
        self._cab = tempfile.TemporaryDirectory()
        self._old_root = validate.ROOT
        self._old_engine = getattr(validate, "ENGINE_DIR", None)
        validate.ROOT = self._root.name
        validate.ENGINE_DIR = os.path.join(self._root.name, ".engine")
        os.makedirs(validate.ENGINE_DIR, exist_ok=True)
        with open(os.path.join(validate.ENGINE_DIR, "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"engine_release": "1.2.3"}, fh)
        os.environ["ENGINE_MEMORY_DIR"] = self._cab.name
        self._old_slug = bv._project_slug
        bv._project_slug = lambda: "test-org/test-project"          # hermetic project slug

    def tearDown(self):
        validate.ROOT = self._old_root
        if self._old_engine is not None:
            validate.ENGINE_DIR = self._old_engine
        os.environ.pop("ENGINE_MEMORY_DIR", None)
        bv._project_slug = self._old_slug
        self._root.cleanup()
        self._cab.cleanup()

    def _seed_and_backup(self, notes):
        """Plant `notes` into the throwaway ledger and back them up into a fresh _FakeVault. Returns the fake."""
        fake = bv._FakeVault()
        for n in notes:
            bv._demo_plant(n)
        res = bv.setup(scope="shared", transport=fake.transport, consent="y")
        self.assertTrue(res.get("ok"))
        return fake

    def _wipe_local(self):
        rv._quiet_remove(ledger.ledger_path())
        rv._quiet_remove(ledger.meta_path())


class RoundTripTests(_Base):
    def test_round_trip_restores_identical_and_searchable(self):
        fake = self._seed_and_backup(["banner ships in the spring release", "never deploy on a friday ZQXWORD"])
        original = _rb(ledger.ledger_path())
        self.assertEqual(len(index.query("zqxword").records), 1)     # searchable before
        self._wipe_local()
        self.assertTrue(rv._local_structurally_empty())
        res = rv.restore_now(transport=fake.transport, consent="y", github=None)
        self.assertTrue(res["ok"])
        self.assertTrue(res["restored"])
        self.assertEqual(_rb(ledger.ledger_path()), original)   # byte-identical
        self.assertEqual(len(index.query("zqxword").records), 1)     # searchable again via the rebuilt index

    def test_a_large_ledger_round_trips_via_git_data(self):
        notes = [f"note number {i} " + "x" * 200 for i in range(60)]  # well past the Contents API's ~1MB-or-bust path
        fake = self._seed_and_backup(notes)
        original = _rb(ledger.ledger_path())
        self.assertGreater(len(original), 12_000)
        self._wipe_local()
        res = rv.restore_now(transport=fake.transport, consent="y", github=None)
        self.assertTrue(res["ok"])
        self.assertEqual(_rb(ledger.ledger_path()), original)


class IntegrityTests(_Base):
    def test_fetch_blob_rejects_a_sha_mismatch_but_accepts_the_real_id(self):
        def bad(method, path, body=None):
            return 200, {"sha": "deadbeef", "content": base64.b64encode(b"hello").decode("ascii"),
                         "encoding": "base64"}
        self.assertIsNone(rv._fetch_blob(bv._Boundary(bad), "o", "r", "deadbeef"))   # bytes don't hash to the id
        real = bv._git_blob_sha1(b"hello")

        def good(method, path, body=None):
            return 200, {"sha": real, "content": base64.b64encode(b"hello").decode("ascii"),
                         "encoding": "base64", "size": 5}
        self.assertEqual(rv._fetch_blob(bv._Boundary(good), "o", "r", real), b"hello")

    def test_a_torn_backup_is_rejected_and_nothing_is_swapped(self):
        bv._demo_plant("keep me safe")                              # a good local ledger that must NOT be clobbered
        before = _rb(ledger.ledger_path())
        torn = b'{"kind":"a","text":"ok"}\n{"kind":"b","text":"tor'   # final line lacks a newline => torn
        orig = rv.fetch_snapshot
        rv.fetch_snapshot = lambda **k: {"ok": True, "error": None, "ledger_bytes": torn,
                                         "manifest": {"ledger-version": 1, "ledger-generation": 0,
                                                      "timestamp": "t", "engine-version": "x"},
                                         "owner": "o", "repo": "r", "namespace": "n"}
        try:
            res = rv.restore_now(consent="y", override=True, github=None)   # override past the resurrection guard
        finally:
            rv.fetch_snapshot = orig
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "corrupt")
        self.assertEqual(_rb(ledger.ledger_path()), before)  # the good local ledger survived

    def test_format_version_mismatch_is_declined_in_plain_words(self):
        orig = rv.fetch_snapshot
        rv.fetch_snapshot = lambda **k: {"ok": True, "error": None, "ledger_bytes": b'{"kind":"x"}\n',
                                         "manifest": {"ledger-version": 2, "ledger-generation": 0,
                                                      "timestamp": "t", "engine-version": "x"},
                                         "owner": "o", "repo": "r", "namespace": "n"}
        try:
            res = rv.restore_now(consent="y", github=None)
        finally:
            rv.fetch_snapshot = orig
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "version-mismatch")
        for banned in ("http", "git", "ledger", "index", "blob"):
            self.assertNotIn(banned, res["message"].lower())
        # The old copy admitted the fix "isn't built yet" — an under-build admission. The honest degrade names
        # the consequence and one action instead, never a not-built-yet claim.
        for stale in ("isn't built", "built yet", "later slice", "not yet built"):
            self.assertNotIn(stale, res["message"].lower())

    def test_a_bridgeable_older_shape_is_carried_forward_and_restored(self):
        # A fixture-registered step proves the routing is a LIVE mechanism: an older-shaped backup is carried
        # up to the current shape in memory, then restored normally. (No such step ships in v1.)
        self._wipe_local()                                          # empty local -> the resurrection guard is skipped
        def _to_current(b):
            return b.replace(b'"kind":"old"', b'"kind":"new"')
        orig = rv.fetch_snapshot
        rv.fetch_snapshot = lambda **k: {"ok": True, "error": None, "ledger_bytes": b'{"kind":"old","text":"hi"}\n',
                                         "manifest": {"ledger-version": 0, "ledger-generation": 0,
                                                      "timestamp": "t", "engine-version": "x"},
                                         "owner": "o", "repo": "r", "namespace": "n"}
        try:
            with mock.patch.dict(lm._REGISTRY, {(0, ledger.LEDGER_FORMAT_VERSION): _to_current}, clear=False):
                res = rv.restore_now(consent="y", github=None)
        finally:
            rv.fetch_snapshot = orig
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["restored"])
        self.assertIn(b'"kind":"new"', _rb(ledger.ledger_path()))   # the transform ran before the swap

    def test_a_malformed_backup_version_declines_and_leaves_local_unchanged(self):
        # A missing/malformed version routes to the migrations home, which declines by default; local is never
        # touched and nothing raises. `True` is the coercion trap (True == 1) — it must still decline.
        bv._demo_plant("keep me safe")
        before = _rb(ledger.ledger_path())
        for bad in (None, "two", {"x": 1}, True):
            orig = rv.fetch_snapshot
            rv.fetch_snapshot = lambda _bad=bad, **k: {"ok": True, "error": None, "ledger_bytes": b'{"kind":"x"}\n',
                                                       "manifest": {"ledger-version": _bad, "ledger-generation": 0,
                                                                    "timestamp": "t", "engine-version": "x"},
                                                       "owner": "o", "repo": "r", "namespace": "n"}
            try:
                res = rv.restore_now(consent="y", github=None)
            finally:
                rv.fetch_snapshot = orig
            self.assertFalse(res["ok"], bad)
            self.assertEqual(res["error"], "version-mismatch", bad)
        self.assertEqual(_rb(ledger.ledger_path()), before)         # local survived every malformed attempt

    def test_a_failing_migration_step_declines_distinctly_and_leaves_local_unchanged(self):
        # A registered step that RAISES is a bug in that step, not a "no path" case: it declines with a distinct
        # error code (migration-failed), leaves local memory unchanged, and never crashes the restore.
        bv._demo_plant("keep me safe")
        before = _rb(ledger.ledger_path())
        def _boom(_raw):
            raise RuntimeError("a broken migration step")
        orig = rv.fetch_snapshot
        rv.fetch_snapshot = lambda **k: {"ok": True, "error": None, "ledger_bytes": b'{"kind":"x"}\n',
                                         "manifest": {"ledger-version": 0, "ledger-generation": 0,
                                                      "timestamp": "t", "engine-version": "x"},
                                         "owner": "o", "repo": "r", "namespace": "n"}
        try:
            with mock.patch.dict(lm._REGISTRY, {(0, ledger.LEDGER_FORMAT_VERSION): _boom}, clear=False):
                res = rv.restore_now(consent="y", github=None)
        finally:
            rv.fetch_snapshot = orig
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "migration-failed")          # distinct from a genuine "no path" decline
        self.assertEqual(_rb(ledger.ledger_path()), before)         # a failed step never touches local memory


class ResurrectionTests(_Base):
    def test_an_older_backup_is_surfaced_and_declined_but_fresh_proceeds(self):
        fake = self._seed_and_backup(["alpha note", "beta note"])  # local non-empty; backup generation is 0
        ledger.set_generation(10)                                  # pretend this machine compacted/erased since
        before = _rb(ledger.ledger_path())
        calls = []
        orig = rv.surface_resurrection
        rv.surface_resurrection = lambda *a, **k: calls.append((a, k))
        try:
            res = rv.restore_now(transport=fake.transport, consent="y")
        finally:
            rv.surface_resurrection = orig
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "resurrection")
        self.assertEqual(_rb(ledger.ledger_path()), before)  # untouched
        self.assertTrue(calls)                                              # surfaced through the open-findings path
        # A fresh/empty local is the normal recovery case — it proceeds.
        self._wipe_local()
        res2 = rv.restore_now(transport=fake.transport, consent="y", github=None)
        self.assertTrue(res2["ok"])

    def test_a_non_empty_ledger_with_an_unknown_generation_is_treated_as_resurrection(self):
        fake = self._seed_and_backup(["only note"])
        rv._quiet_remove(ledger.meta_path())                       # wipe the sidecar -> generation UNKNOWN
        self.assertFalse(rv._generation_known())
        res = rv.restore_now(transport=fake.transport, consent="y", github=None)
        self.assertEqual(res["error"], "resurrection")             # can't prove it's not a resurrection -> decline

    def test_an_explicit_override_restores_an_older_backup(self):
        fake = self._seed_and_backup(["alpha"])
        ledger.set_generation(10)
        res = rv.restore_now(transport=fake.transport, consent="y", override=True, github=None)
        self.assertTrue(res["ok"])
        self.assertTrue(res["restored"])


class OfferAndConsentTests(_Base):
    def test_offer_only_when_a_backup_is_configured_and_local_is_empty(self):
        self.assertIsNone(rv.detect_restore_offer())               # unconfigured + empty -> no offer
        fake = self._seed_and_backup(["a note"])
        self.assertIsNone(rv.detect_restore_offer())               # configured but populated -> no offer
        rv._quiet_remove(ledger.ledger_path())
        self.assertTrue(rv.detect_restore_offer())                 # configured + empty -> offer
        _ = fake

    def test_consent_no_leaves_local_unchanged(self):
        fake = self._seed_and_backup(["note one"])
        self._wipe_local()
        res = rv.restore_now(transport=fake.transport, consent="n", github=None)
        self.assertTrue(res.get("declined"))
        self.assertTrue(rv._local_structurally_empty())            # nothing was restored

    def test_consent_prompt_names_the_overwrite_flatly(self):
        prompt = rv._restore_consent_prompt(42, 40)
        self.assertIn("42", prompt)
        self.assertIn("40", prompt)
        self.assertIn("will be gone", prompt)


class Floor4Tests(_Base):
    def test_not_configured_is_a_plain_message_not_a_raise(self):
        res = rv.restore_now(transport=bv._FakeVault().transport, consent="y", github=None)
        self.assertEqual(res["error"], "not-configured")
        self.assertFalse(res["restored"])

    def test_unreachable_fetch_is_floor4_and_never_raises(self):
        self._seed_and_backup(["x"])                               # configures the pointer
        res = rv.restore_now(transport=lambda *a, **k: (None, None), consent="y", github=None)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "unreachable")
        for banned in ("http", "traceback", "exception", "ledger", "index"):
            self.assertNotIn(banned, res["message"].lower())


class MigrationRevertTests(_Base):
    """Restore the local ledger from a retained PRE-migration snapshot TAG (the migration-revert path)."""

    def _snapshot_tag(self, fake, migration_id="mod@1.0.0"):
        """Mint a retained pre-migration snapshot tag over the CURRENT ledger; return its name."""
        snap = bv.snapshot_for_migration("recall-ledger", "9.9.9", migration_id=migration_id, transport=fake.transport)
        self.assertIsNotNone(snap)                                  # a real, addressable retained snapshot exists
        return snap["tag"]

    def test_restores_pre_migration_memory_byte_identical_from_the_tag(self):
        fake = self._seed_and_backup(["pre-update plan is locked PLUMBUS"])
        ledger.set_generation(3)                                    # the pre-migration generation the snapshot carries
        pre = _rb(ledger.ledger_path())
        tag = self._snapshot_tag(fake)
        bv._demo_plant("post-update row the new schema added GRUMBO")   # the migration reshapes the live store
        bv.push_now(transport=fake.transport)                      # the routine rolling backup over the reshaped store
        self.assertEqual(len(index.query("grumbo").records), 1)    # the reshaped store has the new row
        res = rv.restore_pre_migration(tag=tag, transport=fake.transport, consent="y", github=None)
        self.assertTrue(res["ok"])
        self.assertTrue(res["restored"])
        self.assertEqual(_rb(ledger.ledger_path()), pre)           # the TRUE pre-update memory is back (retained tag,
        self.assertEqual(len(index.query("grumbo").records), 0)    # not the rolling head the routine backup clobbered)
        self.assertEqual(len(index.query("plumbus").records), 1)

    def test_a_large_pre_migration_ledger_round_trips_from_the_tag(self):
        fake = self._seed_and_backup([f"note {i} " + "y" * 200 for i in range(60)])   # past the ~1MB path
        ledger.set_generation(2)
        pre = _rb(ledger.ledger_path())
        self.assertGreater(len(pre), 12_000)
        tag = self._snapshot_tag(fake)
        bv._demo_plant("a later reshape note")
        res = rv.restore_pre_migration(tag=tag, transport=fake.transport, consent="y", github=None)
        self.assertTrue(res["ok"])
        self.assertEqual(_rb(ledger.ledger_path()), pre)

    def test_missing_snapshot_tag_is_a_distinct_plain_message_never_silent(self):
        fake = self._seed_and_backup(["a note that must survive a missing-snapshot restore"])
        before = _rb(ledger.ledger_path())
        res = rv.restore_pre_migration(tag="engine-snapshot/recall/does-not-exist",
                                       transport=fake.transport, consent="y", github=None)
        self.assertFalse(res["ok"])
        self.assertFalse(res["restored"])
        self.assertEqual(res["error"], "snapshot-missing")         # NOT collapsed into no-backup-data
        self.assertEqual(res["message"], rv._MSG_SNAPSHOT_MISSING)
        self.assertNotIn(res["message"], (rv._MSG_NO_BACKUP_DATA, rv._MSG_UNREACHABLE))   # distinct from both
        self.assertEqual(_rb(ledger.ledger_path()), before)        # nothing on this computer changed
        for banned in ("tag", "ref", "generation", "http", "git", "ledger", "index"):
            self.assertNotIn(banned, res["message"].lower())       # plain handle, no engineer-jargon

    def test_empty_tag_argument_is_refused_as_snapshot_missing(self):
        fake = self._seed_and_backup(["x"])
        res = rv.restore_pre_migration(tag="   ", transport=fake.transport, consent="y", github=None)
        self.assertEqual(res["error"], "snapshot-missing")
        self.assertEqual(res["message"], rv._MSG_SNAPSHOT_MISSING)

    def test_legitimate_revert_with_unchanged_generation_proceeds(self):
        # No erasure-compaction in the revert window: the pre-migration generation == local generation, so the
        # resurrection guard does NOT fire on a routine revert.
        fake = self._seed_and_backup(["pre-update note"])
        ledger.set_generation(5)
        tag = self._snapshot_tag(fake)
        bv._demo_plant("a reshaped row")                           # a non-compacting reshape leaves generation at 5
        res = rv.restore_pre_migration(tag=tag, transport=fake.transport, consent="y", github=None)
        self.assertTrue(res["ok"])
        self.assertTrue(res["restored"])

    def test_resurrection_guard_fires_when_an_erasure_advanced_the_local_generation(self):
        # An erasure-compaction ran in the revert window: local generation > the snapshot's -> SURFACED, not applied.
        fake = self._seed_and_backup(["pre-update note"])
        ledger.set_generation(3)
        tag = self._snapshot_tag(fake)
        before = _rb(ledger.ledger_path())
        ledger.set_generation(12)                                  # an erasure bumped the local generation since
        calls = []
        orig = rv.surface_resurrection
        rv.surface_resurrection = lambda *a, **k: calls.append((a, k))
        try:
            res = rv.restore_pre_migration(tag=tag, transport=fake.transport, consent="y")
        finally:
            rv.surface_resurrection = orig
        self.assertEqual(res["error"], "resurrection")
        self.assertEqual(_rb(ledger.ledger_path()), before)        # untouched
        self.assertTrue(calls)                                      # surfaced through the open-findings path
        res2 = rv.restore_pre_migration(tag=tag, transport=fake.transport, consent="y", override=True, github=None)
        self.assertTrue(res2["ok"])                                 # an explicit override still restores it

    def test_restore_now_behavior_is_preserved_after_the_shared_core_refactor(self):
        # The extracted _restore_from_fetch must keep restore_now's rolling-head behavior byte-identical.
        fake = self._seed_and_backup(["a rolling-backup note WICKET"])
        original = _rb(ledger.ledger_path())
        self._wipe_local()
        res = rv.restore_now(transport=fake.transport, consent="y", github=None)
        self.assertTrue(res["ok"])
        self.assertEqual(_rb(ledger.ledger_path()), original)


class MigrationRevertDetectorTests(_Base):
    """The OFFLINE code-older-than-data detector + the whole-update-undo lifecycle (#303)."""

    def _set_running(self, version):
        with open(os.path.join(validate.ENGINE_DIR, "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"engine_release": version}, fh)

    def _floor(self, fake, version, migration_id="core@0.0.0"):
        """Mint the upgrade's reversibility-floor snapshot (stamped) over the current ledger; return its tag."""
        snap = bv.snapshot_for_migration("recall-ledger", version, migration_id=migration_id,
                                         reversibility_floor=True, transport=fake.transport)
        self.assertIsNotNone(snap)
        return snap["tag"]

    def test_no_stamp_means_no_offer(self):
        self._seed_and_backup(["a note"])
        self.assertIsNone(bv.read_migration_stamp())
        self.assertIsNone(rv.detect_migration_revert(github=None))

    def test_no_offer_when_the_code_is_at_or_ahead_of_the_data(self):
        fake = self._seed_and_backup(["a note"])
        self._set_running("2.0.0")
        self._floor(fake, "2.0.0")                                  # data migrated by the SAME running version
        self.assertIsNone(rv.detect_migration_revert(github=None))

    def test_offer_when_the_store_is_ahead_of_the_code(self):
        fake = self._seed_and_backup(["a note"])
        self._set_running("1.0.0")                                  # code reverted to the old version
        tag = self._floor(fake, "2.0.0")                           # data was migrated by the newer version
        offer = rv.detect_migration_revert(github=None)
        self.assertIsNotNone(offer)
        self.assertEqual(offer["tag"], tag)                        # the floor tag rides as executor payload
        self.assertEqual(offer["stamped"], "2.0.0")
        self.assertEqual(offer["running"], "1.0.0")

    def test_no_false_fire_when_the_running_version_is_unreadable(self):
        fake = self._seed_and_backup(["a note"])
        self._floor(fake, "2.0.0")                                  # a real stamp exists
        os.remove(os.path.join(validate.ENGINE_DIR, "engine.json"))  # gone -> _engine_version() == "unknown" -> (0,)
        self.assertEqual(bv._engine_version(), "unknown")
        self.assertIsNone(rv.detect_migration_revert(github=None))  # a (0,) running version must NOT false-fire

    def test_online_promotes_the_durable_tracked_issue_with_a_plain_handle(self):
        fake = self._seed_and_backup(["a note"])
        self._set_running("1.0.0")
        self._floor(fake, "2.0.0")
        import module_manager
        calls = []
        orig = module_manager.surface_stamp_mismatch
        module_manager.surface_stamp_mismatch = lambda *a, **k: (calls.append((a, k)) or 7)
        try:
            offer = rv.detect_migration_revert(github=object())     # a truthy github -> the durable path runs
        finally:
            module_manager.surface_stamp_mismatch = orig
        self.assertIsNotNone(offer)
        self.assertTrue(calls)                                      # the orphaned primitive now has a live caller
        restore_command = calls[0][0][3]                           # 4th positional arg
        for banned in ("refs/", "engine-snapshot/", "@"):
            self.assertNotIn(banned, restore_command)               # plain handle, never a raw tag/ref

    def test_whole_update_undo_restores_the_batch_floor_byte_identical(self):
        # #303: across a 3-migration upgrade, the stamp cites the FIRST snapshot (S0); the detector cites it and
        # restoring it brings back the TRUE pre-update memory (before A), not the before-C state.
        fake = self._seed_and_backup(["pre-update note ZORP"])
        ledger.set_generation(4)
        pre = _rb(ledger.ledger_path())
        self._set_running("1.0.0")
        s0 = self._floor(fake, "2.0.0", migration_id="core@0.0.0")          # the batch floor (stamped)
        bv._demo_plant("after migration A reshaped the store")
        bv.snapshot_for_migration("recall-ledger", "2.0.0", migration_id="core@0.1.0", transport=fake.transport)  # S1
        bv._demo_plant("after migration B reshaped the store")
        bv.snapshot_for_migration("recall-ledger", "2.0.0", migration_id="core@0.2.0", transport=fake.transport)  # S2
        offer = rv.detect_migration_revert(github=None)
        self.assertEqual(offer["tag"], s0)                                 # cites the batch floor, not the last step
        res = rv.restore_pre_migration(tag=offer["tag"], transport=fake.transport, consent="y", github=None)
        self.assertTrue(res["ok"])
        self.assertEqual(_rb(ledger.ledger_path()), pre)                   # the true pre-update memory is back
        self.assertIsNone(bv.read_migration_stamp())                       # stamp cleared -> the offer self-clears

    def test_restore_clears_the_stamp_but_a_missing_snapshot_keeps_it(self):
        fake = self._seed_and_backup(["a note"])
        ledger.set_generation(2)                                    # a known generation so the restore isn't conservatively guarded
        self._set_running("1.0.0")
        tag = self._floor(fake, "2.0.0")
        miss = rv.restore_pre_migration(tag="engine-snapshot/recall/gone", transport=fake.transport,
                                        consent="y", github=None)
        self.assertEqual(miss["error"], "snapshot-missing")
        self.assertIsNotNone(bv.read_migration_stamp())                    # a gone tag keeps disclosing -> NOT cleared
        ok = rv.restore_pre_migration(tag=tag, transport=fake.transport, consent="y", github=None)
        self.assertTrue(ok["ok"])
        self.assertIsNone(bv.read_migration_stamp())                       # a real restore clears the stamp

    def test_restore_now_does_not_clear_a_migration_stamp(self):
        fake = self._seed_and_backup(["a rolling note"])
        ledger.set_generation(2)                                           # known generation -> a clean successful restore
        bv.push_now(transport=fake.transport)                             # refresh the rolling head at that generation
        self._set_running("1.0.0")
        self._floor(fake, "2.0.0")
        res = rv.restore_now(transport=fake.transport, consent="y", github=None)
        self.assertTrue(res["ok"])                                         # a genuine rolling restore...
        self.assertIsNotNone(bv.read_migration_stamp())                    # ...must not touch the migration stamp


class ResurrectionMessageLeakTests(unittest.TestCase):
    def test_the_resurrection_finding_carries_no_internals(self):
        finding = rv._resurrection_finding()
        text = (json.dumps(finding)).lower()
        for banned in ("generation", "ledger", "index", "sha", "http", "namespace"):
            self.assertNotIn(banned, text)


class DemoSelfCheckTests(_Base):
    def test_offline_demo_self_check_passes(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = rv._demo()
        self.assertEqual(rc, 0)


class TestReadDiagnostic(_Base):
    """Item 3 (#224): the engine-run test read that ENDS provisioning's turn-on. One message per fetch_snapshot
    code, each naming the exact fault + the one fix in plain words (Floor 4), never a git/HTTP error. It exercises
    the SAME fetch path the scheduled run uses, so a green result proves the committed pointer AND the token."""

    def test_ok_when_the_backup_is_readable(self):
        fake = self._seed_and_backup(["a decision I made"])
        out = rv.test_read(transport=fake.transport)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["error"])
        self.assertIn("Success", out["message"])

    def test_not_configured_names_the_pointer_file(self):
        out = rv.test_read(transport=bv._FakeVault().transport)         # no pointer written
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "not-configured")
        self.assertIn(bv.POINTER_REL, out["message"])                  # points at the exact file to commit

    def test_no_token_names_the_secret_to_set(self):
        self._seed_and_backup(["a note"])                              # a configured pointer is in place
        saved = bv._gh
        bv._gh = lambda transport=None: None                           # no access token resolves
        try:
            out = rv.test_read()
        finally:
            bv._gh = saved
        self.assertEqual(out["error"], "no-token")
        self.assertIn("MEMORY_VAULT_TOKEN", out["message"])            # names the secret to set

    def test_unreachable_steers_to_token_scope_and_repo(self):
        self._seed_and_backup(["a note"])
        out = rv.test_read(transport=lambda *a, **k: (None, None))     # configured, but the read can't open it
        self.assertEqual(out["error"], "unreachable")
        for cue in ("repository", "permission"):                       # the dominant fault: wrong scope/repo/perm
            self.assertIn(cue, out["message"].lower())

    def test_a_missing_project_folder_is_named_distinctly(self):
        fake = self._seed_and_backup(["a note"])
        p = bv.read_pointer()
        bv.write_pointer(p["owner"], p["repo"], p["branch"], "0" * 32)  # repoint at a folder that was never pushed
        out = rv.test_read(transport=fake.transport)
        self.assertEqual(out["error"], "namespace-missing")
        self.assertIn("no longer in it", out["message"])

    def test_no_diagnostic_message_leaks_a_git_or_http_error(self):
        for code, msg in rv._TEST_READ_MESSAGES.items():
            for banned in ("http", "404", "403", "git ", "exception", "traceback"):
                self.assertNotIn(banned, msg.lower(), f"{code} message leaked {banned!r}")


class NamespaceMissingTests(_Base):
    def test_a_now_missing_folder_in_a_populated_vault_is_a_distinct_finding(self):
        fake = self._seed_and_backup(["a note"])                       # project A's folder IS in the vault
        p = bv.read_pointer()
        bv.write_pointer(p["owner"], p["repo"], p["branch"], "0" * 32)  # repoint at a namespace that was never pushed
        snap = rv.fetch_snapshot(transport=fake.transport)             # ... so "my" folder is gone, others remain
        self.assertFalse(snap["ok"])
        self.assertEqual(snap["error"], "namespace-missing")          # distinct, NOT a silent "no backup yet"
        msg = rv._floor4_fetch("namespace-missing")
        self.assertIn("removed from the backup", msg)                 # consequence
        self.assertIn("set up the backup again", msg)                 # a recovery action
        for banned in ("namespace", "http", "git"):
            self.assertNotIn(banned, msg.lower())

    def test_a_fresh_vault_with_no_folders_stays_no_backup_data(self):
        fake = bv._FakeVault()
        fake.transport("POST", "/user/repos", {"name": "engine-memory-vault", "private": True, "auto_init": True})
        bv.write_pointer("demo-user", "engine-memory-vault", "main", "f" * 32)
        snap = rv.fetch_snapshot(transport=fake.transport)            # repo exists but holds NO project folders yet
        self.assertFalse(snap["ok"])
        self.assertEqual(snap["error"], "no-backup-data")            # "no backup yet", never "your folder was deleted"


class CoexistenceTests(_Base):
    def test_project_A_memory_survives_project_B_adopting_the_same_vault(self):
        """The headline shared-vault guarantee: a 2nd project adopting the shared vault never clobbers the first's folder."""
        fake = bv._FakeVault()
        bv._demo_plant("project A note ALPHAWORD")
        a_ledger = _rb(ledger.ledger_path())
        res_a = bv.setup(scope="shared", transport=fake.transport, consent="y")
        self.assertTrue(res_a["ok"])
        a_pointer = dict(bv.read_pointer())

        with tempfile.TemporaryDirectory() as cab_b:                  # project B: its own ledger, the SAME vault
            os.environ["ENGINE_MEMORY_DIR"] = cab_b
            os.remove(bv._pointer_path())                             # B starts unconfigured (its own pointer)
            bv._project_slug = lambda: "test-org/project-b"
            bv._demo_plant("project B note BETAWORD")
            res_b = bv.setup(scope="shared", transport=fake.transport, consent="y")
            self.assertTrue(res_b.get("adopted"))                    # B ADOPTED the existing vault
            self.assertNotEqual(res_b["namespace"], res_a["namespace"])   # ... with its OWN fresh id

        os.environ["ENGINE_MEMORY_DIR"] = self._cab.name             # back to project A
        bv.write_pointer(a_pointer["owner"], a_pointer["repo"], a_pointer["branch"], a_pointer["namespace"])
        self._wipe_local()
        res_r = rv.restore_now(transport=fake.transport, consent="y", github=None)
        self.assertTrue(res_r["ok"])
        self.assertEqual(_rb(ledger.ledger_path()), a_ledger)        # A's memory survived B adopting the vault

        bv.write_pointer("demo-user", "engine-memory-vault", "main", res_b["namespace"])   # and B's folder is really there
        snap_b = rv.fetch_snapshot(transport=fake.transport)
        self.assertTrue(snap_b["ok"])
        self.assertIn(b"BETAWORD", snap_b["ledger_bytes"])           # both projects coexist in the one vault


class SavedMemoryProjectionTests(_Base):
    """`read_saved_memory` — the audit's PURE read-projection of the backed-up memory into the durable saved
    beliefs the self-review reviews. The durable-belief SELECTION is single-sourced on memory's own authority:
    `forget.live_records` (drops markers, crashed-pass orphans, gist-superseded raws, the archived tier) +
    `records`' kind vocabulary (only episodic + gist are human-content beliefs). This proves the round-trip
    through the backup decodes and filters correctly — no reimplementation of memory's retirement semantics."""

    def _seed_records(self, records):
        for r in records:
            ledger.append(r)

    def test_projects_live_durable_beliefs_excluding_markers_and_superseded(self):
        import time
        from memory import records as rec
        now = int(time.time())
        b1, b2 = "b1" + "0" * 30, "b2" + "0" * 30      # b1 a consolidation pass, b2 a roll-up pass
        idA, idB, idC, idG = "a" * 32, "b" * 32, "c" * 32, "g" * 32
        self._seed_records([
            # three live episodics, all closed by b1's consolidation marker (so none is an orphan)
            {"v": 1, "kind": rec.EPISODIC_KIND, rec.RECORD_ID_KEY: idA, "session_id": "S", "ts": now - 10,
             "role": "decision", "text": "Chose the blue launch plan.", "tags": [rec.DEFAULT_EPISODIC_TAG],
             rec.BATCH_KEY: b1},
            {"v": 1, "kind": rec.EPISODIC_KIND, rec.RECORD_ID_KEY: idB, "session_id": "S", "ts": now - 1000,
             "role": "lesson", "text": "Never deploy on a Friday.", "tags": [rec.DEFAULT_EPISODIC_TAG],
             rec.BATCH_KEY: b1},
            {"v": 1, "kind": rec.EPISODIC_KIND, rec.RECORD_ID_KEY: idC, "session_id": "S", "ts": now - 2000,
             "role": "observation", "text": "Old raw note now living in a gist.",
             "tags": [rec.DEFAULT_EPISODIC_TAG], rec.BATCH_KEY: b1},
            {"v": 1, "kind": rec.MARKER_KIND, rec.RECORD_ID_KEY: "m" * 32, "session_id": "S", "ts": now - 5,
             "tags": [rec.MARKER_TAG], rec.BATCH_KEY: b1},                       # a provenance marker — excluded
            # a gist that rolled C up, and the markers that close the roll-up (so C is superseded, G is not orphan)
            {"v": 1, "kind": rec.GIST_KIND, rec.RECORD_ID_KEY: idG, "ts": now - 100, "role": "observation",
             "text": "Older notes rolled together.", "tags": [rec.GIST_TAG, rec.DEFAULT_EPISODIC_TAG],
             rec.BATCH_KEY: b2, rec.SOURCE_IDS_KEY: [idC]},
            {"v": 1, "kind": rec.SUPERSEDED_KIND, rec.RECORD_ID_KEY: "s" * 32, rec.TARGET_KEY: idC,
             rec.SUPERSEDED_BY_KEY: idG, rec.BATCH_KEY: b2, "ts": now - 100, "tags": []},
            {"v": 1, "kind": rec.ROLLUP_KIND, rec.RECORD_ID_KEY: "r" * 32, rec.BATCH_KEY: b2, "ts": now - 100,
             "tags": []},
            {"kind": rec.REINFORCEMENT_KIND, rec.TARGET_KEY: idA, "ts": now - 5},   # an access marker — excluded
            # a raw turn-delta: LIVE (recall surfaces it) but NOT a durable belief — only the kind filter drops
            # it, so it makes that filter load-bearing (a marker has no `text`, so the text-check would drop it).
            {"kind": "turn-delta", rec.RECORD_ID_KEY: "t" * 32, "role": "observation", "ts": now - 3,
             "text": "A raw in-the-moment note not yet summarized."},
        ])
        fake = bv._FakeVault()
        self.assertTrue(bv.setup(scope="shared", transport=fake.transport, consent="y").get("ok"))
        snap = rv.read_saved_memory(transport=fake.transport)

        self.assertTrue(snap["ok"], snap)
        texts = [b["text"] for b in snap["beliefs"]]
        self.assertIn("Chose the blue launch plan.", texts)               # a live episodic
        self.assertIn("Never deploy on a Friday.", texts)                 # a live episodic
        self.assertIn("Older notes rolled together.", texts)              # the gist (consolidated belief)
        self.assertNotIn("Old raw note now living in a gist.", texts)     # the superseded raw is dropped
        self.assertNotIn("A raw in-the-moment note not yet summarized.", texts)   # a live non-belief (kind filter)
        self.assertEqual(len(snap["beliefs"]), 3)                         # marker + reinforcement + turn-delta excluded
        self.assertEqual({b["kind"] for b in snap["beliefs"]}, {"episodic", "gist"})
        self.assertEqual(texts[0], "Chose the blue launch plan.")        # newest-first
        self.assertIsInstance(snap["as_of"], str)                        # the backup timestamp (point-in-time)

    def test_not_configured_returns_disclosure_without_touching_the_network(self):
        # No backup set up (no pointer written) -> not-configured BEFORE any transport call, so the audit can
        # disclose the gap with no network and no token (the default public-template path).
        def explode(*a, **k):
            raise AssertionError("the network must not be touched on the not-configured path")
        snap = rv.read_saved_memory(transport=explode)
        self.assertFalse(snap["ok"])
        self.assertEqual(snap["error"], "not-configured")
        self.assertIsNone(snap["beliefs"])
        self.assertIsNone(snap["as_of"])

    def test_an_unreachable_backup_degrades_cleanly(self):
        self.assertTrue(bv.setup(scope="shared", transport=bv._FakeVault().transport, consent="y").get("ok"))
        snap = rv.read_saved_memory(transport=lambda *a, **k: (None, None))   # configured, but the host is dead
        self.assertFalse(snap["ok"])
        self.assertEqual(snap["error"], "unreachable")
        self.assertIsNone(snap["beliefs"])


if __name__ == "__main__":
    unittest.main()
