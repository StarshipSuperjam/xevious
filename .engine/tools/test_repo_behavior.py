"""The repository-behavior settings leg (issue #541). These tests attest the load-bearing facts: each
setting branches on the call's HTTP status, is read BEFORE it is written (augment-never-override — already-on
is left untouched and reported as already yours), verifies after the write and NEVER reports a setting on when
the enable didn't confirm, discloses an organization-reserved Dependabot switch instead of forcing it, never
touches the branch ruleset / required checks, and never leaks an HTTP status or API field name onto the
operator surface. The eyes-on disclosure is covered by demo_repo_behavior."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_behavior as rb   # noqa: E402
import protection_guard      # noqa: E402
import demo_repo_behavior    # noqa: E402
import quiet_call            # noqa: E402  (capture the demo walkthrough so it can't bury the summary)

REPO = "you/your-project"


def _fake(*, repo_settings=None, patch=(200, {}), alerts_on=False, alerts_put=(204, None),
          fixes_on=False, fixes_put=(204, None), fixes_enabled=True, record=None):
    """A fake GitHub: `repo_settings` is the live repo-settings dict the GET returns (a PATCH mutates it, so
    the read-back really reflects the write); the Dependabot switches are stateful the same way —
    `alerts_on`/`fixes_on` are the starting states, a successful PUT flips them (to `fixes_enabled` for the
    fixes switch, so a lying server — write accepted, state unchanged — stays expressible).
    `record` (a list) captures every (method, path) so a test can assert what was NOT called."""
    state = dict(repo_settings if repo_settings is not None
                 else {"delete_branch_on_merge": False, "allow_update_branch": False})
    alerts = {"on": alerts_on}
    fixes = {"on": fixes_on}

    def t(method, path, body=None):
        if record is not None:
            record.append((method, path))
        if path.endswith("/vulnerability-alerts"):
            if method == "PUT":
                if alerts_put[0] < 400:
                    alerts["on"] = True
                return alerts_put[0], alerts_put[1], {}
            return (204, None, {}) if alerts["on"] else (404, None, {})
        if path.endswith("/automated-security-fixes"):
            if method == "PUT":
                if fixes_put[0] < 400:
                    fixes["on"] = fixes_enabled
                return fixes_put[0], fixes_put[1], {}
            return 200, {"enabled": fixes["on"], "paused": False}, {}
        if method == "PATCH" and isinstance(body, dict):
            if patch[0] < 400:
                state.update(body)
            return patch[0], patch[1], {}
        if method == "GET" and path.startswith("/repos/"):
            return 200, dict(state, full_name=REPO), {}
        return 404, None, {}
    return t


def _leg(transport):
    return rb.RepoBehavior(REPO, "tok", transport=transport)


class TestMergeHygieneBranches(unittest.TestCase):
    def test_off_settings_are_enabled_and_confirmed_on(self):
        toggles = _leg(_fake()).enable_merge_hygiene()
        self.assertEqual({t.key: t.state for t in toggles},
                         {"delete-branch-on-merge": rb.ON, "update-branch": rb.ON})

    def test_already_on_is_left_untouched_no_write(self):
        record = []
        settings = {"delete_branch_on_merge": True, "allow_update_branch": True}
        toggles = _leg(_fake(repo_settings=settings, record=record)).enable_merge_hygiene()
        self.assertTrue(all(t.state == rb.ALREADY for t in toggles))
        self.assertFalse(any(m == "PATCH" for m, _p in record),
                         "an already-on setting is never re-written (augment, never override)")

    def test_partial_already_on_writes_only_the_off_one(self):
        settings = {"delete_branch_on_merge": True, "allow_update_branch": False}
        toggles = {t.key: t.state for t in _leg(_fake(repo_settings=settings)).enable_merge_hygiene()}
        self.assertEqual(toggles["delete-branch-on-merge"], rb.ALREADY)
        self.assertEqual(toggles["update-branch"], rb.ON)

    def test_write_ok_but_readback_still_off_is_unverified_never_on(self):
        # The PATCH answers 200 but the fake refuses to flip the state (patch status < 400 normally
        # mutates; force a lying server by mutating nothing).
        def t(method, path, body=None):
            if method == "PATCH":
                return 200, {}, {}
            if method == "GET" and path.startswith("/repos/"):
                return 200, {"delete_branch_on_merge": False, "allow_update_branch": False}, {}
            return 404, None, {}
        toggles = _leg(t).enable_merge_hygiene()
        self.assertTrue(all(t2.state == rb.UNVERIFIED for t2 in toggles),
                        "a write the read-back does not confirm is never reported on")

    def test_network_failure_is_unverified_never_on(self):
        def boom(method, path, body=None):
            raise rb.bootstrap.BootstrapError("unreachable")
        toggles = _leg(boom).enable_merge_hygiene()
        self.assertTrue(all(t.state == rb.UNVERIFIED for t in toggles))

    def test_patch_error_is_failed_for_written_fields_only(self):
        settings = {"delete_branch_on_merge": True, "allow_update_branch": False}
        toggles = {t.key: t.state for t in
                   _leg(_fake(repo_settings=settings, patch=(500, {}))).enable_merge_hygiene()}
        self.assertEqual(toggles["delete-branch-on-merge"], rb.ALREADY)
        self.assertEqual(toggles["update-branch"], rb.FAILED)


class TestDependabotAlertsBranches(unittest.TestCase):
    def test_off_is_enabled_and_confirmed_on(self):
        self.assertEqual(_leg(_fake()).enable_dependabot_alerts().state, rb.ON)

    def test_already_on_is_left_untouched_no_write(self):
        record = []
        t = _leg(_fake(alerts_on=True, record=record)).enable_dependabot_alerts()
        self.assertEqual(t.state, rb.ALREADY)
        self.assertFalse(any(m == "PUT" for m, _p in record))

    def test_org_policy_403_is_disclosed_not_forced(self):
        t = _leg(_fake(alerts_put=(403, {"message": "org policy"}))).enable_dependabot_alerts()
        self.assertEqual(t.state, rb.UNSUPPORTED)
        self.assertEqual(t.unlock, rb.ORG_CONTROLLED)

    def test_put_ok_but_readback_off_is_unverified(self):
        def t(method, path, body=None):
            if path.endswith("/vulnerability-alerts"):
                return (204, None, {}) if method == "PUT" else (404, None, {})
            return 404, None, {}
        self.assertEqual(_leg(t).enable_dependabot_alerts().state, rb.UNVERIFIED)


class TestDependabotFixesBranches(unittest.TestCase):
    def test_enabled_and_confirmed_is_on(self):
        self.assertEqual(_leg(_fake()).enable_dependabot_fixes().state, rb.ON)

    def test_already_on_is_left_untouched_no_write(self):
        # Read-first like every other setting (a review caught the first draft writing unconditionally
        # and disclosing the no-op as a change).
        record = []
        t = _leg(_fake(fixes_on=True, record=record)).enable_dependabot_fixes()
        self.assertEqual(t.state, rb.ALREADY)
        self.assertFalse(any(m == "PUT" and p.endswith("/automated-security-fixes") for m, p in record))

    def test_org_policy_403_is_disclosed(self):
        t = _leg(_fake(fixes_put=(403, {}))).enable_dependabot_fixes()
        self.assertEqual(t.state, rb.UNSUPPORTED)
        self.assertEqual(t.unlock, rb.ORG_CONTROLLED)

    def test_readback_enabled_false_is_failed_never_on(self):
        self.assertEqual(_leg(_fake(fixes_enabled=False)).enable_dependabot_fixes().state, rb.FAILED)

    def test_hard_error_is_failed(self):
        self.assertEqual(_leg(_fake(fixes_put=(422, {}))).enable_dependabot_fixes().state, rb.FAILED)


class TestApplyAdvisoryAndNoRulesetTouch(unittest.TestCase):
    def test_apply_returns_all_four_and_discloses(self):
        said = []
        toggles = _leg(_fake()).apply(announce=said.append)
        self.assertEqual({t.key for t in toggles},
                         {"delete-branch-on-merge", "update-branch", "dependabot-alerts", "dependabot-fixes"})
        self.assertTrue(said, "apply discloses the outcome")

    def test_never_writes_the_ruleset_or_required_checks(self):
        record = []
        _leg(_fake(record=record)).apply(announce=lambda _t: None)
        for method, path in record:
            self.assertNotIn("/rulesets", path, "the settings never touch the branch ruleset")
            self.assertNotIn("/rules/", path)
        self.assertEqual(protection_guard.REQUIRED_CHECKS, ["engine-ci", "engine-guard"])


class TestDisclosureIsPlainLanguage(unittest.TestCase):
    """The rendered operator surface never leaks an HTTP status, an API field name, or the raw response
    body — across every outcome state."""

    def _all_renders(self) -> str:
        blobs = []
        for key in ("delete-branch-on-merge", "update-branch", "dependabot-alerts", "dependabot-fixes"):
            for state in (rb.ON, rb.ALREADY, rb.UNVERIFIED, rb.FAILED):
                blobs.append(rb.render([rb.Toggle(key, state)]))
        blobs.append(rb.render([rb.Toggle("dependabot-alerts", rb.UNSUPPORTED, rb.ORG_CONTROLLED),
                                rb.Toggle("dependabot-fixes", rb.UNSUPPORTED, rb.ORG_CONTROLLED)]))
        blobs.append(rb.render([]))
        return "\n".join(blobs).lower()

    def test_no_http_status_or_api_field_leaks(self):
        blob = self._all_renders()
        for term in ("403", "404", "204", "422", "500", "delete_branch_on_merge", "allow_update_branch",
                     "vulnerability-alerts", "automated-security-fixes", "endpoint", "ruleset",
                     "transport", "http", " patch ", " put "):
            self.assertNotIn(term, blob, f"the operator surface must not contain {term!r}")

    def test_org_held_render_names_the_organization_not_a_status(self):
        text = rb.render([rb.Toggle("dependabot-alerts", rb.UNSUPPORTED, rb.ORG_CONTROLLED)])
        self.assertIn("organization", text)


class TestDisableUnusedSurfaces(unittest.TestCase):
    """#541 item 4: on a fresh repo, turn OFF the wiki and (when the board-sync module isn't installed)
    project boards — read-first, verify-after, augment-never-override, never on brownfield."""

    def _on(self):
        return {"has_wiki": True, "has_projects": True,
                "delete_branch_on_merge": False, "allow_update_branch": False}

    def test_wiki_and_projects_turned_off_and_confirmed(self):
        toggles = _leg(_fake(repo_settings=self._on())).disable_unused_surfaces(True, True)
        self.assertEqual({t.key: t.state for t in toggles}, {"wiki": rb.OFF, "projects": rb.OFF})

    def test_already_off_is_left_untouched_no_write(self):
        record = []
        settings = {"has_wiki": False, "has_projects": False,
                    "delete_branch_on_merge": False, "allow_update_branch": False}
        toggles = _leg(_fake(repo_settings=settings, record=record)).disable_unused_surfaces(True, True)
        self.assertTrue(all(t.state == rb.OFF_ALREADY for t in toggles))
        self.assertFalse(any(m == "PATCH" for m, _p in record), "an already-off surface is never re-written")

    def test_projects_retained_only_wiki_considered(self):
        # disable_projects=False (the board-sync module is installed) -> project boards are not touched.
        record = []
        toggles = _leg(_fake(repo_settings=self._on(), record=record)).disable_unused_surfaces(True, False)
        self.assertEqual({t.key: t.state for t in toggles}, {"wiki": rb.OFF})
        self.assertNotIn("projects", {t.key for t in toggles}, "retained boards are not in the toggle set")

    def test_brownfield_disables_nothing(self):
        record = []
        toggles = _leg(_fake(repo_settings=self._on(), record=record)).disable_unused_surfaces(False, False)
        self.assertEqual(toggles, [])
        self.assertFalse(any(m == "PATCH" for m, _p in record))

    def test_write_ok_but_readback_still_on_is_unverified_never_off(self):
        def t(method, path, body=None):
            if method == "PATCH":
                return 200, {}, {}                          # accepts but the fake never flips the state
            if method == "GET" and path.startswith("/repos/"):
                return 200, {"has_wiki": True, "has_projects": True}, {}
            return 404, None, {}
        toggles = _leg(t).disable_unused_surfaces(True, True)
        self.assertTrue(all(x.state == rb.UNVERIFIED for x in toggles),
                        "a turn-off the read-back doesn't confirm is never reported off")

    def test_network_failure_is_unverified(self):
        def boom(method, path, body=None):
            raise rb.bootstrap.BootstrapError("unreachable")
        toggles = _leg(boom).disable_unused_surfaces(True, True)
        self.assertTrue(all(t.state == rb.UNVERIFIED for t in toggles))

    def test_all_good_counts_the_off_states(self):
        self.assertTrue(rb.all_good([rb.Toggle("wiki", rb.OFF), rb.Toggle("projects", rb.OFF_ALREADY)]))
        self.assertFalse(rb.all_good([rb.Toggle("wiki", rb.UNVERIFIED)]))

    def test_disclosure_names_the_off_surfaces_plainly(self):
        text = rb.render([rb.Toggle("wiki", rb.OFF), rb.Toggle("projects", rb.OFF)])
        self.assertIn("wiki is now off", text)
        self.assertIn("Project boards are now off", text)
        for term in ("has_wiki", "has_projects", "PATCH", "404", "http"):
            self.assertNotIn(term, text.lower())

    def test_apply_wires_the_disable_into_the_step(self):
        # apply() with the disable flags folds the turn-off toggles into its returned set + disclosure.
        said = []
        toggles = _leg(_fake(repo_settings=self._on())).apply(
            announce=said.append, disable_wiki=True, disable_projects=True)
        keys = {t.key for t in toggles}
        self.assertTrue({"wiki", "projects"} <= keys)
        self.assertTrue(rb.all_good(toggles))


class TestRepoBehaviorDemo(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(demo_repo_behavior.main), 0)


if __name__ == "__main__":
    unittest.main()
