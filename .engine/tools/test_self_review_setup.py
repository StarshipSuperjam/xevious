#!/usr/bin/env python3
"""Tests for the operator setup page `.engine/audits/self-review-setup.md`.

This page is the returnable, plain-language guide that tells the operator how to arm the engine's scheduled
self-review (the one-time token), keep it running (expiry / usage limits / re-arm), change how-often / which
model, and optionally run it off-schedule as a Claude Cloud Routine or a Codex Automation. It survives first-run (the year-later token re-arm depends on
it), so it lives with the audit's own files and is owned by audit-library's `provides`.

These pin the load-bearing facts a future edit must not silently drop — the EXACT secret name, the two-step
shape, the Cloud-Routine eliminations, the un-run-at-v1 honesty, the monthly cadence line — and the
plain-language bar (no maintainer/audit backstage vocabulary reaches the operator). All assertions read the
committed files directly; this surviving test never imports the retired first-run machinery it reasons about
(the first-run reference-closure invariant).
"""
from __future__ import annotations
import json
import os
import unittest

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ENGINE = os.path.dirname(_TOOLS)
PAGE_REL = ".engine/audits/self-review-setup.md"
PAGE_PATH = os.path.join(_ENGINE, "audits", "self-review-setup.md")
MANIFEST_PATH = os.path.join(_ENGINE, "modules", "audit-library", "manifest.json")
FIRST_RUN_ASSETS_PATH = os.path.join(_ENGINE, "provisioning", "first-run-assets.json")


def _page_text() -> str:
    with open(PAGE_PATH, encoding="utf-8") as fh:
        return fh.read()


