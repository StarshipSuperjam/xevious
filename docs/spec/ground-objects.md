---
status: draft
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Ground objects

Covers mechanics catalog rows GND-01 through GND-07, in ID order. Values cite the pinned reference
(`reference_pin` in [the index](index.md)) as `file label lines`; citations are `src/xevious_main.68k`
unless noted. Point values are owned by [Scoring, lives, and game over](scoring-lives-and-game-over.md);
placements, fire masks, and stop-firing rows per area by the committed
[schedule data](data/area-schedules.json); bullet rules by
[Player craft and weapons](player-craft-and-weapons.md).

License status of extracted values: the reference states no reusable license (recorded in [the index](index.md) and every data file).

## Summary

Ground objects are the bombing game: turrets and domes glued to the terrain, tank patrols that react to
the player's aim, and a path-following slider. They are destroyed by bombs only — never by the blaster —
and their firing is governed by the shared per-area permissions. Two rules give the ground war its
texture: destroyed land objects leave permanent craters that scroll away with the map, and several
objects watch the player's own reticles and respond.

Excluded here (Super Xevious only, catalog EX-02/EX-03): the jet with its score-reset trap, the
helicopter, the tank, and the bridge are never scheduled or built.

## Behavior

**Shared ground rules.** Terrain-fixed objects move only with the map's scroll. Firing families take
their fire-permission mask at spawn and reload their shot timers from the shared random stream masked by
it, ticking every 8th arcade frame; each family also stops arming once it scrolls past the area's
stop-firing row (both values per area in the schedule data). Every ground shot is an aimed bullet per
the bullet rules. Bombed land objects play the shared explosion and leave a permanent crater that
remains in the object's slot, scrolling with the terrain until culled (`handle_bomb_explosion`
4904–4941); bombed water objects and composite scoring nodes vanish completely instead
(`explode_and_remove_object` 4963–4997).

**Barra and Garu Barra (GND-01).** Barra is the passive target: terrain-fixed, never fires, crater on
death (2644–2654). Garu Barra spawns as a pair: a larger double-size base born permanently
indestructible (decorative, never explodes) plus a destructible scoring node 8 pixels above it that
vanishes cleanly when bombed (2657–2682). Neither variant fires. Scores per the scoring table.

**Zolbak (GND-02).** Terrain-fixed and never fires; bombing it scores per the scoring table and — this
document's own rule — lowers the adaptive AI level by 2, floored at zero (`handle_1F_Zolbak`,
`reduce_enemy_ai_by_2` 2684–2704). The strategic dimension: destroying radar domes eases the pressure
[Difficulty and formations](difficulty-and-formations.md) applies.

**Logram (GND-03).** The opening dome: after a random masked wait it runs a 28-tick (~224 arcade-frame)
open-and-close animation through its seven-stage sprite cycle, firing exactly one aimed shot at the
fully-open midpoint (tick 12), then rolls a new wait (2708–2789). Bombed at any stage: scores, crater.

**Derota and Garu Derota (GND-04).** Derota is the turret: terrain-fixed, firing on the Derota mask with
randomized reloads (2792–2818). Garu Derota is its heavy pair — indestructible double-size base plus a
firing, destructible node using the same Derota mask (2821–2852). The "rotation" is in the shots, not
the sprite: each shot launches toward wherever the craft is at that instant (recorded finding — no
visual rotation logic exists in the reference).

**Boza Logram (GND-05).** A five-part composite spawned as one schedule record: four outer domes in a
diamond (row offsets 0/+12/+12/+24 px, lateral 0/+12/−12/0) around a center dome (2861–2919). Outer
domes behave as Lograms (same fire cycle, on the Boza mask) and crater when bombed; each outer death
also downgrades the center's value per the scoring table's recorded downgrade
(`update_centre_points_value` 2986–2989). The center never fires; bombing it kills every surviving
outer dome in cascade (2921–2942). Whether the cascaded outer deaths also score is recorded as
uncertain (the crediting path is outside the handler labels; resolution by fixture or arcade
observation at build time).

