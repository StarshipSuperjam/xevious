---
status: locked
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Player craft and weapons

Covers mechanics catalog rows PLY-01, PLY-02, and WPN-01 through WPN-05. Values cite the pinned reference
(`reference_pin` in [the index](index.md)) as `file label lines`. Position units: the reference stores
positions in 1/32-pixel fixed point and applies velocities doubled, so pixel speeds below are stated after
conversion; timings are arcade frames at 60 per second.

## Summary

The player flies the Solvalou: an eight-direction craft over scrolling terrain with two simultaneous
weapons — a forward blaster against airborne enemies, and a bomb dropped on a crosshair-marked ground
target. One life ends the moment anything touches the craft; the craft then explodes, and the next craft
takes the area per the recorded restart rule in
[Area progression and terrain](area-progression-and-terrain.md).

## Behavior

**Movement (PLY-01).** Input maps to a nine-entry direction table (eight directions plus neutral); each
active axis moves 1 pixel per frame, diagonals move 1 pixel on *both* axes — the arcade does not normalize
diagonal speed (`xevious_main.68k` `dir_delta_tbl` 2171–2180, applied in `update_solvalou_sprite_XY`
2113–2137). Movement clamps to X 144–304 and Y 16–224 in screen pixels (same routine). The craft spawns —
first spawn and every respawn — at the fixed point (296, 120) (`main_fn_1__handle_solvalou` 1999–2003).

**Blaster (WPN-01, WPN-02 interface).** At most 3 player shots exist, in three dedicated slots
(`main_fn_30__handle_shooting` 2297–2331). Holding fire shoots immediately on the first frame, then
reloads every 20 frames while held; releasing resets the reload so a fresh press fires at once
(`process_button_1` 2333–2349, reload constant at 2365 region). One fire event spawns exactly one shot —
the fire flag is consumed by the first idle slot (`main_fn_30_shot_fn` 2356–2370). Shots fly straight
forward at 6 pixels per frame with no lateral drift (`move_shot` 2419–2424) and expire off the top of the
screen (2392–2396). Special case: a shot that hits a Bacura is not destroyed with its target — it bounces,
reversing at 1.5 pixels per frame through an 8-frame bounce animation before disappearing
(`shot_destroyed` 2400–2418).

**Crosshair (WPN-03).** The crosshair sits rigidly 96 pixels ahead of the craft at the craft's own lateral
position, recomputed every frame (`update_crosshair` 2262–2271). Once every 8 frames it tests the 14
targetable ground slots and switches to its lock color when one sits under it; the bomb-in-flight state
drives its base color independently (`handle_crosshairs` 2239–2295).

**Bomb (WPN-04).** One bomb in flight at a time: arming requires the bomb-target slot idle
(`init_bombing` 2445–2448). The target point fixes at the crosshair position at the moment of release and
then scrolls with the world; the bomb accelerates toward it (velocity grows 2 raw units per frame rather
than flying at constant speed), stepping through a two-stage sprite animation with a four-color cycle
(2452–2496), and detonates when it reaches the scrolled target (`check_bomb_finished` 2502–2514). The
blast tests all 16 ground slots with the recorded hit window — vertical bias 10 width 20, horizontal
bias 5 width 10, in the reference's half-pixel "shadow" units, the same window the crosshair lock uses
(`check_object_on_target` 2629–2641; exact pixel conversion recorded as approximate). Bomb impact resolution and scoring are specified in
[Scoring, lives, and game over](scoring-lives-and-game-over.md) and per ground family in
[Ground objects](ground-objects.md). *Uncertain:* the code site that re-arms the bomb slot after
detonation was not located; the one-bomb lockout itself is confirmed, the re-arm path is not yet pinned.

