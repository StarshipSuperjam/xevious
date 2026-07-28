"""#599 Slice 4 (Gap 3B) — the systematic deployed-self-test belt.

A deployed repo runs this engine's ENTIRE `test_*.py` suite in its own `engine-ci`. A self-test that asserts a
construction-repo-only invariant without a deployed-skip guard therefore fails in every deployed repo and blocks its
upgrade pull request — the #599 class at the test layer (it is how test_self_map / test_hard_check_bite failed a
deployed v0.2.0->v0.3.2 upgrade). This belt catches that at CONSTRUCTION cut time instead: it projects THIS engine to
a deployed shape (retire the first-run assets, swap in the deployed floors, regenerate the deployed-state indexes)
and runs the suite against that projection, asserting green.

Construction-scoped by design: the projected tree is NOT a construction checkout, so the nested run skips this very
test (no recursion), and a deployed repo — where this ships inert — already runs the suite directly in its engine-ci.
Robust: it skips cleanly where git or an offline `git archive` is unavailable rather than red-failing.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                          # noqa: E402
import module_manager                    # noqa: E402  (_archive_tree — the offline HEAD projection source)
import census_completeness_check as _ccc  # noqa: E402  (reuse its construction-repo marker read, not a new copy)

_NESTED_ENV = "ENGINE_NESTED_SELFTEST"    # set on the child run so this belt never recurses into itself


class _ProjectionError(RuntimeError):
    """A failure while BUILDING the deployed projection (git/index-regen setup) — a harness problem, distinct
    from the suite-under-test failing. The test skips on it rather than red-flagging a construction-only guard."""


def _project_to_deployed(dest: str) -> None:
    """Turn an archived home-repo tree at `dest` into the shape a deployed repo actually runs — the same
    projection first-run provisioning applies: RETIRE the first-run-only assets (read from the tree's own
    self-describing manifest), git-init with a DEPLOYED origin remote (origin != recorded home, so the re-keyed
    home-repo checks read this as a copy and deployed-state reads resolve), and REGENERATE the deployed-state
    indexes (self-map + knowledge graph) that now describe the reduced surface. Since #323 the committed root
    CLAUDE.md/AGENTS.md ARE the adopter floor a copy inherits, so there is no separate floor to swap in."""
    manifest = validate.load_json(os.path.join(dest, ".engine", "provisioning", "first-run-assets.json"))
    for rel in list(manifest.get("files", [])) + list(manifest.get("directories", [])):
        p = os.path.join(dest, rel)
        if os.path.isfile(p):
            os.remove(p)
        elif os.path.isdir(p):
            shutil.rmtree(p)
    env = {**os.environ, _NESTED_ENV: "1"}
    for cmd in (["init", "-b", "main"],
                ["remote", "add", "origin", "https://github.com/acme/deployed-product.git"],
                ["-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
                ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "deployed"]):
        r = subprocess.run(["git", "-C", dest, *cmd], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:      # a harness-setup failure — NOT a construction-only-test finding
            raise _ProjectionError(f"git {cmd[0]} failed: {(r.stderr or '').strip()[-300:]}")
    for gen in ("self_map.py", "knowledge_gen.py"):
        r = subprocess.run([sys.executable, os.path.join("tools", gen), "generate"],
                           cwd=os.path.join(dest, ".engine"), env=env, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:      # regenerating the deployed indexes is setup, not the suite under test
            raise _ProjectionError(f"{gen} generate failed: {(r.stderr or '').strip()[-300:]}")


class TestDeployedSelfTests(unittest.TestCase):
    @unittest.skipUnless(_ccc._in_home_repo(),
                         "the belt runs where a release is cut (the construction repo); it ships inert to a "
                         "deployed repo, whose own engine-ci runs the suite directly")
    @unittest.skipIf(os.environ.get(_NESTED_ENV),
                     "this IS the nested deployed run the belt spawned — do not recurse")
    def test_deployed_projection_passes_the_whole_self_test_suite(self):
        if not shutil.which("git"):
            self.skipTest("git is unavailable")
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "deployed")
            try:
                module_manager._archive_tree("HEAD", dest)     # offline; no network, no token
            except Exception as exc:                            # noqa: BLE001 — no git object / shallow -> skip
                self.skipTest(f"could not archive HEAD offline ({exc})")
            try:
                _project_to_deployed(dest)
            except _ProjectionError as exc:                     # a harness-setup failure is not a real finding
                self.skipTest(f"could not build the deployed projection ({exc})")
            env = {**os.environ, _NESTED_ENV: "1"}
            run = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tools", "-p", "test_*.py", "-b"],
                cwd=os.path.join(dest, ".engine"), env=env, capture_output=True, text=True, timeout=1200)
            self.assertEqual(
                run.returncode, 0,
                "a self-test failed against a DEPLOYED projection of this engine — most likely a self-test that "
                "asserts a construction-only invariant without a deployed-skip guard (the #599 class). Guard it with "
                "skipUnless(construction), as test_self_map / test_hard_check_bite do. Tail of the run:\n"
                + (run.stderr or "")[-3000:])


if __name__ == "__main__":
    unittest.main()
