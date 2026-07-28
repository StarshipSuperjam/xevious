"""Unit tests for memory.scrub — capture-time secret redaction.

Run via the engine test suite: `uv run --directory .engine --frozen -- python -m unittest discover -s
tools -p 'test_*.py'`. Two properties carry the weight: PRECISION (every credential shape is redacted)
and NON-CORRUPTION (ordinary conversation — prose, hashes, ids, paths, code, emails, phones — passes
through byte-identical). The non-corruption matrix is the load-bearing one: a false positive permanently
destroys unrecoverable memory (eADR-0038), so any future pattern that breaks these must be justified.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on path
from memory import scrub  # noqa: E402

# Synthetic, NON-REAL secrets — shaped like the vendor formats but with obviously-fake bodies.
_AWS = "AKIAIOSFODNN7EXAMPLE"
_GH_PAT = "ghp_" + "A" * 36
_GH_FINE = "github_pat_" + "B" * 30
_ANTHROPIC = "sk-ant-" + "C" * 40
_OPENAI = "sk-" + "aB3xY7z9" * 6          # realistic key body: hyphen-free, contains digits
_OPENAI_PROJ = "sk-proj-" + "aZ9bY8c7" * 6
_STRIPE = "sk_live_" + "E" * 24
_SLACK = "xoxb-" + "1" * 20
_GOOGLE = "AIza" + "F" * 35
_GOOGLE_OAUTH = "123456789012-" + "a" * 24 + ".apps.googleusercontent.com"
_JWT = "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 20
# A SYNTHETIC PEM header for the redaction test — flagged as an intentional test fixture so the advisory
# repo secret-scan (gitleaks) does not warn on it; the body is obviously fake ("madeupbase64").
_PEM = ("-----BEGIN RSA PRIVATE KEY-----\n" + "MIIEmadeupbase64" * 3  # gitleaks:allow
        + "\n-----END RSA PRIVATE KEY-----")


class PrecisionTests(unittest.TestCase):
    """Each real credential shape is redacted, the raw secret gone, the correct typed placeholder present."""

    def _assert_redacted(self, raw, kind):
        text = "before the token %s after the token" % raw
        out = scrub.scrub_text(text)
        self.assertNotIn(raw, out, "raw secret survived: %s" % kind)
        self.assertIn("[redacted:%s]" % kind, out, "placeholder missing: %s" % kind)
        self.assertIn("before the token", out)  # surrounding prose preserved
        self.assertIn("after the token", out)

    def test_aws_key(self):
        self._assert_redacted(_AWS, "aws-key")

    def test_github_pat(self):
        self._assert_redacted(_GH_PAT, "github-token")

    def test_github_fine_grained(self):
        self._assert_redacted(_GH_FINE, "github-token")

    def test_anthropic_key(self):
        self._assert_redacted(_ANTHROPIC, "anthropic-key")

    def test_openai_key(self):
        self._assert_redacted(_OPENAI, "openai-key")

    def test_openai_project_key(self):
        self._assert_redacted(_OPENAI_PROJ, "openai-key")

    def test_stripe_key(self):
        self._assert_redacted(_STRIPE, "stripe-key")

    def test_slack_token(self):
        self._assert_redacted(_SLACK, "slack-token")

    def test_google_key(self):
        self._assert_redacted(_GOOGLE, "google-key")

    def test_google_oauth(self):
        self._assert_redacted(_GOOGLE_OAUTH, "google-oauth")

    def test_jwt(self):
        self._assert_redacted(_JWT, "jwt")

    def test_pem_private_key_block(self):
        out = scrub.scrub_text("here is my key:\n%s\ndone" % _PEM)
        self.assertNotIn("PRIVATE KEY", out)
        self.assertNotIn("madeupbase64", out)
        self.assertIn("[redacted:private-key]", out)
        self.assertIn("here is my key:", out)
        self.assertIn("done", out)

    def test_authorization_header_keeps_scaffold(self):
        token = "aZ9bY8c7dX6e" * 3   # synthetic test token — gitleaks:allow
        out = scrub.scrub_text("Authorization: Bearer " + token)
        self.assertIn("Authorization: Bearer [redacted:auth-credential]", out)
        self.assertNotIn(token, out)

    def test_url_credential_keeps_host(self):
        out = scrub.scrub_text("db at postgres://admin:hunter2@db.example.com:5432/app")
        self.assertNotIn("hunter2", out)
        self.assertIn("postgres://[redacted:url-credential]@db.example.com:5432/app", out)

    def test_anthropic_wins_over_generic_sk(self):
        out = scrub.scrub_text(_ANTHROPIC)
        self.assertIn("[redacted:anthropic-key]", out)
        self.assertNotIn("[redacted:openai-key]", out)


class NonCorruptionTests(unittest.TestCase):
    """The load-bearing property: ordinary conversation passes through BYTE-IDENTICAL. A break here means
    a false positive is destroying real, unrecoverable memory — every case below must stay untouched."""

    def _assert_untouched(self, text):
        self.assertEqual(scrub.scrub_text(text), text)

    def test_plain_prose(self):
        self._assert_untouched("the production database password lives in the vault, never in the repo")

    def test_git_sha(self):
        self._assert_untouched("reverted at commit a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0")

    def test_uuid(self):
        self._assert_untouched("session 3241edb4-3a01-43b2-bb66-fe7b6aac3f62 finished")

    def test_semantic_version(self):
        self._assert_untouched("release 0.3.2 shipped after 1.2.3-beta.4")

    def test_file_path(self):
        self._assert_untouched("edit /Users/shanekidd/Developer/engine-template/.engine/tools/memory/scrub.py")

    def test_host_port_without_credentials(self):
        self._assert_untouched("the dev server runs on localhost:5432 and redis on 127.0.0.1:6379")

    def test_code_snippet(self):
        self._assert_untouched("def scrub_text(text): return text  # the fail-soft path returns input")

    def test_decision_record_sentence(self):
        self._assert_untouched("eADR-0038 records that memory is a transcript-first archive scrubbed at capture")

    def test_email_left_intact(self):
        # PII is DELIBERATELY out of scope — an email is not a credential and is often a search anchor.
        self._assert_untouched("email me at shanekidd702@gmail.com about the merge")

    def test_phone_left_intact(self):
        self._assert_untouched("call +1 (555) 867-5309 or 555-867-5309 to confirm")

    def test_vendor_word_without_token_body(self):
        # Mentioning a prefix in prose, with no long token body, must not trip the length-anchored pattern.
        self._assert_untouched("our AWS keys start with AKIA but I will not paste a real one here")

    def test_short_sk_word(self):
        self._assert_untouched("the sk- prefix is short here, not a key")

    def test_sk_prefixed_hyphenated_slug_left_intact(self):
        # A `sk-`-prefixed hyphenated slug (CSS class, branch name) is NOT a key — hyphens + no long
        # digit-bearing contiguous run. A false positive here would destroy real, unrecoverable memory.
        self._assert_untouched("the spinner uses sk-chasing-dots-container-wrapper as its class name")
        self._assert_untouched("our branch sk-refactor-the-memory-subsystem-now is ready to merge")

    def test_sk_prefixed_digitless_word_left_intact(self):
        # A long `sk-`-prefixed word with no digit is not a key (real keys carry digits).
        self._assert_untouched("we called it sk-internationalization in the prototype")

    def test_authorization_scheme_prose_left_intact(self):
        # Prose ABOUT auth — a plain word after the scheme, no digit — must survive verbatim.
        self._assert_untouched("authorization: bearer credentials are rotated weekly")
        self._assert_untouched("the Authorization: Bearer authentication scheme is standard here")


class IdempotencyTests(unittest.TestCase):
    """Re-scrubbing scrubbed text is a no-op (capture can re-file an interrupted turn — capture.py:925)."""

    def test_idempotent_over_corpus(self):
        corpus = "\n".join([_AWS, _GH_PAT, _ANTHROPIC, _OPENAI, _STRIPE, _SLACK, _GOOGLE, _JWT, _PEM,
                             "Authorization: Bearer " + "Z" * 30, "postgres://u:p@h/db",
                             "ordinary prose with a1b2c3d4e5f6 and localhost:5432"])
        once = scrub.scrub_text(corpus)
        self.assertEqual(scrub.scrub_text(once), once)

    def test_placeholder_is_not_re_redacted(self):
        self.assertEqual(scrub.scrub_text("[redacted:aws-key]"), "[redacted:aws-key]")


class FailSoftTests(unittest.TestCase):
    def test_empty_and_none(self):
        self.assertEqual(scrub.scrub_text(""), "")
        self.assertIsNone(scrub.scrub_text(None))


if __name__ == "__main__":
    unittest.main()
