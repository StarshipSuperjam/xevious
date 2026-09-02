# Blaster-to-air hit, 30-point award, and the enemy explosion

- Mechanic: The player shot destroying a flying enemy (WPN-02.air-hit) — the first live collision in the
  game. A shot overlapping an active Toroid resolves the hit through the single-hit path (SYS-03), awards
  the enemy's point value (ECO-01, 30 for a Toroid), and the struck enemy explodes and is freed. This
  retires the debug **S** scoring fixture: the real producer of `award value` is now a kill.
- Derived behavior: Each frame, every active flying enemy is tested against the three player-shot slots.
  A shot overlaps when, in the reference's half-pixel "shadow" units, shot Y − enemy Y ∈ [−32, 31] and
  enemy X − shot X ∈ [−16, 15] — the reference window (bias 16/8) DOUBLED, a playtest-driven deviation
  recorded below (deviation 5). On the first overlapping shot the enemy is marked struck and its value is
  added to the score once; the shot is consumed (it cannot hit a second enemy that frame). A struck enemy
  plays a 20-arcade-frame explosion (five 4-frame phases, doubling in size at frame 8) while still
  drifting on its velocity, then its slot is freed. While exploding it neither hits nor is hit.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. The shot-vs-flying test
  is `check_shot_hit_flying_enemy` ($19A6, `src/xevious_main.68k` 2565–2577): `sub #16; add #32` on the Y
  shadow MSBs and `sub #8; add #16` on the X shadow MSBs, the same (bias,width) carry idiom as the player
  window `check_bullet_or_flying_hit_solvalou` ($1670, 2207–2219). The explosion is `flying_enemy_hit`
  ($311F, 4865–4902). The window and unit are recorded in [player craft and weapons](../spec/player-craft-and-weapons.md)
  (WPN-02), alongside the player and Bacura windows; the single-hit path and value table are recorded in
  [core game systems](../spec/core-game-systems.md) (SYS-03) and [scoring, lives, and game over](../spec/scoring-lives-and-game-over.md)
  (ECO-01). No source text or media was copied.
- Transfer class: Behavioral port and numeric constant (instruction-derived collision windows and the
  single-hit/score control flow; the explosion is a timed animation; no source text or media copied).
- Scratch interpretation: The detector `check air shot hit` is a warp Stage proc called from
  `update toroid` for each active flying slot. It floors each slot position to its half-px shadow MSB
  (`slot units / 16`) and applies HIT_WINDOW_SHOT_FLYING on the exact half-px delta. On a hit it sets
  `hit slot` to the enemy, `award value` to `item (slot pts) of value table` (type-agnostic — the enemy's
  1-based value-table position), calls `resolve hit` (the one path that marks the slot struck and runs the
  single `score`), resets the enemy's `slot timer` as the explosion clock, and sets the shot slot to a
  `SHOT_SPENT` state (distinct from the enemy's HIT state, so the one slot-state→HIT write stays inside
  `resolve hit`). The blaster clone mirrors its live pixel position into its shot slot's `slot x`/`slot y`
  (the render map inverted and floored) each travel iteration, and exits — freeing its slot and deleting —
  when it reaches the top OR its slot leaves the ACTIVE state; so the shot's clone, not the walk, frees
  the slot, and the 3-shot cap can never desync. A struck enemy runs `explode toroid tick` (advance the
  clock, keep drifting, free at 20 frames) instead of its normal update; the renderer shows the referenced
  explosion costumes for the current phase (doubling size at the big phase).
- Scratch evidence: `install_check_air_hit`, `install_explode_toroid_tick`, the state branch in
  `install_update_toroid`, the shot position mirror and spent-exit in `blaster_blocks`, and the explosion
  render branch in `toroid_blocks`, all in `tools/game_director.py`; the constants `HIT_WINDOW_SHOT_FLYING`,
  `SLOT_UNITS_PER_SHADOW`, `SHOT_SPENT`, `TOROID_HIT_DURATION_FRAMES`; the explosion costumes appended to
  the toroid target by reference from `solv_death` in `expected_project`; the structural contract
  `_wpn02_failures` and its per-clause negatives, the retired-fixture check
  `test_air_hit_replaces_score_fixture_as_award_producer`, and the extended SYS-03 tests in
  `tests/test_scratch_project.py`; the live scenario `blaster-kills-toroid-and-scores` (with a biting
  negative) and the kill-driven `score-digits-render` in `harness/lib/catalog.js`.
