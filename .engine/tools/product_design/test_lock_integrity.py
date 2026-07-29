#!/usr/bin/env python3
"""Regression tests for the lock-integrity re-acceptance check (engine/check/product-lock-integrity).

The pure decision (`classify`) is tested directly with crafted inputs — fake only the boundary. The I/O wrapper
(`emit_findings`) is tested by faking exactly the boundary it talks to: the authenticated GitHub API (a
monkeypatched `api_get`), the pull-request event file, and the working-tree root. The live API is never called,
so the `engine-ci` self-test step (which runs unittest with no PR context) stays green.
"""
from __future__ import annotations
import base64
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on sys.path
from product_design import lock_integrity  # noqa: E402
import validate  # noqa: E402

_SETTLED = "---\nstatus: locked\n---\n\n# Checkout\n\nThe checkout flow.\n"
_EDITED = "---\nstatus: locked\n---\n\n# Checkout\n\nThe checkout flow, revised.\n"
_REOPENED = "---\nstatus: draft\n---\n\n# Checkout\n\nThe checkout flow.\n"
_RAW_TOKENS = ("stub", "draft", "locked")


def _word(token: str, text: str) -> bool:
    import re
    return bool(re.search(rf"\b{token}\b", text))


# --------------------------------------------------------------------------------------------------
# The pure core — classify()
# --------------------------------------------------------------------------------------------------

class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.base = {"docs/spec/checkout.md": _SETTLED}

    def test_unchanged_settled_doc_is_clean(self):
        self.assertEqual(lock_integrity.classify(self.base, {"docs/spec/checkout.md": _SETTLED}, False, "hard"), [])

    def test_edited_settled_doc_without_label_is_hard_naming_doc_and_label(self):
        fs = lock_integrity.classify(self.base, {"docs/spec/checkout.md": _EDITED}, False, "hard")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("docs/spec/checkout.md", fs[0]["message"])
        self.assertIn("guardrail-ack", fs[0]["message"])
        self.assertEqual(fs[0]["location"], {"file": "docs/spec/checkout.md", "line": None})

    def test_edited_settled_doc_with_label_is_clean(self):
        self.assertEqual(lock_integrity.classify(self.base, {"docs/spec/checkout.md": _EDITED}, True, "hard"), [])

    def test_reopen_status_flip_without_label_is_hard(self):
        fs = lock_integrity.classify(self.base, {"docs/spec/checkout.md": _REOPENED}, False, "hard")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        # The reopen finding never leaks the raw stage token and never reads as a safety event.
        self.assertNotIn("draft", fs[0]["message"])
        self.assertIn("product description", fs[0]["message"])

    def test_removed_settled_doc_without_label_is_hard_worded_as_removal(self):
        fs = lock_integrity.classify(self.base, {"docs/spec/checkout.md": None}, False, "hard")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("removes", fs[0]["message"])

    def test_doc_not_settled_at_base_is_never_gated(self):
        # A draft/stub doc is simply absent from base_locked_docs (the I/O layer filters it out).
        self.assertEqual(lock_integrity.classify({}, {"docs/spec/x.md": _EDITED}, False, "hard"), [])

    def test_line_ending_and_bom_only_difference_is_not_a_change(self):
        crlf_bom = "﻿" + _SETTLED.replace("\n", "\r\n") + "\n\n"
        self.assertEqual(lock_integrity.classify(self.base, {"docs/spec/checkout.md": crlf_bom}, False, "hard"), [])

    def test_multiple_settled_docs_only_the_changed_unacked_one_fires(self):
        base = {"docs/spec/a.md": _SETTLED, "docs/spec/b.md": _SETTLED}
        head = {"docs/spec/a.md": _EDITED, "docs/spec/b.md": _SETTLED}
        fs = lock_integrity.classify(base, head, False, "hard")
        self.assertEqual(len(fs), 1)
        self.assertIn("docs/spec/a.md", fs[0]["message"])

    def test_violations_carry_the_passed_tier(self):
        hard = lock_integrity.classify(self.base, {"docs/spec/checkout.md": _EDITED}, False, "hard")
        soft = lock_integrity.classify(self.base, {"docs/spec/checkout.md": _EDITED}, False, "soft")
        self.assertEqual(hard[0]["severity"], "hard")
        self.assertEqual(soft[0]["severity"], "soft")

    def test_findings_never_leak_a_raw_lifecycle_token(self):
        for head in ({"docs/spec/checkout.md": _EDITED}, {"docs/spec/checkout.md": _REOPENED},
                     {"docs/spec/checkout.md": None}):
            for f in lock_integrity.classify(self.base, head, False, "hard"):
                for token in _RAW_TOKENS:
                    self.assertFalse(_word(token, f["message"]),
                                     f"finding leaked raw token '{token}': {f['message']}")

    def test_findings_frame_a_product_change_not_a_safety_event(self):
        f = lock_integrity.classify(self.base, {"docs/spec/checkout.md": _EDITED}, False, "hard")[0]
        self.assertIn("product description", f["message"])
        self.assertNotIn("safety gate", f["message"])
        self.assertNotIn("safety check", f["message"])


