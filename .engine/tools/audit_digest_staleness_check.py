#!/usr/bin/env python3
"""Audit-digest freshness signal (audit-library) — the thin custom/script entry for
engine/check/audit-digest-staleness.

Runs as a `custom/script` rule in the `audit-prep` suite (report-only): it reads the run-date on the
committed self-review file (`.engine/audits/audit-digest.md`) and warns when the engine has not
self-reviewed in more than the staleness bound, OR when no self-review has run yet — so a self-review that
quietly stopped (an expired token, a disabled schedule, a setup never finished) is surfaced on the
operator's return rather than missed. It NEVER blocks: the audit-prep suite is report-only, and the
finding is `soft`; the wall-clock read it makes is why it stays out of the reproducible CI gate.

It emits finding.v1 JSON on stdout and returns 0 on a successful evaluation: an empty array when the digest
is current, one `soft` finding when it is stale, absent, or unreadable. An internal crash returns non-zero,
which the custom/script kind turns into a hard fail-closed finding.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_digest  # noqa: E402


def main() -> int:
    f = audit_digest.staleness()  # now defaults to today() inside; wall-clock read lives here
    print(json.dumps([f] if f["severity"] != "note" else []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
