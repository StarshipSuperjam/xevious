"""The audit-prep scheduled self-review workflow (audit-library): a committed workflow that runs the
read-only audit persona on a schedule and commits the digest it produces. These tests attest the
load-bearing facts a non-engineer cannot read off the YAML: the file exists, is engine-owned (travels on
upgrade + renders into CODEOWNERS), runs on a schedule, gates cleanly when unarmed, pins the
operator-configurable model knob's default, and wires the tested seal/refresh plumbing. The end-to-end run
itself is disclosed-unrun (no token in this construction repo); its grammar is covered by the actionlint
workflow. Mirrors test_actionlint.py / test_secret_scan.py."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402
import module_coherence    # noqa: E402
import module_manager      # noqa: E402

WORKFLOW_REL = ".github/workflows/audit-prep.yml"


class TestAuditPrepIsAnEngineOwnedTraveler(unittest.TestCase):
    """The workflow is a FOUNDATION_INFRA member, so it travels on upgrade (FOUNDATION_CODE) and is owned in
    CODEOWNERS (foundation_infra_paths) — the same treatment as the other engine workflows."""

    def test_workflow_is_present_in_the_tree(self):
        self.assertTrue(os.path.isfile(os.path.join(validate.ROOT, WORKFLOW_REL)),
                        f"{WORKFLOW_REL} must exist")

    def test_is_a_foundation_infra_member(self):
        self.assertIn(WORKFLOW_REL, module_coherence.FOUNDATION_INFRA)

    def test_travels_on_upgrade_via_foundation_code(self):
        self.assertIn(WORKFLOW_REL, module_manager.FOUNDATION_CODE)

    def test_renders_into_codeowners_via_foundation_infra_paths(self):
        owned = module_coherence.foundation_infra_paths()
        self.assertIn(WORKFLOW_REL, owned)
        self.assertFalse(any("*" in p for p in owned), "paths are concrete, never bare globs")


class TestAuditPrepShape(unittest.TestCase):
    """The load-bearing grammar a non-engineer cannot read off the YAML."""

    def _text(self):
        return validate.read(os.path.join(validate.ROOT, WORKFLOW_REL))

    def test_runs_on_a_schedule(self):
        text = self._text()
        self.assertIn("schedule:", text)
        self.assertIn("cron:", text)

    def test_runs_the_audit_persona_read_only_via_direct_claude(self):
        # Direct `claude` (not an action that would let the agent commit) keeps the persona read-only — the
        # workflow's own later steps commit.
        text = self._text()
        self.assertIn("claude -p", text)
        self.assertIn("--agent engine-audit", text)

    def test_gates_cleanly_when_unarmed(self):
        # The skip-when-unarmed pattern: a gate job's output the real job is conditioned on, so a repo with
        # no token never accumulates failing runs.
        text = self._text()
        self.assertIn("needs.gate.outputs.armed", text)

    def test_model_knob_defaults_to_opus(self):
        # The operator-configurable judgment model, defaulted so an unset variable never drops the run to a
        # platform default. Pinned here so the default cannot silently change.
        self.assertIn("vars.AUDIT_MODEL || 'opus'", self._text())

    def test_seals_the_digest_and_refreshes_the_cache(self):
        text = self._text()
        self.assertIn("audit_digest.py", text)
        self.assertIn("--body-file", text)
        self.assertIn("telemetry.py refresh", text)

    def test_seal_path_is_engine_relative_not_doubled(self):
        # Regression (#176): the Seal step runs with `--directory .engine` (cwd becomes .engine), so its
        # positional path must be RELATIVE to .engine — `seal audits/audit-digest.md`, which resolves to the
        # canonical .engine/audits/audit-digest.md that the boot staleness read, the fingerprint check, and the
        # later `git add` all key on. A `.engine/`-prefixed path nested to `.engine/.engine/audits/...` — the
        # doubled-path bug that wrote the digest where nothing could find it (the `git add` then aborted with
        # exit 128, and boot could never see it). Pin the `seal `-prefixed form so the trap cannot return.
        text = self._text()
        self.assertIn("seal audits/audit-digest.md", text)
        self.assertNotIn("seal .engine/audits/audit-digest.md", text)

    def test_branch_name_is_collision_proof(self):
        # Regression (round-2 finding 4): the first real run pushed `audit-prep/<date>`, and a same-day re-run
        # reused that exact name — the push failed non-fast-forward, so a leftover branch from a failed run
        # blocked the next run. The branch now carries the unique-per-run id (and attempt), so no leftover
        # branch can ever collide; uniqueness sidesteps a force-push and the protected-branch deletion guard
        # (which scopes to the default branch only) entirely. The id/attempt are passed via env (RUN_ID /
        # RUN_ATTEMPT) rather than interpolated into the shell, so pin both the wiring and the branch form.
        text = self._text()
        # Both must be wired in: the run id (unique across distinct runs) AND the run attempt (the only thing
        # that differs when the SAME run is re-run — which is the original same-day-re-run failure). Dropping
        # `-${RUN_ATTEMPT}` would silently re-break the re-run case, so pin both and the full branch form.
        self.assertIn("github.run_id", text)
        self.assertIn("github.run_attempt", text)
        self.assertIn("audit-prep/${stamp}-${RUN_ID}-${RUN_ATTEMPT}", text)  # the full collision-proof form
        self.assertNotIn('branch="audit-prep/${stamp}"', text)               # the collision-prone form must not return

    def test_fetches_the_issue_backlog_and_feeds_the_read_only_persona(self):
        # #183 / Option 1: the persona never reaches GitHub. The workflow fetches the engine-labelled backlog
        # through the telemetry boundary and feeds it into the persona's prompt — so concern #2 has its data
        # without the read-only persona ever holding a GitHub token.
        text = self._text()
        self.assertIn("telemetry.py engine-issues", text)           # the workflow fetches the backlog
        self.assertIn("BEGIN OPEN ENGINE-LABELLED ISSUES", text)    # …and feeds it into the persona's prompt

    def test_fetches_the_never_firing_checks_and_feeds_the_read_only_persona(self):
        # The never-firing signal reaches the persona as a feed (the persona has Bash disallowed and
        # cannot compute it itself). The workflow fetches it above the persona step and splices it into the
        # prompt between fresh markers — a token-less local read, so this concern has its data without the
        # read-only persona ever holding a GitHub token.
        text = self._text()
        self.assertIn("telemetry.py never-fired", text)                 # the workflow computes the signal
        self.assertIn("BEGIN ENGINE CHECKS MATCHING NO FILES", text)    # …and feeds it into the persona's prompt
        # the fetch step must sit ABOVE the persona step (so the token-hygiene slice below is unaffected)
        self.assertLess(text.index("telemetry.py never-fired"), text.index("Run the read-only self-review"))

    def test_issues_scope_is_write_for_the_promote_step(self):
        # #273 half 2: the workflow now WRITES the engine-labelled issues (the promote step opens/
        # updates a tracked length-budget issue), so the scope rises from read to write. Pin `issues: write`
        # so a future edit can't silently drop the promoter's ability to track a finding — and so the
        # privilege increase stays a visible, deliberate line.
        text = self._text()
        self.assertIn("issues: write", text)
        self.assertNotIn("issues: read", text)   # the bare read scope must not linger and mislead

    def test_persona_step_stays_token_less(self):
        # The read-only persona must never receive a GitHub token — the fetch/refresh/PR steps are the only
        # holders. Slice the persona step (its own `claude -p` block) and assert no GitHub token reaches it,
        # so a future edit can't quietly hand the persona write-capable creds.
        text = self._text()
        start = text.index("Run the read-only self-review")
        persona_step = text[start:text.index("- name:", start)]     # up to the next step (Seal)
        self.assertIn("claude -p", persona_step)                    # sanity: this is the persona step
        self.assertNotIn("GITHUB_TOKEN", persona_step)
        self.assertNotIn("GH_TOKEN", persona_step)

    def test_feed_frames_the_backlog_as_complete_not_a_sample(self):
        # #198: given pasted issue data with no provenance, the persona hedged ("couldn't confirm this is the
        # complete open list"). The feed now states the backlog is the COMPLETE open set (read to exhaustion,
        # a read failure surfaced in-band) so the persona treats it as the whole backlog and stops hedging.
        text = self._text()
        self.assertIn("COMPLETE set of currently-open engine-labelled issues", text)
        self.assertIn("read to exhaustion", text)

    def test_pr_body_is_the_digest_review_not_boilerplate(self):
        # The digest PR's body IS the review prose: the `body` verb strips the sealed front-matter from the
        # committed digest, and that becomes the PR body — so the operator reads the actual self-review in the
        # PR rather than generic boilerplate. The path is .engine-relative (the doubled-path trap, #176).
        text = self._text()
        self.assertIn("audit_digest.py body audits/audit-digest.md", text)
        self.assertNotIn("body .engine/audits/audit-digest.md", text)   # never the doubled path

    def test_pr_body_keeps_the_load_bearing_disclaimer(self):
        # The body verb alone would drop the framing the operator needs: that a where-we-stand snapshot rides
        # along in the same diff, and that the merge attests to the review, not to the auto-derived numbers.
        # A short footer keeps both, so the operator still knows what is in the diff and what they're vouching for.
        # Assert on footer-UNIQUE phrases — a bare "snapshot" also appears in a step name and a comment, so it
        # would pass even if the footer sentence were deleted.
        text = self._text()
        self.assertIn("snapshot of where the project stands", text)   # the footer names what rides along
        self.assertIn("attests to the self-review", text)             # …and the merge-attests framing

    def test_fetches_prior_digests_and_feeds_them_to_the_persona(self):
        # #200 audit-over-audit: the workflow fetches the engine's OWN recent committed digests (the
        # `prior` verb) and feeds them into the read-only persona's prompt between fresh markers — so the
        # persona reads its history as corroboration without ever reaching GitHub itself.
        text = self._text()
        self.assertIn("audit_digest.py prior", text)                 # the workflow fetches the prior digests
        self.assertIn("BEGIN PRIOR SELF-REVIEWS", text)              # …and feeds them into the persona's prompt
        self.assertIn("END PRIOR SELF-REVIEWS", text)

    def test_prior_digests_feed_frames_corroboration_never_decision(self):
        # The feed wording must hold the contract the persona obeys: read the history ONLY as
        # corroboration, never as a decision; the call rests on a fresh check THIS cycle; and if there is
        # nothing to compare against, degrade plainly rather than invent a trend.
        text = self._text()
        self.assertIn("corroboration", text)
        self.assertIn("THIS cycle", text)
        self.assertIn("never invent a trend", text)

    def test_prior_digests_read_is_the_gh_api_not_a_deep_clone(self):
        # The recorded build-spec leaf: read the last N digest versions over the GitHub API on the normal
        # shallow checkout — deliberately NOT `actions/checkout fetch-depth: 0`, whose deep clone grows with
        # the repo's whole history just to read one file. Pin that no checkout step actually SETS fetch-depth
        # (the explanatory comment naming it does not count — parse the YAML, don't substring the comment).
        import yaml  # the engine runtime ships pyyaml; parse so a comment mentioning fetch-depth doesn't trip us
        doc = yaml.safe_load(self._text())
        for job in doc["jobs"].values():
            for step in job.get("steps", []):
                if "checkout" in str(step.get("uses", "")):
                    self.assertNotIn("fetch-depth", step.get("with", {}) or {},
                                     "the digest history is read over the gh API, never a deep clone")

    def test_prior_digests_step_degrades_in_band_when_the_fetch_fails(self):
        # Mirrors the issue-feed step: if the fetch step itself fails, leave an honest marker so the persona
        # still degrades to a point-in-time review rather than the run dying or concern silently skipped.
        text = self._text()
        self.assertIn("PRIOR SELF-REVIEWS: none are available to compare against this run (the fetch step failed)", text)

    def test_fetches_saved_memory_and_feeds_it_to_the_persona(self):
        # Concern #1 (saved memory): the workflow reads the project's off-repo memory BACKUP (the `memory` verb)
        # and feeds it into the read-only persona's prompt between fresh markers — so the persona can review the
        # saved beliefs without ever reaching the gitignored memory or GitHub itself.
        text = self._text()
        self.assertIn("audit_digest.py memory", text)                # the workflow reads the saved-memory backup
        self.assertIn("BEGIN YOUR SAVED MEMORY", text)               # …and feeds it into the persona's prompt
        self.assertIn("END YOUR SAVED MEMORY", text)

    def test_saved_memory_feed_frames_the_saved_memory_review_and_forbids_claiming_empty(self):
        # The feed wording must point the persona at its saved-memory review — in plain words, never the
        # backstage "concern #N" frame — and hold the honesty contract: when the backup can't be read,
        # disclose the gap and NEVER claim the project has no saved memory, phrased about what this review could
        # reach, never an absolute.
        text = self._text()
        self.assertIn("For your saved memory", text)
        self.assertNotIn("concern #1", text)   # the backstage ordinal frame must not reach the persona prompt
        self.assertIn("NEVER claim the project has no saved memory", text)

    def test_saved_memory_step_degrades_in_band_when_the_fetch_fails(self):
        # Mirrors the other feed steps: if the fetch step itself fails, leave an honest marker so the persona
        # still discloses concern #1's gap rather than the run dying or claiming memory is empty.
        text = self._text()
        self.assertIn("YOUR SAVED MEMORY: I couldn't read your saved memory this run (the fetch step failed)", text)

    def test_memory_step_reads_the_vault_with_a_least_privilege_vault_token(self):
        # Item 2 (#224): the own-repo GITHUB_TOKEN can't reach the SEPARATE private vault, so the memory
        # step reads it with MEMORY_VAULT_TOKEN (a contents:read token scoped to the vault). The swap is on the
        # memory step ONLY — the issue / prior / refresh / PR steps keep the own-repo token.
        text = self._text()
        start = text.index("Fetch the project's saved memory")
        mem_step = text[start:text.index("- name:", start)]
        self.assertIn("GITHUB_TOKEN: ${{ secrets.MEMORY_VAULT_TOKEN }}", mem_step)
        self.assertNotIn("secrets.GITHUB_TOKEN", mem_step)           # the own-repo token was swapped out HERE…
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", text)   # …but the same-repo steps still use it

    def test_visibility_is_detected_with_the_own_repo_token_before_the_memory_read(self):
        # Item 5 (#224): the saved-memory privacy gate keys on repo visibility, detected with the OWN-repo
        # token (the vault token can't read this repo; the `schedule` event payload omits visibility) and handed
        # to the memory step via the environment — so it MUST run before the memory read.
        text = self._text()
        self.assertIn("gh repo view", text)                          # reads its own repo's visibility
        self.assertIn("MEMORY_AUDIT_REPO_VISIBILITY", text)
        self.assertIn('>> "${GITHUB_ENV}"', text)                    # …hands it to the later memory step
        self.assertLess(text.index("MEMORY_AUDIT_REPO_VISIBILITY"), text.index("audit_digest.py memory"))

    def test_fetches_soft_findings_and_feeds_the_persona(self):
        # #273 half 2: a SOFT validator finding is otherwise printed to a CI log and discarded. The workflow
        # collects the firing soft findings (the feed tool) and feeds them into the read-only persona's prompt
        # between fresh markers, so the audit can finally see a recurring nudge.
        text = self._text()
        self.assertIn("audit_soft_findings.py", text)                # the workflow collects the firing soft findings
        self.assertIn("BEGIN CURRENTLY-FIRING SOFT FINDINGS", text)  # …and feeds them into the persona's prompt

    def test_soft_findings_feed_classifies_by_lane_and_forbids_a_tally(self):
        # The feed instructs the persona to classify each finding by its two lanes (machinery -> escalate
        # upstream, local state -> local reconcile) AND respects the no-count principle: recurrence is
        # corroboration only, never a count/threshold/tally. Pin both so an edit can't drop either.
        text = self._text()
        self.assertIn("escalate-upstream", text)
        self.assertIn("local-reconcile", text)
        self.assertIn("never as a count, threshold, or 'seen N times' tally", text)

    def test_soft_findings_step_stays_token_less(self):
        # The feed reads committed files only and needs no GitHub token; pin the step token-less so a future
        # edit can't quietly hand it creds it doesn't need.
        text = self._text()
        start = text.index("Fetch the currently-firing soft validator findings")
        soft_step = text[start:text.index("- name:", start)]
        self.assertIn("audit_soft_findings.py", soft_step)           # sanity: this is the soft-findings step
        self.assertNotIn("GITHUB_TOKEN", soft_step)
        self.assertNotIn("GH_TOKEN", soft_step)

    def test_soft_findings_step_degrades_in_band_when_the_fetch_fails(self):
        # Mirrors the other feed steps: if the fetch step itself fails, leave an honest marker so the persona
        # discloses the gap rather than reading silence as "nothing is firing".
        text = self._text()
        self.assertIn("CURRENTLY-FIRING SOFT FINDINGS: could not be read this run (the fetch step failed)", text)

    def test_promote_step_tracks_standing_budget_findings_as_issues(self):
        # #273 half 2: after the persona reads the firing soft findings, the workflow durably tracks
        # a STANDING length-budget one as a deduped, lane-aware engine issue, so it reaches boot + the tracker
        # and not just this week's digest.
        text = self._text()
        self.assertIn("audit_soft_promote.py", text)

    def test_promote_step_uses_the_own_repo_token_never_the_claude_token(self):
        # The promote step writes engine issues with the OWN-repo GitHub token (issues:write), never the Claude
        # OAuth token (which only auths the persona run). Slice the step and pin its creds so a future edit
        # can't hand it the wrong token or drop the GitHub one it needs.
        text = self._text()
        start = text.index("Track standing length-budget findings as engine issues")
        step = text[start:text.index("- name:", start)]
        self.assertIn("audit_soft_promote.py", step)                 # sanity: this is the promote step
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", step)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", step)            # never the persona's Claude token

    def test_promote_step_cannot_block_the_digest_pull_request(self):
        # The promoter is fail-open, and the step additionally guards with `|| true` so this best-effort side
        # action can never fail the run and strand the digest PR. Pin both the guard and that the promote step
        # runs before the seal/PR steps (so a non-`|| true` rewrite that aborts can't silently block them).
        text = self._text()
        self.assertIn("audit_soft_promote.py || true", text)
        self.assertLess(text.index("audit_soft_promote.py"), text.index("Seal the digest"))


if __name__ == "__main__":
    unittest.main()
