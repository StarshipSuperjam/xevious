---
status: draft
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Area progression and terrain

Covers mechanics catalog rows AREA-01 through AREA-04. Values cite the pinned reference
(`reference_pin` in [the index](index.md)) as `file label lines`.

## Summary

The game advances through sixteen areas of continuously scrolling terrain. One monotonic scroll clock per
area drives everything: the terrain imagery, the scheduled appearance of every ground object, formation
change, and special event, and the transition to the next area. Completing area 16 returns the game to
area 7 — the arcade has no win screen; this loop is its ending.

## Behavior

**The scroll clock.** Each area runs one 16-bit scroll counter, initialized to 0x0D00 at gameplay start
(`xevious_main.68k` `main_gameplay_loop` region, lines 474 and 1305) and decreased by 16 per frame — the
scroll delta is −8 (`xevious_main.68k` line 346) and the per-frame map step applies it twice
(`xevious_sub.68k` `get_map_row` 247–290). The counter's high byte is the *scroll row*, descending 0x0D,
0x0C … 0x00, wrapping to 0xFF and continuing down. When it reaches 0x0E, the area is complete
(`xevious_sub.68k` `sub_fn_3__handle_next_area` 696–730): one full area is 0xFF00 counter steps ≈ 4080
frames ≈ 68 seconds at the arcade's 60 frames per second.

**Area advance and the 16→7 loop.** On completion the area number increments; if the finished area is
area 16, play continues at area 7, not area 1 and not a victory screen (`sub_fn_3__handle_next_area`,
lines 707–710). Each area sets its terrain start column from the per-area map-offset table (16 entries,
[data/difficulty.json](data/difficulty.json) table `area_offset_in_map_tbl`, cited there) and points the
scheduler at that area's schedule table.

**The schedule.** Each area has one schedule table — the complete sixteen are decoded in
[data/area-schedules.json](data/area-schedules.json) with per-record source lines
(`xevious_sub.68k` `area_1_obj_tbl_normal` … `area_16_obj_tbl_normal` 863–1129). Records are consumed
strictly in order: each waits until the scroll row equals its trigger row, then executes and advances to
the next (`xevious_sub.68k` `sub_fn_2__handle_objects` 574–602). A record either places a ground object
into a numbered slot (with its map-anchored vertical position), sets or resets the incoming flying
formation, raises the adaptive difficulty, sets a per-family fire-permission mask, controls Bacura or
Sheonite or Andor Genesis events, or re-tunes difficulty from the player's score — the record kinds and
their meanings are enumerated in the data file's `dispatch` note and detailed in
[Difficulty and formations](difficulty-and-formations.md) and [Core game systems](core-game-systems.md).
Every table ends with a single 0x0D sentinel row that can never trigger, because the area advances at
row 0x0E first; the extractor proves every table decodes exactly to its sentinel.

**Terrain.** Terrain imagery is the arcade map scrolled at the clock above; the Scratch build renders its
own terrain art (per the asset provenance policy) anchored to the same scroll positions. Terrain must be
continuous for the whole area — the craft never flies over undrawn space — and the map column for each
area comes from the offset table above. The detailed map tile data (`map_rom.68k`) is deliberately not
transcribed; the Scratch terrain is a visual interpretation anchored to the schedule's coordinate system,
recorded here as a port necessity.

**Reset scope.** Player death does not reset the scroll clock: the craft respawns with terrain position
preserved (the arcade resumes the area in place). Starting a new game resets to area 1 at scroll 0x0D00.

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| All 16 schedule tables decode exactly — label to sentinel, no leftover bytes, no Super-table bytes | `python3 tools/reference_extract.py --verify` against a fresh clone at the pin | engine |
| The committed schedule data matches a re-derivation from the pinned commit | Same `--verify` run byte-compares the committed JSON | engine |
| An accelerated full-game trace consumes every record of all 16 areas in order with no unknown record kind | Deterministic schedule-trace fixture over the committed data | engine |
| Terrain scrolls continuously through a full area with no black gap, at one steady rate | Play the built `.sb3`: fly area 1 end to end watching for gaps | operator |
| Completing area 16 continues at area 7 | Play (or accelerated trace) confirming the 16→7 transition, with no win screen | operator |
| Ground objects appear at the same map landmarks on every run | Play the same area twice; the same objects appear at the same terrain positions | operator |
| Death preserves area position; a new game starts at area 1 | Play: die mid-area and confirm resume-in-place; restart and confirm area 1 | operator |
