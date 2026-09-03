#!/usr/bin/env python3
"""Produce the playtest build only when it is faithful to the pinned reference.

This is the one command a build session runs to hand the operator a playable
`.sb3` for the playtest gate. It refuses to emit a build until the reference
checks pass, so a build that adapted to a wrong spec (a citation that no longer
points where it claims) cannot reach the playtest — the exact gap the slice-8
Toroid regression exposed, where a build was handed over, played, and approved
before anyone read the arcade source.

It runs, in order and stopping at the first failure:

    1. reference_checkout ensure  — a verified clone at the pin
    2. reference_extract --verify — the generated data still re-derives
    3. reference_citations        — every citation still resolves
    4. scratch_project build      — only now is the .sb3 built

then prints the archive path, its SHA-256, and the citation summary. The build
step is a subprocess of the guarded builder, unmodified.

A change that produces a playtest build is a gameplay change, and a gameplay
build legitimately depends on the reference; if the checkout cannot be obtained
(cold cache and upstream unreachable), the tool says so and how to fix it rather
than emit an unverified build.

Usage:
    python tools/playtest_package.py [--dir CHECKOUT] [--output dist/Xevious.sb3]
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reference_checkout as checkout  # noqa: E402
import reference_citations as citations  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class HandoverError(RuntimeError):
    """A reference check failed; no playtest build was produced."""


def _run(label: str, args: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, *args], cwd=ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise HandoverError(
            f"{label} failed; refusing to build a playtest package.\n"
            + (result.stderr.strip() or result.stdout.strip())
        )


def package(checkout_dir: Path | None, output: Path) -> tuple[Path, str, int]:
    # 1. A verified checkout at the pin.
    try:
        ref = checkout.ensure(checkout_dir if checkout_dir is not None
                              else checkout.default_dir())
    except checkout.CheckoutError as exc:
        raise HandoverError(
            f"no verified reference checkout, so fidelity cannot be confirmed: {exc}"
        ) from exc

    # 2. The generated data still re-derives from the pin.
    _run("reference_extract --verify",
         ["tools/reference_extract.py", "--verify", "--checkout", str(ref)])

    # 3. Every citation still resolves.
    _, unresolved = citations.check(ref, [ROOT / "docs" / "spec", ROOT / "docs" / "mechanics"])
    if unresolved:
        lines = "\n".join(f"  {c.doc}:{c.line} {c.raw}" for c in unresolved[:20])
        raise HandoverError(
            f"{len(unresolved)} citation(s) do not resolve against the pin; a build "
            f"is not ready for handover while the spec cites the source wrongly:\n{lines}"
        )

    # 4. Only now build the playable archive.
    _run("scratch_project build",
         ["tools/scratch_project.py", "build", "--output", str(output)])

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return output, digest, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "Xevious.sb3")
    args = parser.parse_args(argv)
    try:
        path, digest, _ = package(args.dir, args.output)
    except HandoverError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"playtest build ready: {path}")
    print(f"sha256: {digest}")
    print("every reference citation resolves and the generated data re-derives at the pin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
