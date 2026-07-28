"""The security floor (issue #124): the native-scanning toggles. These tests attest the
load-bearing facts: each surface branches on the call's HTTP status (the right code per toggle), verifies
after the write and NEVER reports a feature on when the enable didn't succeed, never touches the branch
ruleset / required checks (advisory), and never leaks an HTTP status, a bare product-tier name, or an API
response body onto the operator surface. The eyes-on disclosure is covered by demo_security_floor."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import security_floor as sf  # noqa: E402
import protection_guard      # noqa: E402
import demo_security_floor   # noqa: E402
import quiet_call            # noqa: E402  (capture the demo walkthrough so it can't bury the summary)

REPO = "you/your-project"

def _fake(*, secrets=(200, {}), secrets_status="enabled", code_patch=(202, {}), code_state="configured",
          pvr_put=(204, None), pvr_enabled=True, record=None):
    """A fake GitHub: each enable call returns the given (status, body); the read-back reflects the given
    confirmed state. `record` (a list) captures every (method, path) so a test can assert what was NOT called."""
    def t(method, path, body=None):
        if record is not None:
            record.append((method, path))
        if path.endswith("/code-scanning/default-setup"):
            return (code_patch[0], code_patch[1], {}) if method == "PATCH" else (200, {"state": code_state}, {})
        if path.endswith("/private-vulnerability-reporting"):
            return (pvr_put[0], pvr_put[1], {}) if method == "PUT" else (200, {"enabled": pvr_enabled}, {})
        if method == "PATCH" and isinstance(body, dict) and "security_and_analysis" in body:
            return secrets[0], secrets[1], {}
        if method == "GET" and path.startswith("/repos/"):
            return 200, {"security_and_analysis": {"secret_scanning": {"status": secrets_status}}}, {}
        return 404, None, {}
    return t


def _floor(transport):
    return sf.SecurityFloor(REPO, "tok", transport=transport)


class TestSecretScanningBranches(unittest.TestCase):
    def test_enabled_verified_is_on(self):
        self.assertEqual(_floor(_fake(secrets=(200, {}), secrets_status="enabled")).enable_secret_scanning().state, sf.ON)

    def test_free_private_403_is_unsupported(self):
        t = _floor(_fake(secrets=(403, {"message": "Advanced Security is not enabled"}))).enable_secret_scanning()
        self.assertEqual(t.state, sf.UNSUPPORTED)
        self.assertEqual(t.unlock, sf.PUBLIC_OR_PAID)

    def test_write_ok_but_readback_not_enabled_is_failed_never_on(self):
        self.assertEqual(_floor(_fake(secrets=(200, {}), secrets_status="disabled")).enable_secret_scanning().state, sf.FAILED)

    def test_network_failure_is_unverified_never_on(self):
        def boom(method, path, body=None):
            raise sf.bootstrap.BootstrapError("unreachable")
        self.assertEqual(_floor(boom).enable_secret_scanning().state, sf.UNVERIFIED)


class TestCodeScanningBranches(unittest.TestCase):
    def test_202_then_configured_is_on(self):
        self.assertEqual(_floor(_fake(code_patch=(202, {}), code_state="configured")).enable_code_scanning().state, sf.ON)

    def test_202_not_yet_configured_is_pending_not_on(self):
        self.assertEqual(_floor(_fake(code_patch=(202, {}), code_state="not-configured")).enable_code_scanning().state, sf.PENDING)

    def test_403_is_unsupported(self):
        self.assertEqual(_floor(_fake(code_patch=(403, {}))).enable_code_scanning().state, sf.UNSUPPORTED)

    def test_409_transient_retries_then_succeeds(self):
        calls = {"n": 0}
        def t(method, path, body=None):
            if path.endswith("/code-scanning/default-setup") and method == "PATCH":
                calls["n"] += 1
                return (409, {}, {}) if calls["n"] == 1 else (202, {}, {})
            if path.endswith("/code-scanning/default-setup"):
                return 200, {"state": "configured"}, {}
            return 404, None, {}
        self.assertEqual(_floor(t).enable_code_scanning().state, sf.ON)
        self.assertEqual(calls["n"], 2, "a transient 409 is retried exactly once")

    def test_422_is_transient_retried_not_unsupported(self):
        # For CODE SCANNING a 422 means a setup run in progress (retry), NOT the PVR public-only 422.
        calls = {"n": 0}
        def t(method, path, body=None):
            if path.endswith("/code-scanning/default-setup") and method == "PATCH":
                calls["n"] += 1
                return (422, {}, {}) if calls["n"] == 1 else (202, {}, {})
            if path.endswith("/code-scanning/default-setup"):
                return 200, {"state": "configured"}, {}
            return 404, None, {}
        toggle = _floor(t).enable_code_scanning()
        self.assertEqual(toggle.state, sf.ON)
        self.assertNotEqual(toggle.state, sf.UNSUPPORTED, "code-scanning 422 is transient, never public-only")
        self.assertEqual(calls["n"], 2)

    def test_hard_error_is_failed(self):
        self.assertEqual(_floor(_fake(code_patch=(500, {}))).enable_code_scanning().state, sf.FAILED)


class TestPvrBranches(unittest.TestCase):
    def test_204_then_enabled_is_on(self):
        self.assertEqual(_floor(_fake(pvr_put=(204, None), pvr_enabled=True)).enable_pvr().state, sf.ON)

    def test_private_repo_422_is_unsupported_public_only(self):
        t = _floor(_fake(pvr_put=(422, {"message": "public repositories only"}))).enable_pvr()
        self.assertEqual(t.state, sf.UNSUPPORTED)
        self.assertEqual(t.unlock, sf.PUBLIC_ONLY)

    def test_enabled_false_readback_is_failed_never_on(self):
        self.assertEqual(_floor(_fake(pvr_put=(204, None), pvr_enabled=False)).enable_pvr().state, sf.FAILED)


class TestApplyAdvisoryAndNoRulesetTouch(unittest.TestCase):
    def test_apply_returns_toggles_and_discloses(self):
        said = []
        toggles = _floor(_fake()).apply(announce=said.append)
        self.assertEqual({t.key for t in toggles}, {"secret-scanning", "code-scanning", "pvr"})
        self.assertTrue(said, "apply discloses the outcome")

    def test_never_writes_the_ruleset_or_required_checks(self):
        record = []
        _floor(_fake(record=record)).apply(announce=lambda _t: None)
        for method, path in record:
            self.assertNotIn("/rulesets", path, "the toggles never touch the branch ruleset (advisory)")
            self.assertNotIn("/rules/", path)
        # the advisory invariant at the source: the toggles' names are not required checks
        self.assertNotIn("code-scanning", protection_guard.REQUIRED_CHECKS)
        self.assertNotIn("secret-scanning", protection_guard.REQUIRED_CHECKS)
        self.assertEqual(protection_guard.REQUIRED_CHECKS, ["engine-ci", "engine-guard"])


class TestDisclosureIsPlainLanguage(unittest.TestCase):
    """The rendered operator surface never leaks an HTTP status, a bare product-tier name, an API field, or
    the raw API response body — across every outcome state."""

    def _all_renders(self) -> str:
        blobs = []
        # every state for every key, plus the four demo scenarios' real renders
        for key in ("secret-scanning", "code-scanning", "pvr"):
            for state in (sf.ON, sf.PENDING, sf.UNVERIFIED, sf.FAILED):
                blobs.append(sf.render([sf.Toggle(key, state)]))
        blobs.append(sf.render([sf.Toggle("secret-scanning", sf.UNSUPPORTED, sf.PUBLIC_OR_PAID),
                                sf.Toggle("code-scanning", sf.UNSUPPORTED, sf.PUBLIC_OR_PAID),
                                sf.Toggle("pvr", sf.UNSUPPORTED, sf.PUBLIC_ONLY)]))
        return "\n".join(blobs).lower()

    def test_no_http_status_or_tier_name_or_api_field(self):
        blob = self._all_renders()
        for term in ("403", "422", "202", "409", "500", "advanced security", "code security", "ghas",
                     "codeql", "security_and_analysis", "default-setup", "endpoint", "ruleset",
                     "transport", "oauth", "http", " patch ", " put "):
            self.assertNotIn(term, blob, f"the operator surface must not contain {term!r}")

    def test_render_never_echoes_the_api_response_body(self):
        # GitHub's own 403 body literally says "Advanced Security is not enabled" — a banned tier name. The
        # render maps status -> hand-written prose and must never interpolate the response body.
        t = _floor(_fake(secrets=(403, {"message": "Advanced Security is not enabled"}),
                         code_patch=(403, {"message": "Advanced Security is not enabled"}),
                         pvr_put=(422, {"message": "public repositories only"}))).apply(announce=lambda _t: None)
        rendered = sf.render(t).lower()
        self.assertNotIn("advanced security", rendered)


class TestSecurityFloorDemo(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(demo_security_floor.main), 0)


if __name__ == "__main__":
    unittest.main()
