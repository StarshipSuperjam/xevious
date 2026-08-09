---
status: draft
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Ground objects

Covers mechanics catalog rows GND-01 through GND-07. Values cite the pinned reference
(`reference_pin` in [the index](index.md)) as `file label lines`; citations are `src/xevious_main.68k`
unless noted. Score values are owned by [Scoring, lives, and game over](scoring-lives-and-game-over.md);
placements, fire masks, and stop-firing rows per area are in the committed
[schedule data](data/area-schedules.json).

## Summary

Ground objects are the bombing game: turrets and domes glued to the terrain, tank patrols that react to
the player's aim, and a path-following slider. They are destroyed by bombs only — never by the blaster —
and their firing is governed by the shared per-area permissions. Two rules give the ground war its
texture: destroyed land objects leave permanent craters that scroll away with the map, and several
objects watch the player's own reticles and respond.

## Behavior

**Shared ground rules.** Terrain-fixed objects move only with the map's scroll. Firing families take
their fire-permission mask at spawn and reload their shot timers from the shared random stream masked by
it, ticking every 8th frame; each family also stops arming once it scrolls past the area's
stop-firing row (both values per area in the schedule data). Every ground bullet aims once, at the
craft's position at the instant of firing, then flies straight at 2 px/frame
([Player craft and weapons](player-craft-and-weapons.md)). Bombed land objects play the shared explosion
and leave a permanent crater that scrolls with the terrain (`handle_bomb_explosion` 4904–4941); bombed
water objects and composite scoring nodes vanish completely instead (`explode_and_remove_object`
4963–4997).

**Barra and Garu Barra (GND-01).** Barra is the passive target: terrain-fixed, never fires, crater on
death (2644–2654). Garu Barra spawns as a pair: a larger double-size base born permanently
indestructible (decorative, never explodes) plus a destructible scoring node 8 pixels above it that
vanishes cleanly when bombed (2657–2682). Neither variant fires.

**Derota and Garu Derota (GND-04).** Derota is the turret: terrain-fixed, firing on the Derota mask with
randomized reloads (2792–2818). Garu Derota is its heavy pair — indestructible double-size base plus a
firing, destructible node using the same Derota mask (2821–2852). The "rotation" is in the shots, not
the sprite: each shot launches toward wherever the craft is at that instant (recorded finding — no
visual rotation logic exists in the reference).

**Logram (GND-03).** The opening dome: after a random masked wait it runs a 28-tick (~224-frame)
open-and-close animation through its seven-stage sprite cycle, firing exactly one aimed shot at the
fully-open midpoint (tick 12), then rolls a new wait (2708–2789). Bombed at any stage: 300 points,
crater.

**Boza Logram (GND-05).** A five-part composite spawned as one schedule record: four outer domes in a
diamond (row offsets 0/+12/+12/+24 px, lateral 0/+12/−12/0) around a center dome (2861–2919). Outer
domes behave as Lograms (same fire cycle, on the Boza mask) and crater when bombed; each outer death
also downgrades the center's value from 2000 to 600 (`update_centre_points_value` 2986–2989). The center
never fires; bombing it kills every surviving outer dome in cascade (2921–2942). Whether the cascaded
outer deaths also score their own values is recorded as uncertain (the crediting path is outside the
handler labels; resolution by fixture or arcade observation at build time).

**Grobda (GND-06).** The tank family — twelve variants, none of which fires (recorded finding: the
catalog's older "fire" phrasing is unsupported). Their game is movement reacting to the player's aim.
Speeds are relative to the terrain: "stopped" means matching the scroll exactly; forward is
+0.375 px/frame over ground, backward −0.375, dart +0.875 (4289–4532, baseline math 4842–4854). Two
reticle triggers exist: the blaster crosshair window and the bomb-target window, each a narrow alignment
band that arms a 48-frame reaction (4581–4611). Variants combine one trigger with one reaction —
start-forward-forever, freeze-then-resume, dart-backward-then-stop, surge-fast-then-resume, and a
repeating fast-dash — plus always-forward and always-stopped patrols; the four water variants mirror
land behaviors but vanish without craters when bombed. Full variant-by-variant behavior and scores per
the reference handlers cited above; scores range 200–10,000 by variant.

**Domogram (GND-07).** The path-following slider: its schedule record carries a per-instance path — a
list of duration-and-vector-index steps — whose vectors come from one shared 34-entry table, producing
back-and-forth patrols relative to the terrain (4620–4743; the per-instance paths are decoded in the
committed schedule data). It fires on the Domogram mask through a 24-frame shot animation releasing the
bullet at its midpoint, and keeps its last vector when its scripted path ends. Bombed: 800 points,
crater.

**Zolbak (GND-02).** Specified with its AI-reduction effect in
[Difficulty and formations](difficulty-and-formations.md) family terms: terrain-fixed, never fires, and
its destruction both scores and lowers the adaptive difficulty by 2 (2684–2704).

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| Ground objects appear at their scheduled slots and positions, terrain-fixed unless their variant moves | Data fixture: build placements equal the committed schedule; play confirms terrain lock | engine |
| Bombs destroy ground objects; the blaster never does; land kills leave scrolling craters, water kills vanish | Play: bomb and shoot a Barra; bomb a water Grobda | operator |
| Firing families obey their area mask and stop-firing row (seeded runs repeat) | Seeded fixture over an area's fire events against the committed schedule values | engine |
| Logram fires exactly once per open cycle, at full-open | Play area 1's Lograms; observe the open-fire-close rhythm | operator |
| Boza Logram: outer-first downgrades the center to 600; center-first cascades the outers | Play (or fixture) both orders | operator |
| Grobda variants react to the crosshair and bomb reticles with their recorded 48-frame reactions and never fire | Play a Grobda area: line up the reticles and watch the reactions | operator |
| Domogram follows its scheduled path and fires mid-animation | Play its scheduled appearances; seeded fixture for the path trace | engine |
| Zolbak's death reduces difficulty (subsequent waves visibly ease) | Seeded fixture on the AI level; play for the felt effect | engine |