**Grobda (GND-06).** The tank family — twelve variants, none of which fires (recorded finding: the
catalog's older "fire" phrasing is unsupported). Their game is movement reacting to the player's aim.
Speeds are relative to the terrain: "stopped" means matching the scroll exactly; the raw speed values
8 (stop), 14 (forward), 2 (backward), and 22 (dart) convert to 0 / +0.375 / −0.375 / +0.875 pixels per
frame over ground (baseline math 4842–4854, 346). Two reticle triggers exist — the blaster crosshair
window and the bomb-target window, each a narrow alignment band (~[−2, +1] row units) arming a 48-frame
reaction (4581–4611). The twelve variants, exhaustively (handlers 4289–4532):

| Variant (type) | Terrain | Trigger | Reaction |
| --- | --- | --- | --- |
| Stationary (0x2C) | land | none | holds position |
| Forward (0x35) | land | none | drives forward continuously |
| Crosshairs-forward (0x36) | land | crosshair aligns | starts forward, permanently |
| Forward, crosshairs-stop (0x38) | land | crosshair aligns | freezes 48 frames, then resumes forward |
| Targeted, back, stop (0x39) | land | bomb reticle aligns | darts backward 48 frames, then stops for good |
| Forward, crosshairs-dart (0x3A) | land | crosshair aligns | surges to dart speed 48 frames, resumes forward |
| Forward, targeted-back (0x3B) | land | bomb reticle aligns | darts backward 48 frames, resumes forward |
| Targeted fast-forward (0x3C) | land | bomb reticle aligns | dashes at dart speed 48 frames, stops, re-arms — repeatable |
| Stationary, water (0x3D) | water | none | holds position |
| Forward, water (0x3E) | water | none | drives forward continuously |
| Crosshairs-forward, water (0x3F) | water | crosshair aligns | starts forward, permanently |
| Forward, targeted-back, water (0x40) | water | bomb reticle aligns | darts backward 48 frames, resumes forward |

Land variants crater when bombed; water variants vanish. Scores per the scoring table, by variant.

**Domogram (GND-07).** The path-following slider: its schedule record carries a per-instance path — a
list of (duration, vector-index) steps, decoded in the committed schedule data — where each index
selects a (dy, dx) delta from Domogram's own 32-entry vector table, committed in
[data/domogram.json](data/domogram.json) (`domogram_vector_tbl` 4695–4728). It holds each vector for
its step's duration, producing back-and-forth patrols relative to the terrain, and keeps its last
vector when the scripted path ends (4620–4689). It fires on the Domogram mask through a 24-frame shot
animation releasing the bullet at its midpoint. Bombed: scores, crater.

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| Ground objects appear at their scheduled slots and positions, terrain-fixed unless their variant moves | Data fixture: build placements equal the committed schedule data | engine |
| Bombs destroy ground objects; the blaster never does; land kills leave scrolling craters, water kills vanish | Play: bomb and shoot a Barra; bomb a water Grobda | operator |
| The build's fire masks and stop-firing rows equal the committed schedule data | Data comparison over the build's generated area tables | engine |
| Firing families visibly begin and stop firing at their scheduled points | Play an area with scheduled ground fire (area 2 onward) | operator |
| Logram fires exactly once per open cycle, at full-open | Play area 1's Lograms; observe the open-fire-close rhythm | operator |
| Boza Logram: outer-first triggers the recorded downgrade; center-first cascades the outers | Play (or fixture) both orders | operator |
| Grobda variants react to the reticles per the table above and never fire | Play a Grobda area: line up the reticles and watch the reactions | operator |
| The build's Domogram paths and vector table equal the committed data | Data comparison against the schedule data and [data/domogram.json](data/domogram.json) | engine |
| Zolbak's destruction visibly eases subsequent pressure | Play: clear Zolbaks in one run, spare them in another, compare waves | operator |
