"""Unit tests for github_client — the shared authenticated GitHub REST API client.

Run via the engine suite: `uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'`.
Fully offline: the only network call goes through `github_client._urlopen`, which every test here replaces
with an in-memory fake, so the REAL request-building, off-host guard, pagination, and decode logic runs with
no token and no network.
"""

import base64
import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # .engine/tools on path
import github_client  # noqa: E402


def _headers(req) -> dict:
    """Case-insensitive header view of a urllib Request (urllib title-cases header keys)."""
    return {k.lower(): v for k, v in req.header_items()}


class _FakeResp:
    """A minimal urlopen() context-manager stand-in: body bytes + an optional Link header."""

    def __init__(self, body, link=None, status=200):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.status = status
        self.headers = {"Link": link} if link is not None else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeNetwork:
    """Records each request and serves a queued response (or raises a queued error)."""

    def __init__(self):
        self.requests = []
        self._responses = []

    def queue(self, resp):
        self._responses.append(resp)
        return self

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class RequestBuilderTests(unittest.TestCase):
    def test_relative_path_is_prefixed_with_the_api_host(self):
        req = github_client.request("/repos/o/r/pulls/1", "tok", user_agent="ua")
        self.assertEqual(req.full_url, "https://api.github.com/repos/o/r/pulls/1")

    def test_headers_carry_bearer_auth_accept_version_and_user_agent(self):
        h = _headers(github_client.request("/x", "secret-tok", user_agent="engine-test-ua"))
        self.assertEqual(h["authorization"], "Bearer secret-tok")
        self.assertEqual(h["accept"], "application/vnd.github+json")
        self.assertEqual(h["x-github-api-version"], "2022-11-28")
        self.assertEqual(h["user-agent"], "engine-test-ua")

    def test_get_request_sends_no_content_type(self):
        # A GET caller's headers stay byte-identical to the old read-only builders (no Content-Type).
        req = github_client.request("/x", "tok", user_agent="ua")
        self.assertNotIn("content-type", _headers(req))
        self.assertIsNone(req.data)
        self.assertEqual(req.get_method(), "GET")

    def test_content_type_appears_only_when_a_body_is_sent(self):
        body = json.dumps({"name": "x"}).encode("utf-8")
        req = github_client.request("/labels", "tok", user_agent="ua", method="POST", data=body)
        self.assertEqual(_headers(req)["content-type"], "application/json")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data, body)


class OffHostGuardTests(unittest.TestCase):
    """The security falsification: a token-bearing request must never be redirected off-host by a
    crafted Link header. An absolute URL off the GitHub API host MUST raise; this is the one behavior the
    extraction may never weaken."""

    def test_an_off_host_absolute_url_raises(self):
        with self.assertRaises(ValueError):
            github_client.request("https://evil.example.com/repos/o/r", "tok", user_agent="ua")

    def test_a_lookalike_subdomain_host_raises(self):
        with self.assertRaises(ValueError):
            github_client.request("https://api.github.com.evil.example/x", "tok", user_agent="ua")

    def test_an_on_host_absolute_url_is_allowed_verbatim(self):
        url = "https://api.github.com/repositories/1/pulls/1/files?per_page=100&page=2"
        req = github_client.request(url, "tok", user_agent="ua")
        self.assertEqual(req.full_url, url)


class GetTests(unittest.TestCase):
    def setUp(self):
        self._orig = github_client._urlopen

    def tearDown(self):
        github_client._urlopen = self._orig

    def test_get_json_returns_the_parsed_body(self):
        github_client._urlopen = _FakeNetwork().queue(_FakeResp({"changed_files": 7}))
        self.assertEqual(github_client.get_json("/repos/o/r/pulls/1", "tok", user_agent="ua"),
                         {"changed_files": 7})

    def test_get_json_raises_httperror_unwrapped_on_4xx(self):
        # lock_integrity's 404 branch and the guards' fail-closed posture both depend on this propagating
        # the raw urllib.error.HTTPError, not a wrapped/normalized error.
        err = urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", None, io.BytesIO(b""))
        github_client._urlopen = _FakeNetwork().queue(err)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            github_client.get_json("/x", "tok", user_agent="ua")
        self.assertEqual(ctx.exception.code, 404)

    def test_get_page_returns_body_and_link_header(self):
        net = _FakeNetwork().queue(_FakeResp([{"filename": "a"}], link='<https://api.github.com/x?page=2>; rel="next"'))
        github_client._urlopen = net
        body, link = github_client.get_page("/x?page=1", "tok", user_agent="ua")
        self.assertEqual(body, [{"filename": "a"}])
        self.assertIn('rel="next"', link)

    def test_get_page_user_agent_reaches_the_request(self):
        net = _FakeNetwork().queue(_FakeResp([]))
        github_client._urlopen = net
        github_client.get_page("/x", "tok", user_agent="distinct-ua")
        self.assertEqual(_headers(net.requests[0])["user-agent"], "distinct-ua")


class NextLinkTests(unittest.TestCase):
    def test_parses_rel_next_and_returns_none_when_absent(self):
        header = ('<https://api.github.com/r/1/files?page=2>; rel="next", '
                  '<https://api.github.com/r/1/files?page=9>; rel="last"')
        self.assertEqual(github_client.next_link(header), "https://api.github.com/r/1/files?page=2")
        self.assertIsNone(github_client.next_link('<https://api.github.com/r/1/files?page=9>; rel="last"'))
        self.assertIsNone(github_client.next_link(None))


class DecodeContentTests(unittest.TestCase):
    def _obj(self, text, encoding="utf-8"):
        return {"encoding": "base64", "content": base64.b64encode(text.encode(encoding)).decode()}

    def test_utf8_decode_round_trips(self):
        self.assertEqual(github_client.decode_content(self._obj("héllo — recall")), "héllo — recall")

    def test_utf8_sig_strips_a_committed_byte_order_mark(self):
        obj = self._obj("﻿settled doc")
        # the default utf-8 codec leaves the BOM; utf-8-sig strips it (lock_integrity's BOM tolerance)
        self.assertEqual(github_client.decode_content(obj), "﻿settled doc")
        self.assertEqual(github_client.decode_content(obj, codec="utf-8-sig"), "settled doc")

    def test_bad_bytes_are_lossy_not_fatal(self):
        obj = {"content": base64.b64encode(b"\xff\xfeok").decode()}
        # never raises — matches the callers' "replace" tolerance
        self.assertIn("ok", github_client.decode_content(obj))


if __name__ == "__main__":
    unittest.main()
