---
status: draft
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Scoring, lives, and game over

Covers mechanics catalog rows ECO-01 through ECO-04. Values cite the pinned reference
(`reference_pin` in [the index](index.md)) as `file label lines`; all citations are
`src/xevious_main.68k` unless noted. Scores in the reference are BCD-encoded; every value below is
decoded to decimal. **This document is the normative home for all point values and life rules** — other
documents name a score's owner ("scores per the scoring table") and link here.

## Summary

Every point in the game flows through one scoring path with one master value table; lives are granted at
DIP-configurable thresholds; losing the last craft routes through the high-score check to game over and
back to the cabinet's attract flow. The economy is what makes risk meaningful, and its values are exact.

## Behavior

**The single scoring path (ECO-01).** Each scoreable object carries an index into the master value table
(`object_value_tbl` 6264–6287); a scoring hit — bomb via `handle_bombed_obj_and_award_points` 2597–2623,
blaster via `check_flying_enemies_shot` 2516–2577 — looks the value up and adds it through `add_to_score`
64–78, which also updates the running high score (80–107) and triggers the bonus-life check after every
award. The master table's values: 10, 20, 30, 50, 70, 100, 150, 200, 250, 300, 400, 500, 600, 700, 800,
900, 1000, 1500, 2000, 2500, 4000, 10000.

**Per-object values.** Air kills: Giddo Spario 10, Toroid 30 (both variants, `init_toroid` 3338), Torkan
50, Zoshi 100 (top/bottom) or 70 (random variant), Zakato 100/200/150/300 (slow / close / fast /
continuous), Jara 150 (both variants), Kapi 300, Brag Spario 500, Brag Zakato 600 (random) or 1500
(proximity), Terrazi 700, Garu Zakato 1000 (each per its handler init, 3080–4015). Ground kills: Barra
100, Zolbak 200 (and reduces the AI level by 2), Logram 300, Garu Barra 300 (its paired half is
indestructible), Domogram 800, Derota 1000, Garu Derota 2000 (companion object; the main object is
indestructible), Boza Logram outer domes 300 each, Boza Logram center 2000 if hit before any outer dome
falls, downgraded to 600 the moment one does (`update_centre_points_value` 2988), Grobda land variants
200–10000 by variant (4292–4450), Grobda water variants 200–2500 (4474–4514), Sol Tower 2000 at reveal
and 2000 again at destruction, hidden easter-egg object 10. Andor Genesis: core 4000, each of four gun
ports 1000, the nine armor plates indestructible and worth nothing — and the boss's shell slot carries an
in-source-documented arcade bug, awarding whatever leftover value the slot last held
(`handle_4B` region 5380–5391); the spec preserves this as arcade behavior, flagged. Bacura is
indestructible and never scores. The Bonus Flag bypasses the table entirely
([Secrets](secrets.md)): 10,000 points or an extra life by DIP switch. (Full citations per family in
[Aerial enemies](aerial-enemies.md), [Ground objects](ground-objects.md),
[Andor Genesis](andor-genesis.md), [Secrets](secrets.md); the values are stated only here.)

**Score cap (ECO-01).** The score is three BCD bytes; overflow pins it at 9,999,990
(`set_score_to_9999990` 1836–1840). Arcade quirk, preserved and flagged: at the cap, the next-bonus-life
threshold can never exceed the score again, so every further award grants an extra life
(`check_for_extra_solvalou` 114–118 region).

**Starting and bonus lives (ECO-03).** Starting craft come from a four-entry DIP-indexed table: 5, 2, 1,
or 3 (`starting_solvalou_tbl` 1174–1175; the raw-index-to-physical-switch mapping is recorded as
uncertain — the code exposes only the raw bits). Bonus lives use a first-threshold table and a repeat
increment table, both selected by the lives setting and a three-bit bonus DIP field
(`first_bonus_life_tbls` 1179–1199; `bonus_tbl_ptrs` 1854–1877): first bonus at 10,000–30,000 by setting,
then every 40,000–100,000 by setting; one setting is a sentinel that disables bonus lives, and one
setting stops after the second bonus life (`check_for_extra_solvalou` 109–183). Which pair of tables
applies to which lives setting carries a recorded uncertainty, independently confirmed by two decoders:
the reference's own two selection sites disagree — the game-start seeding applies an extra inversion the
repeat-award path lacks, so they choose opposite tables for the same DIP setting (1854–1877 vs the init
path near 419–425). This internal inconsistency may be the reference's acknowledged remaining bug; the
build follows the repeat-award site's rule (the one that runs during play) and records the deviation,
with arcade observation as the resolution path. The threshold check runs after every point award, on the score's top
four digits.

**HUD (ECO-02).** The screen shows the current player's score, the running high score, remaining craft,
and the flashing 1UP/2UP indicator for the active player (`display_player_scores`,
`display_high_score`, `display_solvalou_left`; `src/xevious_sub.68k` `sub_fn_6__display_1UP_2UP`
737–767). Displayed values always match internal state.

**Game over (ECO-04).** Losing the last craft first runs the high-score check
(`check_for_high_score` 1618–1672): the five-entry best-five table admits any score beating fifth place,
shifting lower entries down; a qualifying score enters the initials screen
([Cabinet flow](cabinet-flow.md)); a non-qualifying score goes straight to GAME OVER. The GAME OVER
message holds 128 frames (~2.1 s, `game_over` 549–591) before the cabinet returns to attract. In a
two-player game the alternation rules in [Cabinet flow](cabinet-flow.md) apply first. The default
best-five table ships with 40,000 / 35,000 / 30,000 / 25,000 / 20,000 (`ROM_high_score_tbl_normal`
1587–1602; the ten-character name fields decode to the original developers' credits).

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| Every value in this document matches the build's generated score data | Data-table comparison fixture between the build's Scratch lists and this document's committed values | engine |
| All scoring flows through one path; no object can award twice for one hit | Deterministic fixture: repeated-contact cases award once | engine |
| Bonus-life thresholds fire per the recorded tables, including the disable and stop-after-two settings | Fixture over recorded score sequences per DIP setting | engine |
| The score caps at 9,999,990 | Fixture: award past the cap; score pins | engine |
| Destroying a known enemy shows the right score on screen | Play the built `.sb3`: bomb a Barra (100) and a Derota (1000); HUD reflects both | operator |
| An extra craft is granted at the configured first threshold with its sound | Play to the first threshold and observe the award | operator |
| Losing the last craft shows GAME OVER (~2 s) and returns to the title/attract flow | Play a full game to its end | operator |
| A qualifying score enters the initials screen; a non-qualifying one skips it | Play: end games above and below fifth place | operator |
