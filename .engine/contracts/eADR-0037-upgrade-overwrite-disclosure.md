---
id: eADR-0037
title: Upgrade-overwrite disclosure — a distinct, non-blocking notice, separate from the weakening acknowledgment
status: accepted
date: 2026-07-18
---

## Decision

When a pull request in a **deployed** repository changes an engine file the next engine update will
overwrite, the engine tells the operator at that pull request, in plain language, that the change will not
survive the update — a **non-blocking** comment posted on the pull request. It never blocks a merge. This is
a signal **distinct from** the guardrail-weakening acknowledgment (`guardrail-ack` / `engine-guard`,
eADR-0011): that guards *weakening a protection* (a deliberate, blocking consent act); this discloses *a
change that won't survive an update* (a routine heads-up). eADR-0011 is left entirely intact.

The mechanism is bound by these rules:

- **Deployed-only, by a real property.** It discloses only when the repository's recorded update *home*
  differs from its own origin — an upstream that will overlay it. The self-hosting engine repository is its
  own home, so it is silent there (it never fires on engine-template's own pull requests).
- **Same-repo only; exempt for engine's own lifecycle PRs; failure is visible.** It runs on `pull_request`
  (not `pull_request_target`) and skips fork pull requests, keeping the fork-write-token attack surface
  closed. It is silent on an engine-authored lifecycle pull request (the update / removal / arrival PRs,
  identified by their deterministic head branch), whose purpose is to bring or remove this content — warning
  there would read backwards and cry wolf on the longest possible file list. Only paths from the overlay's
  own overwrite set are listed, each passed through a whitelist path-sanitizer that keeps the rendered code
  span airtight — markdown backslash-escaping is deliberately not used, because it has no effect inside a
  code span, so a crafted rename target is neutralized by dropping unsafe characters. A run that cannot
  complete is surfaced as a *visible* (non-blocking) failure, never a green that would read as "nothing will
  be overwritten."
- **The overwrite set is the overlay's own membership, so it cannot drift from the overlay's own logic.** The
  notice reads `module_manager.overlay_replace_paths()`, which shares the single `_overlay_copy_map`
  enumeration the real update overlay copies (present modules' `provides` files + their manifests +
  `FOUNDATION_CODE`). It never warns about a file the update **preserves** (engine config, operator config
  and conduct overrides, the per-deployment decision-record stream, the keyed-merge foundation files). It is
  deliberately **not** `module_coherence.engine_owned_paths` and must not be folded into it.

**Boundaries (accepted, so "no comment" is not a full-coverage guarantee).** (1) The notice covers files the
update **replaces wholesale**. It does **not** cover the keyed-merge foundation files — `CLAUDE.md`,
`AGENTS.md`, `.gitignore` — whose engine-fenced block is re-asserted while the operator's sections are
preserved; an edit *inside* that fence is overwritten but not disclosed. (2) The overwrite set is globbed
against the **live tree** standing in for a future release, so it is an honest approximation of that
release's file set, not a guarantee: a file a future release **adds** to the overlay is not warned about
until it exists locally. (3) A run that fails is visible only as a red mark on an advisory check (the Actions
surface), which a non-engineer may not read — an accepted residual of never blocking the merge.

## Significance

The engine already carried the "your local engine change is overwritten on update" idea, but only inside the
self-review agent, at audit time. This moves it to the moment of change, where a deployed operator can act
before the fix silently vanishes — closing a real trust gap (a surprise regression the operator cannot
self-diagnose) for exactly the engine files that get no other signal, since the weakening acknowledgment
covers only the gate files. Keeping it distinct from that acknowledgment protects both: the heavy consent act
stays reserved for weakening a protection, and the routine heads-up stays routine. Any later reader touching
the update overlay must keep `_overlay_copy_map` the single source both the overlay and this disclosure
consume, or the notice silently drifts from what the update actually does.

## Rationale

A soft validator finding lands only in the Actions log, which a non-engineer never reads, so the disclosure
had to ride the one surface that reaches them at the merge — a plain pull-request comment (the
`release_terminal` posture). The rubber-stamp fatigue that prompted this work is a *self-hosting* artifact:
the engine repository edits its own machinery every pull request, so its gates fire constantly here; a
deployed operator rarely touches engine files, so both this notice and the weakening acknowledgment fire
rarely there and each keeps its weight. `pull_request` over `pull_request_target` because a non-blocking
advisory does not need — and should not take — the fork-covering write token that the fail-closed required
guard needs. Non-blocking, because "this won't survive an update" informs a choice; it does not gate one. But
the failure must still be visible, or a broken net would masquerade as a clean "nothing to overwrite."

## Anti-choice

The rejected alternative was the original proposal: **repurpose the `guardrail-ack` guard itself** to cover
every file an update overwrites — one broadened guardrail instead of a new, separate signal. It lost on two
counts. First, it would delete eADR-0011's one irreducible promise: the guarded set exists to make
*weakening a protection* non-silent, and pointing it at the overwrite set would drop that meaning. Second, it
would re-create the exact over-firing that an earlier narrowing removed — flagging routine engine-file edits with the
heavy, deliberate acknowledgment trains the rubber-stamping the guard exists to prevent. A distinct,
non-blocking notice keeps the heavy consent act rare and meaningful and lets the routine heads-up be routine.
The narrower rejected option — detecting edits *inside* the keyed-merge fences of CLAUDE.md/AGENTS.md/
.gitignore — was deferred, not taken: file-granularity warning there would cry wolf on the common case (an
operator editing their own preserved section), and precise in-fence detection is a larger build; the residual
gap is recorded above rather than papered over.

## Status

accepted — one contrast updated by eADR-0040 (2026-08-08): the guardrail acknowledgment this record
distinguishes itself from is no longer a blocking act on every guarded touch; it blocks only at the
killswitch floor, and ordinary guarded modifications carry a non-blocking disclosure of their own. The
distinction this record drew (overwrite disclosure is not a weakening signal) stands unchanged.
