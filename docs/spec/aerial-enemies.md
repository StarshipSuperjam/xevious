---
status: locked
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Aerial enemies

Covers mechanics catalog rows AIR-01 through AIR-12. Values cite the pinned reference
(`reference_pin` in [the index](index.md)) as `file label lines`; citations are `src/xevious_main.68k`
unless noted. Score values are owned by [Scoring, lives, and game over](scoring-lives-and-game-over.md);
formation composition by [Difficulty and formations](difficulty-and-formations.md) and the type table in
[data/object-types.json](data/object-types.json). Speeds are pixels per frame at 60 frames per second
(converted from the reference's raw fixed-point units).

License status of extracted values: the reference states no reusable license (recorded in [the index](index.md) and every data file).

## Summary

Twelve airborne families attack in scheduled waves of one to six, each with its own approach, firing
rule, and exit. Shared machinery underlies them all: one homing-angle system with four speed tiers (1.5,
2, 3, and 4 px/frame), one shared random stream feeding every fire timer, per-family fire-permission
masks from the area schedules, a common six-slot air-enemy pool, one shared hit window, and one shared
explosion. The families are variations on that machine — which is why the spec records the shared rules
once and each family as its differences.

Excluded here (Super Xevious only, catalog EX-01): the Galaxian bonus enemy is never scheduled or
built.

**Per-family verification status.** This document is settled so its slice-8 leaves can proceed, but not
every family below was re-verified against the reference to the same bar when it settled. Verified
line-by-line against the pinned reference and built: the **Shared rules** and the **Toroid (AIR-01)**
paragraphs (the slice-8 vertical slice). The other eleven families (Torkan, Zoshi, Jara, Kapi, Terrazi,
the Zakato line, Brag/Garu Zakato, Sheonite, the Sparios, Bacura) are transcribed from the reference as
the plan of record, but their line-by-line verification lands with their own build slice (10–11); each is
confirmed against the reference — and this document amended where it diverges, with the operator's
acknowledgement — as that slice builds. Treat an unbuilt family's description as drafted-pending-
verification, not as checked ground.

## Behavior

**Shared rules.** Air enemies fill the six flying slots from the current formation: while the formation
count is N (1–6), the spawner refills the first N flying slots every frame, so each slot re-spawns a fresh
enemy the moment its occupant leaves or is destroyed — the continuous wave pressure — until a
reset-formation record zeroes the count (`main_fn_4__spawn_flying_enemies` 5171–5186). A refilled slot
re-draws its lateral column but inherits the previous occupant's scroll-axis position, so a replacement
may enter mid-field rather than from the top — recorded as coded (`add_obj_handler` 4801–4815,
`check_scroll_offscreen` 4827–4839). Homing aims use the four angle tables (6290–6394; speed
tiers 1.5 / 2 / 3 / 4 px/frame). Periodic fire is gated to every 8th frame; each reload draws from the
shared random stream masked by the family's fire-permission byte captured at spawn — the mask caps the
random reload interval (`chk_timer_fire_bullet_reinit_timer` and inline equivalents). All families share
one blaster hit window — vertical ±16, horizontal ±8 in the reference's shadow units
(`check_shot_hit_flying_enemy` 2565–2577) — and one ~20-frame hit explosion (`flying_enemy_hit`
4865–4902), except where noted. Aimed bullets fly at 2 px/frame, radiating bullets at 3
([Player craft and weapons](player-craft-and-weapons.md) owns bullet rules).

**Toroid (AIR-01).** Spawns at a random lateral column, aimed at the craft at 1.5 px/frame (`init_toroid`
3332–3341); the spawn column is drawn from the shared stream, rejected and redrawn until it is on-screen
and not within eight columns of the craft (`gen_rnd_spriteY` 5155–5169). When nearly level with the craft
laterally (a narrow lateral-column window, offset ~[−2, 1], derived), it commits to a swing that
**reverses** its lateral course: it was spawned aimed at the craft, and the swing nudges that lateral
velocity by one unit per frame *against* the approach (`toroid_toggle_dir` → `toroid_swing_right`
`subq #1,_dY` / `toroid_swing_left` `addq #1,_dY`), so it decelerates, stops, and peels away from the
side it was closing on — the arcade "swing," not a homing dive. An eight-code flap animation advances
every other frame (played in opposite order for the two swing directions: right descending F..8, left
ascending 8..F), while its approach along the scroll axis continues (3289–3327); once triggered it never
re-tests the window. The
shooting variant fires exactly one aimed bullet at that trigger, never again (3281–3286, 3323–3327).

**Torkan (AIR-02).** Approaches aimed at 2 px/frame with an initial fire delay of 64–127 frames drawn
from the stream (3357–3369); fires one aimed bullet, then on a ~64-frame cycle recomputes the angle to
the craft, flips it 180°, and retreats at 3 px/frame (`torkan_update_dir` 3395–3410) — attack, shoot,
break away.

**Zoshi (AIR-03).** Three scheduled variants share one movement core (3414–3499): the top and bottom
spawners (bottom entering at a fixed edge position) fire *aimed* shots; the random variant fires in a
genuinely random direction (its angle index is a raw draw from the stream) and scores lower. All three
fire periodically under the Zoshi mask.

**Jara (AIR-04).** Aimed approach at 3 px/frame; at a wider proximity window (~[−6, 5], derived) it
banks into a left/right spin with a 6-frame sprite cycle (3502–3595); the shooting variant fires exactly
one aimed bullet at the trigger. Scores per the scoring table.

**Kapi (AIR-05).** Aimed approach at 2 px/frame; on its fire trigger it picks the craft's side and
power-dives — vertical acceleration toward the craft with steady horizontal deceleration — firing
repeatedly under the Kapi mask while diving (3602–3664). Its initial fire-delay constant carries a
recorded uncertainty: the reference's code and its own comment disagree (an unmasked double-add versus
the commented 48–111 range), noted as a probable transcription slip in the reference; the build follows
the commented range and records the deviation.

