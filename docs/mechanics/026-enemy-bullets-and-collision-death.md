# Enemy bullets, the shooting Toroid, and player death from real contact

- Mechanic: The shooting Toroid (type 0x0B) firing one aimed enemy bullet, the bullet flying and
  expiring (AIR-12.standard), and the player craft dying from real aerial contact — a Toroid or an enemy
  bullet touching the craft (PLY-02.air-trigger). This retires the debug **D** (request respawn) and **G**
  (request terminal death) keyboard fixtures: death is now produced by live combat, and the death → respawn
  / game-over decision (PLY-02.decision, built in slice 4) is driven by that real contact.
- Derived behavior: When a type-0x0B Toroid commits its lateral swing it fires exactly one aimed bullet
  and never again — it allocates one idle bullet slot, places the bullet at its own position, and aims it
  at the craft's current cell on the generic aimed-bullet tier (2 px/frame). The bullet then flies straight
  on that fixed aimed velocity each tick (it does not re-track) and is culled the moment it leaves any
  screen edge. Each tick, every active flying enemy and every active enemy bullet is tested against the
  craft; an object overlaps the craft when, in the reference's half-pixel "shadow" units, craft Y − obj Y
  ∈ [−8, 7] and obj X − craft X ∈ [−4, 3]. On the first overlap the craft is marked hit. The slot walk's
  terminal death check — gated on `player hit` = 1 **and** `invuln` = 0 — spends one craft, clears the hit
  flag, and runs the single player-dead transition; the death-complete handler then decides respawn vs
  game-over from the craft counter, exactly as before.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. The shooting Toroid
  fires one aimed bullet at its swing trigger, once only (`src/xevious_main.68k` 3281–3286, 3323–3327);
  aimed bullets travel at 2 px/frame from the angle tables (6290–6394), decoded in record
  [023](023-aiming-and-slot-positions.md) and baked as the magnitude-32 tier. The craft-hit window is
  `check_bullet_or_flying_hit_solvalou` ($1670, 2207–2219) — the same (bias, width) carry idiom as the
  shot-vs-flying window, recorded in [player craft and weapons](../spec/player-craft-and-weapons.md)
  (PLY-02, bullet/flying window) which owns the bullet rules; [aerial enemies](../spec/aerial-enemies.md)
  (AIR-12) records the shared bullet behavior and the Toroid's single-shot rule; [core game
  systems](../spec/core-game-systems.md) (SYS-03) records the enemy-shot-vs-player and air-enemy-vs-player
  collision groups. No source text or media was copied.
- Transfer class: Behavioral port and numeric constant (instruction-derived aim vector, collision window,
  and the fire-once / hit / death control flow; no source text or media copied).
- Scratch interpretation: `update bullet` is a warp Stage proc, dispatched from the slot walk for each
  active bullet (type 2): it adds the tick-scaled velocity to the bullet's `slot x`/`slot y`, raises
  `player hit` when the bullet overlaps the craft (the shared `_craft_overlap_reporter`, HIT_WINDOW_
  BULLET_FLYING on the exact half-px delta), and calls `cull slot` off any edge. The shooting Toroid's fire
  path lives in `update toroid`: on the swing commit, a type-0x0B Toroid calls `alloc bullet slot` (the
  live consumer of the 19-slot bullet pool), and on success copies its own position into the allocated
  slot, resolves an aim to the craft's cell with `compute aim index`, and writes the bullet's velocity from
  `aim dx 32`/`aim dy 32`. The same `_craft_overlap_reporter` runs first in the active Toroid's own update,
  so a Toroid on the craft's cell also raises `player hit`. The death check is the walk loop's terminal
  statement: an `if (player hit = 1) and (invuln = 0)` that spends a craft, clears `player hit`, and calls
  the one transition proc; `invuln` is a dormant debug flag never set by game logic (the headless harness
  sets it to keep an agency-less craft alive for observation, and clears it to exercise real death). Two
  clone-only renderers show the pool: the pre-existing `toroid` target (6 clones) and a new `enemy_bullet`
  target (19 clones, slots 40–58), each snapshotting a fixed slot and, per tick, hiding an empty slot or
  showing/positioning/sizing an occupied one.
