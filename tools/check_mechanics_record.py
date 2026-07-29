#!/usr/bin/env python3
"""Require a structured mechanics record when Scratch behavior changes."""

from __future__ import annotations

import subprocess
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PROJECT_SOURCE = "src/xevious/project.json"
RECORD_PREFIX = "docs/mechanics/"
REQUIRED_FIELDS = (
    "Mechanic:",
    "Derived behavior:",
    "Reference provenance:",
    "Transfer class:",
    "Scratch interpretation:",
    "Scratch evidence:",
    "Acceptance criteria:",
    "Fidelity status:",
    "License status:",
    "Known deviations or uncertainty:",
)
NO_SOURCE_COPY_ATTESTATION = (
    "- [x] No assembly or other source code was copied into the Scratch project."
)
NO_ROM_HANDLING_ATTESTATION = (
    "- [x] No arcade ROM files were acquired, opened, extracted, or distributed."
)
MEDIA_PROVENANCE_ATTESTATION = (
    "- [x] Any transferred graphics or audio are recorded in "
    "`src/xevious/assets/provenance.json`."
)


class MechanicsRecordError(RuntimeError):
    """A behavior change has no usable mechanics evidence record."""


def validate_record(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise MechanicsRecordError(
            f"mechanics record must be a regular, non-symlink file: {path}"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MechanicsRecordError(f"cannot read mechanics record {path}: {exc}") from exc
    missing = [field for field in REQUIRED_FIELDS if field not in text]
    if missing:
        raise MechanicsRecordError(
            f"{path} is missing required fields: {', '.join(missing)}"
        )
    missing_attestations = [
        attestation
        for attestation in (
            NO_SOURCE_COPY_ATTESTATION,
            NO_ROM_HANDLING_ATTESTATION,
            MEDIA_PROVENANCE_ATTESTATION,
        )
        if attestation not in text
    ]
    if missing_attestations:
        raise MechanicsRecordError(
            f"{path} is missing required checked attestations: "
            + ", ".join(missing_attestations)
        )
    for field in REQUIRED_FIELDS:
        value = text.split(field, 1)[1].splitlines()[0].strip()
        if not value:
            raise MechanicsRecordError(f"{path} has an empty {field}")


def changed_paths(base: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MechanicsRecordError(
            f"cannot compare mechanics records with {base}: {result.stderr.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line]


def check(base: str) -> list[Path]:
    changed = changed_paths(base)
    if PROJECT_SOURCE not in changed:
        return []
    records = [
        ROOT / path
        for path in changed
        if path.startswith(RECORD_PREFIX)
        and path.endswith(".md")
        and Path(path).name.lower() != "readme.md"
    ]
    if not records:
        raise MechanicsRecordError(
            f"{PROJECT_SOURCE} changed without a changed record under "
            f"{RECORD_PREFIX}"
        )
    for record in records:
        validate_record(record)
    return records


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_mechanics_record.py BASE_REF", file=sys.stderr)
        return 2
    try:
        records = check(args[0])
    except MechanicsRecordError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if records:
        print("validated mechanics records: " + ", ".join(str(path) for path in records))
    else:
        print("project.json unchanged; no mechanics record required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
