#!/usr/bin/env python3
"""Ensure a verified local clone of the pinned arcade reference exists.

The project's fidelity reference is the public ``jotd666/xevious`` repository at
the commit pinned in ``docs/spec/index.md`` (and in ``tools/reference_extract.py``
as ``PINNED_COMMIT`` / ``EXPECTED_SHA256``). The spec keeps no copy of it; this
tool makes a throwaway, regenerable clone at that exact commit so a build session
or CI can open the cited source lines and re-verify the derived data.

The clone lives OUTSIDE the repository by default (a shared cache, so many git
worktrees reuse one clone and it can never be committed), overridable for CI:

    XEVIOUS_REFERENCE_DIR   environment override
    --dir PATH              per-invocation override
    default                 ${XDG_CACHE_HOME:-~/.cache}/xevious-reference/jotd666-xevious

Subcommands:
    ensure   clone at the pin if missing (or fetch+checkout the pin if the
             checkout is at the wrong commit), verify the five source hashes,
             print the path. A no-op with no network when already at the pin.
    path     print the verified checkout path, or fail if absent/unverified.
             Never touches the network.
    verify   re-verify the five source hashes only. Never touches the network.

Hash verification reuses ``reference_extract.SourceFile`` so there is exactly one
implementation of "is this the pinned commit's bytes"; a mismatch is fatal.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reference_extract as rx  # noqa: E402  (module object, so the hash table is read live)

# The public reference. `reference_extract` carries only the pin and the hashes,
# not a fetch URL, so the clone source is declared here.
REMOTE = "https://github.com/jotd666/xevious.git"


class CheckoutError(RuntimeError):
    """The reference checkout is missing, at the wrong commit, or tampered."""


def default_dir() -> Path:
    """Where the clone lives when neither --dir nor the env override is set."""
    env = os.environ.get("XEVIOUS_REFERENCE_DIR")
    if env:
        return Path(env).expanduser()
    cache_root = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache_root).expanduser() if cache_root else Path.home() / ".cache"
    return base / "xevious-reference" / "jotd666-xevious"


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def head_commit(path: Path) -> str | None:
    """The checkout's current HEAD SHA, or None if it is not a git repo yet."""
    if not (path / ".git").exists():
        return None
    result = _git(path, "rev-parse", "HEAD", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def verify_files(path: Path) -> None:
    """Raise CheckoutError unless all five source files match the pinned hashes."""
    for relpath in rx.EXPECTED_SHA256:
        try:
            rx.SourceFile(path, relpath)
        except FileNotFoundError as exc:
            raise CheckoutError(
                f"{path}: missing reference file {relpath}; the checkout is not a "
                f"complete clone at the pin"
            ) from exc
        except rx.ExtractionError as exc:
            raise CheckoutError(str(exc)) from exc


def clone_at_pin(path: Path) -> None:
    """Fetch exactly the pinned commit into `path` and check it out, detached.

    GitHub serves a shallow fetch of any commit reachable from a ref, so a single
    ``git fetch --depth 1 origin <pin>`` brings just that commit. If the pinned
    commit later becomes unreachable upstream (a force-push), this fails loudly.
    """
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        _git(path, "init", "-q")
    # Idempotent remote setup.
    existing = _git(path, "remote", check=False).stdout.split()
    if "origin" not in existing:
        _git(path, "remote", "add", "origin", REMOTE)
    else:
        _git(path, "remote", "set-url", "origin", REMOTE)
    try:
        _git(path, "fetch", "--depth", "1", "origin", rx.PINNED_COMMIT)
    except subprocess.CalledProcessError as exc:
        raise CheckoutError(
            f"cannot fetch the pinned commit {rx.PINNED_COMMIT} from {REMOTE} "
            f"({exc.stderr.strip() or exc}). The pinned reference is a public "
            f"repository; check your connection, confirm the commit is still "
            f"reachable upstream, or point --dir at an existing clone."
        ) from exc
    _git(path, "checkout", "--detach", "FETCH_HEAD", "-q")


def ensure(path: Path, *, allow_network: bool = True) -> Path:
    """Return a verified checkout at `path`, cloning or correcting it if needed."""
    head = head_commit(path)
    if head == rx.PINNED_COMMIT:
        verify_files(path)  # confirm the bytes, no network
        return path
    if not allow_network:
        raise CheckoutError(
            f"{path}: HEAD is {head or 'absent'}, not the pinned commit "
            f"{rx.PINNED_COMMIT}, and network access is disabled"
        )
    clone_at_pin(path)
    if head_commit(path) != rx.PINNED_COMMIT:
        raise CheckoutError(
            f"{path}: after fetching, HEAD is not the pinned commit "
            f"{rx.PINNED_COMMIT}"
        )
    verify_files(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("ensure", "path", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--dir", type=Path, default=None)

    args = parser.parse_args(argv)
    path = args.dir if args.dir is not None else default_dir()

    try:
        if args.command == "ensure":
            resolved = ensure(path)
            print(resolved)
            return 0
        if args.command == "path":
            if head_commit(path) != rx.PINNED_COMMIT:
                raise CheckoutError(
                    f"no verified reference checkout at {path}; run: "
                    f"python tools/reference_checkout.py ensure"
                )
            verify_files(path)
            print(path)
            return 0
        if args.command == "verify":
            verify_files(path)
            print(f"verified the pinned reference at {path}")
            return 0
    except CheckoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
