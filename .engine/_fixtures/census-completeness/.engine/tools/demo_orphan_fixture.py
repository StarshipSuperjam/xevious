#!/usr/bin/env python3
"""Fixture-only orphan demo for engine/check/census-completeness (#424 U13c).

This file exists ONLY inside the check's negative fixture tree. On this seeded mini-tree it is NOT on the
first-run removal list (its first-run-assets.json omits it) and no surviving non-demo file imports it — so the
census-completeness check must flag it as orphan drift (the `hard-check-bite` witness that the guard bites).
It is never executed; the check only enumerates it by name. It lives under `.engine/_fixtures/`, not the real
`.engine/tools/`, so the production check never sees it.
"""
