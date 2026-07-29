#!/usr/bin/env python3
"""Behavioral demo — the upgrade-overwrite disclosure, end to end.

Exercises the REAL tool logic (`overwritten_paths` → `compose_comment` → `reconcile`, the deployed gate, the
engine-authored-PR exemption, and the render sanitizer) against a fake GitHub transport and a fixture
overwrite set — faking ONLY the GitHub boundary and the tree-read, the way a workflow's real inputs arrive.
It can FAIL: each scenario asserts the observable behaviour, so a regression breaks the run.

Shows:
  (a) a deployed repo where the pull request edits an overlay file → one plain comment naming the file, the
      durable upstream home, and stating it does not block the merge;
  (b) a deployed repo where the pull request touches only a PRESERVED file → silence (no comment);
  (c) a rename to an attacker-chosen name → the crafted name is sanitized, never injected;
  (d) an engine-authored update PR → the disclosure is off;
  (e) which repositories it speaks in at all, against REAL throwaway git checkouts: the engine's own home is
      silent, a deployed copy is told, a case- or `.git`-skewed recorded home stays silent, an environment
      variable cannot flip either verdict, and a damaged engine record is reported rather than passed off as
      a quiet all-clear.

Fate: it TRAVELS, and is covered by the permanent regression test `test_overlay_disclosure.py`, which imports
and runs it — the sanctioned fate for a demo that is not on the first-run retirement list.
"""
import json
import os
import subprocess
import tempfile

import overlay_disclosure as od

# The engine's real home slug — used by scenario (e), which places throwaway checkouts relative to it.
# Distinct from HOME below, the fictional upstream the comment scenarios route to.
_REAL_HOME = "StarshipSuperjam/engine-template"

# The stand-in overwrite set (what module_manager.overlay_replace_paths() returns from a real deployed tree):
# an engine tool + a module manifest (the manifest category the overlay overwrites). A PRESERVED file
# (operator config, the CLAUDE.md fence) is simply NOT in this set, so it can never be warned about. The
# crafted rename target is present (a rename put it into the tree, so it is a set member).
CRAFTED = ".engine/tools/a`b](http://evil.com).py"


def _repo(tmp: str, name: str, *, origin: "str | None", home: str = _REAL_HOME) -> str:
    """A throwaway git checkout with a REAL origin remote and a REAL engine record. Nothing here is faked: the
    gate's whole job is to read this checkout's own origin and its own recorded update home."""
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, ".engine"), exist_ok=True)
    subprocess.run(["git", "-C", root, "init", "-q"], capture_output=True, check=False)
    if origin:
        subprocess.run(["git", "-C", root, "remote", "add", "origin", origin],
                       capture_output=True, check=False)
    with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
        json.dump({"engine_release": "0.0.0", "home_repository": home}, fh)
    return root


OVERWRITE = {".engine/tools/boot.py", ".engine/modules/core/manifest.json", CRAFTED}
HOME = "acme/engine-home"


class _FakeGitHub:
    """Records posts/edits; answers list_comments from its own store — the injected GitHub boundary. A posted
    comment is stored bot-authored, as the real Actions token would be."""

    def __init__(self, comments=None):
        self.comments = list(comments or [])
        self.posted = []
        self._id = 1000

    def __call__(self, method, path, body=None):
        if method == "GET" and "/comments" in path:
            return 200, list(self.comments)
        if method == "POST" and path.endswith("/comments"):
            self._id += 1
            self.comments.append({"id": self._id, "body": body["body"], "user": {"type": "Bot"}})
            self.posted.append(body["body"])
            return 201, {"id": self._id}
        if method == "PATCH" and "/comments/" in path:
            cid = int(path.rsplit("/", 1)[-1])
            for c in self.comments:
                if c["id"] == cid:
                    c["body"] = body["body"]
            return 200, {"id": cid}
        return 200, None


def _run(title, changed, overwrite):
    """Run the REAL filter + comment + reconcile against a fresh fake transport. Returns (status, fake)."""
    orig = od.module_manager.overlay_replace_paths
    od.module_manager.overlay_replace_paths = lambda: overwrite
    try:
        paths = od.overwritten_paths(changed)
        fake = _FakeGitHub()
        status = od.reconcile(od._Comments("acme/product", "tok", transport=fake), 7, paths, HOME)
    finally:
        od.module_manager.overlay_replace_paths = orig
    print(f"\n=== {title} ===")
    print(f"  changed files : {[c['filename'] for c in changed]}")
    print(f"  would overwrite: {paths or '(none)'}")
    print(f"  reconcile     : {status}; comments posted = {len(fake.posted)}")
    if fake.posted:
        print("  ---- comment ----")
        for line in fake.posted[0].splitlines():
            print(f"  | {line}")
    return status, fake


