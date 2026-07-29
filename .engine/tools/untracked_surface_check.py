#!/usr/bin/env python3
"""Untracked-surface signal (issue #281) — the thin custom/script entry for engine/check/untracked-surface.

Runs as a `custom/script` rule in CI and audit-prep. It relays module_coherence.untracked_surface_findings:
every file under the engine surface that git neither tracks nor ignores — a sync-conflict duplicate a
file-sync tool dropped, or a new engine file not yet committed — is a soft finding naming the file. CI runs
on a clean checkout, so this is silent there (the pollution it guards against is local-only); it surfaces on
a local validate run and in the audit digest. It NEVER blocks: the finding is soft and the underlying git
read is fail-soft.

It emits finding.v1 JSON on stdout and exits 0. Any internal error is swallowed to an empty array + exit 0,
so a transient git hiccup can never become a HARD fail-closed finding (the custom/script kind turns a
non-zero exit into a hard finding regardless of the rule's tier).
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    tier = os.environ.get("ENGINE_RULE_TIER") or "soft"
    try:
        import module_coherence  # deferred: resolves in the CLI / suite-runner context
        findings = module_coherence.untracked_surface_findings(tier)
    except Exception:  # noqa: BLE001 — never escalate an internal error to a hard fail-closed finding
        findings = []
    print(json.dumps(findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
