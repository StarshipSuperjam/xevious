#!/usr/bin/env python3
"""Tests for the solo<->team migration operation (#408)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import team_switch  # noqa: E402
from bootstrap import Result  # noqa: E402


def _read(mpath):
    with open(mpath, encoding="utf-8") as fh:
        return json.load(fh)


class _FakeCP:
    """A stand-in ControlPlane whose apply() reports a chosen protection outcome, so the switch's delegation is
    exercised without a live ruleset."""

    def __init__(self, protected=True):
        self.protected = protected
        self.applied = 0

    def apply(self, branch=None):
        self.applied += 1
        return Result("applied" if self.protected else "degraded", branch or "main",
                      [] if self.protected else ["x"], None, True)


def _mk(tmp, *, identity="solo", handle="owner", perm=None, codeowners=True, cp=None):
    mpath = os.path.join(tmp, "engine.json")
    manifest = {"engine_release": "0.0.0", "packages": {}, "identity": identity, "handle": handle}
    if identity == "team":                    # a real team manifest always carries the distinct identity
        manifest["engine_identity"] = {"login": "bot", "email": "bot@x.invalid"}
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)

    def transport(method, path, body=None):
        return (404 if perm is None else 200), (None if perm is None else {"permission": perm})

    ts = team_switch.TeamSwitch("o/r", "tok", transport=transport,
                                cp_factory=lambda tier: (cp or _FakeCP()),
                                run_git=lambda args: None, manifest_path=mpath)
    ts.codeowners_names_operator = lambda: codeowners
    return ts, mpath


class TestVerify(unittest.TestCase):
    def test_non_collaborator_is_a_gap(self):
        with tempfile.TemporaryDirectory() as d:
            ts, _ = _mk(d, perm=None)
            gaps = ts.unmet_prerequisites("bot")
            self.assertTrue(any("collaborator" in g.lower() for g in gaps))

    def test_admin_collaborator_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            ts, _ = _mk(d, perm="admin")
            gaps = ts.unmet_prerequisites("bot")
            self.assertTrue(any("admin" in g.lower() for g in gaps))

    def test_write_collaborator_with_codeowners_is_ready(self):
        with tempfile.TemporaryDirectory() as d:
            ts, _ = _mk(d, perm="write", codeowners=True)
            self.assertEqual(ts.unmet_prerequisites("bot"), [])

    def test_missing_codeowners_is_a_gap(self):
        with tempfile.TemporaryDirectory() as d:
            ts, _ = _mk(d, perm="write", codeowners=False)
            gaps = ts.unmet_prerequisites("bot")
            self.assertTrue(any("codeowners" in g.lower() for g in gaps))


class TestApply(unittest.TestCase):
    def test_blocked_when_not_ready_never_half_switches(self):
        with tempfile.TemporaryDirectory() as d:
            ts, mpath = _mk(d, perm=None)
            res = ts.apply("bot", branch="main")
            self.assertEqual(res["status"], "blocked")
            # nothing was written — the manifest stays solo
            self.assertEqual(_read(mpath)["identity"], "solo")
            self.assertNotIn("engine_identity", _read(mpath))

    def test_applies_records_and_flips_when_ready(self):
        with tempfile.TemporaryDirectory() as d:
            cp = _FakeCP(protected=True)
            ts, mpath = _mk(d, perm="write", codeowners=True, cp=cp)
            res = ts.apply("bot", "bot@x.invalid", branch="main")
            self.assertEqual(res["status"], "applied")
            self.assertEqual(cp.applied, 1)                       # delegated the ruleset apply
            m = _read(mpath)
            self.assertEqual(m["identity"], "team")
            self.assertEqual(m["engine_identity"], {"login": "bot", "email": "bot@x.invalid"})

    def test_degraded_ruleset_apply_does_not_record(self):
        with tempfile.TemporaryDirectory() as d:
            ts, mpath = _mk(d, perm="write", codeowners=True, cp=_FakeCP(protected=False))
            res = ts.apply("bot", branch="main")
            self.assertEqual(res["status"], "degraded")
            self.assertEqual(_read(mpath)["identity"], "solo")   # verify-before-record

    def test_apply_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            ts, _ = _mk(d, identity="team", perm="write", codeowners=True)
            self.assertEqual(ts.apply("bot", branch="main")["status"], "already")

    def test_git_author_is_wired_to_the_machine_user(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            ts, _ = _mk(d, perm="write", codeowners=True)
            ts._run_git = lambda args: calls.append(args)
            ts.apply("bot", "bot@x.invalid", branch="main")
            self.assertIn(["config", "user.name", "bot"], calls)
            self.assertIn(["config", "user.email", "bot@x.invalid"], calls)


class TestReverse(unittest.TestCase):
    def test_reverse_drops_identity_and_reapplies_solo(self):
        with tempfile.TemporaryDirectory() as d:
            cp = _FakeCP(protected=True)
            ts, mpath = _mk(d, identity="team", cp=cp)   # _mk gives a team manifest its engine_identity
            res = ts.reverse(branch="main")
            self.assertEqual(res["status"], "reversed")
            self.assertIn("guardrail-ack", res["message"])         # names the weakening consent
            out = _read(mpath)
            self.assertEqual(out["identity"], "solo")
            self.assertNotIn("engine_identity", out)

    def test_reverse_is_idempotent_on_solo(self):
        with tempfile.TemporaryDirectory() as d:
            ts, _ = _mk(d, identity="solo")
            self.assertEqual(ts.reverse(branch="main")["status"], "already")


class TestStatus(unittest.TestCase):
    def test_status_names_the_next_step_when_not_ready(self):
        with tempfile.TemporaryDirectory() as d:
            ts, _ = _mk(d, perm=None)
            s = ts.status("bot")
            self.assertEqual(s["tier"], "solo")
            self.assertTrue(s["next"])
            self.assertIn("collaborator", s["message"].lower())

    def test_status_ready_when_prereqs_met(self):
        with tempfile.TemporaryDirectory() as d:
            ts, _ = _mk(d, perm="write", codeowners=True)
            s = ts.status("bot")
            self.assertIsNone(s["next"])
            self.assertIn("ready to switch", s["message"].lower())

    def test_status_in_team_mode_confirms(self):
        with tempfile.TemporaryDirectory() as d:
            ts, _ = _mk(d, identity="team")             # _mk gives a team manifest its engine_identity
            s = ts.status()
            self.assertEqual(s["tier"], "team")
            self.assertIn("bot", s["message"])


class TestDemo(unittest.TestCase):
    def test_demo_self_check_passes(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = team_switch.main(["demo"])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("OK", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
