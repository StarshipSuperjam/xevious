#!/usr/bin/env python3
"""Standing template shape-spec check (engine issue #405).

The validator reads a prose surface's required shape from its TEMPLATE frontmatter (catalog -> template -> shape
-> instance), so the template's shape settings are the single, authoritative source kind_shape checks every
instance against. This check governs that source: every committed template that DECLARES shape settings (opens
with a `---` frontmatter block) must have settings that are well-formed against template.v1 — a required-sections
list, an optional allowed-sections list, and an optional length budget, and nothing else. Without this, a new or
edited template could ship a malformed shape-spec that merges green and then breaks (or silently no-ops) the shape
check for every file of that kind. This is the standing rule that replaces the per-surface template-vs-rule drift
tests those retired once the shape-spec had a single source.

Templates that declare NO shape settings are not governed here and are skipped: the HTML-comment scaffolds
(build-issue.md, control-plane-bootstrap.md, first-run.md) that are authoring aids, not shape-checked surfaces,
and the conduct scaffold, whose shape is data-driven (each code's own title) and governed by conduct_shape_check,
not a fixed-section spec.

Runs as a hard CI custom/script check: finding.v1 JSON on stdout, exit 0. A crash returns non-zero, which the kind
turns into a hard fail-closed finding (a guard can never silently pass). The template.v1 schema is always read
from the real repository; ENGINE_ROOT only redirects WHICH templates directory is scanned, so the negative-fixture
meta-check can seed a malformed template without shipping a copy of the schema.
"""
from __future__ import annotations
import glob as _glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (finding.v1, ROOT, frontmatter, read)

_TEMPLATES_REL = os.path.join(".engine", "templates")
_SCHEMA_REL = os.path.join(".engine", "schemas", "template.v1.json")


def _message(rel: str, detail: str) -> str:
    """Operator-facing finding: what is wrong, why it matters, and the fix — in plain words."""
    return (f"The template `{rel}` sets out the required shape the engine both writes new files from and checks "
            f"them against, but that shape is not valid: {detail}. Until it is fixed, the engine cannot reliably "
            f"check any file of that kind. Correct the settings at the top of `{rel}` to match the shape the other "
            f"templates use — a list of required sections, an optional list of allowed sections, and an optional "
            f"length budget, and nothing else — before merging.")


def check(root: str | None = None) -> list:
    """Every committed template that declares shape settings must conform to template.v1; one hard finding per
    violation (empty = all well-formed). A template with no `---` frontmatter block declares no shape and is
    skipped."""
    from jsonschema import Draft202012Validator     # lazy: tool-runtime dep
    root = root or validate.ROOT
    schema = validate.load_json(os.path.join(validate.ROOT, _SCHEMA_REL))
    findings = []
    for path in sorted(_glob.glob(os.path.join(root, _TEMPLATES_REL, "*.md"))):
        rel = os.path.relpath(path, root)
        if not validate.read(path).startswith("---"):
            continue                                # declares no shape settings — not governed here
        try:
            spec = validate.frontmatter(path)
        except Exception as exc:                    # malformed YAML — fail loud, not a silent skip
            findings.append(validate.finding("hard", _message(rel, f"its settings block could not be read ({exc})")))
            continue
        for err in sorted(Draft202012Validator(schema).iter_errors(spec), key=lambda e: list(e.path)):
            findings.append(validate.finding("hard", _message(rel, err.message)))
    return findings


def main() -> int:
    # ENGINE_ROOT (unset in production) points the scan at a seeded mini-tree so the meta-check witnesses this
    # check biting a real malformed template.
    print(json.dumps(check(validate.env_override_path("ENGINE_ROOT"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
