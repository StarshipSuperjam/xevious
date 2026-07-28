---
title: Boot — the SessionStart orientation pack
---

## Purpose

Orient a cold session: assemble a bounded, prioritized, plain-language `Project status` pack from
committed state and the substrates that exist, and inject it before the first prompt so the session
starts grounded instead of blind. This is the heaviest member of the orientation family and runs
automatically — there is no manual "start the engine" step. Enter it whenever a session begins
(`startup`, `resume`, or `clear`); it is **read-only of canonical state** (its one local write is the
gitignored standing-alarm presentation ledger — see Notes) and never blocks. Beneath it, and independent of it,
sits the floor: the root `CLAUDE.md` the platform always loads, which grounds the session even if this
pack does not run.

## Steps

The mechanism is `.engine/tools/boot.py`, wired as a `SessionStart` hook in `.claude/settings.json`; it
runs these steps in order and degrades over any source that is absent — one failure costs that line
only, never the whole pack, and the session never halts.

1. Read the committed state cursor (`.engine/state/state.json`). If it is unreadable or not a
   schema-version-1 cursor, say so plainly ("I couldn't read where the project stands") and treat
   project status as unknown — never halt.
2. Detect the governance-critical alarms to pin at the top of the status dashboard: the protected-branch signal
   (relayed from `protection_guard`; off → a nag, unverifiable → an honest "don't assume it's on", on
   → silent) and the engine's open self-monitoring findings (read-only, from telemetry's register). Also relay
   a **stranded operator checkout** — the boot-invoked `checkout_health` detector finding the top-level project
   folder stuck off its branch or missing the engine's files — read-only at the open-findings tier, BELOW the
   governance alarms (a stranded local checkout cannot reach the protected branch), and **offer to repair it**.
   Boot only surfaces and offers; it never repairs.
3. Consume the attention ranking (`attention.rank_live`) in its given precedence order — never re-rank —
   and resolve each ranked item to a plain-language line under "Needs your attention".
4. Read the integration-debt readout (offline count from state, rendered loud-if-stale) and the
   recently-shipped digest (from merged pull requests — there is no changelog).
