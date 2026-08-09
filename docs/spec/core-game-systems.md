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
player-dead → (respawning → playing | game-over) → title`. The complete recorded edge set, including the
cabinet extension (whose behaviors are specified in [Cabinet flow](cabinet-flow.md)):

| From | To | When |
| --- | --- | --- |
| boot | attract-title | cold start |
| attract-title | attract-demo | title stage completes |
| attract-demo | attract-scores | demonstration craft destroyed |
| attract-scores | attract-demo | scores stage completes (cycle continues to attract-title) |
| any attract state | ready | a paid start (coin-up resets the attract cycle first) |
| ready | playing | READY presentation completes |
| playing | player-dead | any lethal contact (or a recorded fixture key) |
| player-dead | respawning | craft remain (and, two-player, after player-change) |
| player-dead | game-over | no craft remain (after the high-score check routes entry if earned) |
| respawning | playing | respawn READY completes |
| game-over | high-score-entry | qualifying score |
| game-over / high-score-entry | attract-title | flow complete |

The cabinet-side states (attract stages, high-score entry, player change) are the recorded plan for the
cabinet slices; the gameplay-side edges are built today. Every ordinary transition
additionally passes through a short transient `resetting` state — the director's internal step between
stopping old work and entering the destination; it is part of the recorded machine and structural
fixtures must expect it. In the Scratch build the Stage is the sole writer of state; all transitions pass
through one transition block that stops old work, applies a named reset scope (cold-start, new-game,
new-life, game-over — each with the postconditions recorded below), and enters the
destination exactly once. One postcondition is
already known to diverge from the arcade: the new-life scope preserves terrain position, while the arcade
restarts the area from its top on every new life
([Area progression and terrain](area-progression-and-terrain.md)) — recorded as an interim fixture for
correction with the life-economy work. Presentation timings around these states, honestly marked: the READY hold (currently 30 ticks) is a
**project-defined placeholder with no reference basis** — and the READY presentation state itself is a
recorded deliberate presentation choice pending arcade confirmation of its arcade equivalent; the
current 0.7-second death presentation is a **recorded divergence from the known arcade value** (the
56-frame explosion plus 32-frame pause owned by
[Player craft and weapons](player-craft-and-weapons.md)), corrected by the recovery build; the GAME OVER
hold has its reference value — 128 frames — normatively owned by
[Scoring, lives, and game over](scoring-lives-and-game-over.md). The READY speech-bubble presentation in the
current build is an unsupported invention already recorded for correction.

The reset scopes' postconditions (this document is their
normative home):

| Reset scope | Postcondition |
| --- | --- |
| cold-start | Stop sounds and old work; remove or hide clones, weapons, targets, bomb, and death effects; rewind terrain to its canonical area-1 state; reset player and reticle positions; hide the player; show one title screen. |
| new-game | The cold-start world reset with the title kept hidden, then one entry into READY. |
| new-life | Stop old work; clear weapons, clones, targets, bomb, and death effects; reset player and reticle positions; then one entry into respawn READY. Interim divergence: the current build preserves terrain here, while the arcade restarts the area from its top (above). |
| game-over | Stop gameplay and audio, clear transient gameplay, hold the final terrain under the GAME OVER display; the following cold-start rewinds the world before title. |

Per-state input rules (project-defined for the current slice, pending cabinet work): title accepts only
the start key; READY, player-dead, respawning, and game-over accept no gameplay input; playing accepts
movement, fire, and bomb — plus, until the life economy lands, the two recorded temporary fixtures `D`
(request respawn) and `G` (request terminal death), which exist only to exercise the death paths and are
removed by the life-economy work. Repeated keys can never duplicate transitions, loops, shots, or bombs; the
green flag from any state performs the cold-start reset; stop halts the project.

**Entity lifecycle and capacities (SYS-02).** The reference runs 64 fixed object slots, each a small
state record; every entity passes idle → active → hit/destroyed → idle, and per frame the main loop walks
the slots in ascending order dispatching each to its per-type handler
(`xevious_main.68k` `main_fn_2__handle_objects` 4745–4798, `add_obj_handler` 4801–4815; empty slots carry
type 0, skipped). The slot ranges and their capacities are recorded once, in
[Player craft and weapons](player-craft-and-weapons.md), and bind as gameplay-visible limits. Two
capacity questions closed here: destroyed land objects' craters remain *in their slot*, scrolling until
culled at the recorded margin — craters are not a separate entity class and cannot accumulate beyond the
ground slots; and during a boss encounter Andor Genesis's fifteen parts occupy ground slots 1–15
(`xevious_sub.68k` `sub_2_fn_20__andor_genesis_start` 544–563), leaving slot 0 for ordinary ground
traffic — the schedule data respects this. Explosion and bounce presentations animate within their
owner's slot. Scrolling
carries map-anchored entities; an entity leaving the play area is culled at the recorded margins
(`check_scroll_offscreen` 4826–4839) and its slot freed with no leaked state. The complete object-type
vocabulary — all 93 codes — is the generated registry in
[data/object-types.json](data/object-types.json): sprite-bearing object types carry their handler and
name; the 25 pure schedule-control codes carry no sprite handler and are named by their
`schedule_action` instead. Prose documents refer to object types by name and control codes by action,
never restating the numbers.

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
(`xevious_main.68k` `pseudo_random_gen` 1428–1445; direction variant `gen_rnd_dir` 2156–2165). The full
update rule, instruction-derived: the low byte becomes 5·low + 1 (mod 256); the extend bit is the carry
of that +1, forced to 1 when the high byte's bits 7 and 2 are both set or both clear; the high byte
rotates left through the extend bit; the returned value is the sum (mod 256) of the new low and high
bytes. The rule and tool-computed golden sequences from five recorded fixture seeds are committed in
[data/rng.json](data/rng.json) — the normative definition a build implements and its fixtures compare
against. Consumers draw on demand during the per-frame slot walk, so the draw order within a frame is
the slot-walk order. **Recorded port decision:** to reproduce that ordering, the build runs one
centralized update loop — the Stage walks the slots in index order each tick — with clones acting as
renderers of their slot's state; free-running per-clone update threads cannot guarantee draw order and
are ruled out for stream consumers. Consumers are the behaviors marked *random* in their own documents;
each says "draws from the shared stream," never defines its own.

**Units and the clock (port conversion — the single normative home).** The acceptance runtime is
Scratch 3 at its 30 ticks per second; TurboWarp compatibility is welcome but non-normative. One build
tick represents **two arcade frames** (the arcade runs 60 per second). Frame counts in this spec halve
accordingly, rounding to the nearest tick with ties rounding up, and every rounded value is recorded in
the build's data next to its arcade original. Gameplay never uses wall-clock waits, the timer, or
Scratch's own random blocks — all gameplay timing counts ticks and all randomness draws from the shared
stream, or seeded runs cannot repeat. The spatial factor (stage units per arcade pixel, and the
playfield rectangle on the 480×360 stage) is a single pair of values that will be recorded **in this
section** by the movement slice; the current build's de facto 7 stage units per tick is unratified until
then. Current control mapping, recorded as the port's own: arrow keys move, Space fires, B bombs, plus
the temporary D/G fixtures above; a rebinding is a spec amendment.

## Acceptance criteria

No runtime harness exists in this repository — no check can execute the game. Engine rows below are
static reads of the built project's files and data; every observation of running behavior is the
operator's, until a runtime harness is built (a tracked follow-up, not a promise).

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| One state machine owns all transitions; the director block graph contains every recorded edge and no state write outside the transition path | Structural fixture walks the built project's director block graph | engine |
| Each reset scope's transition script contains its recorded cleanup actions, in order | Structural fixture over the generated transition scripts | engine |
| Reset scopes leave their postconditions on screen (title shown once, player hidden, terrain rewound per scope) | Play the built `.sb3` through each transition | operator |
| State-timing placeholders and divergences are marked in the build's mechanics records until fidelity work replaces them | The PR author records them; the operator's review confirms | operator |
| Entities never leak across a long session | Play an extended session watching for missing spawns or stuck sprites | operator |
| Capacity limits are honored in play — never a fourth shot or a second bomb on screen | Play the built `.sb3` and probe the limits | operator |
| The build's generator encodes the recorded update rule and its data equals the committed golden sequences | Static comparison of the build's generated RNG data against [data/rng.json](data/rng.json) | engine |
| Seeded runs repeat exactly | Two runs from one seed look identical to the operator (fixture-automated when a runtime harness exists) | operator |
| A hit never scores twice | The block graph routes all scoring through the single path (engine, structural); simultaneous-contact behavior confirmed in play | operator |
