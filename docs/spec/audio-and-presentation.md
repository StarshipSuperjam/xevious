---
status: locked
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Audio and presentation

Covers mechanics catalog row CAB-05, the playfield framing (roadmap criterion PRES-01), and the
presentation-fidelity boundary. Values cite the pinned reference (`reference_pin` in [the index](index.md))
as `file label lines`.

License status of extracted values: the reference states no reusable license (recorded in [the index](index.md) and every data file).

## Summary

How the game looks and sounds: sprites drawn from the credited sheets, animation timed in arcade frames,
and audio cues bound to the game states that own them. Presentation is deliberately separated from
gameplay rules — a costume or sound never decides behavior — and its fidelity has its own honest
boundary: imagery and audio are interpretations under the asset policy, while timing and binding are
reference-derived where extracted.

## Behavior

**Sprites and imagery.** Game imagery derives from the five imported sprite sheets recorded in
`docs/ASSET_CREDITS.md` (credited, no license stated), sprites rendered from the pinned reference's own
graphics data (recorded in the asset provenance files), and the preserved 2017 baseline's own art; the
sprite-extraction pipeline (`docs/SPRITE_EXTRACTION.md`) produces
gameplay-ready costumes deterministically. The reference's per-type sprite-code assignments exist as
scattered per-handler constants, not a registry; sprite *choice* in the build is a visual interpretation,
while sprite *behavior* — which frames animate when, sizes doubling (for example the Sol Tower's mid-rise
growth and the explosion sizes), and anchor alignment to hit positions — follows each mechanic's recorded
rules. Terrain imagery is anchored to the schedule coordinate system; its source art, and how it couples to
the area clock, are owned by [Area progression and terrain](area-progression-and-terrain.md).

**Playfield framing.** The playfield is the 480×360 landscape stage — the port's recorded answer to the
arcade's portrait screen ([Core game systems](core-game-systems.md), units and the clock) — so framing
never re-tunes the movement, shot, or crosshair constants recorded there. The stage carries **no border**:
nothing is drawn over the playfield edges, so no frame can cover the craft or the HUD. The craft clamps its
own position (`src/xevious_main.68k` `update_solvalou_sprite_XY` 2113–2137) — in the port, at fixed
positional stop lines on each axis — and the crosshair is never clamped, riding at its fixed lead from the
craft (`src/xevious_main.68k` `update_crosshair` 2262–2271). A player shot is retired as it passes arcade
row 0, as the arcade deletes it once its position wraps past the top (`src/xevious_main.68k`
`main_fn_30_shot_fn` 2391–2393, `delete_shot` 2394–2396). World objects — enemies, enemy bullets, ground objects, and the Bonus
Flag — are drawn only while their row is on the field, rows 0–39; the arcade culls an object at row 40
(`src/xevious_main.68k` `check_scroll_offscreen` 4827–4839, which also culls an object past the side
edge) and shows rows 4–39 of the objects it keeps.
Hiding a whole sprite at the row-0 cut line is a port necessity: Scratch cannot clip a sprite at a screen
edge, and with no border nothing else masks an object above the field. Drawn layers run, front to back:
the craft and its weapons, the HUD, then the world objects. The craft body over the HUD over the world
objects follows the reference, which gives only the craft body's hardware sprites priority over the
objects and redraws its foreground text layer over them (its Amiga display setup, `src/amiga/amiga.68k`
lines 985–987 in platform_init and 2107–2109 in redraw_fg_tiles). Drawing the craft's weapons — shots,
crosshair, bomb, and explosion — over the HUD and the enemy bullets is the owner's choice: in the
reference they are objects drawn under the text. The stop-line values, the row-0 cut, and the layer
order are recorded with their reasons in the build's framing mechanics record.

**Animation timing.** Where this spec records frame counts — the ~56-frame player explosion, the bomb's
two-stage flight animation and four-color cycle, the Sol Tower's seven-step rise, bullet color pulsing
(`src/xevious_sub.68k` `sub_fn_5__handle_pulsing_colours` 208–232, an eight-entry two-palette cycle
driven by the frame counter) — the build times those animations in the game's frame clock to the
recorded counts. Animations not yet extracted keep the preserved baseline's proven presentation until a
fidelity pass records the arcade value; replacing a working animation without a recorded value is the
regression class the principles forbid.

**Audio binding.** Every audio cue is owned by a state or event, never free-running: weapons fire, hits
and explosions, score awards, the extra-life sound, the Bacura deflection sound, the coin sound, the
Bonus Flag sound, state transitions, the boss encounter, and the game-over and high-score flows each
trigger at their owning event and must fit inside their state's window without being cut off by a
transition (the death cue is the worked example: the post-death pause lets its measured 1.361 s finish
before the transition stops sounds — [record 003](../mechanics/003-game-director-and-state-reset.md)).
The current inventory is the preserved baseline's music and sounds, the credited arcade sound effects
recorded in `docs/ASSET_CREDITS.md`, and sounds taken from the pinned reference (recorded in the asset
provenance files); the reference's cue sites (sound calls throughout
`src/xevious_main.68k`) name *when* a cue exists, and matching each cue's sound content is arcade
observation work, recorded per mechanic as it lands.

**What presentation may never do.** No presentation element may invent gameplay meaning: the preserved
baseline's READY and GAME OVER speech bubbles were the standing examples of unsupported invention and were
removed in the recovery build (fidelity audit A1/A2, [record 003](../mechanics/003-game-director-and-state-reset.md)),
and the principles' three-marker rule applies to presentation exactly as to mechanics.

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| Every committed cue and costume traces to the credited sheets, the preserved baseline, or a recorded provenance entry | Asset-provenance validation over the built project | engine |
| Recorded animation frame counts appear in the build's data, matching this spec's owning documents | Data/structural fixture over generated animation constants | engine |
| Cues play at their owning events and complete within their state windows — no cutoffs | Play the built `.sb3` through fire, hit, death, award, and transition moments | operator |
| The game sounds and looks like Xevious to its owner — music, key effects, and title presentation are present and right | Playtest judgment across a full session | operator |
| No border covers the craft or the HUD; the craft stops at its fixed stop lines, shots retire at row 0, and world objects show only on rows 0–39 — with the locked movement, shot, and crosshair constants unchanged | Headless scenarios drive the craft into each stop line, a shot past row 0, and an object across the cut line; structural fixtures pin the frame removal, the in-view gate, and the layer order | engine |
| The borderless playfield reads well: the craft and HUD are never covered, and objects enter and leave the field cleanly | Play the built `.sb3`, driving the craft into every edge and corner during busy play | operator |
| No presentation element carries invented gameplay meaning without a recorded marker | Fidelity-audit review of presentation elements against this spec | engine |
