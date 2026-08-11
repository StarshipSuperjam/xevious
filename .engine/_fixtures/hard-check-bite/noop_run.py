#!/usr/bin/env python3
"""Test-support fixture for the negative-fixture meta-check's applicability tests
(test_hard_check_bite.py :: TestFailedBiteApplicability).

A custom/script check that RUNS and stays SILENT: it exits 0 and emits an empty finding.v1 array, so the
meta-check's `_cover_script_instance` reaches its "ran but did not bite" fall-through, where the bounded
applicability declarations (#512 home-scoped, #531 declared-environment) are consulted. It is NOT a live
check — it lives under the coverage-exempt `_fixtures` namespace and is named only by that test, never
registered under `.engine/check/`. It replaced the disposition-issue-resolution check, which the test used
as its run-but-no-bite vehicle before that check was removed.
"""
import json

print(json.dumps([]))