- Acceptance criteria: A shot overlapping a Toroid raises the score by exactly the Toroid's value once and
  consumes the shot (harness `blaster-kills-toroid-and-scores`, negative: the detector neutralized → no
  score); the HUD digits decode to the earned score (harness `score-digits-render`, kill-driven); the
  detector is warp, produces `award value` from the value table, and resolves through the one score path,
  the explosion frees the slot, and the shot mirrors both position axes (`_wpn02_failures`, each clause
  corrupted bites); the S key hat is gone; two clean builds stay byte-identical and survive the
  build→import round-trip. The explosion's on-screen look and the exact hit feel stay the operator playtest.
- Fidelity status: **Live and playable — the first working combat.** Shooting a Toroid destroys it, scores
  30, and plays the explosion; the S fixture is retired. The enemy's own shot (type 0x0B) and player death
  from a flying enemy or a bullet land in the next commit of this slice.
- License status: The pinned reference states no reusable license; no reference source text or media was
  copied — the windows and control flow are an instruction-derived port. The explosion reuses the verified
  `solv_death` frames (credited to https://www.spriters-resource.com/arcade/xevious/ in
  `src/xevious/assets/provenance.json`) as a stand-in; no new crops were added.
- Known deviations or uncertainty: (1) **No shadow-byte wrap.** The reference compares 8-bit shadow MSBs,
  so two objects ~128 half-px apart can wrap into a phantom hit; this port compares the exact half-px delta
  and never does, trading that rare arcade quirk for no long-range phantom hits. (2) **One-tick shot lag.**
  The shot's position is mirrored by its clone and read by the walk on the following tick, so the detector
  sees the shot up to one tick (≤10 arcade px at 6 px/frame) behind its drawn position. (3) **Explosion
  stand-in.** The burst reuses the `solv_death` frames by reference; the mechanic (explode → score → gone,
  20 frames, size-doubling phase) is exact, but the frame-8 one-cell recentre and dedicated Toroid-burst
  crops are deferred to a later art pass (operator pixel-verifies any new crop rects). (4) **Award is
  type-agnostic.** `resolve hit` scores from the struck slot's `slot pts`, so every future family scores
  through this one path with no per-type branch. (5) **Single-column hit box (post-playtest fix).** The
  window is built as an AND of two two-sided bounds per axis. Each axis delta must be rebuilt as its own
  reporter subtree for the `<` and the `>` compare: a reporter block attaches to only one parent, so sharing
  one delta block across both compares let the `>` steal it from the `<`, leaving the lower bound reading an
  empty operand (always in range) — the box degraded to a whole row, and a held shot destroyed any Toroid in
  its row regardless of column (the operator playtest caught it). The fresh-per-compare build restores the
  bounded box; `air-shot-hit-column-bounded` asserts a shot two columns off does NOT score so the
  regression cannot return. (6) **Window doubled to (32,64,16,32) — anti-tunneling + sprite match
  (post-playtest).** The reference window (16,32,8,16) is 2 cells tall, but the self-propelled port's
  blaster shot advances `changeyby 20` = 2.5 cells per frame (20 stage-px ÷ the 8-px scroll cell), so a
  fired shot stepped clean OVER a Toroid between per-frame collision samples; because every shot in a held
  stream starts at the craft's row they share one sampling phase, so a Toroid in a gap was immune to the
  whole stream — the operator saw "many rounds into a group and nothing happens." Doubling the window to a
  4-cell height makes it exceed the per-frame step (with margin for the enemy's own closing motion) so every
  crossing is sampled, and the ±1-cell width now matches the 36-px rendered Toroid so a visible overlap
  kills — the arcade mow-down feel. The headless harness cannot reproduce per-frame timing (it runs threads
  to settling), so the numeric invariant `B8-no-tunnel` (shot cell-step ≤ window height − 1 cell margin) is
  the automated guard; on-screen feel remains the operator playtest's. The craft hurtbox
  (`HIT_WINDOW_BULLET_FLYING`) is deliberately NOT widened — forgiving offence, precise defence. A more
  faithful alternative (swept collision keeping the tight window) was set aside as the larger change; the
  doubled window also serves the sprite-match, so it is the smaller fix that solves both.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
