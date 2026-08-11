# Xevious mechanics catalog

This is the work queue for the normal arcade derivation. Its unit is a
player-visible mechanic, not an assembly routine or Scratch target.

Statuses are `present`, `partial`, `missing`, `uncertain`, and `excluded`.
Every locator below means
`jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`.
`main` means `src/xevious_main.68k`; `sub` means
`src/xevious_sub.68k`. Labels are cited instead of copying source text.

Acceptance criteria are normatively owned by the product spec under `docs/spec/` — the owning document
for each ID family: SYS → [core game systems](spec/core-game-systems.md); PLY, WPN →
[player craft and weapons](spec/player-craft-and-weapons.md); ECO →
[scoring, lives, and game over](spec/scoring-lives-and-game-over.md); AREA →
[area progression and terrain](spec/area-progression-and-terrain.md); DIF, FORM →
[difficulty and formations](spec/difficulty-and-formations.md); AIR →
[aerial enemies](spec/aerial-enemies.md); GND → [ground objects](spec/ground-objects.md); SEC →
[secrets](spec/secrets.md); BOSS → [Andor Genesis](spec/andor-genesis.md); CAB →
[cabinet flow](spec/cabinet-flow.md) and [audio and presentation](spec/audio-and-presentation.md).
The "Acceptance outcome" column below is a one-line summary for queue triage; where it and a spec
document differ, the spec document wins.

## Runtime, player, and weapons

| ID | Mechanic | Status | Depends on | Source labels | Acceptance outcome |
| --- | --- | --- | --- | --- | --- |
| SYS-01 | Explicit game-state director | present | — | main: `main_thread_main_loop`, `main_gameplay_loop`, `game_over` | One title/ready/playing/dead/respawn/game-over state owns input and transitions. |
| SYS-02 | Shared entity lifecycle | partial | SYS-01 | main: `add_obj_handler`, `handle_scroll_offscreen` | Every entity spawns, updates, takes one hit, explodes if applicable, and is removed without leaked state. 64-slot array + reset-clear built ([005](mechanics/005-entity-slot-lifecycle.md)); dispatch walk and occupants (beyond the shot cap) are dormant, foundation-only. |
| SYS-03 | Collision groups and single-hit resolution | partial | SYS-02 | main: `check_solvalou_hit`, `check_flying_enemies_shot`, `handle_bombed_obj_and_award_points` | Air, ground, player, bullet, and Bacura interactions stay distinct and cannot double-score. Single-hit path + five-group vocabulary built ([006](mechanics/006-collision-groups.md)); a provisional skeleton — no participant exists yet, so detection is delegated to the enemy slices. |
| SYS-04 | Pseudo-random behavior | partial | SYS-01 | main: `pseudo_random_gen`, `gen_rnd_dir` | Seeded fixtures repeat and dependent mechanics share one advancing random stream. Rule built and golden-verified ([004](mechanics/004-shared-random-stream.md)); no consumer yet — dormant, foundation-only. |
| PLY-01 | Solvalou movement and bounds | present | SYS-01 | main: `handle_solvalou_inputs`, `set_solvalou_dXdY` | Input moves within the frame with recorded speed and direction rules. Movement, un-normalized diagonals, the frame-touch bounds coupled to the crosshair, and the fixed spawn are the validated recovery-build behavior, recorded as port-tuned constants ([007](mechanics/007-spatial-factor-and-movement.md)). Confirming the movement, bounds, and crosshair coupling feel right is an operator playtest (checklist step 7). |
| PLY-02 | Player collision, explosion, life loss, and respawn | partial | SYS-03, ECO-03 | main: `check_solvalou_hit` through `finish_solvalou_exploding` | Bullets, flying enemies, and Bacura cause one death and safe respawn or game over. The lives-driven respawn-vs-game-over decision, the fixed-point respawn with no invulnerability, and terrain-restart-on-death are built and driven by the D/G death fixtures ([013](mechanics/013-death-decision-and-terrain-restart.md)); the collision hit windows are recorded ([008](mechanics/008-enemy-bullet-foundation.md)) and the near-end terrain checkpoint is now built in the area clock ([015](mechanics/015-area-clock.md)), but the real collision *trigger* needs an attacker (slice 8). |
| WPN-01 | Blaster firing and flight | partial | SYS-01 | main: `handle_shooting`, `move_shot` | The constrained shot stream travels, expires, and stops outside play. Cadence, the 3-shot cap, and shot speed (a port-tuned constant, [007](mechanics/007-spatial-factor-and-movement.md)) are built to spec; the Bacura shot-bounce is deferred to the Bacura family (slice 11), so this stays partial. |
| WPN-02 | Blaster-to-air hit | missing | WPN-01, SYS-03 | main: `check_flying_enemies_shot`, `check_shot_hit_flying_enemy`, `check_shot_hit_bacura` | A valid target reacts once; Bacura uses its distinct behavior. |
| WPN-03 | Crosshair and ground targeting | partial | PLY-01, SYS-03 | main: `handle_crosshairs`, `check_targeted_ground_object` | Crosshairs track the craft and select only an eligible ground object. The crosshair reads the craft's movement input and leads it, kept on screen by the craft↔crosshair coupling — it clamps at the top border and backs the craft down with it ([007](mechanics/007-spatial-factor-and-movement.md)); the bomb-targets-crosshair coupling and lock-over-target land at the ground-object slice (slice 9). |
| WPN-04 | Bomb launch, travel, and impact | partial | WPN-03 | main: `main_fn_31__handle_bombing`, `init_bombing`, `check_bomb_finished` | One bomb follows the recorded path and resolves at its target. |
| WPN-05 | Bomb reaction and score | missing | WPN-04, ECO-01 | main: `handle_bombed_obj_and_award_points`, `check_object_on_target` | Each ground object performs its reaction and awards once. |

