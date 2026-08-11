---
id: eADR-0040
title: The guardrail-ack becomes rare — disclosure by default, blocking only at the killswitch floor
status: accepted
date: 2026-08-08
---

## Decision

The guardrail-weakening guard keeps watching the same property-defined set (D-268 is unchanged), but a match
now answers in one of **two tiers**, and the acknowledgment ceremony survives only at the lower, rarer one:

- **Disclosure (the new default).** A modification to a guarded enforcement file yields a plain-language
  finding at soft severity — which files changed and why each is load-bearing — and the check passes. No
  label, no block; the notice says plainly it needs no action and must never shape a design. It renders in
  the check output, the run summary, and a static checks-tab annotation, with every pull-request-controlled
  path whitelist-sanitized before rendering.
- **The killswitch floor (the ack survives).** Events whose weakening a merge-time diff read would plausibly
  miss still block until the operator applies `guardrail-ack`: the four value detectors (update-home repoint
  or deletion, build-target arming or repoint, team→solo downgrade, instance-declaration shrink); any touch
  to a path a deployment declared in its instance floor (that operator opted into the friction — their
  declaration keeps its meaning, and declaring engine paths there is hereby a blessed use, amending
  eADR-0011's "own territory" framing); removal or rename of **any** guarded file; the hard floor
  (`_HARD_EXACT` in the guard: `suites.json`, `uv.lock`, CODEOWNERS, the Codex wiring mirrors, the guard's
  own file, the ruleset/write-target seams `team_switch.py`/`mechanic_build.py`/`repo_identity.py`, the
  hard-check-bite meta-check, and the three check scripts whose bite is declared not-applicable); four
  fail-closed **directional detectors** that escalate gate-shaped modifications of disclosure-tier files
  (a check-rule edit is benign only when every touched line is provably the rule's `message` string; a
  workflow edit escalates on trigger/permission/checkout/invocation lines or an action swap, while a
  same-action version pin stays soft; a `settings.json` edit escalates when a gate-hook wire is removed; a
  `bootstrap.py` edit escalates on ruleset vocabulary); and every fail-closed path.
- **The ack downgrades, never erases.** With the label applied, a killswitch finding emits as a soft
  ACKNOWLEDGED record; disclosures are untouched by the label. (Previously the label erased every finding —
  including on the oversized-pull-request path, which promised the ack would clear it but never checked.)
- **The fail-safe direction for tier is HARD.** eADR-0011's when-in-doubt-guard rule carries over with a new
  axis: anything the tier machinery cannot cleanly classify — an unreadable patch, an anomalous line, an
  unclassifiable rule edit — resolves to the blocking tier, never to disclosure-by-else.
- **Authoring invariant.** The tier machinery (`_HARD_EXACT`, `classify()`, the directional detectors) lives
  in `weakening_guard.py`, itself a hard-floor member judged from the trusted base — so reclassifying a hard
  event as soft is itself a hard event. It must not be extracted to a sibling module or a data file.

## Significance

This supersedes the **blocking clause** of eADR-0011 ("a weakening change … hard-blocks the merge until the
operator performs one deliberate affirmative act") for ordinary modifications, and adopts — deliberately, on
evidence — a scoped form of the alternative eADR-0011's anti-choice rejected. The audit that decided it
(2026-08-08, method: direction-classification of every acknowledged pull request merged after the StarshipSuperjam/engine-template#370
narrowing, from title, body, and diff): **307 of 455 merged pull requests (67%) carried the label**; after
StarshipSuperjam/engine-template#370's property narrowing the rate stayed at **92 of 182 (51%)**; of those 92, **8 genuinely weakened** a
protection (9%), 8 were mixed, 33 strengthened one, 43 were neutral. Every genuine weakening was already
self-disclosed in its pull-request body; project memory records **no instance** of the red check changing a
merge outcome; and the ceremony measurably deformed builds (sessions dropping or relocating correct fixes to
avoid the label — the pressure behind the operator's standing "never under-build to avoid an ack" directive).
eADR-0011 predicted "undifferentiated alarms destroy the signal" and priced over-inclusion at "an extra
acknowledgment"; the audit shows the alarm fired undifferentiated at 51% and the real price was worse builds.
The rubber-stamping it feared arrived *through* the blocking ceremony, not through disclosure. What makes the
scoped surfacing tier honest where the rejected alternative was not: the killswitch floor still blocks where
a diff read plausibly misses the weakening, and two mechanical correlates back the demoted set — the
hard-check-bite meta-check proves every merge-blocking check still catches its planted violation (a neutered
check script or validator kind fails CI), and the protected-branch merge review reads every diff. Known
residuals, accepted: a path-conditioned backdoor in a check script escapes the bite proof and is caught only
by the diff read; the disclosure surface (run summary + annotation) is weaker than a red check, recorded here
in the style of eADR-0037's accepted residuals; and `guardrail-ack` is shared with the product-lock
re-acceptance check, so one label clears both checks' hard findings on the same pull request — pre-existing,
now on the record. In solo tier the honest bound was always "non-silent", not "impossible" (admin can bypass
the ruleset); this decision does not move that bound.

Adopters inherit the demotion at upgrade: it ships with a version-keyed retired-capability declaration naming
what no longer blocks, where the disclosure appears, and the instance-floor opt-back-in — a minor-version
floor, not major, because no interface breaks and the announcement plus a working opt-back-in restore the
prior posture for any deployment that wants it.

## Rationale

The label was built to make genuine weakening deliberate; at a 51% fire rate it made nothing deliberate.
Narrowing the guarded set was already tried (StarshipSuperjam/engine-template#370, D-268) and did not cure it, because in this repository the
enforcement machinery is the product — any file-set version of "blocking" re-creates the noise. Only
splitting *what happens on a match* changes the outcome. The tier criterion is a property, not a roster:
**hard where no mechanical correlate or readable diff would catch the weakening — and for the guard's own
set-defining machinery**; disclosure where a correlate exists. That criterion also drove the four members
kept hard beyond the operator's enumerated choice (the identity downgrade, build-target first-set, the
guard-file modification, the fail-closed paths) — each confirmed with the operator rather than ridden in.

## Anti-choice

A second guarded-set narrowing (keep the always-blocking ceremony, trim the set again) — refused because StarshipSuperjam/engine-template#370
executed exactly that cure and the fire rate stayed at half of all merges. Full retirement of the ack
(disclosure everywhere, no blocking tier) — refused because the killswitch class (supply-chain repoints,
one-token check demotions, ruleset gutting) is precisely where a diff read fails and a mechanical stop still
carries signal. Direction-aware classification (flag only true weakenings) — refused as unbuildable at this
guard's honesty tier: judging a diff's *direction* needs semantics the trusted-base, no-code-execution
posture cannot have, and a wrong "strengthening" verdict is a silent bypass.

## Supersedes

The blocking clause of **eADR-0011** (its classification property, trusted-base isolation, frozen names, and
fail-safe rule all stand; its anti-choice is answered above, and its "own territory" framing of the instance
floor is amended to bless engine-path declarations). Amends **eADR-0034**'s statement that the
provider-exceptions ledger is "held for the operator's deliberate acknowledgment" (it is disclosure-tier now,
directional escalation aside) and **eADR-0037**'s reasoning that `guardrail-ack` is reserved as a blocking
consent act (it now blocks only at the killswitch floor).

## Status

Accepted — operator decision, 2026-08-08, following a four-lens plan review of the audit and the design.
Backtest, recorded honestly: replaying this floor over the audited corpus (the 92 acknowledged pull requests
merged after StarshipSuperjam/engine-template#370), **40 would still block** — 22% of that window's merges, down from 51%, catching 5 of the
8 genuine weakenings mechanically. The residue concentrates in the guard's own file and check-rule structural
edits — the two killswitch classes this record refuses to demote — and is specific to this workshop, where
the enforcement machinery is the product; a deployed repository, whose operators rarely touch `.engine`,
sees the ack genuinely rarely. The operator accepted the measured rate with the bootstrap token narrowing.