def main() -> int:
    # (a) deployed + an overlay-file edit → a comment naming the file + the home.
    status, fake = _run(
        "Deployed repo, a change to an engine file the update overwrites",
        [{"filename": ".engine/tools/boot.py", "status": "modified"}], OVERWRITE)
    assert status == "posted", status
    body = fake.posted[0]
    assert ".engine/tools/boot.py" in body, "the comment must name the file"
    assert "does not block your merge" in body, "the comment must say it is non-blocking"
    assert "upstream" in body and HOME in body, "the comment must route to the named durable home"

    # (b) deployed + only a preserved carve-out edit → silence.
    status, fake = _run(
        "Deployed repo, a change to only a PRESERVED file (operator config)",
        [{"filename": ".engine/operator-overrides.json", "status": "modified"}], OVERWRITE)
    assert status == "clean", status
    assert not fake.posted, "a preserved file must never draw a comment"

    # (c) a rename to an attacker-chosen name → the crafted name is SANITIZED, never injected.
    status, fake = _run(
        "Deployed repo, an engine file renamed to an attacker-chosen name",
        [{"filename": CRAFTED, "previous_filename": ".engine/tools/boot.py", "status": "renamed"}], OVERWRITE)
    assert status == "posted", status
    assert "http://evil.com" not in fake.posted[0], "a crafted link must never render"
    assert "`b]" not in fake.posted[0], "a backtick break-out must never render"

    # (d) engine-authored update PR, and self-hosting repo → the disclosure is off.
    print("\n=== The disclosure is off for the engine's own flows ===")
    exempt = od._is_engine_authored({"pull_request": {"head": {"ref": "engine-update-v0.2.0"}}})
    print(f"  engine-update PR exempt = {exempt}")
    assert exempt is True, "an engine-authored update PR must be exempt"

    # (e) The deployed gate itself, against REAL throwaway git checkouts. Nothing is faked on this path: git is
    # local and offline, and reading a checkout's own origin and manifest is the whole of what the gate does.
    print("\n=== Which repositories the disclosure speaks in (real throwaway checkouts) ===")
    with tempfile.TemporaryDirectory() as tmp:
        cells = [
            ("the engine's own home (origin == recorded home)",
             _repo(tmp, "home", origin=f"https://github.com/{_REAL_HOME}.git"), False),
            ("a deployed copy (origin differs)",
             _repo(tmp, "copy", origin="https://github.com/acme/product.git"), True),
            ("home recorded with different case and a .git suffix",
             _repo(tmp, "skew", origin=f"git@github.com:{_REAL_HOME.lower()}.git", home=f"{_REAL_HOME}.git"), False),
        ]
        for label, root, expected in cells:
            got = od.is_deployed(root)
            print(f"  {label}: speaks up = {got}")
            assert got is expected, f"{label}: expected {expected}, got {got}"

        # The environment must not be able to flip a verdict about a checkout on disk.
        home_root = _repo(tmp, "envhome", origin=f"https://github.com/{_REAL_HOME}.git")
        prior = os.environ.get("GITHUB_REPOSITORY")
        os.environ["GITHUB_REPOSITORY"] = "acme/product"
        try:
            got = od.is_deployed(home_root)
            print(f"  the engine's own home, with GITHUB_REPOSITORY claiming otherwise: speaks up = {got}")
            assert got is False, "an environment variable must not make the engine's own home look deployed"
        finally:
            if prior is None:
                os.environ.pop("GITHUB_REPOSITORY", None)
            else:
                os.environ["GITHUB_REPOSITORY"] = prior

        # A damaged engine record must be a LOUD failure, never a reassuring silence.
        broken = _repo(tmp, "broken", origin="https://github.com/acme/product.git")
        with open(os.path.join(broken, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not valid json ")
        try:
            od.is_deployed(broken)
            raise AssertionError("a damaged engine record must not read as a quiet 'nothing to disclose'")
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loud failure is the behaviour under demonstration
            print(f"  a damaged engine record: refuses to guess ({type(exc).__name__}) — reported, not silent")

    print("\nAll disclosure scenarios behaved as expected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