## Economy, progression, and difficulty

| ID | Mechanic | Status | Depends on | Source labels | Acceptance outcome |
| --- | --- | --- | --- | --- | --- |
| ECO-01 | Object score, high score, and cap | present | SYS-03 | main: `add_to_score`, `update_high_score`, `set_score_to_9999990` | Recorded values update current/high score once and stop at the cap. The single `score` path — award, 9,999,990 cap, running high score, and the bonus hook — is built ([009](mechanics/009-scoring-path.md)) and driven by a debug S fixture until an enemy awards points in play (slice 8); the operator playtest gates it. |
| ECO-02 | Score and life HUD | present | ECO-01, ECO-03 | main: `display_player_scores`, `display_high_score`, `display_solvalou_left`; sub: `sub_fn_6__display_1UP_2UP` | Displayed values match internal score, player, and remaining craft. Score and high score render as digit-costume clones (white) and remaining craft as life icons — from the CC BY 3.0 HUD font — with a flashing white 1UP and the yellow HIGH SCORE label ([012](mechanics/012-hud.md)); the on-stage layout, the yellow/white split, and the reserve-vs-total life convention are playtest-tuned. The 2UP indicator is deferred with two-player (CAB-03). |
| ECO-03 | Starting and bonus lives | partial | ECO-01 | main: `starting_solvalou_tbl`, `first_bonus_life_tbls`, `check_for_extra_solvalou` | Configured starting, threshold, and Bonus Flag awards occur once. Starting craft, the per-setting bonus thresholds and increments, the disable sentinel, and the at-cap grant quirk are built from the committed tables ([011](mechanics/011-lives-and-bonus-economy.md)); the stop-after-two setting (its index unpinned in the data) and the Bonus Flag award (a ground secret) are deferred. |
| ECO-04 | Game over | partial | PLY-02, ECO-03 | main: `game_over`, `game_over_1_player`, `display_game_over` | Losing the last craft stops play and routes to high-score/attract flow. The GAME OVER presentation (128-frame hold) and the best-five qualification verdict are built ([014](mechanics/014-game-over.md)); the initials-entry screen a qualifying score would show is deferred to the cabinet-flow slice (19). |
| AREA-01 | Terrain scroll and map position | partial | SYS-01 | sub: `sub_fn_30__handle_scroll`, `get_map_row`, `area_offset_in_map_tbl` | Terrain exposes one monotonic position for scheduling. The per-area scroll clock (monotonic `area progress`, the derived scroll row, area completion at row 0x0E with the 16→7 loop mechanism, the per-area terrain-column seam) and the near-end death checkpoint are built ([015](mechanics/015-area-clock.md)); foundation-only — the visual terrain stays decoupled (coupled at slice 20) and the object scheduler is AREA-02. |
| AREA-02 | Area object scheduler | partial | AREA-01, SYS-02 | sub: `sub_fn_2__handle_objects`, `obj_fn_tbl`, `area_object_tbl_tbl_normal` | Normal events spawn in recorded order and position. The ordered schedule consume, a lossless slice-6-ready representation of one fixture area (area 1) with a materialized sentinel, and the `schedule fired` observable are built ([016](mechanics/016-area-scheduler.md)); foundation-only — the per-record dispatch is an empty seam (handlers arrive slices 8+) and all 16 tables + the full trace are slice 6. |
| AREA-03 | All 16 normal area tables | missing | AREA-02 | sub: `area_1_obj_tbl_normal` through `area_16_obj_tbl_normal` | An accelerated trace consumes every normal table without unknown or Super objects. |
| AREA-04 | Transitions and 16-to-7 loop | missing | AREA-03 | main: `main_gameplay_loop`; sub: `sub_fn_3__handle_next_area` | Areas advance 1–16 then continue at 7, with no win screen. |
| DIF-01 | Difficulty setting | missing | AREA-02 | sub: `difficulty_tbl`, `sub_2_fn_3__inc_enemy_AI_and_flying_enemies` | Each normal setting starts at its recorded pressure. |
| DIF-02 | Score-per-life adaptive AI | missing | DIF-01, ECO-01 | sub: `sub_2_fn_23__adjust_AI_level_based_on_score`, `avg_score_per_solvalou` | Fixtures show ordered AI changes from score per craft. |
| DIF-03 | Per-family fire frequency | missing | DIF-01, SYS-04 | sub: `sub_2_fn_8__fire_freq_mask_derota` through `sub_2_fn_22__fire_freq_mask_andor_genesis` | Family fire fixtures follow recorded masks. |
| FORM-01 | Normal flying formations | missing | AREA-02, DIF-01 | sub: `flying_enemy_type_offset_tbl_normal`, `sub_2_fn_2__set_flying_enemies` | Fixtures preserve normal type, count, offsets, and order. |

