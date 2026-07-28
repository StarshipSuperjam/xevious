#!/usr/bin/env python3
"""Tests for the in-tool demo failure-path floor (engine-template #171). The floor must FLAG a
print-only showcase demo (whose only exit is a literal 0/None), PASS a demo that self-checks (an explicit
non-zero return, an inline `0 if ok else 1`, or delegation to a can-fail handler), NOT be fooled by a non-zero
return that lives only inside a nested helper scope, exclude the standalone `demo_*.py` and `test_*.py`
populations, and report the live repo as clean (every shipped in-tool demo has a reachable failure path)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import in_tool_demo_failure_path_check as floor  # noqa: E402
import validate  # noqa: E402

_SHOWCASE = (
    "def _demo():\n"
    "    print('just showing')\n"
    "    return 0\n\n\n"
    "def main(argv):\n"
    "    if argv and argv[0] == 'demo':\n"
    "        return _demo()\n"
    "    return 2\n"
)

_SELF_CHECK = (
    "def _demo():\n"
    "    ok = compute()\n"
    "    if not ok:\n"
    "        return 1\n"
    "    return 0\n\n\n"
    "def main(argv):\n"
    "    if argv and argv[0] == 'demo':\n"
    "        return _demo()\n"
    "    return 2\n"
)

_INLINE_SELF_CHECK = (
    "def main(argv):\n"
    "    if argv and argv[0] == 'demo':\n"
    "        return 0 if check_it() else 1\n"
    "    return 2\n"
)

# A non-zero `return` that lives ONLY inside a nested helper must NOT count as the demo's own failure path.
_NESTED_ONLY = (
    "def _demo():\n"
    "    def helper():\n"
    "        return 1\n"
    "    print('showcase with a nested helper')\n"
    "    return 0\n\n\n"
    "def main(argv):\n"
    "    if argv and argv[0] == 'demo':\n"
    "        return _demo()\n"
    "    return 2\n"
)


def _fixture(root: str, files: dict) -> None:
    """A throwaway tool tree the floor can scan: `.engine/tools/<files>` plus an empty retired-asset manifest."""
    tools = os.path.join(root, ".engine", "tools")
    os.makedirs(tools)
    prov = os.path.join(root, ".engine", "provisioning")
    os.makedirs(prov)
    with open(os.path.join(prov, "first-run-assets.json"), "w", encoding="utf-8") as fh:
        fh.write('{"files": [], "directories": []}')
    for name, src in files.items():
        with open(os.path.join(tools, name), "w", encoding="utf-8") as fh:
            fh.write(src)


class TestInToolDemoFailurePathFloor(unittest.TestCase):
    def test_flags_a_showcase_demo(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture(d, {"showcase_tool.py": _SHOWCASE})
            findings = floor.check(d)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["location"]["file"], ".engine/tools/showcase_tool.py")

    def test_passes_self_checking_demos(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture(d, {"sc_tool.py": _SELF_CHECK, "inline_tool.py": _INLINE_SELF_CHECK})
            self.assertEqual(floor.check(d), [])

    def test_a_nested_only_nonzero_return_does_not_count(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture(d, {"nested_tool.py": _NESTED_ONLY})
            self.assertEqual(len(floor.check(d)), 1)

    def test_excludes_standalone_demo_and_test_files(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture(d, {"demo_thing.py": _SHOWCASE, "test_thing.py": _SHOWCASE})
            self.assertEqual(floor.check(d), [])

    def test_live_repo_is_clean(self):
        # Every in-tool demo in this repo has a reachable failure path — the floor is green on HEAD, and this
        # guards against a future showcase demo slipping in.
        self.assertEqual(floor.check(validate.ROOT), [])


if __name__ == "__main__":
    unittest.main()
