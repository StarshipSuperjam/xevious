---
status: locked
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Difficulty and formations

Covers mechanics catalog rows DIF-01 through DIF-03 and FORM-01. Values cite the pinned reference
(`reference_pin` in [the index](index.md)) as `file label lines`.

License status of extracted values: the reference states no reusable license (recorded in [the index](index.md) and every data file).

## Summary

Enemy pressure in Xevious is not fixed: it is driven by one adaptive difficulty number — the AI level —
raised on a schedule, tuned by the cabinet's difficulty setting, and re-tuned by how well the player is
actually scoring. The AI level selects which flying-enemy formations attack and how many enemies each wave
contains. Per-family fire-permission masks, set by the area schedules, control which enemy families may
fire and how often. Together these are why the game feels harder the better you play.

Excluded here (Super Xevious only, catalog EX-04): the Super formation and schedule tables are never
read; the extractor proves no Super data reaches the committed files.

## Behavior

**The AI level and difficulty setting.** A single AI-level value accumulates during play. Schedule records
of the `raise_ai_level_and_set_formation` kind — one of the schedule's two most common record kinds —
add the cabinet difficulty increment to the AI level *and then re-select the incoming formation from the
new level* (the same lookup the set-formation record uses, with the raised level as the index): the four DIP-selectable settings add
2, 0, 6, or 16 respectively (`xevious_sub.68k` `difficulty_tbl` 338–342, decoded in
[data/difficulty.json](data/difficulty.json); consumed by `sub_2_fn_3__inc_enemy_AI_and_flying_enemies`
317–329). If the raise would take the level to 0x80 or above, 0x40 is subtracted first (same routine) — the
level saturates by folding back, not by clamping.

**Score-adaptive re-tune.** Schedule records of the `adjust_ai_level_from_score` kind — 21 across the
sixteen areas, zero to four per area (four areas have none; the per-area counts are the committed
schedule data's) — recompute pressure from performance: the player's score in thousands is divided by the number of
craft in reserve, capped at 16, and added to the AI level (`xevious_sub.68k`
`sub_2_fn_23__adjust_AI_level_based_on_score` 344–353 and `avg_score_per_solvalou` 360–372). A player
scoring heavily with many lives left meets sharply higher pressure; a struggling player is spared.

**Formation selection.** Schedule records of the *set-formation* kind carry a signed offset. The effective
index is that offset plus the current AI level, doubled, into the formation table
(`xevious_sub.68k` `sub_2_fn_2__set_flying_enemies` 300–311). The table — decoded completely, including
its 32 negative-index entries, in [data/formations.json](data/formations.json)
(`flying_enemy_type_offset_tbl_normal`; exact lines in the data file) — yields two values per entry: the number of flying
enemies in the incoming wave (observed range 1–6) and an offset into the flying-enemy type table that
determines *which* enemy types compose the wave (the type table itself is documented in
[Aerial enemies](aerial-enemies.md)). A *reset-formation* record zeroes both, ending the pressure between
waves (`sub_2_fn_5__reset_flying_enemies` 331–335).

**Fire-permission masks.** Area schedules set one mask byte per firing family — Derota, Logram, Zoshi,
Terrazi, Kapi, Boza Logram, Domogram, Andor Genesis — plus a ground-stop-firing row
(`xevious_sub.68k` `sub_2_fn_8` through `sub_2_fn_22`, 375–419; every mask value each area sets, with its
trigger row, is in [data/area-schedules.json](data/area-schedules.json)). A family's mask gates how often
its members may fire; the per-family firing behavior that consumes each mask is specified in that family's
document ([Aerial enemies](aerial-enemies.md), [Ground objects](ground-objects.md),
[Andor Genesis](andor-genesis.md)).

**What resets.** The AI level, formation state, and masks belong to the per-player game state: they persist
across death and respawn within a game, and reset for a new game. In two-player alternation each player
carries their own difficulty state ([Cabinet flow](cabinet-flow.md)).

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| The committed formation table (including negative indices) and difficulty tables match a re-derivation from the pinned commit | `python3 tools/reference_extract.py --verify --checkout <clone>` with a fresh clone at the pin (clone recipe in [the index](index.md)); the run passes or names the failing table | operator |
| The four difficulty-setting increments are 2, 0, 6, 16 and the build's data matches the committed file | Data-table comparison in the deterministic build fixtures | engine |
| A model fixture over the committed data reproduces formation lookups (AI level + offset, fold-back at 0x80); the build's in-game selection is confirmed in play (fixture-automated when a runtime harness exists) | Python fixture over the committed tables; operator play for the in-game half | engine |
| A model fixture over representative score/lives pairs computes the re-tune rule (score per reserve craft, capped at 16); the build's in-game re-tune is confirmed in play (fixture-automated when a runtime harness exists) | Python fixture implementing the documented rule; operator play for the in-game half | engine |
| Wave sizes stay within the table's recorded range and grow as the game progresses at a fixed setting | Play several areas at one setting; waves grow denser and never exceed six enemies | operator |
| Playing better produces visibly harder waves | Play one area twice — once scoring heavily, once minimally — and compare wave pressure (paired with the seeded re-tune fixture above, since the two runs also differ in what was destroyed) | operator |
| Enemy families fire only when their area's schedule has permitted them | Play area 1's Lograms (its scheduled firing family): they begin firing at their scheduled point, not from the start | operator |