## Flying enemies and projectiles

| ID | Mechanic | Status | Depends on | Source labels | Acceptance outcome |
| --- | --- | --- | --- | --- | --- |
| AIR-01 | Toroid movement, animation, firing variant | missing | FORM-01, DIF-03 | main: `handle_0A_Toroid`, `handle_0B_Toroid_shoots`, `init_toroid` | A formation completes approach, swing, optional shot, exit, hit, and score. |
| AIR-02 | Torkan tracking and firing | missing | FORM-01, DIF-03 | main: `handle_0F_Torkan`, `torkan_shoot`, `torkan_update_dir` | Direction changes and fire occur under recorded conditions. |
| AIR-03 | Zoshi variants | missing | FORM-01, SYS-04 | main: `handle_0E_Zoshi_bottom`, `handle_0D_Zoshi_top`, `handle_0C_Zoshi_rnd` | Top, bottom, and random paths remain distinct. |
| AIR-04 | Jara pair and firing variant | missing | FORM-01, DIF-03 | main: `handle_56_Jara`, `handle_55_Jara_shoots`, `jara_check_proximity` | Pairs move, turn, separate, fire, and score correctly. |
| AIR-05 | Kapi | missing | FORM-01, DIF-03 | main: `handle_10_Kapi`, `kapi_10_fire` | Steering and fire timing match recorded conditions. |
| AIR-06 | Terrazi | missing | FORM-01, DIF-03 | main: `handle_11_Terrazi` | The full attack/exit path and collisions complete. |
| AIR-07 | Zakato normal variants | missing | FORM-01, SYS-04 | main: `handle_12_Zakato_slow` through `handle_15_Zakato` | Slow, proximity, fast, and standard variants stay distinct. |
| AIR-08 | Brag and Garu Zakato | missing | AIR-07, DIF-03 | main: `handle_16_Brag_Zakato_rnd`, `handle_17_Brag_Zakato_closeY`, `handle_18_Garu_Zakato` | Teleport, explosion, and fire behaviors complete. |
| AIR-09 | Sheonite pair | missing | FORM-01, SYS-02 | main: `handle_31_right_sheonite`, `handle_32_left_sheonite`; sub: `sub_2_fn_18__sheonite_start`, `sub_2_fn_19__sheonite_end` | The linked pair coordinates and exits without an orphan. |
| AIR-10 | Giddo and Brag Spario | missing | SYS-02, SYS-04 | main: `handle_08_Giddo_Spario`, `handle_09_Brag_Spario` | Each projectile uses its distinct acceleration and collision rules. |
| AIR-11 | Bacura | missing | SYS-02, PLY-02 | main: `handle_01_Bacura`, `check_bacura`, `check_shot_hit_bacura`; sub: `sub_2_fn_6__set_bacura_inc_cnt` | Bacura scrolls/rotates, resists blaster fire distinctly, and hits the craft. |
| AIR-12 | Standard and radiating bullets | partial | SYS-02, SYS-04 | main: `handle_06_Bullet`, `handle_07_Garu_Zakato_Bullet`, `init_new_bullet`, `init_radiating_bullet` | Bullets use recorded vectors, expire, and cause one hit. The 19-slot allocator and the collision hit-window data are built as dormant foundation ([008](mechanics/008-enemy-bullet-foundation.md)); the aimed vector, movement, colour pulse, and firing are the air slices (8/11). |

