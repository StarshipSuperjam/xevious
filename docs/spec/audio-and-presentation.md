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

**Playfield framing.** The game runs inside a cabinet frame, as it did in the arcade: the whole 224×288
arcade screen is drawn at the port's one spatial scale ([Core game systems](core-game-systems.md), units
and the clock) as a 280×360 play window centred on the 480×360 stage, and the 100 units either side carry
**cabinet bezel side panels** — third-party bezel art taken in with the owner's approval and recorded in
the asset provenance files and `docs/ASSET_CREDITS.md`. The panels are opaque and drawn in front of the
world objects, so an object overhanging the window's side edge is covered at the edge exactly as the
arcade monitor's edge covered it. Nothing is drawn over the play window itself. The HUD stays **inside**
the window, on the arcade's own text-layer cells — the score labels and scores on rows 0–1, the remaining
craft icons on row 35 — and the attract, title, high-score, and entry text sits on the text cells the
reference draws them on (the cells are cited in the build's screen-proportions mechanics record). The
craft clamps its own position (`src/xevious_main.68k` `update_solvalou_sprite_XY` 2113–2137) — through
the scale, its sprite stops flush against the window's side and bottom edges — and the crosshair is never
clamped, riding at its fixed lead from the craft (`src/xevious_main.68k` `update_crosshair` 2262–2271).
A player shot is retired as it passes arcade row 0, as the arcade deletes it once its position wraps past
the top (`src/xevious_main.68k` `main_fn_30_shot_fn` 2391–2393, `delete_shot` 2394–2396); rows 0–3 lie
above the visible window, so the shot keeps hit-testing there while its sprite is hidden. World objects —
enemies, enemy bullets, ground objects, and the Bonus Flag — are drawn only while their row is on the
visible screen, rows 4–39: the arcade culls an object at row 40 (`src/xevious_main.68k`
`check_scroll_offscreen` 4827–4839, which also culls an object past the side edge) and shows rows 4–39 of
the objects it keeps. Hiding a whole sprite at the row-4 cut line is a port necessity: Scratch cannot clip a
sprite at a screen edge. Drawn layers run, front to back: the craft and its weapons, the HUD, the bezel
panels, then the world objects. The craft body over the HUD over the world objects follows the reference,
which gives only the craft body's hardware sprites priority over the objects and redraws its foreground
text layer over them (its Amiga display setup, `src/amiga/amiga.68k` lines 985–987 in platform_init and
2107–2109 in redraw_fg_tiles). Drawing the craft's weapons — shots, crosshair, bomb, and explosion — over
the HUD and the enemy bullets is the owner's choice: in the reference they are objects drawn under the
text. The window geometry, the clamp values, the row-4 cut, the text-cell placements, and the layer order
are recorded with their reasons in the build's framing and screen-proportions mechanics records.

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
| The arcade screen draws at one 1.25 scale in a centred 280×360 window between the bezel panels; the craft stops at the arcade clamp, shots retire at row 0, world objects show only on rows 4–39, and the HUD sits on the arcade text cells inside the window | Headless scenarios drive the craft into each clamp, a shot past row 0, and an object across the cut line; structural fixtures pin the render map, the bezel target and its layer, the in-view gate, the HUD text grid, and the layer order | engine |
| The framed playfield reads like the cabinet: nothing covers the craft or the HUD, the bezel frames the window cleanly, and objects enter and leave the screen cleanly | Play the built `.sb3`, driving the craft into every edge and corner during busy play | operator |
| No presentation element carries invented gameplay meaning without a recorded marker | Fidelity-audit review of presentation elements against this spec | engine |