**Terrazi (AIR-06).** Aimed approach at 3 px/frame, firing under the Terrazi mask while distant; inside
a narrow window (~[−4, 3], derived) it stops firing and glides — decelerating and reversing over roughly
24 frames (3667–3729). The catalog's older description of a distinctive "expand" attack is unsupported
by the reference and is recorded as ruled out.

**Zakato line (AIR-07).** All Zakatos teleport in with a ~20-frame sparkle during which they cannot be
hit (`init_teleport` 3961–4006), then live briefly and fire **exactly once**: firing is terminal — the
Zakato launches its single aimed bullet and immediately self-destructs through its own ~20-frame flash,
awarding nothing (`zakato_shoot` 3761–3771, `zakato_explode` 3931–3950). Points are scored only by
killing it first. Variants: slow (1 px/frame drift, random 1–256-frame fuse), close-Y (same drift, fires
when level with the craft), fast (aimed at 2 px/frame, 1–64-frame fuse), continuous-type (aimed, fires
when level) — per their handlers 3733–3859.

**Brag Zakato (AIR-08).** Teleports in like a Zakato; its terminal event is a five-bullet aimed fan
(five radiating bullets two angle-steps apart at 3 px/frame, `brag_zakato_shoot` 5054–5073) — the random
variant on a 1–64-frame fuse, the proximity variant when level with the craft (3863–3924).

**Garu Zakato (AIR-08).** Independently scheduled (not spawned from another Zakato, and with no
teleport-in — recorded as a correction to the catalog's older phrasing): drifts at 3 px/frame on a
32–63-frame fuse (4010–4029). Shot in time, it dies normally and scores. Left alone, it detonates into a
full 16-bullet 360° ring at 3 px/frame plus four Brag Sparios launched in the cardinal directions at
2 px/frame (5075–5116), awards nothing, and vanishes without an explosion animation. (A stray "500"
in the reference's comment has no code path; recorded uncertain.)

**Sheonite (AIR-09).** The indestructible escort pair around the Andor Genesis encounter: born in the
benign state, never hit-tested, started and ended by their schedule records. Each homes at 4 px/frame on
a point straddling the craft, then locks rigidly to a ±16-pixel offset from the craft's live position,
holds through an asymmetric docking phase (224 frames right, 32 frames left — recorded as coded), after
which the right one retreats at 6 px/frame and the left simply vanishes (4052–4245).

**Giddo Spario and Brag Spario (AIR-10).** Giddo Spario is the fast flyby: aimed once at spawn at
4 px/frame — the fastest tier — with no firing and its own short ~8-frame hit explosion (5219–5257).
Brag Spario is the accelerating homer: it re-aims continuously, accelerating toward the craft's current
position without bound (3080–3129); it arrives scheduled or four-at-a-time from a Garu Zakato
detonation.

**Bacura (AIR-11).** The indestructible spinning slab: spawned one per second up to the area's scheduled
quota (`main_fn_5__inc_num_bacura` 5201–5217; quota set per schedule record), drifting at 1 px/frame.
Blaster shots die against it — deflected per the bounce rule in
[Player craft and weapons](player-craft-and-weapons.md) — it never scores, bombs do not affect it, and
touching it kills the craft through its own larger hit window.

**Bullets (AIR-12).** Enemy bullet rules — pool, speeds, aiming, and expiry — are owned by
[Player craft and weapons](player-craft-and-weapons.md); this document owns only which families emit
which patterns.

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| Every family's speeds, fuses, windows, and per-variant differences in the build's data match this document | Data-table comparison fixture over the build's generated family constants | engine |
| Each family plays its recorded pattern — approach, trigger, fire rule, exit | Play scheduled waves of each family in the built `.sb3` against this document's descriptions | operator |
| A Zakato that fires its shot self-destructs scoring nothing; one killed first scores | Play: let one fire, kill one early | operator |
| A Garu Zakato left alone rings 16 bullets and releases four Brag Sparios | Play (or seeded fixture) the detonation | operator |
| Sheonites cannot be killed and track the craft in the recorded pincer-and-dock pattern | Play the boss approach | operator |
| All families share one blaster hit window and one explosion; Giddo Spario's short variant excepted | Structural fixture over the build's collision and animation data | engine |
| Fire timing draws from the shared stream under the family mask (seeded waves repeat exactly) | Seeded fixture: identical seeds reproduce identical wave behavior | engine |