class TestSetupPagePresenceAndOwnership(unittest.TestCase):
    def test_page_exists(self):
        self.assertTrue(os.path.isfile(PAGE_PATH), f"the setup page must be present at {PAGE_REL}")

    def test_page_is_owned_by_audit_library(self):
        # Without this the file is an ownership orphan (every .engine/ file is claimed by exactly one
        # module's provides). It rides audit-library's `audits` group, beside the seeded concern-list.
        with open(MANIFEST_PATH, encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertIn(PAGE_REL, manifest["provides"]["audits"],
                      "the setup page must be claimed by audit-library's provides or it is an orphan")

    def test_page_survives_first_run(self):
        # Load-bearing: the year-later token re-arm sends the operator back to this page, so it must NOT be
        # among the first-run-only assets the instantiator retires. Read the committed manifest of the
        # retired set (never the instantiator itself).
        with open(FIRST_RUN_ASSETS_PATH, encoding="utf-8") as fh:
            retired = json.load(fh)
        self.assertNotIn(PAGE_REL, retired["files"],
                         "the setup page must survive first-run — the re-arm guidance depends on it")


class TestSetupPageContent(unittest.TestCase):
    """The facts the page exists to carry. A future edit that drops one fails here, name-to-fact."""

    def setUp(self):
        self.text = _page_text()

    def test_names_the_exact_secret(self):
        # A typo in the secret name fails the run silently, so the page must carry it EXACTLY.
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", self.text)

    def test_carries_the_three_step_setup(self):
        # Setup is THREE steps now — the sign-in token, the GitHub secret, then the create/approve-PR setting
        # that lets the run open its summary (round-2: without it the run dies with no summary opened). The
        # phantom step from #175 ("turn on access for scheduled runs", a Claude-account toggle that does NOT
        # exist) stays banned — the real third step is the GitHub setting, not that fiction.
        self.assertIn("claude setup-token", self.text)        # step 1: the sign-in token
        self.assertIn("three one-time steps", self.text)      # three, not two — and never the phantom toggle
        self.assertNotIn("two one-time steps", self.text)     # the old count must not linger
        # The #175 phantom toggle must never return:
        self.assertNotIn("scheduled or developer", self.text)
        self.assertNotIn("turn on access for scheduled runs", self.text.lower())

    def test_names_the_cli_prerequisite_for_the_token(self):
        # `claude setup-token` needs Claude Code's command-line tool, which
        # the engine's Claude-Desktop operator may not have installed — the page must name that (and offer to
        # help set it up), not silently assume it.
        self.assertIn("command-line tool", self.text)

    def test_documents_the_create_and_approve_pr_setting(self):
        # Round-2 / Option A: the run can only open its summary if the operator enables GitHub's "create and
        # approve pull requests" setting (off by default for EVERY repo). Without this step on the page, every
        # adopter's self-review runs but silently opens no summary.
        self.assertIn("create and approve pull requests", self.text)

    def test_discloses_it_now_reads_the_open_issue_list(self):
        # Round-2: the review now also reads the open engine issues (concern #2), so the coverage note says so
        # — keeping the honesty contract that names what the review can and cannot see.
        self.assertIn("open issue", self.text)

    def test_cloud_routine_eliminates_the_wrong_choices(self):
        # The design's Cloud-Routine walkthrough names Remote (not Local) on a recurring schedule.
        self.assertIn("Remote", self.text)
        self.assertIn("recurring", self.text)

    def test_discloses_both_off_schedule_routines_prove_out_only_on_a_live_run(self):
        # The off-schedule conveniences — the Claude Cloud Routine AND the Codex Automation — run for real only on
        # the operator's own schedule and account; the engine can't exercise them for the operator. The page must
        # disclose that honestly for both, so a future edit that quietly drops the run-it-yourself steer fails here.
        self.assertIn("neither routine above runs end-to-end until it runs on your own", self.text)

    def test_foregrounds_the_read_only_sandbox_as_the_codex_write_wall(self):
        # The from-Codex convenience is safe ONLY because its Codex Automation runs sandbox_mode=read-only — that,
        # not approval_policy, is the write wall. A future edit must not drop the read-only setting, let the
        # never-ask setting stand in for it, or point the paste at a generated render. Pin the load-bearing facts.
        t = self.text
        self.assertIn("Codex Automation", t)                 # the from-Codex arm exists
        self.assertIn('sandbox_mode = "read-only"', t)       # the write-safety wall, exact
        self.assertIn(".claude/agents/engine-audit.md", t)   # the paste names the canonical persona, not a render

    def test_discloses_the_cloud_path_leaves_no_committed_freshness_record(self):
        # #406: the Cloud-Routine path yields a chat summary but never refreshes the committed record the
        # freshness reminder reads — so a cloud-only operator is told the review is due even while it runs. The
        # page must disclose this substantive fact (not a cosmetic string): that the cloud path leaves no record
        # the engine reads, AND it must name the actual boot phrasing so the disclosure is honest for the
        # never-run sub-case (not just the "gone too long" one).
        self.assertIn("never leaves the record the engine reads", self.text)
        # Both actual boot phrasings must be mirrored so the disclosure is honest for either sub-case — the
        # never-run one ("hasn't run yet", audit_digest.py) and the aged one ("hasn't reviewed its own health").
        self.assertIn("hasn't run yet", self.text)
        self.assertIn("hasn't reviewed its own health", self.text)

    def test_gives_a_copy_paste_monthly_cadence_line(self):
        self.assertIn("17 7 1 * *", self.text)

    def test_names_the_model_knob(self):
        self.assertIn("AUDIT_MODEL", self.text)

    def test_discloses_the_saved_memory_read_and_its_precondition(self):
        # The off-repo memory backup is built, so the review CAN read saved memory — but only once two things are
        # in place (a backup is set up AND the scheduled run is given read access to it). The page says so
        # honestly: the actionable how-to, the two-part access precondition, and the honest fallback (it discloses
        # the gap rather than pretending memory is empty) — so coverage is never over-claimed.
        self.assertIn("saved memory", self.text)
        self.assertIn("ask me to set one up", self.text)                  # the actionable how-to for the backup
        self.assertIn("given read access", self.text)                    # the two-part access precondition
        self.assertIn("never pretends your memory is empty", self.text)  # the honest fallback

    def test_walks_through_the_saved_memory_read_turn_on(self):
        # Item 3 (#224): the heavy-consent turn-on must be a real, followable walkthrough — not the
        # dead-end "ask me" #224 was filed to fix. It names the secret, pre-translates the platform terms, lands
        # the shared-vault blast-radius disclosure at the paste with the per-project escape, steers to a no-expiry
        # key with the org-cap fallback, and ends with the engine-run test read.
        t = self.text
        self.assertIn("MEMORY_VAULT_TOKEN", t)                          # names the secret exactly
        self.assertIn("fine-grained personal access token", t)         # pre-translates the platform term
        self.assertIn("Contents → Read", t)                            # the read-only scope, in plain words
        self.assertIn("No expiration", t)                              # the no-expiry steer…
        self.assertIn("organization", t)                               # …with the org-cap fallback
        self.assertIn("every** project's saved memory", t)             # blast-radius, at the paste moment
        self.assertIn("own private vault", t)                          # …with the per-project escape
        self.assertIn("ask me to test the read", t)                    # ends with the engine-run test read
        self.assertIn("runs for real only on your own schedule", t)       # only-a-live-run-proves-it disclosure

    def test_vault_read_key_rearm_is_credential_specific_not_the_oauth_token(self):
        # The corrected re-arm copy (item 4 owner): the vault key's recovery is specific to THAT key (re-make it +
        # re-set MEMORY_VAULT_TOKEN), explicitly NOT `claude setup-token` (the unrelated sign-in token).
        t = self.text
        self.assertIn("memory-vault read key can lapse", t)
        self.assertIn("not** `claude setup-token`", t)                 # the explicit contrast against the wrong fix


if __name__ == "__main__":
    unittest.main()
