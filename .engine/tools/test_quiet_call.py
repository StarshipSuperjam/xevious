"""Tests for quiet_call, plus the durability guard that keeps the demo-output flood from returning.

`quiet_call.run(main)` runs a callable with its stdout captured and returns its exit code. Four
security-floor self-tests use it to attest a demo's `main()` exits 0 WITHOUT printing the walkthrough
into the test run — which, without unittest's `-b`, buries the `Ran N … OK` summary and forces a
re-run. The guard below fails the suite if any `test_*.py` calls a demo `main()` DIRECTLY (which would
flood again) instead of going through the helper — turning "the first test command stays clean" into
an enforced invariant rather than a one-time cleanup.
"""
import ast
import contextlib
import io
import os
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quiet_call  # noqa: E402

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))

# The forbidden shape is a demo function invoked DIRECTLY (called, with parens) inside a test —
# `demo_x.main()`, `demo_x._scan_planted_secret()`, any of them — which prints before the helper can
# capture it. The sanctioned form passes the function by REFERENCE — `quiet_call.run(demo_x.some_fn)` —
# an attribute load, not a call. We detect this from each file's PARSE TREE, not its text, resolving the
# file's own imports first, so that (1) an aliased `import demo_x as d; d.main()` and a `from demo_x
# import main; main()` are caught — a line regex keyed on the literal `demo_` prefix misses both — and
# (2) a demo call named only inside a comment or string is NOT a false alarm. A bare demo CONSTANT
# (`demo_x.SOME_NAME`, no call) does not print and is left alone.


def _demo_bindings(tree):
    """The local names in one parsed test module that refer to a `demo_*` module or one of its
    functions: `import demo_x [as d]` binds a MODULE name; `from demo_x import f [as g]` binds a
    FUNCTION name. Returns (module_names, func_names) — the names a direct call would appear under,
    whatever alias the file chose."""
    modules, funcs = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0].startswith("demo_"):
                    modules.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0].startswith("demo_"):
                for a in node.names:
                    funcs.add(a.asname or a.name)
    return modules, funcs


def _direct_demo_call_lines(source):
    """The line numbers in `source` where a demo function is CALLED directly, resolved through the
    file's own imports so an alias or a from-import cannot hide the call. A reference handed to
    quiet_call.run is an attribute/name load (not a Call node), so it is correctly not reported."""
    tree = ast.parse(source)
    modules, funcs = _demo_bindings(tree)
    lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id in modules:
            lines.add(node.lineno)          # demo.main() / demo_x.fn()  (plain or aliased module)
        elif isinstance(fn, ast.Name) and fn.id in funcs:
            lines.add(node.lineno)          # main()  (from demo_x import main)
    return sorted(lines)


class TestQuietCallCapturesStdoutAndReturnsCode(unittest.TestCase):
    def test_returns_the_callables_result(self):
        # exit codes (main legs) AND bools (predicate legs like _scan_planted_secret) both pass through
        self.assertEqual(quiet_call.run(lambda: 0), 0)
        self.assertEqual(quiet_call.run(lambda: 7), 7)
        self.assertIs(quiet_call.run(lambda: True), True)
        self.assertIs(quiet_call.run(lambda: False), False)

    def test_captures_stdout_so_nothing_leaks(self):
        # Redirect the REAL stdout to a buffer; if quiet_call did its job, the callable's own prints
        # were swallowed by ITS inner capture and this outer buffer stays empty.
        outer = io.StringIO()
        with contextlib.redirect_stdout(outer):
            code = quiet_call.run(lambda: (print("WOULD FLOOD THE TAIL " * 8), 0)[1])
        self.assertEqual(code, 0)
        self.assertEqual(outer.getvalue(), "",
                         "the callable's stdout must be captured by quiet_call, not leak to the run")

    def test_passes_through_args_and_kwargs(self):
        self.assertEqual(quiet_call.run(lambda a, b=0: a + b, 3, b=4), 7)

    def test_exception_propagates_not_swallowed(self):
        with self.assertRaises(ValueError):
            quiet_call.run(lambda: (_ for _ in ()).throw(ValueError("boom")))


class TestDurabilityGuardResolvesImports(unittest.TestCase):
    """The guard's detector is name-agnostic: it catches a direct demo call however the demo was
    imported (plain, aliased, from-import), and does NOT flag the sanctioned reference form, a bare
    constant, a demo name inside a comment/string, or a same-shaped call on a non-demo module. Pinning
    these here means a regression to a fragile text match (the aliased/from blind spot that motivated
    this guard) fails a test instead of silently re-opening the flood."""

    def _lines(self, src):
        return _direct_demo_call_lines(textwrap.dedent(src))

    def test_flags_plain_attribute_call(self):
        self.assertEqual(self._lines("import demo_x\ndemo_x.main()\n"), [2])

    def test_flags_aliased_module_call(self):
        self.assertEqual(self._lines("import demo_x as d\nd.main()\n"), [2])

    def test_flags_from_import_call(self):
        self.assertEqual(self._lines("from demo_x import main\nmain()\n"), [2])

    def test_flags_aliased_from_import_call(self):
        self.assertEqual(self._lines("from demo_x import main as m\nm()\n"), [2])

    def test_ignores_reference_passed_to_quiet_call(self):
        self.assertEqual(self._lines("import demo_x as d\nquiet_call.run(d.main)\n"), [])

    def test_ignores_bare_constant_access(self):
        self.assertEqual(self._lines("import demo_x\nname = demo_x.SOME_NAME\n"), [])

    def test_ignores_demo_call_in_comment_or_string(self):
        self.assertEqual(self._lines("import demo_x\ns = 'demo_x.main()'  # see demo_x.main()\n"), [])

    def test_ignores_same_shaped_call_on_a_non_demo_module(self):
        self.assertEqual(self._lines("import first_run_reference_closure_check as frc\nfrc.main()\n"), [])


class TestNoTestCallsADemoFunctionDirectly(unittest.TestCase):
    """Durability guard: no `test_*.py` may call a demo function directly — it floods the run without
    `-b` and re-buries the summary. Route it through `quiet_call.run(demo_x.some_fn)`. Resolving each
    file's imports means an aliased (`import demo_x as d`) or from-imported demo call cannot slip past,
    and reading the parse tree means a demo name inside a comment or string is not a false alarm. This is
    what makes the clean-first-run property survive the next demo-backed test someone adds — however they
    choose to import the demo. (This guard file itself imports no demo, so it needs no self-exemption:
    the shapes it names in prose are strings the parser never sees as calls.)"""

    def test_no_direct_demo_calls_in_any_test_file(self):
        offenders = []
        for dirpath, _dirs, files in os.walk(TOOLS_DIR):
            for fn in files:
                if not (fn.startswith("test_") and fn.endswith(".py")):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8") as fh:
                    source = fh.read()
                for lineno in _direct_demo_call_lines(source):
                    offender = source.splitlines()[lineno - 1].strip()
                    offenders.append(f"{os.path.relpath(path, TOOLS_DIR)}:{lineno}: {offender}")
        self.assertEqual(
            offenders, [],
            "a test calls a demo function directly — without `-b` it floods the run and buries the "
            "pass/fail summary. Pass the function by reference to quiet_call.run(demo_x.some_fn), which "
            "captures the walkthrough:\n  " + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
