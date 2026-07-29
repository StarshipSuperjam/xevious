"""test_retired_settings.py — the retired-setting workflow: what happens to a saved value when the engine
removes the setting it names.

Run: uv run --directory .engine --frozen -- python tools/selftest.py

Removing a tunable setting leaves any deployment that tuned it holding a value that names nothing, and the
stale-saved-setting check reports that as a HARD, merge-blocking finding on their next change — for a change
they did not make. Three pieces answer that, and they are tested together here because they are one workflow:
the register of retirements and their plain-language reasons (`operator_overrides.RETIRED`); the module
migration that removes the entry at upgrade so the block never happens; and, if an entry survives anyway, the
check explaining WHY plus a one-step clear (`tune.forget`) instead of "hand-edit this committed file".
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import operator_overrides  # noqa: E402
import policy_override_check  # noqa: E402
import tune  # noqa: E402
import validate  # noqa: E402

_RETIRED_KEY = "scent_strong_match_threshold"
_MIGRATION = os.path.join(validate.ENGINE_DIR, "modules", "core", "migrations",
                          "retire_scent_threshold.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("retire_scent_threshold", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Overrides(unittest.TestCase):
    """A throwaway saved-settings file; the real committed one is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "operator-overrides.json")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)


class TheRegisterTests(unittest.TestCase):
    def test_the_retired_dial_is_recorded_with_a_reason(self):
        reason = operator_overrides.retirement_reason("attention", _RETIRED_KEY)
        self.assertTrue(reason)
        self.assertNotIn("scent", reason.lower(), "the reason is operator-facing — no internal names")

    def test_a_setting_the_engine_never_retired_has_no_reason(self):
        # The register must not invent one; the caller falls back to its own generic wording.
        self.assertIsNone(operator_overrides.retirement_reason("attention", "never_existed"))
        self.assertIsNone(operator_overrides.retirement_reason("nosuchpolicy", _RETIRED_KEY))

    def test_every_recorded_retirement_names_a_setting_the_policy_really_dropped(self):
        # A register entry for a setting that still exists would produce a "this was retired" message about a
        # live dial. Each key must be genuinely absent from its policy's shipped values.
        for (policy_id, key), reason in operator_overrides.RETIRED.items():
            self.assertNotIn(key, tune.default_values(policy_id),
                             f"{policy_id}.{key} is recorded as retired but the policy still ships it")
            self.assertTrue(reason.strip(), f"{policy_id}.{key} has an empty reason")


class TheCheckExplainsTests(_Overrides):
    def test_a_retired_setting_is_reported_with_its_reason_and_a_one_step_clear(self):
        findings = policy_override_check.findings("hard", override={"attention": {_RETIRED_KEY: 0.9}})
        self.assertEqual(len(findings), 1)
        message = findings[0]["message"]
        self.assertIn("has been retired", message)
        self.assertIn("every message", message)          # the recorded reason reached the operator
        self.assertIn("/engine-tune forget", message)    # ...and so did the remedy that actually exists
        self.assertNotIn("no longer exists", message)    # not the generic line

    def test_an_unrecorded_stale_setting_keeps_the_generic_line(self):
        findings = policy_override_check.findings("hard", override={"attention": {"mystery_dial": 3}})
        self.assertEqual(len(findings), 1)
        self.assertIn("no longer exists", findings[0]["message"])
        self.assertNotIn("has been retired", findings[0]["message"])

    def test_a_live_setting_is_still_silent(self):
        self.assertEqual(policy_override_check.findings("hard",
                                                        override={"attention": {"debt_blocking_threshold": 3}}),
                         [])