# --------------------------------------------------------------------------------------------------
# The I/O wrapper — emit_findings(), faking ONLY the boundary (api_get + event file + ROOT)
# --------------------------------------------------------------------------------------------------

class IoTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self._root = validate.ROOT
        self._api = lock_integrity.get_json
        self._tmp = tempfile.mkdtemp(prefix="engine-lock-io-")
        validate.ROOT = self._tmp

    def tearDown(self):
        import shutil
        os.environ.clear()
        os.environ.update(self._env)
        validate.ROOT = self._root
        lock_integrity.get_json = self._api
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_head(self, rel: str, body: str):
        path = os.path.join(self._tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)

    def _set_event(self, *, base_sha="basesha", labels=()):
        event = {"pull_request": {"number": 1, "base": {"sha": base_sha},
                                  "labels": [{"name": n} for n in labels]}}
        path = os.path.join(self._tmp, "event.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(event, fh)
        os.environ["GITHUB_REPOSITORY"] = "owner/repo"
        os.environ["GITHUB_TOKEN"] = "t0ken"
        os.environ["GITHUB_EVENT_PATH"] = path

    def _fake_api(self, *, dir_entries=None, dirs=None, files=None, dir_error=None):
        """A boundary fake for github_client.get_json: serves docs/spec directory listings (recursively) and per-file content,
        or raises. `dir_entries` is shorthand for the top-level docs/spec listing; `dirs` maps any directory
        path to its entries (nested trees); `files` maps a file path to its content. Unquotes the request path,
        so it exercises the production percent-encoding."""
        listings = dict(dirs or {})
        if dir_entries is not None:
            listings["docs/spec"] = dir_entries
        files = files or {}

        def fake(path, token, **kw):
            if "/contents/" not in path:
                raise AssertionError(f"unexpected api path: {path}")
            p = urllib.parse.unquote(path.split("/contents/", 1)[1].split("?", 1)[0])
            if p == "docs/spec" and dir_error is not None:
                raise dir_error
            if p in listings:
                return listings[p]
            if p in files:
                return {"encoding": "base64", "sha": "blob:" + p,
                        "content": base64.b64encode(files[p].encode()).decode()}
            raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
        lock_integrity.get_json = fake

    def _run(self) -> list:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = lock_integrity.emit_findings()
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def test_edited_settled_doc_in_ci_without_label_blocks_hard(self):
        self._set_event(labels=())
        self._fake_api(dir_entries=[{"type": "file", "path": "docs/spec/index.md"},
                                    {"type": "file", "path": "docs/spec/checkout.md"}],
                       files={"docs/spec/checkout.md": _SETTLED})
        self._write_head("docs/spec/checkout.md", _EDITED)
        fs = self._run()
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("docs/spec/checkout.md", fs[0]["message"])

    def test_edited_settled_doc_in_ci_with_guardrail_ack_clears(self):
        self._set_event(labels=("guardrail-ack",))
        self._fake_api(dir_entries=[{"type": "file", "path": "docs/spec/checkout.md"}],
                       files={"docs/spec/checkout.md": _SETTLED})
        self._write_head("docs/spec/checkout.md", _EDITED)
        self.assertEqual(self._run(), [])

    def test_unsettled_doc_change_is_not_gated(self):
        # checkout.md is draft at base => not collected => no finding even though head differs.
        self._set_event()
        self._fake_api(dir_entries=[{"type": "file", "path": "docs/spec/checkout.md"}],
                       files={"docs/spec/checkout.md": _REOPENED})  # draft at base
        self._write_head("docs/spec/checkout.md", _EDITED)
        self.assertEqual(self._run(), [])

    def test_index_md_is_never_treated_as_a_settled_doc(self):
        # The master index carries no settled stage and is excluded; only checkout is gated.
        self._set_event()
        self._fake_api(dir_entries=[{"type": "file", "path": "docs/spec/index.md"},
                                    {"type": "file", "path": "docs/spec/checkout.md"}],
                       files={"docs/spec/checkout.md": _SETTLED})
        self._write_head("docs/spec/checkout.md", _SETTLED)  # unchanged
        self.assertEqual(self._run(), [])

    def test_no_spec_tree_at_base_404_is_clean(self):
        self._set_event()
        self._fake_api(dir_error=urllib.error.HTTPError("http://x", 404, "Not Found", None, None))
        self.assertEqual(self._run(), [])

    def test_non_404_api_error_fails_closed_hard(self):
        self._set_event()
        self._fake_api(dir_error=urllib.error.HTTPError("http://x", 500, "Server Error", None, None))
        fs = self._run()
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("couldn't read", fs[0]["message"])

    def test_directory_listing_at_the_cap_fails_closed_hard(self):
        self._set_event()
        big = [{"type": "file", "path": f"docs/spec/d{i}.md"} for i in range(lock_integrity._CONTENTS_DIR_CAP)]
        self._fake_api(dir_entries=big)
        fs = self._run()
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")

    def test_removed_settled_doc_in_ci_blocks_hard(self):
        self._set_event()
        self._fake_api(dir_entries=[{"type": "file", "path": "docs/spec/checkout.md"}],
                       files={"docs/spec/checkout.md": _SETTLED})
        # No head file written => removed at head.
        fs = self._run()
        self.assertEqual(len(fs), 1)
        self.assertIn("removes", fs[0]["message"])

    def test_bom_at_base_settled_doc_is_still_gated(self):
        # A settled doc carrying a committed UTF-8 BOM is valid per the form check (its reader is utf-8-sig);
        # the base reader must strip the BOM too, or the doc is invisible here — a silent under-gate.
        self._set_event()
        self._fake_api(dir_entries=[{"type": "file", "path": "docs/spec/checkout.md"}],
                       files={"docs/spec/checkout.md": "﻿" + _SETTLED})
        self._write_head("docs/spec/checkout.md", _EDITED)
        fs = self._run()
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("docs/spec/checkout.md", fs[0]["message"])

    def test_settled_doc_in_a_subdirectory_is_discovered_and_gated(self):
        # The recursive BFS over /contents must find a settled doc nested under docs/spec/ — else a nested
        # settled doc could change unguarded (an under-gate). Matches spec_form's whole-tree discovery.
        self._set_event()
        self._fake_api(dirs={"docs/spec": [{"type": "dir", "path": "docs/spec/payments"}],
                             "docs/spec/payments": [{"type": "file",
                                                     "path": "docs/spec/payments/checkout.md"}]},
                       files={"docs/spec/payments/checkout.md": _SETTLED})
        self._write_head("docs/spec/payments/checkout.md", _EDITED)
        fs = self._run()
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("docs/spec/payments/checkout.md", fs[0]["message"])

    def test_a_settled_doc_filename_with_a_space_is_requested_validly(self):
        # A spec filename with a space must percent-encode into a valid request, not a false-block.
        self._set_event()
        self._fake_api(dir_entries=[{"type": "file", "path": "docs/spec/my feature.md"}],
                       files={"docs/spec/my feature.md": _SETTLED})
        self._write_head("docs/spec/my feature.md", _EDITED)
        fs = self._run()
        self.assertEqual(len(fs), 1)
        self.assertIn("docs/spec/my feature.md", fs[0]["message"])


# --------------------------------------------------------------------------------------------------
# Dispatch / fail-open-locally / demo
# --------------------------------------------------------------------------------------------------

class DispatchTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _run_main(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = lock_integrity.main(argv)
        return rc, buf.getvalue()

    def test_no_pr_context_fails_open_with_a_single_soft_note(self):
        # The local / self-test posture: no token, no event => one soft note, exit 0 (never a false local block).
        for var in ("GITHUB_TOKEN", "GITHUB_EVENT_PATH", "GITHUB_REPOSITORY"):
            os.environ.pop(var, None)
        rc, out = self._run_main([])
        self.assertEqual(rc, 0)
        fs = json.loads(out)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertNotIn("guardrail-ack", fs[0]["message"])  # not a blocking ask, just a disclosed no-op

    def test_main_routes_demo(self):
        rc, out = self._run_main(["demo"])
        self.assertEqual(rc, 0)
        self.assertIn("DEMO PASSED", out)

    def test_main_bare_invocation_prints_a_json_array(self):
        for var in ("GITHUB_TOKEN", "GITHUB_EVENT_PATH", "GITHUB_REPOSITORY"):
            os.environ.pop(var, None)
        rc, out = self._run_main([])
        self.assertEqual(rc, 0)
        self.assertIsInstance(json.loads(out), list)

    def test_demo_passes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = lock_integrity.demo()
        self.assertEqual(rc, 0, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
