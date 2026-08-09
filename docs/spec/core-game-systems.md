---
status: draft
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Core game systems

Covers mechanics catalog rows SYS-01 through SYS-04. Values cite the pinned reference
(`reference_pin` in [the index](index.md)) as `file label lines`. This document is the normative home for
the object-type vocabulary, the collision-group matrix, and the random-stream rules; other documents name
these and link here.

## Summary

Four systems underlie everything the player sees: a single game-state director that owns every allowed
state transition; a shared entity lifecycle that every enemy, projectile, and effect passes through; a
fixed set of collision groups in which any hit resolves exactly once; and one deterministic pseudo-random
stream that all stochastic behavior draws from. They exist so the game is testable and its behavior
reproducible — the properties the rest of this spec depends on.

## Behavior

**Game-state director (SYS-01).** One state machine owns the game: `boot → title → ready → playing →
player-dead → (respawning → playing | game-over) → title`, extended by the cabinet states (attract cycle,
high-score entry, player change) specified in [Cabinet flow](cabinet-flow.md). In the Scratch build the
Stage is the sole writer of state; all transitions pass through one transition block that stops old work,
applies a named reset scope (cold-start, new-game, new-life, game-over — each with the postconditions the
merged slice-2 contract records), and enters the destination exactly once. One slice-2 postcondition is
already known to diverge from the arcade: the new-life scope preserves terrain position, while the arcade
restarts the area from its top on every new life
([Area progression and terrain](area-progression-and-terrain.md)) — recorded as an interim fixture for
correction with the life-economy work. The current presentation
timings around these states (READY held one second, a 0.7-second death animation, GAME OVER held two
seconds) are **project-defined placeholders with no reference basis**, recorded as such pending the
presentation-fidelity work; the arcade's own state timings are specified where extracted (for example the
56-frame explosion plus 32-frame pause in
[Player craft and weapons](player-craft-and-weapons.md)). The READY speech-bubble presentation in the
current build is an unsupported invention already recorded for correction.

**Entity lifecycle and capacities (SYS-02).** The reference runs 64 fixed object slots, each a small
state record; every entity passes idle → active → hit/destroyed → idle, and per frame the main loop walks
the slots in ascending order dispatching each to its per-type handler
(`xevious_main.68k` `main_fn_2__handle_objects` 4745–4798, `add_obj_handler` 4801–4815; empty slots carry
type 0, skipped). The slot ranges and their capacities — 16 ground objects, 16 Bacura, the
bomb/crosshair/craft group, 3 player shots, 19 enemy bullets, 6 flying enemies — are recorded in
[Player craft and weapons](player-craft-and-weapons.md) and bind as gameplay-visible limits. Scrolling
carries map-anchored entities; an entity leaving the play area is culled at the recorded margins
(`check_scroll_offscreen` 4826–4839) and its slot freed with no leaked state. The complete object-type
vocabulary — all 93 codes, each with its handler, name, schedule action, and Super-only flag — is the
generated registry in [data/object-types.json](data/object-types.json); prose documents refer to types by
name and never restate the numbers.

**Collision groups and single-hit resolution (SYS-03).** Five groups exist, and no others: player shots
versus air enemies; bombs versus ground objects; enemy shots versus the player; air enemies versus the
player; Bacura versus the player. A hit resolves exactly once — the reference marks the struck object's
state to *hit* in the frame of contact, and scoring flows through the single scoring path
([Scoring, lives, and game over](scoring-lives-and-game-over.md)), so nothing can double-score. The
exception verdicts, exhaustively:

| Interaction | Verdict | Where specified |
| --- | --- | --- |
| Player shot hits Bacura | Bacura is not destroyed; the shot bounces back and expires | [Player craft and weapons](player-craft-and-weapons.md), [Aerial enemies](aerial-enemies.md) |
| Bomb hits Bacura | No effect — Bacura is indestructible | [Aerial enemies](aerial-enemies.md) |
| Player shot hits a ground object | No effect — ground objects are bombed, never shot | this table |
| Bomb near a hidden Sol Tower / Bonus Flag | Reveals rather than destroys; staged rules | [Secrets](secrets.md) |
| Andor Genesis parts | Only the core path destroys the boss; parts have their own rules | [Andor Genesis](andor-genesis.md) |
| Player collides with a ground object | No collision — the craft flies above ground level | this table |
| Anything hits the player | One death, regardless of source; distinct Bacura hit window | [Player craft and weapons](player-craft-and-weapons.md) |

**Pseudo-random stream (SYS-04).** One 16-bit seed serves the whole game
(`xevious_main.68k` `pseudo_random_gen` 1428–1445; direction variant `gen_rnd_dir` 2156–2165). The low
byte updates as low ← 5·low + 1 (mod 256); the high byte rotates left through a carry derived from its
own bits 7 and 2 — the exact carry rule in the edge case is recorded by fixture rather than formula (see
acceptance below) because a closed-form paraphrase risks exactly the silent error this spec forbids.
Consumers draw on demand during the per-frame slot walk, so the draw order within a frame is the slot
order — the property the build must reproduce for seeded runs to repeat. Consumers are the behaviors
marked *random* in their own documents (Zoshi random paths, Zakato variants, Brag Zakato teleports,
radiating bursts, attract-mode piloting); each says "draws from the shared stream," never defines its own.

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| One state machine owns all transitions; every edge on the recorded list, none besides | Structural fixture walks the built project's director block graph | engine |
| Every reset scope leaves its recorded postconditions | Deterministic reset fixtures per scope | engine |
| State-timing placeholders are marked project-defined in the build's mechanics records until fidelity work replaces them | Mechanics-record review on the PR that touches them | engine |
| Entities never leak: an accelerated run leaves every slot reclaimable | Deterministic soak fixture counts live entities before and after | engine |
| Capacity limits are honored in play — never a fourth shot or a second bomb on screen | Play the built `.sb3` and probe the limits | operator |
| The random generator reproduces the reference sequence | Fixture: from recorded seeds, the build's generator matches the reference-derived expected sequence for 1000 steps | engine |
| Seeded full-area runs repeat exactly | Two runs from one seed produce identical event traces | engine |
| A hit never scores twice | Fixture: simultaneous-contact cases award once | engine |
