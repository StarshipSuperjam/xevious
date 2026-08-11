#!/usr/bin/env python3
"""Negative fixture for engine/check/manifest-write-funnel: a tool that writes the deployed engine manifest
(.engine/engine.json, reached through the _engine_manifest_path helper) with a raw open() in write mode,
without routing through the guarded write funnel. The check must flag it as a manifest write that bypasses
the funnel (the exact out-of-tree-write shape #862/#923 close)."""
import json
import os

import validate


def _engine_manifest_path():
    return os.path.join(validate.ROOT, ".engine", "engine.json")


def bump_manifest(engine):
    # BYPASS: writes the deployed manifest slot directly, never through engine_write — the check bites this.
    with open(_engine_manifest_path(), "w", encoding="utf-8") as fh:
        json.dump(engine, fh, indent=2)
        fh.write("\n")