## Ground objects and secrets

| ID | Mechanic | Status | Depends on | Source labels | Acceptance outcome |
| --- | --- | --- | --- | --- | --- |
| GND-01 | Barra and Garu Barra | missing | AREA-02, WPN-05 | main: `handle_1E_Barra`, `handle_20_Garu_Barra` | Both variants target, react, and score distinctly. |
| GND-02 | Zolbak AI reduction | missing | WPN-05, DIF-01 | main: `handle_1F_Zolbak`, `reduce_enemy_ai_by_2` | Bombing applies score and difficulty effect once. |
| GND-03 | Logram | missing | AREA-02, DIF-03 | main: `handle_26_Logram`, `handle_logram_init`, `handle_logram_main` | It animates, fires, takes a bomb, scores, and leaves cleanly. |
| GND-04 | Derota and Garu Derota | missing | AREA-02, DIF-03 | main: `handle_1B_Derota`, `handle_21_Garu_Derota` | Both turret variants target, fire, and score correctly. |
| GND-05 | Boza Logram composite | missing | GND-03, SYS-02 | main: `handle_2D_Boza_Logram`, center/outer handlers | Center and outer parts coordinate destruction, values, fire, and cleanup. |
| GND-06 | Grobda variants | missing | WPN-03, WPN-05 | main: `handle_2C_Grobda_stationary` through `handle_40_Grobda_fwd_targeted_back_fwd_in_water`, `check_grobda_hit` | Land/water and targeting variants perform recorded movement and response. |
| GND-07 | Domogram patrol and fire | missing | AREA-02, DIF-03 | main: `handle_2E_Domogram`, `domogram_main`, `domogram_vector_tbl` | It follows its path, animates, fires, takes a bomb, and exits. |
| SEC-01 | Sol Tower reveal and rise | missing | WPN-05 | main: `handle_1D_Sol_Tower`, `handle_sol_tower_rising` | The valid hidden hit reveals, raises, and destroys the tower in stages. |
| SEC-02 | Bonus Flag reveal and collection | missing | WPN-05, ECO-03 | main: `handle_54_Bonus_Flag`, `reveal_bonus_flag`, `score_bonus_flag`, `check_flag_collected` | The flag reveals and awards the configured normal reward once. |
| SEC-03 | Hidden copyright event | uncertain | AREA-03, SYS-04 | main: `handle_53_Easter_Egg`, `check_copyright_strings` | Implement only after arcade QA confirms the normal trigger and presentation. |

