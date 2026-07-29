#!/usr/bin/env python3
"""Audit-digest seal gate (audit-library) — the thin custom/script entry for
engine/check/audit-digest-fingerprint.

Runs as a `custom/script` rule in the CI suite: it confirms the committed self-review file
(`.engine/audits/audit-digest.md`) still matches the check-value the audit recorded over it, so a silent
hand-edit of the file turns engine-ci red until the audit re-runs and re-seals it. While no self-review
file exists yet (none does until the scheduled run first writes one), it passes — there is nothing to
verify.

It reads local committed files only — no network, no token — so it runs unchanged in the head-checkout
engine-ci context. It emits finding.v1 JSON on stdout (the custom/script machine channel) and returns 0 on
a successful evaluation: an empty array when the seal is intact (or no digest exists), one `hard` finding
on a mismatch or a malformed file. An internal crash returns non-zero, which the custom/script kind turns
into a hard fail-closed finding.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_digest  # noqa: E402
import validate  # noqa: E402  (the negative-fixture meta-check's input-substitution seam)


def main() -> int:
    # ENGINE_AUDIT_DIGEST_PATH (unset in production) lets the negative-fixture meta-check point the
    # seal gate at a seeded tampered digest, so it is witnessed biting a real bad input (#286).
    f = audit_digest.check(validate.env_override_path("ENGINE_AUDIT_DIGEST_PATH"))
    print(json.dumps([f] if f["severity"] == "hard" else []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