**Player death and respawn (PLY-02).** Each frame the craft is tested against 19 enemy bullets and 6
flying enemies with one hit window (Y bias 8 width 16, X bias 4 width 8, shadow units), and against 16
Bacura slots with a distinctly larger window (Y bias 28 width 40, X bias 8 width 16) matching Bacura's
size (`check_solvalou_hit` 2182–2237). Any hit kills: the explosion animates 7 cycles of 8 frames
(~56 frames), then a 32-frame pause, then the next craft (if any remain — the life economy is owned by
[Scoring, lives, and game over](scoring-lives-and-game-over.md)) spawns at the fixed spawn point
(`explode_solvalou` through `finish_solvalou_exploding` 2034–2090). What happens to area position on
death — the restart-from-top rule and its near-end checkpoint exception — is owned entirely by
[Area progression and terrain](area-progression-and-terrain.md). **There is no
respawn invulnerability window** — the only invincibility in the reference is a development build flag,
off in a normal build (6123–6124, 2018–2024). A build that adds one is inventing a deviation and must
record it.

**Enemy bullets (AIR-12 — this is their normative home).** All enemy fire, air and ground alike, shares
one pool of 19 bullet slots (`init_new_bullet` 5012–5019; `find_idle_and_init_radiating_bullet`
5021–5029). An aimed bullet computes its vector once, at the frame of firing, toward the craft's
position at that instant — from a 32-direction angle table at magnitude 2 pixels per frame — and then
flies ballistically, never re-aiming (`handle_06_Bullet` 4278–4283; the four angle tables span
6290–6427 with speed tiers 1.5, 2, 3, and 4 px/frame). Radiating bullets use the 3 px/frame tier
(`init_radiating_bullet` path); the patterns that emit them (five-shot fans, the sixteen-bullet ring)
belong to their firing families in [Aerial enemies](aerial-enemies.md). Bullets expire at the recorded
screen-edge margins — beyond roughly 320 pixels on the scroll axis or 248 laterally, in reference
pixels (`check_scroll_offscreen` 4826–4839) and pulse through the shared four-color cycle
(`xevious_sub.68k` `sub_fn_5__handle_pulsing_colours` 208–232).

**Object slots (shared vocabulary).** The reference runs 64 32-byte object slots: 16 ground objects
(0x00–0x0F, of which 0x02–0x0F are crosshair-targetable), 16 Bacura (0x10–0x1F), bomb target 0x20, bomb
0x21, crosshair 0x22, Solvalou 0x23, 3 player shots (0x24–0x26), 19 enemy bullets (0x27–0x39), 6 flying
enemies (0x3A–0x3F) (`xevious.inc` 61–81, `xevious_ram.68k` 110–111, boundaries corroborated at the
collision and init sites cited above). The schedule data's `slot` parameters are decoded object-slot
indices (the source bytes are RAM offsets, twice the slot): placements target the ground range, and a
few scheduled air spawns target flying slots. The
Scratch build may represent these differently, but the *capacities* — 3 shots, 19 bullets, 6 flying
enemies, 16 ground, 16 Bacura, 1 bomb — are gameplay-visible limits and bind. Axis-to-screen orientation
is recorded as a strong inference (from the 224-pixel clamp literal), not a labeled fact.

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| Craft moves in 8 directions at equal per-axis speed with the recorded bounds, no diagonal normalization | Play the built `.sb3`: move along edges and diagonals; the craft pins at the same margins everywhere | operator |
| Holding fire produces a shot immediately, then a steady repeat while held, and moving while holding never interrupts it | Play: hold fire 5+ seconds while moving in circles; cadence stays steady | operator |
| At most 3 player shots are on screen; each flies straight and disappears at the top | Play: rapid fire at the screen edge and count | operator |
| A shot hitting Bacura visibly bounces back instead of vanishing | Play area 3 (the earliest scheduled Bacura quota) and watch the deflection | operator |
| Exactly one bomb can be in flight; the next arms only after detonation | Play: hammer the bomb key; bombs never overlap | operator |
| The crosshair leads the craft by a fixed distance and signals lock over a targetable ground object | Play: approach a Barra; the crosshair changes when it covers it | operator |
| Death from bullet, enemy, or Bacura contact triggers the explosion sequence and respawn at the fixed point, with area position per the area document's restart rule, and no invulnerability window | Play: die each way; the craft respawns immediately vulnerable and area position follows the recorded rule | operator |
| The build's movement/weapon constants match this document's recorded values | Data-table comparison fixtures over the build's generated Scratch lists | engine |
| Capacity limits (3 shots, 1 bomb, 19 bullets, 6 flying, 16 ground, 16 Bacura) are encoded in the build's data | Structural fixture reads the built project's capacity constants | engine |
