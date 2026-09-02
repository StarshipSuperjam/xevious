"""Guards for tools/reference_checkout.py — the pinned reference clone helper.

Every test here runs offline. The reference's real bytes are never needed: a
tiny fake checkout is built in a tempdir and ``reference_extract.EXPECTED_SHA256``
is patched to the fakes' own digests, which is why both the tool and these tests
must share one ``reference_extract`` module object (a plain ``import``, never a
fresh ``importlib`` load, or the patch would not reach the ``SourceFile`` the
tool builds).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import reference_extract as rx  # noqa: E402
import reference_checkout as checkout  # noqa: E402


def _write_fake_sources(root: Path) -> dict[str, str]:
    """Write the five reference files with arbitrary bytes; return their digests."""
    digests: dict[str, str] = {}
    for relpath in rx.EXPECTED_SHA256:
        p = root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        body = f"; fake {relpath}\nlabel_{p.stem}:\n\tnop\n".encode()
        p.write_bytes(body)
        digests[relpath] = hashlib.sha256(body).hexdigest()
    return digests


def _git(path: Path, *args: str) -> None:
    # A committer identity is supplied inline so this works on a CI runner that
    # has no global git identity configured.
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


class DefaultDir(unittest.TestCase):
    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"XEVIOUS_REFERENCE_DIR": "/tmp/xref-override"}):
            self.assertEqual(checkout.default_dir(), Path("/tmp/xref-override"))

    def test_xdg_cache_is_used_when_no_override(self):
        env = {k: v for k, v in os.environ.items() if k != "XEVIOUS_REFERENCE_DIR"}
        env["XDG_CACHE_HOME"] = "/tmp/xdg"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                checkout.default_dir(),
                Path("/tmp/xdg/xevious-reference/jotd666-xevious"),
            )


class VerifyFiles(unittest.TestCase):
    def test_accepts_a_fake_checkout_with_patched_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digests = _write_fake_sources(root)
            with mock.patch.object(rx, "EXPECTED_SHA256", digests):
                checkout.verify_files(root)  # no raise

    def test_rejects_a_tampered_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digests = _write_fake_sources(root)
            (root / rx.SUB).write_bytes(b"; tampered\n")
            with mock.patch.object(rx, "EXPECTED_SHA256", digests):
                with self.assertRaisesRegex(checkout.CheckoutError, "does not match the pinned"):
                    checkout.verify_files(root)

    def test_reports_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digests = _write_fake_sources(root)
            (root / rx.SUB).unlink()
            with mock.patch.object(rx, "EXPECTED_SHA256", digests):
                with self.assertRaisesRegex(checkout.CheckoutError, "missing reference file"):
                    checkout.verify_files(root)


class Ensure(unittest.TestCase):
    def test_is_a_no_op_when_already_at_the_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digests = _write_fake_sources(root)
            _git(root, "init", "-q")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "fake pin")
            sha = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            # Any fetch attempt must fail the test: an at-pin ensure is offline.
            with mock.patch.object(rx, "EXPECTED_SHA256", digests), \
                 mock.patch.object(rx, "PINNED_COMMIT", sha), \
                 mock.patch.object(checkout, "clone_at_pin",
                                   side_effect=AssertionError("must not fetch")):
                self.assertEqual(checkout.ensure(root), root)

    def test_refuses_a_wrong_commit_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_sources(root)
            _git(root, "init", "-q")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "not the pin")
            with mock.patch.object(rx, "PINNED_COMMIT", "0" * 40):
                with self.assertRaisesRegex(checkout.CheckoutError, "not the pinned commit"):
                    checkout.ensure(root, allow_network=False)


class CloneAtPin(unittest.TestCase):
    def test_clones_and_verifies_from_a_local_origin(self):
        # Exercise clone_at_pin end to end offline: a local git repo stands in for
        # the remote, REMOTE is pointed at it, and the pin/hashes are patched to
        # the fake's.
        with tempfile.TemporaryDirectory() as tmp:
            origin = Path(tmp) / "origin"
            origin.mkdir()
            digests = _write_fake_sources(origin)
            _git(origin, "init", "-q")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-q", "-m", "pin")
            sha = subprocess.run(
                ["git", "-C", str(origin), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            dest = Path(tmp) / "cache"
            with mock.patch.object(rx, "EXPECTED_SHA256", digests), \
                 mock.patch.object(rx, "PINNED_COMMIT", sha), \
                 mock.patch.object(checkout, "REMOTE", origin.as_uri()):
                got = checkout.ensure(dest)
                self.assertEqual(checkout.head_commit(got), sha)
                checkout.verify_files(got)  # no raise

    def test_prepare_failure_raises_checkout_error(self):
        # A file where the cache dir should be makes `git init` fail; the tool
        # must surface a clean CheckoutError, not a raw traceback.
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "cache"
            blocker.write_text("not a directory-friendly path")
            with mock.patch.object(rx, "PINNED_COMMIT", "0" * 40):
                with self.assertRaises(checkout.CheckoutError):
                    checkout.clone_at_pin(blocker)


class PathSubcommand(unittest.TestCase):
    def test_fails_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope"
            self.assertEqual(checkout.main(["path", "--dir", str(missing)]), 1)


if __name__ == "__main__":
    unittest.main()