## Andor Genesis and cabinet flow

| ID | Mechanic | Status | Depends on | Source labels | Acceptance outcome |
| --- | --- | --- | --- | --- | --- |
| BOSS-01 | Andor arrival and composite lifecycle | missing | AREA-02, SYS-02 | sub: `sub_2_fn_20__andor_genesis_start`, `andor_genesis_data`, `sub_2_fn_21__andor_genesis_end`; main: `handle_41_Andor_Genesis_obj_09` through `handle_52_Andor_Genesis_obj_10` | Parts enter, align, animate, and leave or clean up as one encounter. |
| BOSS-02 | Gun ports, Bragza, and fire | missing | BOSS-01, AIR-12 | main: `handle_Bragza`, `handle_4F_Andor_Genesis_obj_13` through `handle_52_Andor_Genesis_obj_10` | Ports and defenses fire correctly within the entity budget. |
| BOSS-03 | Core hit, destruction, departure | missing | BOSS-01, WPN-05 | main: `andor_genesis_core_hit`, `andor_genesis_destroyed`, `andor_genesis_leave` | Only the valid core path destroys/scores the boss; otherwise it departs. |
| CAB-01 | Attract sequence | missing | SYS-01, AREA-02 | main: `attract_mode_jump_tbl` and three attract handlers | A cold run cycles title, demonstration, and high-score stages without scoring/life loss. |
| CAB-02 | Credits and 1P/2P start | missing | SYS-01 | sub: `sub_fn_4__handle_credits_and_start`; main: `check_credits` | Credits cap and start only an affordable requested mode. |
| CAB-03 | Two-player alternation | missing | CAB-02, AREA-04 | main: `next_player`, `swap_curr_other_player`, `main_gameplay_loop` | Players preserve independent score, lives, area, and bonus state. |
| CAB-04 | High-score table and initials | missing | ECO-01, ECO-04 | main: `init_high_score_table`, `check_for_high_score`, `move_high_score_entry_down`, name-entry labels | A qualifying score enters the five-entry table with initials. |
| CAB-05 | Gameplay audio and feedback | partial | implemented slices | main sound call sites plus existing Scratch sounds | Weapons, hits, awards, warnings, transitions, death, boss, and cabinet states have synchronized feedback. |

## Deliberate exclusions

| ID | Mechanic | Status | Source labels | Reason |
| --- | --- | --- | --- | --- |
| EX-01 | Galaxian | excluded | main: `handle_58_Galaxian` | Marked Super-only. |
| EX-02 | Jet and score-reset trap | excluded | main: `handle_59_Jet_Taking_Off`, `bomb_explode_and_zero_player_score` | Marked Super-only. |
| EX-03 | Helicopter, tank, bridge | excluded | main: `handle_5B_Helicopter`, `handle_5C_Tank`, `handle_5D_Bridge` | Super additions. |
| EX-04 | Super formations and areas | excluded | sub: `flying_enemy_type_offset_tbl_super`, `area_object_tbl_tbl_super` | Normal tables are authoritative. |
| EX-05 | Pause and persistent high-score I/O | excluded | reference `readme.md`; main `OPT_ENABLE_HIGH_SCORE_IO` branches | Port convenience, not normal cabinet behavior. |
| EX-06 | Conventional win screen | excluded | main: `main_gameplay_loop`; sub: `sub_fn_3__handle_next_area` | Area 16 loops to area 7. |

## Completion rule

A row becomes `present` only when its PR records exact source labels and input
classes, names the Scratch targets/data/fixtures, passes the acceptance outcome
in Scratch 3, passes deterministic validation, and records any uncertainty or
deliberate deviation.