class TheMigrationTests(_Overrides):
    """The piece that means the merge-block never happens for an operator who upgrades normally."""

    def setUp(self):
        super().setUp()
        self.mig = _load_migration()
        self._prev_root = validate.ROOT

    def _run_against(self, tmp_root):
        # The migration resolves the deployment through validate.ROOT, exactly as the real runner arranges.
        self.mig.validate.ROOT = tmp_root
        try:
            return self.mig.migrate({"kind": "config", "to_version": "0.3.0"})
        finally:
            self.mig.validate.ROOT = self._prev_root

    def _seed(self, data):
        root = tempfile.mkdtemp(dir=self._tmp.name)
        os.makedirs(os.path.join(root, ".engine"), exist_ok=True)
        p = os.path.join(root, ".engine", "operator-overrides.json")
        if data is not None:
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        return root, p

    def test_it_removes_the_retired_entry_and_keeps_every_other_setting(self):
        root, p = self._seed({"attention": {_RETIRED_KEY: 0.9, "debt_blocking_threshold": 4},
                              "triage-threshold": {"persistence": 5}})
        report = self._run_against(root)
        self.assertTrue(report["changed"])
        with open(p, encoding="utf-8") as fh:
            after = json.load(fh)
        self.assertNotIn(_RETIRED_KEY, after["attention"])
        self.assertEqual(after["attention"]["debt_blocking_threshold"], 4)   # untouched
        self.assertEqual(after["triage-threshold"], {"persistence": 5})      # untouched

    def test_an_emptied_slice_is_dropped_rather_than_left_as_an_empty_object(self):
        root, p = self._seed({"attention": {_RETIRED_KEY: 0.9}})
        self.assertTrue(self._run_against(root)["changed"])
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {})

    def test_it_is_a_no_op_when_the_setting_was_never_tuned(self):
        root, p = self._seed({"triage-threshold": {"persistence": 5}})
        self.assertFalse(self._run_against(root)["changed"])
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"triage-threshold": {"persistence": 5}})

    def test_it_is_a_no_op_when_there_is_no_saved_settings_file(self):
        root, _ = self._seed(None)
        self.assertFalse(self._run_against(root)["changed"])

    def test_an_unreadable_file_is_LEFT_ALONE_rather_than_rewritten(self):
        # The load-bearing refusal: a transform that "helpfully" rewrote a file it could not parse would
        # destroy tuning it could not read. Under-reaching leaves a stale entry the check still explains.
        root, p = self._seed(None)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        report = self._run_against(root)
        self.assertFalse(report["changed"])
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not json at all")

    def test_it_never_raises_whatever_it_finds(self):
        # An upgrade must not fail over a setting that is already being ignored.
        for data in ({"attention": "not-a-dict"}, [], {"attention": {}}, {"attention": None}):
            root, p = self._seed(None)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            self.assertIn("changed", self._run_against(root))

    def test_the_manifest_declares_it_with_an_operator_readable_description(self):
        with open(os.path.join(validate.ENGINE_DIR, "modules", "core", "manifest.json"),
                  encoding="utf-8") as fh:
            core = json.load(fh)
        migs = core.get("migrations") or {}
        self.assertTrue(migs, "core must declare the migration or it never runs")
        entry = migs["0.3.0"]
        self.assertEqual(entry["kind"], "config")
        self.assertEqual(entry["run"], "migrations/retire_scent_threshold.py")
        self.assertTrue(os.path.isfile(_MIGRATION), "the declared run path must resolve")
        for jargon in ("scent", "override", "policy", "frontmatter"):
            self.assertNotIn(jargon, entry["description"].lower(),
                             "the description is shown to the operator in the upgrade's pull request")

    def test_a_deployment_upgrading_past_the_key_actually_RUNS_the_migration(self):
        # A migration keyed to a version nothing reaches is silently skipped forever, so the key has to be
        # checked — but as a DURABLE property, not as a snapshot of today's version numbers. Asserting "the key
        # equals core's version bumped one minor" holds only until the release cut writes that bumped version
        # into the manifest, at which point the assertion recomputes against the NEW version, demands a further
        # bump, and fails — turning the guard on the upgrade into the thing that blocks the release shipping it.
        # What is true forever is the behaviour: replay the real selector for a deployment sitting on any
        # version below the key, and the migration must be chosen.
        import module_manager
        with open(os.path.join(validate.ENGINE_DIR, "modules", "core", "manifest.json"),
                  encoding="utf-8") as fh:
            core = json.load(fh)
        key = next(iter(core["migrations"]))
        selected = module_manager.select_migrations({"core": "0.1.0"}, {"core": key}, [core])
        self.assertEqual([(s["module_id"], s["version"]) for s in selected], [("core", key)])
        # ...and an upgrade that has already passed it does not run it twice.
        self.assertEqual(module_manager.select_migrations({"core": key}, {"core": key}, [core]), [])


