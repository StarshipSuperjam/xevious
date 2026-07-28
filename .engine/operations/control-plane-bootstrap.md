---
title: Bootstrap the control plane — turn the protected-branch safety gate on
---

## Purpose

How the engine turns on the **branch protection** that makes a non-engineer's merge gate real — the #1
trust dependency every other guardrail sits downstream of. The branch ruleset is a GitHub *setting*, not a
file, so it does not travel with the template and must be applied once per repository by an
operator-privileged actor. This runbook is the permanent, re-runnable mechanism: it checks whether the
operator's GitHub login can manage the repository's protection rules, shows a plain-language explanation and
(only if needed) the GitHub authorization screen the operator approves, applies the protection floor, then
verifies it actually took. Enter this runbook to understand or explain how protection is turned on, why each
step needs the operator, and what happens when it can't be turned on. The tool is `tools/bootstrap.py`; the
operator-facing copy is the `control-plane-bootstrap` template (`.engine/templates/control-plane-bootstrap.md`).

## Steps

The operation is idempotent and safe to run any time — it never weakens protection already in place, and a
re-run when the gate is already on is a clean no-op. The engine **cannot grant itself** the permission to
set protection rules; the authorization screen is the consent gate, consistent with the merge-as-consent
model.

1. **Check whether the gate is already on.** Read the evaluated protection rules for the protected branch
   (the per-branch rules endpoint, which any token can read) and compare them to the protection floor
   (reused from the committed `protection_guard`: a pull request before merging, the engine's required
   checks bound, resolved review conversations, no force-push, no deletion). If the floor is fully in force
   — by any ruleset, the engine's own or the product's — stop here: ensure the engine label exists and
   report the no-op. *Always runs.*
2. **Check capability, and get consent if needed.** Read whether the operator's token carries repository
   administration (the standard `repo` permission, read from the token's scopes). If it does, proceed. If
   it does not, show the **pre-bootstrap explanation** first — plain language, pre-translating the literal
   permission the operator is about to see — then trigger `gh auth refresh` so the operator approves the
   GitHub authorization screen, and **verify the permission actually persisted afterward** (some sign-in
   flows complete without saving it). If it still isn't present, degrade loudly (step 5).
3. **Apply the protection floor.** Create the engine's own named ruleset carrying the floor, or repair it in
   place if it already exists. The floor is *augmented, never weakened* — applying never removes or loosens
   a product's existing protection rules. (The reverse — de-bootstrapping the
   engine's binding on clean removal — shipped in core; in-place augment of a pre-existing
   *product* ruleset is owned by a later brownfield step. Both are named in the tool's header.)
4. **Verify.** Re-read the evaluated rules and confirm the floor is now actually in force — never assume the
   write took. Then ensure the engine-domain label exists (inheriting the first producer's minimal ensure;
   the engine never makes the operator hand-create a label).
5. **Degrade, never fake.** Where the permission genuinely can't be obtained, the engine discloses and
   degrades — it never pretends the gate is on. It surfaces a plain-language account naming the concrete
   risk ("branch protection is not active — work can merge unreviewed") and a next action matched to the
   cause: if the operator doesn't administer the repository, forward the one-time setup to whoever does; if
   an org policy blocks the permission, point the operator at their org admin (team mode is NOT an escape —
   its identity is deliberately non-admin, so it cannot hold the blocked branch-protection permission); if the
   approval didn't save, retry. Never a dead-end.

This runbook runs the **single first-run attempt and surfaces its own outcome**. The **standing**
unprotected-state surfacing across every later session — the continuous "your safety gate is off" reminder
— is [boot](boot-session-start.md)'s, which already renders it from the same evaluation.

## Done when

The protection floor is confirmed in force on the protected branch (a pull request, the engine's required
checks, resolved conversations, no force-push, no deletion) — or, where the permission could not be
obtained, the operator has been told plainly that protection is off, why, and the one concrete next step,
with no silent green. The engine-domain label exists. A re-run when the gate is already on changes nothing.

## Notes

**The skeleton is posture; the only wall is the protected-branch merge.** This operation *establishes* that
wall. Until it runs successfully, the committed CI guard fails loud on every pull request and boot surfaces
the unprotected state every session, so an unprotected repo is never silently the operating baseline — but
nothing mechanically forces the operator to complete it. That honest limit is the same one the other
lifecycle operations carry; the structural close for the residual (an engine that holds the operator's
credentials in solo and *could* act on the ruleset) is the operator's choice of the team identity tier.

**The token-handling detail is the corrected build-spec leaf.** The locked design illustrates the required
permission as `admin:repo_ruleset`; that is not a real GitHub scope (verified against GitHub's live
documentation), so this operation uses the standard `repo` permission (or a fine-grained "Administration"
permission), which a normal GitHub login already carries. The locked *contract* — operator-privileged
actor, consent at the authorization screen, verify-after, degrade-never-fake, the protection floor — is
unchanged; only the inaccurate scope name is corrected, and the design prose is flagged for amendment.

**Never fake the gate.** A degraded outcome is always disclosed in plain language with a real next action;
the engine never reports protection as on when it is not, and never auto-deletes or weakens protection.