5. Assemble the AI-facing **briefing** and inject it as `additionalContext`. The briefing reaches the
   model, never the operator's screen, so it instructs the assistant to render a short titled `Project
   status` block first (all-clear, or a `⚠` line when something fired), relay the governance-critical
   alarms to the operator in plain words, and surface a brief needs-attention headline; the operator-toned
   status dashboard follows for grounding. The present-marker line and the must-push set are a fixed relay
   over the signals the substrates already detected — boot computes nothing new.
   - **Anti-habituation collapse.** A standing governance alarm renders on **every** session it is live, but
     one **unchanged since last relayed in full** collapses to a **terse one-line reminder that still names the
     consequence and offers the fix**; a **new, changed, or worsened** condition relays in **full**.
     New-versus-old is carried in words ("still … (unchanged)" vs "this has grown"), never length alone. The
     **present-marker line and the all-clear never collapse** — only the payload behind the marker varies. The
     read, the terse-versus-full decision, and the write all run in the deterministic hook (`boot_alarm_ledger`),
     never the model, and are **fail-toward-full**: any missing/unreadable/write-failed ledger or ambiguity
     renders the alarm in full (repetition is tolerable; suppression is not). The relay is a **once-per-session
     act in the grounding reply** — each alarm named with its consequence, never wrapped in an invented "boot
     check" preamble, and the "(unchanged)" framing is **not re-surfaced on later turns** (if asked again,
     answer plainly without restapling the boot wrapper).

To print the assembled briefing by hand (a debug view of what the hook injects): `python tools/boot.py pack`.

## Done when

The assistant opens its first reply with a short titled `Project status` block (the present marker a
grounded session always renders; its absence is how the floor tells the operator the engine did not
ground) and relays any governance alarm in plain words. Any degraded or unverifiable source is named in
plain language rather than silently dropped, and no canonical state was regenerated — boot reads and
surfaces, its sole local write the gitignored standing-alarm presentation ledger.

## Notes

`compact` is deliberately not a trigger: a full re-render after compaction is a deferred enhancement
that must never be depended on, so the reliable post-compaction floor stays the re-injected `CLAUDE.md`
plus the next per-prompt scent. That scent fires on every prompt, so the floor is reached reliably — but what
it carries is a reminder to consult saved memory, not orientation content, so it restores the reflex to go
looking rather than the picture itself. The memory set-aside readout and the modes stance line
render only once those substrates exist, so on a fresh engine they are simply absent.

**The standing-alarm presentation ledger is boot's one local write.** It is a small, local,
gitignored, non-canonical marker at `.engine/boot/.cache/standing-alarms.json` (`boot_alarm_ledger`),
recording each surfaced standing alarm's structured condition and that it was shown in full, so the next
session can collapse an unchanged one. It is read and written by boot's own `SessionStart` hook, lives at
a stable per-instance path under the shared clone root (never an ephemeral worktree, so it spans separate
sessions on the one machine), shares **no code path** with memory's own session-start work, is never
committed, and is **fail-toward-full** (any loss or ambiguity renders the alarm in full).

**Beyond what this pack pushes, a session can reach the wiring map deliberately.** When a change needs an
impact check — what depends on a part, what checks or governs it — or when a part is unfamiliar, the
session can query the project's own map; when and how is `.engine/operations/knowledge-impact-check.md`.

**Boot's repair offers share one contract; each carries its own loss semantics.** When boot surfaces a
recoverable problem it **only surfaces and offers** — the assistant **waits for the operator's explicit
go-ahead** and never runs a repair un-asked. Every repair is **lossless-or-it-does-not-run**: it protects
anything at risk first, or refuses/blocks rather than guess, and the assistant relays the plain-language result
and never forces. What differs is *what* each protects and *how* it declines:

- **A fresh copy still needing setup — walk `/engine-setup` (`instantiator`, #353).** The operator's main
  checkout is a copy of the template whose one-time setup hasn't finished — its origin
  differs from the recorded update home and the one-time setup tool is still present — so it would otherwise
  silently report itself "already set up." Provisioning's `first_run_health` detects it OFFLINE and boot pins
  the onboarding offer at the **top** of the dashboard (the root action that frames every other signal; it also
  suppresses the redundant "your safety gate is off" offer, which setup turns on). Unlike the repairs below the
  fix is not a write boot makes: on the operator's "set up my project" the assistant walks the `/engine-setup`
  verb (the instantiator's confirm → apply → verify → retire), which is idempotent and **resumes** a setup
  interrupted partway. A best-effort ONLINE parentage read (`first_run_health.forked_from_home`) suppresses the
  offer for a contributor's fork of the engine home (not an adopter); offline the offer still shows — read-only
  and low-harm.
- **An engine-mechanic with no usable product checkout — setup (`checkout_health.mechanic_orientation`,
  eADR-0026).** This engine records an executable `product_build_target`, so it builds a SEPARATE owned checkout,
  but this machine's path to that checkout is missing or points at nothing. The reader classifies it OFFLINE
  (`path-unset` / `path-unreachable`); boot pins the setup offer and **suppresses it while first-run setup is
  pending**, so onboarding asks once, not twice. Like the fresh-copy offer above and unlike the repairs below,
  the fix is not a write boot makes: on the operator's "point me at my product checkout" the assistant records
  the folder in the gitignored `.engine/mechanic/product-checkout-path` (durable and per-machine — an
  environment variable would not outlive the session), and on "clone my product for me" it clones the recorded
  product as a SIBLING folder beside the engine's own, never inside it. The unreachable case echoes the
  recorded folder home-contracted (`~/…`) so a typo can be corrected without putting the account name on a card
  the operator may paste; the healthy case shows no path at all. The path is never verified here — the
  fail-closed preflight in `mechanic_build` decides whether the checkout is really that product and safe to write in.
- **A stranded checkout — un-stranding (`checkout_health.unstrand`).** The deployed-floor never-strand-main
  rule's one sanctioned write to the operator checkout: it rescues at-risk work — commits drifted off the branch,
  or unsaved changes — to a safe point first, then re-attaches the folder and restores the missing engine files.
  If it cannot safely tell where to re-attach the folder, it refuses rather than guess.
- **A folder with newer shared work — catch-up (`checkout_health.catch_up`, #335).** One fresh remote snapshot
  reports any upstream commit the default branch lacks, including direct or squash/rebase-shaped work. Ordinary
  drift gets a calm, count-free notice; missing merges beyond the project-relative velocity bar get the existing
  firm warning. If refresh, remote identity, or the remote default cannot be confirmed, boot says the check is
  unavailable and never calls a cached view current. On consent, catch-up revalidates the pinned repository,
  branch, HEAD, and exact target, then advances only along a safe fast-forward. Movement visible at revalidation,
  divergence, or clashing work blocks before mutation; a final postcondition check prevents a racing external
  Git operation from ever being reported as a successful repair.
  After the operator says "bring it up to date," the assistant runs the machine-readable `snapshot` command,
  takes its exact `target_oid`, and supplies that value to `catchup --apply --target <OID>` (or the return arm
  below). Apply refuses without that consent-time target and also refuses if a newly refreshed target differs.
- **A folder parked off its main line — return (`checkout_health.return_to_default`, #342).** The behind
  signal is two-stage: **Stage 1 (off-main)** surfaces gently — caught offline, every session, on day one — that
  the folder points at a side line rather than the main project; **Stage 2 (behind)** escalates to a firm offer
  once its missing work crosses the firm velocity bar; below it, the ordinary drift notice remains calm. One
  consent handle — "bring it up to date" — runs whichever fits: `catch_up`
  on the default branch, `return_to_default` when parked off it. Returning to a *named* side line never orphans its
  commits (the side line keeps them — no rescue needed, unlike the detached un-stranding arm); it switches only
  when nothing is uncommitted, stashed, or mid-operation, then uses the same freshly pinned and revalidated target.
  The honest result is pointed back and brought current, already on the main line, or blocked without mutation
  because the consent target changed, local/shared main lines diverged, or work is unsaved/paused. Spotting an
  off-main park is a newer check — a folder healthy before it existed isn't freshly broken, and the assistant
  says so the first time it surfaces.
- **A stranded pull request — reconcile (`pr_reconcile.reconcile`, #136).** A pull request that can't be merged:
  the reconcile acts only when the clash is confined to the engine's two internal index files — the knowledge
  graph and the self-map, the one clash that is *spurious* (both sides regenerate from one source tree) —
  reconciling against the latest default branch, regenerating those two files, and keeping both pieces of work. If
  anything else clashed it changes nothing, restores the branch exactly, and routes the operator to a
  plain-language decision. It never claims the merge is now guaranteed — a later change can still land first.
- **A half-finished engine update — finish it or undo it (`/engine-upgrade` → `module_manager.rollback`, #594).**
  An update was started but not completed, so the tree sits part-way between versions — detected offline by
  `module_manager._staged_upgrade_dirty` (overlay-code differs from the last commit — a non-engineer's ordinary
  edits don't touch engine code, and no coherence pass is needed). Nothing was merged, so it's safe — a
  recovery offer, not a governance alarm. On the operator's go-ahead the assistant opens `/engine-upgrade`,
  which offers **finish** (`upgrade --confirm`) or **undo** (`rollback --confirm`). The undo is
  lossless-or-it-does-not-run: it saves a recovery point (a local "safe point" branch capturing everything)
  **before** reverting the tree, **refuses** if the operator has unsaved work of their own in files the update
  doesn't touch, resets the update's own files and the shared setup files it changes (keeping the operator's
  version of those on the recovery point, disclosed in the result), and puts back any saved memory the update
  changed (keeping the guard that an older copy never overwrites newer memory).
- **A safety gate that's off — re-enable branch protection (`bootstrap.ControlPlane.apply`, #392).** On the
  operator's "turn my safety gate back on," the assistant runs the already-built `ControlPlane.apply` instead of a
  manual settings walk-through: it re-enables the protection floor on the default branch — idempotent and additive,
  repairing or augmenting the ruleset in place, preserving any protection already there, and reporting "already
  protected" with no change when it is already in force. It runs the operator's OWN `gh` behind a one-time GitHub
  administration approval (never a typed command); if the token can't carry that admin it discloses why and changes nothing.
- **A leftover template license — clear it (a reviewed pull request) or keep it (`boot_alarm_ledger.retire`, #471).**
  The operator's checkout still carries the engine's own template `LICENSE` at its committed root (a repo generated
  before the first-run clear shipped, or drifted back to it); provisioning's `license_health` detects it and boot
  offers. Unlike the repairs above the fix is **not** a write to the checkout: on the operator's "yes, clear it" the
  assistant hands the one-file `LICENSE` removal to [build-orchestration](build-orchestration.md)'s **trivial fast
  path** — a reviewed pull request the operator merges (a live protected repo's committed license is removed durably
  no other way), **titled exactly `Maintenance: remove the leftover template LICENSE`** so the standing detector's
  open-PR dedupe recognizes its own prepared cleanup and re-offers no duplicate — never a boot-time delete, and it
  seeds no replacement (the license is the adopter's choice). On the operator's "I meant to keep this" the assistant
  runs `boot_alarm_ledger.retire` (`python tools/boot_alarm_ledger.py retire`, an Explore-permitted tool call) so the
  offer stops surfacing from this checkout; a plain decline instead collapses it to a terse reminder, never fully silent.
- **No description yet — offer the intake, or dismiss it (`boot_alarm_ledger.retire`, #553).** When the project has
  the `engine-design` intake installed but no product description under `docs/spec/` yet, `greenfield_intake` detects
  the greenfield state and boot **offers** the intake at first engagement so a non-engineer discovers it — a pure
  offer, never an action (the operator starts the intake themselves). It fires only when the intake is actually
  installed (never offering a command that isn't there) and self-resolves the moment the intake runs and writes
  `docs/spec/index.md`; it stays silent in the engine's own home repository, where the thing being built IS the
  engine and a separate product description would be out of place. On the operator's "I'd rather work without a
  written description" the assistant runs `boot_alarm_ledger.retire` (class `greenfield_intake`) so the offer stops
  surfacing — run it as `python tools/boot_alarm_ledger.py retire-greenfield` (an Explore-permitted tool call),
which DERIVES the fingerprint from the live detector so the marker can never silently mismatch and keep the
offer firing; never hand-build the retire call. A plain not-now instead collapses it to a terse reminder,
never fully silent.