- Scratch evidence: `install_update_bullet`, `_fire_toroid_bullet`, the type-0x0B branch and the
  `_craft_overlap_reporter` call in `install_update_toroid`, the bullet dispatch in the slot walk, the
  terminal death check in the non-warp walk, the `enemy_bullet` target and its 19-clone renderer, and the
  `_ensure_gameplay_target` / costume mirror for it in `expected_project`, all in `tools/game_director.py`;
  the constants `UPDATE_BULLET_PROCCODE`, `ENEMY_BULLET_TARGET`, `PLAYER_HIT_ID`, `INVULN_ID`, and the
  reused `HIT_WINDOW_BULLET_FLYING`; the structural contracts `_air12_failures` (bullet flight and aimed
  fire) and `_ply02_failures` (walk-driven death), the live-allocator `_enemy_bullet_pool_failures`, and
  their per-clause negative fixtures in `tests/test_scratch_project.py`; the live scenarios
  `enemy-bullet-fires`, `death-respawn`, and `death-game-over` (each with a biting negative) and the
  harness invulnerability flag in `harness/lib/build.js` / `harness/lib/catalog.js`. The D and G key hats
  are removed.
- Acceptance criteria: A shooting Toroid drawing level with the craft allocates one aimed enemy bullet
  (harness `enemy-bullet-fires`, negative: `update toroid` neutralized → no allocation); a Toroid or bullet
  on the craft's cell runs death → respawn while craft remain and death → game-over on the last craft
  (harness `death-respawn` / `death-game-over`, negatives remove the death edges); structurally, the bullet
  moves on both axes and culls off any edge, the fire path aims the bullet on the 32 tier, the flying and
  bullet updates raise `player hit`, and the invuln-gated death check spends a craft and transitions
  (`_air12_failures`, `_ply02_failures`, each clause corrupted bites); the D/G key hats are gone; two clean
  builds stay byte-identical and survive the build→import round-trip. The rendered bullet/craft collision
  and the exact hit feel stay the operator playtest.
- Fidelity status: **Live and playable — the enemy can now shoot back and the player can die in combat.**
  A shooting Toroid fires one aimed bullet; touching a Toroid or a bullet kills the craft, spends a life,
  and respawns or ends the game. The debug D/G fixtures are retired.
- License status: The pinned reference states no reusable license; no reference source text or media was
  copied — the aim vector, collision window, and control flow are an instruction-derived port. The enemy
  bullet renders with an existing verified costume as a stand-in (credited to
  https://www.spriters-resource.com/arcade/xevious/ in `src/xevious/assets/provenance.json`); no new crops
  were added.
- Known deviations or uncertainty: (1) **Fires once, no fire-rate mask.** The reference reloads its fire
  timer on a masked ~8-frame cycle while level; this slice fires exactly one bullet per shooting Toroid and
  does not consume the fire-permission mask, so DIF-03.play is deliberately not claimed — the fire-rate
  mask lands with the mask-consuming families (slice 10). (2) **No bullet colour pulse.** The bullet's
  `slot code` colour cycle is deferred with the dedicated bullet crops to a later art pass; the bullet uses
  a single stand-in costume, and the mechanic (aim → fly → cull → kill) is exact. (3) **Test-only
  invulnerability flag.** `invuln` exists solely so the headless harness can keep an agency-less craft
  alive; it is never set by game logic, so live play is unaffected (an operator-approved fixture). (4)
  **One-tick craft-position lag.** The craft's live position is read once per walk into `player row`/`col`,
  so a collision test can see the craft up to one tick behind its drawn position, the same bounded lag the
  shot mirror carries. (5) **Single-cell collision box (post-playtest fix).** The craft-overlap window is
  an AND of two two-sided bounds per axis, and each axis delta is rebuilt as its own reporter subtree for
  the `<` and the `>` compare. Sharing one delta block across both compares let the `>` steal it from the
  `<` (a reporter attaches to only one parent), so the lower bound read an empty operand and always passed —
  the box degraded to the quadrant above-and-beside the craft, killing it whenever it crossed the row or
  column of a not-yet-fleeing Toroid (the operator playtest caught it). The fresh-per-compare build restores
  the single-cell box; `craft-collision-is-single-cell` asserts a Toroid one column or one row off does NOT
  touch the craft so the regression cannot return.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
