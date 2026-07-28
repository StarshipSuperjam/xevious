"""test_backup_vault.py — memory's backup vault, EXPORT path.

The REAL backup logic runs fully offline behind the module's own injected `_FakeVault` transport (the
erasure _FakeGH precedent) — only GitHub is faked. Each test redirects a throwaway ledger cabinet
(ENGINE_MEMORY_DIR) AND a throwaway repo root (validate.ROOT) so neither the real ledger nor the real
committed pointer is ever touched.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools
import validate  # noqa: E402
from memory import backup_vault as bv  # noqa: E402
from memory import ledger  # noqa: E402


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
        bv._project_slug = lambda: "test-org/test-project"   # hermetic project slug

    def tearDown(self):
        validate.ROOT = self._old_root
        if self._old_engine is not None:
            validate.ENGINE_DIR = self._old_engine
        os.environ.pop("ENGINE_MEMORY_DIR", None)
        bv._project_slug = self._old_slug
        self._root.cleanup()
        self._cab.cleanup()


class ManifestTests(_Base):
    def test_manifest_has_exactly_the_four_locked_keys(self):
        ledger.append({"kind": "turn-delta", "text": "x"})
        m = bv.build_manifest(ledger_path=ledger.ledger_path())
        self.assertEqual(set(m), {"ledger-version", "ledger-generation", "timestamp", "engine-version"})
        self.assertEqual(m["ledger-version"], ledger.LEDGER_FORMAT_VERSION)
        self.assertEqual(m["engine-version"], "1.2.3")

    def test_manifest_generation_is_populated_live_and_tracks_bump(self):
        self.assertEqual(bv.build_manifest(ledger_path=ledger.ledger_path())["ledger-generation"], 0)
        ledger.bump_generation()
        self.assertEqual(bv.build_manifest(ledger_path=ledger.ledger_path())["ledger-generation"], 1)


class SetupTests(_Base):
    def test_consent_yes_creates_private_repo_and_writes_pointer(self):
        fake = bv._FakeVault()
        res = bv.setup(scope="shared", transport=fake.transport, consent="y")
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("created"))
        self.assertEqual(len(fake.created), 1)
        self.assertFalse(fake.deleted)
        p = bv.read_pointer()
        self.assertIsNotNone(p)
        self.assertTrue(bv._setup_done())
        self.assertEqual(p["repo"], res["repo"])
        self.assertEqual(p["repo"], "engine-memory-vault")              # shared is the default
        self.assertNotEqual(p["namespace"], "test-project")            # namespace is a MINTED id, not the project name
        self.assertRegex(p["namespace"], r"^[0-9a-f]{32}$")            # a uuid4 hex (mirrors records.new_record_id)
        for k in ("owner", "repo", "branch", "namespace", "created_at"):
            self.assertTrue(p[k])

    def test_consent_no_creates_nothing(self):
        fake = bv._FakeVault()
        res = bv.setup(scope="shared", transport=fake.transport, consent="n")
        self.assertTrue(res.get("declined"))
        self.assertEqual(fake.created, [])
        self.assertIsNone(bv.read_pointer())
        self.assertFalse(bv._setup_done())

    def test_missing_repo_scope_discloses_and_creates_nothing(self):
        fake = bv._FakeVault(no_scope=True)
        res = bv.setup(scope="shared", transport=fake.transport, consent="y")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "no-scope")
        self.assertIn("gh auth refresh -s repo", res["message"])
        self.assertIsNone(bv.read_pointer())
        self.assertEqual(fake.created, [])

    def test_wrongly_public_create_is_deleted_and_disclosed(self):
        fake = bv._FakeVault(private=False)   # the create succeeds, but the verify GET reads it as public
        res = bv.setup(scope="shared", transport=fake.transport, consent="y")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "not-private")
        self.assertTrue(fake.deleted)                                  # the wrongly-public repo was removed
        self.assertIsNone(bv.read_pointer())
        for banned in ("http", "git", "status", "404", "403"):
            self.assertNotIn(banned, res["message"].lower())            # never a git/HTTP error


class DisclosureAndFlagTests(_Base):
    """#397: the non-interactive `disclosure` / `setup --scope/--consent` surface for the agent-mediated
    first-run. The tool stays the floor-1 disclosure home (single-homed on _choice_prompt/_consent_prompt); a
    flagged setup still EMITS that disclosure before it acts, so consent-before-create is code-surfaced."""

    def _run(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bv.main(argv)
        return rc, buf.getvalue()

    def test_parse_setup_flags(self):
        self.assertEqual(bv._parse_setup_flags(["--scope", "per-project", "--consent", "y"]),
                         {"scope": "per-project", "consent": "y"})
        self.assertEqual(bv._parse_setup_flags([]), {})

    def test_disclosure_no_scope_prints_the_choice(self):
        rc, out = self._run(["disclosure"])
        self.assertEqual(rc, 0)
        self.assertIn("SHARED BACKUP", out)
        self.assertIn("SEPARATE BACKUP", out)

    def test_disclosure_with_scope_names_the_destination_and_privacy(self):
        rc, out = self._run(["disclosure", "--scope", "shared"])
        self.assertEqual(rc, 0)
        self.assertIn("engine-memory-vault", out)                              # the shared destination is NAMED
        self.assertIn("Nothing leaves your computer until you say yes", out)
        self.assertIn("private", out.lower())

    def test_flagged_setup_emits_the_disclosure_then_declines_and_creates_nothing(self):
        fake = bv._FakeVault()
        orig = bv._gh
        bv._gh = lambda transport=None: bv._Boundary(fake.transport)
        try:
            rc, out = self._run(["setup", "--scope", "shared", "--consent", "n"])
        finally:
            bv._gh = orig
        self.assertEqual(rc, 0)
        self.assertIn("Nothing leaves your computer until you say yes", out)   # the tool emitted the disclosure...
        self.assertEqual(fake.created, [])                                     # ...and created nothing on decline
        self.assertIsNone(bv.read_pointer())

    def test_flagged_setup_yes_emits_disclosure_then_creates_the_chosen_destination(self):
        fake = bv._FakeVault()
        orig = bv._gh
        bv._gh = lambda transport=None: bv._Boundary(fake.transport)
        try:
            rc, out = self._run(["setup", "--scope", "per-project", "--consent", "y"])
        finally:
            bv._gh = orig
        self.assertEqual(rc, 0)
        self.assertIn("Nothing leaves your computer until you say yes", out)   # disclosure emitted first
        self.assertEqual(len(fake.created), 1)                                 # then the chosen repo is created
        p = bv.read_pointer()
        self.assertIsNotNone(p)
        self.assertNotEqual(p["repo"], "engine-memory-vault")                  # per-project, NOT the shared vault


class PointerCommitTests(_Base):
    """Item 1 (#224): setup records the configured pointer IN the project repo (pure GitHub API; the
    config-not-data carve-out) so a CI checkout can locate the vault — and soft-degrades, never raises, when the
    write is refused."""

    def _seed_project_repo(self, fake):
        slug = "test-org/test-project"                              # matches the hermetic _project_slug stub
        fake.repos[slug] = {"default_branch": "main"}
        sha = fake._next("b")                                       # the shipped committed placeholder -> a blob sha
        fake.blobs[sha] = base64.b64encode(b'{"schema_version": 1, "configured": false}\n').decode("ascii")
        fake.contents[f"{slug}@{bv.POINTER_REL}"] = sha

    def test_setup_records_the_configured_pointer_in_the_project_repo(self):
        fake = bv._FakeVault()
        self._seed_project_repo(fake)
        res = bv.setup(scope="shared", transport=fake.transport, consent="y")
        self.assertTrue(res["ok"])
        self.assertTrue(res["pointer_committed"])
        key = f"test-org/test-project@{bv.POINTER_REL}"
        self.assertIn(key, fake.contents)                          # the pointer was PUT to the PROJECT repo
        committed = json.loads(base64.b64decode(fake.blobs[fake.contents[key]]))
        self.assertEqual(committed["namespace"], res["namespace"])  # coordinates, the real minted namespace
        self.assertEqual(set(committed), {"schema_version", "owner", "repo", "branch", "namespace", "created_at"})
        for leaky in ("text", "kind", "role", "summary"):          # coordinates ONLY — never any ledger content
            self.assertNotIn(leaky, committed)

    def test_setup_soft_degrades_when_the_pointer_write_is_refused(self):
        fake = bv._FakeVault(refuse_pointer_put=True)              # e.g. a protected project default branch (409)
        self._seed_project_repo(fake)
        res = bv.setup(scope="shared", transport=fake.transport, consent="y")
        self.assertTrue(res["ok"])                                  # setup still succeeds — the vault IS set up
        self.assertFalse(res["pointer_committed"])
        self.assertIn(bv.POINTER_REL, res["message"])              # names the one residual step in plain words
        self.assertIsNotNone(bv.read_pointer())                    # the LOCAL pointer still stands

    def test_commit_pointer_degrades_when_project_repo_unreachable(self):
        fake = bv._FakeVault()                                      # project repo NOT seeded -> GET 404
        out = bv.commit_pointer_to_project(bv._Boundary(fake.transport), "test-org", "test-project",
                                           {"schema_version": 1, "owner": "o", "repo": "r", "branch": "main",
                                            "namespace": "n", "created_at": "t"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "no-default-branch")        # degraded cleanly, never raised


class PushTests(_Base):
    def test_ledger_pushed_via_git_data_not_contents(self):
        ledger.append({"kind": "turn-delta", "text": "hello"})
        fake = bv._FakeVault()
        bv.setup(scope="shared", transport=fake.transport, consent="y")              # setup pushes the first copy
        self.assertFalse(fake.pushed_ledger_via_contents)            # the ledger NEVER goes via the 1MB Contents API
        self.assertTrue(fake.blobs)                                  # it went via Git Data blobs

    def test_throttle_gates_on_last_success(self):
        self.assertTrue(bv._should_push(10_000))                    # no state -> push now
        bv._record_state(now=100_000, success=True, privacy_ok=True)
        self.assertFalse(bv._should_push(100_000 + 3600))           # 1h after a success -> skip
        self.assertTrue(bv._should_push(100_000 + 25 * 3600))       # >24h after success -> push
        self.assertTrue(bv._should_push(50_000))                    # a FUTURE last-success -> push now (never stuck off)
        bv._record_state(now=100_000 + 7200, success=False, privacy_ok=True)
        self.assertFalse(bv._should_push(100_000 + 3 * 3600))       # a FAILED push did not advance the throttle key

    def test_push_now_requires_setup(self):
        res = bv.push_now(transport=bv._FakeVault().transport)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "not-configured")


class SessionStartTests(_Base):
    def test_silent_no_op_and_no_network_before_setup(self):
        calls = {"n": 0}
        orig = bv.push_now
        bv.push_now = lambda **k: calls.__setitem__("n", calls["n"] + 1) or {"ok": True}
        try:
            decision = bv._session_start_handler({})
        finally:
            bv.push_now = orig
        self.assertEqual(decision, {"action": "proceed"})
        self.assertEqual(calls["n"], 0)                              # never reached the push (no pointer)

    def test_pushes_after_setup_then_silent_within_cooldown(self):
        fake = bv._FakeVault()
        bv.setup(scope="shared", transport=fake.transport, consent="y")             # records a success at ~now
        orig = bv._gh
        bv._gh = lambda transport=None: bv._Boundary(fake.transport)
        try:
            within = bv._session_start_handler({})                  # within cooldown -> no push
            self.assertEqual(within, {"action": "proceed"})
            ref_before = dict(fake.refs)
            elapsed = bv._session_start_handler({}, now=int(time.time()) + 100 * 3600)
            self.assertEqual(elapsed, {"action": "proceed"})        # a clean success injects nothing
            self.assertNotEqual(fake.refs, ref_before)              # a real push DID advance the vault branch
        finally:
            bv._gh = orig

    def test_privacy_flip_surfaced_once_then_silent(self):
        fake = bv._FakeVault()
        bv.setup(scope="shared", transport=fake.transport, consent="y")
        fake.private = False
        orig = bv._gh
        bv._gh = lambda transport=None: bv._Boundary(fake.transport)
        try:
            first = bv._session_start_handler({}, now=int(time.time()) + 100 * 3600)
            self.assertEqual(first["action"], "inject")
            self.assertIn("PUBLIC", first["context"])
            self.assertIn("Private", first["context"])
            second = bv._session_start_handler({}, now=int(time.time()) + 200 * 3600)
            self.assertEqual(second, {"action": "proceed"})         # already reported -> silent (no nag)
        finally:
            bv._gh = orig

    def test_push_failure_discloses_floor4_and_never_raises(self):
        fake = bv._FakeVault()
        bv.setup(scope="shared", transport=fake.transport, consent="y")
        fake.private = True
        fake.fail_blob = True                                       # the repo is reachable + private, the upload fails
        orig = bv._gh
        bv._gh = lambda transport=None: bv._Boundary(fake.transport)
        try:
            decision = bv._session_start_handler({}, now=int(time.time()) + 100 * 3600)
        finally:
            bv._gh = orig
        self.assertEqual(decision["action"], "inject")
        ctx = decision["context"]
        self.assertIn("back up memory now", ctx)
        for banned in ("http", "git", "422", "status", "traceback", "exception"):
            self.assertNotIn(banned, ctx.lower())


class PointerTests(_Base):
    def test_partial_pointer_reads_as_unconfigured(self):
        path = bv._pointer_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "owner": "o", "branch": "main", "namespace": "n"}, fh)  # no repo
        self.assertIsNone(bv.read_pointer())
        self.assertFalse(bv._setup_done())

    def test_placeholder_reads_as_unconfigured(self):
        path = bv._pointer_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "configured": False}, fh)
        self.assertIsNone(bv.read_pointer())

    def test_pointer_is_content_free(self):
        ledger.append({"kind": "turn-delta", "text": "RUMBLEDETHUMPS a secret private note"})
        fake = bv._FakeVault()
        bv.setup(scope="shared", transport=fake.transport, consent="y")
        blob = json.dumps(bv.read_pointer()).lower()
        self.assertNotIn("rumbledethumps", blob)
        self.assertNotIn("secret", blob)


class LeakGuardTests(unittest.TestCase):
    def test_commit_message_and_readme_carry_no_ledger_content(self):
        word = "rumbledethumps"
        self.assertNotIn(word, bv._COMMIT_MESSAGE.lower())
        self.assertNotIn(word, bv._readme_text("test-project").lower())


class DemoGuardTests(unittest.TestCase):
    def test_safe_demo_delete_only_targets_disposable_names(self):
        self.assertTrue(bv._safe_demo_delete("test-project-memvault-demo-abcd1234", "test-project"))
        self.assertFalse(bv._safe_demo_delete("test-project", "test-project"))                       # the project repo
        self.assertFalse(bv._safe_demo_delete("test-project-engine-memory-backup", "test-project"))  # per-project vault
        self.assertFalse(bv._safe_demo_delete("engine-memory-vault", "test-project"))                # the shared vault
        self.assertFalse(bv._safe_demo_delete("some-random-repo", "test-project"))                   # no marker


class DemoSelfCheckTests(unittest.TestCase):
    def test_offline_demo_self_check_passes(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            rc = bv._demo()
        self.assertEqual(rc, 0)

    def test_snapshot_demo_self_check_passes(self):
        # the retained-snapshot construction demo's every [ok]/[FAIL] check must hold (its declared fate: covered
        # by this permanent regression test — the retained snapshot survives a routine backup).
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(bv.snapshot_demo())


class SharedVaultScopeTests(_Base):
    def test_shared_is_the_default_and_names_the_one_vault(self):
        fake = bv._FakeVault()
        res = bv.setup(scope="shared", transport=fake.transport, consent="y")
        self.assertEqual(res["repo"], "engine-memory-vault")
        self.assertEqual(fake.created, ["demo-user/engine-memory-vault"])

    def test_per_project_scope_names_the_per_project_repo(self):
        fake = bv._FakeVault()
        res = bv.setup(scope="per-project", transport=fake.transport, consent="y")
        self.assertEqual(res["repo"], "test-project-engine-memory-backup")

    def test_default_scope_constant_is_shared(self):
        self.assertEqual(bv._DEFAULT_SCOPE, "shared")                       # shared is the recorded default

    def test_minted_namespace_is_an_opaque_id_not_the_project_name(self):
        a, b = bv._mint_namespace(), bv._mint_namespace()
        self.assertRegex(a, r"^[0-9a-f]{32}$")
        self.assertNotEqual(a, b)                                          # distinct per call (collision-free)
        self.assertNotIn("test-project", a)

    def test_shared_consent_discloses_co_location_and_why_per_repo(self):
        consent = bv._consent_prompt("engine-memory-vault", "shared")
        self.assertIn("every project's notes", consent)
        self.assertIn("expose every project at once", consent)             # the read + flip blast radius
        chooser = bv._choice_prompt()
        self.assertIn("more private than your others", chooser)            # a concrete why-per-repo, not just a consequence

    def test_shared_readme_carries_marker_explains_folders_and_delete_cost(self):
        r = bv._readme_text("anything", "shared")
        self.assertTrue(r.startswith(bv._VAULT_README_MARKER))
        self.assertIn("unique id", r)                                     # the folder ids are stated accurately
        self.assertIn("loses that project's memory", r)                   # the delete-a-folder cost is named, not forbidden


class AdoptTests(_Base):
    def test_second_project_adopts_the_shared_vault_with_its_own_id(self):
        fake = bv._FakeVault()
        bv.setup(scope="shared", transport=fake.transport, consent="y")    # project A creates the vault
        ns_a = bv.read_pointer()["namespace"]
        self.assertEqual(fake.created, ["demo-user/engine-memory-vault"])
        os.remove(bv._pointer_path())                                      # a DIFFERENT project: its own pointer
        bv._project_slug = lambda: "test-org/other-project"
        res = bv.setup(scope="shared", transport=fake.transport, consent="y")   # project B adopts
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("adopted"))
        self.assertFalse(res.get("created"))
        self.assertEqual(fake.created, ["demo-user/engine-memory-vault"])  # B created NOTHING new
        self.assertFalse(fake.deleted)                                     # and deleted NOTHING (never touch an existing vault)
        ns_b = bv.read_pointer()["namespace"]
        self.assertNotEqual(ns_a, ns_b)                                    # B minted its OWN fresh id
        self.assertEqual(bv.read_pointer()["repo"], "engine-memory-vault")

    def test_foreign_lookalike_repo_is_not_colonized(self):
        fake = bv._FakeVault()
        fake.preseed("engine-memory-vault", "# not an engine repo\n")      # a same-named repo we did NOT create
        res = bv.setup(scope="shared", transport=fake.transport, consent="y")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "foreign-vault")
        self.assertFalse(fake.created)
        self.assertFalse(fake.deleted)                                     # never delete a foreign repo
        self.assertIsNone(bv.read_pointer())

    def test_existing_public_vault_is_refused_and_never_deleted(self):
        fake = bv._FakeVault(private=False)
        fake.preseed("engine-memory-vault", bv._readme_text("x", "shared"))
        res = bv.setup(scope="shared", transport=fake.transport, consent="y")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "adopt-public")
        self.assertFalse(fake.deleted)                                     # NEVER delete an existing vault (holds others)
        self.assertIn("PUBLIC", res["message"])
        self.assertIsNone(bv.read_pointer())

    def test_name_exists_race_re_probes_and_adopts(self):
        fake = bv._FakeVault()
        fake.preseed("engine-memory-vault", bv._readme_text("x", "shared"))   # exists, with our marker
        fake.hide_next_probe("engine-memory-vault")                           # first probe can't see it -> create 422s
        res = bv.setup(scope="shared", transport=fake.transport, consent="y")
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("adopted"))
        self.assertFalse(fake.created)                                     # POST 422'd; adopted, never a create-failed loop

    def test_name_exists_race_against_a_foreign_repo_is_refused(self):
        fake = bv._FakeVault()
        fake.preseed("engine-memory-vault", "# not an engine repo\n")      # a foreign same-named repo (no marker)
        fake.hide_next_probe("engine-memory-vault")                        # first probe misses -> create 422s
        res = bv.setup(scope="shared", transport=fake.transport, consent="y")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "foreign-vault")                    # the 422->re-probe path ALSO marker-checks
        self.assertFalse(fake.created)
        self.assertFalse(fake.deleted)

    def test_probe_transport_fault_creates_nothing(self):
        def faulty(method, path, body=None):
            if path == "/user":
                return 200, {"login": "demo-user"}
            return None, None                                              # every repo op faults
        res = bv.setup(scope="shared", transport=faulty, consent="y")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "unreachable")
        self.assertIsNone(bv.read_pointer())                              # never blind-create a possible duplicate


class ManifestVersionOverrideTests(_Base):
    """`engine_version` override on build_manifest — the snapshot seam stamps the migration-time version, not
    whatever `engine.json` happens to read; no override reads it live (no regression to existing callers)."""

    def test_build_manifest_stamps_the_override_and_falls_back_without_it(self):
        ledger.append({"kind": "turn-delta", "text": "x"})
        lp = ledger.ledger_path()
        self.assertEqual(bv.build_manifest(ledger_path=lp)["engine-version"], "1.2.3")              # engine.json
        self.assertEqual(bv.build_manifest(ledger_path=lp, engine_version="v7")["engine-version"], "v7")
        # the four locked keys are unchanged by the override (a non-widening extension)
        self.assertEqual(set(bv.build_manifest(ledger_path=lp, engine_version="v7")),
                         {"ledger-version", "ledger-generation", "timestamp", "engine-version"})


class MigrationSnapshotTests(_Base):
    """The pre-migration backup seam module_manager consumes: it lands a DISTINCT, retained `refs/tags`
    snapshot the routine rolling backup never overwrites, refuses a name collision (a replay) rather than
    overwriting, prunes superseded snapshots to the cap without ever cutting the most-recent (citable) one, and
    returns a truthy handle ONLY when a real, addressable snapshot exists — None on every no-backup path so the
    no-backup guard refuses the migration (the {"ok": False}-is-truthy trap)."""

    def _setup_vault(self):
        fake = bv._FakeVault()
        bv.setup(scope="shared", transport=fake.transport, consent="y")
        return fake

    @staticmethod
    def _pushed_manifests(fake):
        out = []
        for content in fake.blobs.values():
            try:
                d = json.loads(base64.b64decode(content))
            except Exception:                                   # noqa: BLE001 — the ledger blob is NDJSON, skip it
                continue
            if isinstance(d, dict) and "engine-version" in d:
                out.append(d)
        return out

    def test_successful_snapshot_creates_a_retained_tag_and_leaves_the_rolling_head(self):
        fake = self._setup_vault()
        ledger.append({"kind": "turn-delta", "text": "pre-migration state"})
        rolling_before = dict(fake.refs)                         # the rolling branch tip before the snapshot
        handle = bv.snapshot_for_migration("recall-ledger", "v9.9.9", migration_id="core@0.2.0",
                                           transport=fake.transport)
        self.assertTrue(handle)                                  # truthy -> the migration may proceed
        self.assertEqual(handle["engine-version"], "v9.9.9")    # the MIGRATION-time version, not engine.json's 1.2.3
        ns = bv.read_pointer()["namespace"]
        # a DISTINCT retained tag was created, named by namespace + migration id ...
        self.assertEqual(handle["tag"], bv._snapshot_tag_name(ns, "core@0.2.0"))
        self.assertIn(handle["tag"], {k.split("@", 1)[1] for k in fake.tags})
        # ... and the ROLLING backup head was NOT advanced (the snapshot is a sibling ref, not the rolling slot)
        self.assertEqual(fake.refs, rolling_before)
        # the snapshot manifest carries the migration identity for the later restore + code-older-than-data detector
        snap = [m for m in self._pushed_manifests(fake) if m.get("kind") == "migration-snapshot"]
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["engine-version"], "v9.9.9")
        self.assertEqual(snap[0]["migration-id"], "core@0.2.0")

    def test_a_name_collision_refuses_the_migration_and_never_overwrites(self):
        fake = self._setup_vault()
        first = bv.snapshot_for_migration("recall-ledger", "v1", migration_id="core@0.2.0", transport=fake.transport)
        self.assertTrue(first)
        tags_after_first = dict(fake.tags)
        # a REPLAY of the same migration (same id) -> same tag name -> a collision -> REFUSED, nothing overwritten
        replay = bv.snapshot_for_migration("recall-ledger", "v1", migration_id="core@0.2.0", transport=fake.transport)
        self.assertIsNone(replay)
        self.assertEqual(fake.tags, tags_after_first)           # the existing snapshot is untouched

    def test_hardened_reflects_the_tag_protection_probe(self):
        fake = self._setup_vault()
        self.assertFalse(bv.snapshot_for_migration("recall-ledger", "v1", migration_id="m@1",
                                                   transport=fake.transport)["hardened"])
        fake.tag_protection = [{"id": 1}]                       # a tag-targeting ruleset present (paid tier)
        self.assertTrue(bv.snapshot_for_migration("recall-ledger", "v1", migration_id="m@2",
                                                  transport=fake.transport)["hardened"])

    def test_prune_keeps_the_floor_and_the_most_recent(self):
        # #303 (reversibility unit = the upgrade): across one upgrade's data migrations, the FIRST is the
        # reversibility floor (stamped); the prune keeps exactly {floor, most-recent} and deletes every intermediate,
        # so a namespace settles to <=2 snapshot tags even for a multi-migration upgrade.
        fake = self._setup_vault()
        s0 = bv.snapshot_for_migration("recall-ledger", "0.3.0", migration_id="core@0.0.0",
                                       reversibility_floor=True, transport=fake.transport)["tag"]   # the batch floor
        bv.snapshot_for_migration("recall-ledger", "0.3.0", migration_id="core@0.1.0",             # an intermediate
                                  transport=fake.transport)
        s2 = bv.snapshot_for_migration("recall-ledger", "0.3.0", migration_id="core@0.2.0",         # the most-recent
                                       transport=fake.transport)["tag"]
        remaining = {k.split("@", 1)[1] for k in fake.tags}
        self.assertEqual(remaining, {s0, s2})                          # exactly the floor + the most-recent
        self.assertEqual(bv.read_migration_stamp()["snapshot_tag"], s0)  # the stamp cites the floor, not the last step

    def test_prune_fail_safe_prunes_nothing_when_the_citation_cannot_be_read(self):
        # #303 citation-bound fail-safe: if the migration stamp (the floor citation) cannot be read, the prune deletes
        # NOTHING — the engine never deletes a snapshot that might be the undo floor, even if the floor's stamp was lost.
        fake = self._setup_vault()
        # two snapshots exist but NO reversibility floor was recorded (no stamp) -> the prune has no citation to trust
        bv.snapshot_for_migration("recall-ledger", "0.1.0", migration_id="core@0.0.0", transport=fake.transport)
        bv.snapshot_for_migration("recall-ledger", "0.2.0", migration_id="core@0.1.0", transport=fake.transport)
        self.assertIsNone(bv.read_migration_stamp())                   # no floor stamp -> citation doubt
        ptr = bv.read_pointer()
        before = dict(fake.tags)
        pruned = bv._prune_snapshots(bv._gh(fake.transport), ptr["owner"], ptr["repo"], ptr["namespace"],
                                     keep_name="anything")
        self.assertEqual(pruned, [])                                    # citation doubt -> prune nothing
        self.assertEqual(fake.tags, before)                            # not one tag deleted
        self.assertEqual(fake.deleted_tags, [])

    def test_reversibility_floor_writes_a_stamp_matching_the_created_tag(self):
        # #303: the floor snapshot records the local reversibility stamp, and it cites EXACTLY the tag it created
        # (one source variable -> the stamp and the ref cannot drift).
        fake = self._setup_vault()
        h = bv.snapshot_for_migration("recall-ledger", "2.0.0", migration_id="core@0.0.0",
                                      reversibility_floor=True, transport=fake.transport)
        stamp = bv.read_migration_stamp()
        self.assertIsNotNone(stamp)
        self.assertEqual(stamp["snapshot_tag"], h["tag"])              # cites the just-created tag, no drift
        self.assertEqual(stamp["migrated_by_version"], "2.0.0")
        self.assertEqual(stamp["store_label"], "recall-ledger")

    def test_a_non_floor_migration_writes_no_stamp(self):
        fake = self._setup_vault()
        bv.snapshot_for_migration("recall-ledger", "2.0.0", migration_id="core@0.1.0", transport=fake.transport)
        self.assertIsNone(bv.read_migration_stamp())                   # only the batch floor records the stamp

    def test_a_non_version_shaped_floor_writes_no_stamp(self):
        # the literal "latest"/"unknown" compares as (0,) on the running side, so a stamp recording it could never
        # fire the detector — write none (the prune then over-retains via the citation fail-safe, never deletes).
        fake = self._setup_vault()
        h = bv.snapshot_for_migration("recall-ledger", "latest", migration_id="core@0.0.0",
                                      reversibility_floor=True, transport=fake.transport)
        self.assertIsNotNone(h)                                        # the snapshot itself still succeeds
        self.assertIsNone(bv.read_migration_stamp())

    def test_a_failed_floor_stamp_never_refuses_the_snapshot_or_lets_a_later_prune_delete_the_floor(self):
        # The floor stamp write is best-effort: a write fault must NOT refuse the (successful) snapshot, and a later
        # migration's prune (now with no readable citation) must delete NOTHING -> the floor survives the lost stamp.
        fake = self._setup_vault()
        orig = bv.write_migration_stamp
        bv.write_migration_stamp = lambda **kw: (_ for _ in ()).throw(OSError("disk full"))
        try:
            h = bv.snapshot_for_migration("recall-ledger", "2.0.0", migration_id="core@0.0.0",
                                          reversibility_floor=True, transport=fake.transport)
        finally:
            bv.write_migration_stamp = orig
        self.assertIsNotNone(h)                                        # the snapshot was NOT refused by the stamp fault
        s0 = h["tag"]
        self.assertIsNone(bv.read_migration_stamp())                   # but the floor stamp was lost
        bv.snapshot_for_migration("recall-ledger", "2.0.0", migration_id="core@0.1.0", transport=fake.transport)
        remaining = {k.split("@", 1)[1] for k in fake.tags}
        self.assertIn(s0, remaining)                                   # the floor survived despite the lost citation

    def test_stamp_round_trip_and_clear(self):
        bv.write_migration_stamp(store_label="recall-ledger", migrated_by_version="2.0.0",
                                 snapshot_tag="engine-snapshot/ns/core-0.0.0")
        self.assertEqual(bv.read_migration_stamp()["migrated_by_version"], "2.0.0")
        bv.clear_migration_stamp()
        self.assertIsNone(bv.read_migration_stamp())
        bv.clear_migration_stamp()                                     # idempotent: clearing an absent stamp is a no-op

    def test_returns_none_when_the_vault_is_not_configured(self):
        self.assertIsNone(                                       # no setup -> no pointer -> not-configured
            bv.snapshot_for_migration("recall-ledger", "v1", transport=bv._FakeVault().transport))

    def test_returns_none_on_a_public_flip(self):
        fake = self._setup_vault()
        fake.private = False                                     # the vault went public -> never snapshot to it
        self.assertIsNone(bv.snapshot_for_migration("recall-ledger", "v1", transport=fake.transport))

    def test_returns_none_on_a_commit_failure(self):
        fake = self._setup_vault()
        fake.fail_blob = True                                    # the blob upload fails -> no commit -> refuse
        self.assertIsNone(bv.snapshot_for_migration("recall-ledger", "v1", transport=fake.transport))

    def test_returns_none_when_the_repo_is_unreachable(self):
        fake = self._setup_vault()
        fake.hide_next_probe(bv.read_pointer()["repo"])         # the privacy re-probe 404s -> unreachable
        self.assertIsNone(bv.snapshot_for_migration("recall-ledger", "v1", transport=fake.transport))

    def test_returns_none_when_no_token_resolves(self):
        self._setup_vault()
        orig = bv._gh
        bv._gh = lambda transport=None: None                    # a non-None transport short-circuits _gh, so force it
        try:
            self.assertIsNone(bv.snapshot_for_migration("recall-ledger", "v1"))
        finally:
            bv._gh = orig

    def test_a_failed_backup_is_falsy_not_a_truthy_result_dict(self):
        # a failed snapshot MUST return None, else module_manager's no-backup guard would read it as success and
        # run the migration against an un-backed-up store.
        fake = self._setup_vault()
        fake.fail_blob = True
        self.assertFalse(bv.snapshot_for_migration("recall-ledger", "v1", transport=fake.transport))


class ImportInvariantTests(unittest.TestCase):
    """`import memory` exposes the seam and does NO filesystem work (the package docstring's invariant — now
    that __init__ also binds backup_vault). Run in a fresh interpreter against a fresh empty cabinet."""

    def test_importing_memory_exposes_the_seam_and_writes_nothing(self):
        import subprocess
        tools = os.path.dirname(os.path.dirname(os.path.abspath(bv.__file__)))   # .engine/tools
        with tempfile.TemporaryDirectory() as cab:
            env = dict(os.environ)
            env["ENGINE_MEMORY_DIR"] = cab
            code = (f"import sys; sys.path.insert(0, {tools!r}); import memory; "
                    "assert callable(memory.snapshot_for_migration); "
                    "assert callable(memory.capture_turn_delta)")
            r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(os.listdir(cab), [])               # importing the package created nothing on disk


if __name__ == "__main__":
    unittest.main()
