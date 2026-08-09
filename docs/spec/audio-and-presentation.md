---
status: draft
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Audio and presentation

Covers mechanics catalog row CAB-05 and the presentation-fidelity boundary. Values cite the pinned
reference (`reference_pin` in [the index](index.md)) as `file label lines`.

License status of extracted values: the reference states no reusable license (recorded in [the index](index.md) and every data file).

## Summary

How the game looks and sounds: sprites drawn from the credited sheets, animation timed in arcade frames,
and audio cues bound to the game states that own them. Presentation is deliberately separated from
gameplay rules — a costume or sound never decides behavior — and its fidelity has its own honest
boundary: imagery and audio are interpretations under the asset policy, while timing and binding are
reference-derived where extracted.

## Behavior

**Sprites and imagery.** Game imagery derives from the five imported sprite sheets recorded in
`docs/ASSET_CREDITS.md` (credited, no license stated) and the preserved 2017 baseline's own art; the
sprite-extraction pipeline (`docs/SPRITE_EXTRACTION.md`) produces gameplay-ready costumes
deterministically. The reference's per-type sprite-code assignments exist as scattered per-handler
constants, not a registry; sprite *choice* in the build is a visual interpretation, while sprite
*behavior* — which frames animate when, sizes doubling (for example the Sol Tower's mid-rise growth and
the explosion sizes), and anchor alignment to hit positions — follows each mechanic's recorded rules.
Terrain imagery is an interpretation anchored to the schedule coordinate system
([Area progression and terrain](area-progression-and-terrain.md)), recorded as a port necessity.

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
transition (the death-cue cutoff is a tracked defect of the current build). The preserved baseline's
music and sounds are the current inventory; the reference's cue sites (sound calls throughout
`src/xevious_main.68k`) name *when* a cue exists, and matching each cue's sound content is arcade
observation work, recorded per mechanic as it lands.

**What presentation may never do.** No presentation element may invent gameplay meaning: the READY
speech bubble in the current build is the standing example of an unsupported invention (recorded for
correction), and the principles' three-marker rule applies to presentation exactly as to mechanics.

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| Every committed cue and costume traces to the credited sheets, the preserved baseline, or a recorded provenance entry | Asset-provenance validation over the built project | engine |
| Recorded animation frame counts appear in the build's data, matching this spec's owning documents | Data/structural fixture over generated animation constants | engine |
| Cues play at their owning events and complete within their state windows — no cutoffs | Play the built `.sb3` through fire, hit, death, award, and transition moments | operator |
| The game sounds and looks like Xevious to its owner — music, key effects, and title presentation are present and right | Playtest judgment across a full session | operator |
| No presentation element carries invented gameplay meaning without a recorded marker | Fidelity-audit review of presentation elements against this spec | engine |