class TheOneStepClearTests(_Overrides):
    def test_forget_removes_the_retired_entry_and_prepares_it_for_approval(self):
        self.write({"attention": {_RETIRED_KEY: 0.9, "debt_blocking_threshold": 4}})
        opened = {}

        def fake_opener(branch, title, body, paths):
            opened.update(branch=branch, title=title, body=body)
            return {"number": 0, "html_url": "https://example.invalid/pr/0"}

        result = tune.forget_value("attention", _RETIRED_KEY, override_path=self.path, opener=fake_opener)
        self.assertTrue(result["ok"])
        self.assertNotIn(_RETIRED_KEY, self.read()["attention"])
        self.assertEqual(self.read()["attention"]["debt_blocking_threshold"], 4)
        self.assertIn("every message", opened["body"], "the pull request tells the operator why")
        self.assertIn("Maintenance:", opened["title"], "release notes group by the change kind")

    def test_forget_REFUSES_a_setting_that_still_exists(self):
        # The dangerous neighbour: a verb that could delete a live, working choice. `set` changes those.
        self.write({"attention": {"debt_blocking_threshold": 4}})
        result = tune.forget_value("attention", "debt_blocking_threshold", override_path=self.path,
                                   opener=None, open_pr=False)
        self.assertFalse(result["ok"])
        self.assertIn("still one of the engine's settings", result["message"])
        self.assertEqual(self.read()["attention"]["debt_blocking_threshold"], 4, "nothing was removed")

    def test_forget_does_not_discard_a_setting_it_could_not_parse(self):
        # The reader this used to go through drops any policy slice that is not a plain object — correct for a
        # reader (degrade to the defaults rather than strand a boot), destructive for a writer. This path is
        # reached precisely when someone has hand-edited the file, which is how a malformed slice gets there.
        self.write({"attention": {_RETIRED_KEY: 0.9}, "triage-threshold": "3"})
        self.assertTrue(tune.drop_override("attention", _RETIRED_KEY, path=self.path))
        after = self.read()
        self.assertEqual(after["triage-threshold"], "3", "an unparseable neighbour was erased")
        self.assertNotIn("attention", after)

    def test_forget_REFUSES_an_unknown_group_rather_than_clearing_anything(self):
        # Fails CLOSED on doubt: an unreadable policy yields no defaults, which would otherwise make every key
        # under it look retired — and with --no-pr that write lands with no review behind it.
        self.write({"nosuchpolicy": {"anything": 1}})
        result = tune.forget_value("nosuchpolicy", "anything", override_path=self.path,
                                   opener=None, open_pr=False)
        self.assertFalse(result["ok"])
        self.assertIn("don't have a group of settings", result["message"])
        self.assertEqual(self.read(), {"nosuchpolicy": {"anything": 1}}, "nothing was cleared")

    def test_forget_DOES_clear_a_fixed_safety_setting_because_set_refuses_it_too(self):
        # A structural key still exists, so the live-setting refusal would catch it — but `set` refuses it as
        # well, leaving a saved value that can never apply, keeps failing the check, and has no way out but
        # hand-editing the file. That is the dead end this verb exists to close, so this one clears.
        structural = sorted(tune.structural_keys("attention"))[0]
        self.write({"attention": {structural: 1}})
        result = tune.forget_value("attention", structural, override_path=self.path,
                                   opener=None, open_pr=False)
        self.assertTrue(result["ok"], result["message"])
        self.assertFalse(os.path.exists(self.path))

    def test_forget_says_so_when_there_is_nothing_saved_to_clear(self):
        self.write({"attention": {"debt_blocking_threshold": 4}})
        result = tune.forget_value("attention", _RETIRED_KEY, override_path=self.path,
                                   opener=None, open_pr=False)
        self.assertFalse(result["ok"])
        self.assertIn("not in your saved settings", result["message"])

    def test_clearing_the_last_setting_removes_the_file_rather_than_committing_an_empty_one(self):
        self.write({"attention": {_RETIRED_KEY: 0.9}})
        self.assertTrue(tune.drop_override("attention", _RETIRED_KEY, path=self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_the_cli_exposes_the_verb_the_checks_message_tells_the_operator_to_type(self):
        # The finding says "type /engine-tune forget <key>". If the verb were renamed, that advice would send
        # the operator at a command that does not exist — the failure this whole workflow exists to remove.
        from quiet_call import run as quiet_run
        self.write({"attention": {_RETIRED_KEY: 0.9}})
        code = quiet_run(tune.main, ["forget", "attention", _RETIRED_KEY,
                                     "--override", self.path, "--no-pr"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.path))
        message = policy_override_check._RETIRED.format(key=_RETIRED_KEY, reason="x")
        self.assertIn("/engine-tune forget", message)


if __name__ == "__main__":
    unittest.main()
