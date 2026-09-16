# Bacura (the indestructible drifting slab)

- Mechanic: The Bacura (AIR-11) — a single wide, bar-like object type (`0x01`) that drifts straight **down**
  the scroll axis and **cannot be destroyed**. It is not a formation flyer: it is **schedule-spawned** into
  its own reserved 16-object band (arcade objects `0x10`–`0x1F`), enters at the top, and slides down the
  field at a steady **1 px/frame** until it leaves the bottom. A player **shot** cannot hurt it — it is never
  offered to the flying-enemy hit test at all — and a **bomb** cannot reach it either. Its one offensive
  property is **contact**: if it overlaps the Solvalou it kills the craft, through a **wider** collision box
  than the flying enemies use. It awards no points under any circumstances. (The player shot *bounces* off a
  Bacura instead of dying on it; that reflection is the sibling leaf WPN-01, [record 038](038-bacura-bounce.md).)
  Bacura is scheduled from **area 3 onward** (areas 3, 4, 7, 11, 14), so it is reachable in normal play, and
  it shares the per-slot fields, the shadow-MSB collision compare and the craft-death path with the earlier
  families ([record 023](023-aiming-and-slot-positions.md) slot fields,
  [record 025](025-blaster-to-air-hit.md) the shot detector it deliberately skips,
  [record 026](026-enemy-bullets-and-collision-death.md) the craft-death path).
- Derived behavior: A Bacura object is stamped `_STATE = 2` (the arcade's normal *active* state — there is no
  invulnerable state), given the size-`1x2` bank-1 attribute, and set moving on the raw scroll-axis velocity
  `_dX = 16` with `_dY` untouched (0). It never re-vectors, never fires, and never changes state on its own;
  each frame `move_object_dX` advances it and, like every object, it is culled once it scrolls off the bottom.
  Its indestructibility is not a flag but a **routing** fact: the shot sweep and the bomb sweep simply never
  visit the Bacura band, and the Bacura handler never calls the flying-enemy hit test. The only test that does
  read a Bacura is `check_bacura_hit_solvalou`, run from the craft-collision pass: an `_STATE == 2` Bacura
  whose shadow-MSB position lands inside the box `(_Y: −28..+11, _X: −8..+7)` kills the craft — a **taller**
  box than the flying-enemy shot box, matching the slab's larger body. Its live population is driven by two
  scheduled coroutines: `set_bacura_count` loads a per-window quota `bacura_inc_cnt`, and once per arcade
  second `inc_num_bacura` admits one more slab (`num_bacura += 1`) until the quota is spent, while
  `init_bacura` re-asserts `_TYPE = 1` on the first `num_bacura` band objects each pass — so the band refills
  as slabs drift off. `reset_num_bacura` zeroes `num_bacura` to end a window. The 16-object band is the
  physical cap; the arcade sets no explicit `num_bacura ≤ 16` clamp because the real quotas (area 3 is
  4 + 2 + 2) stay well under it.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. Line citations are
  `src/xevious_main.68k` unless noted. The drift handler is `handle_01_Bacura` 4247–4264 (`_STATE = 2`,
  `_ATTR = 0x82` size 1x2, `_dX = 16`, `gen_random_Y_store_obj` for the entry column, then `move_object_dX`).
  The craft-contact box is `check_bacura_hit_solvalou` 2225–2237 (`_STATE == 2` gate, `_Y: sub #28 / add #40`,
  `_X: subq #8 / add #16`), invoked from the craft-collision pass at 2200. The live spawn coroutines are
  `main_fn_3__init_bacura` 5188–5199 (activate `num_bacura` objects from `obj_tbl + _OBJSIZE*0x10`, i.e. the
  band `0x10`–`0x1F`) and `main_fn_5__inc_num_bacura` 5201–5217 (`one_second_cntr = 60`, `subq #1` per frame,
  `num_bacura += 1` / `bacura_inc_cnt −= 1` on zero), listed in the main coroutine table at 303/305. The
  schedule dispatch is `sub_2_fn_6__set_bacura_inc_cnt` (`src/xevious_sub.68k` 691–694, reads one count byte
  into `bacura_inc_cnt`) and `sub_2_fn_7__reset_num_bacura` (`src/xevious_sub.68k` 474), reached by object
  types `0x22` / `0x23` through `obj_fn_tbl` (624/625). The Bacura schedule records live in
  `docs/spec/data/area-schedules.json` (area 3: `set_bacura_count` count 4, 2, 2 then `reset_bacura_count`).
  The behavior is a port mapping of that logic, not copied text; the Bacura paragraph of
  [aerial enemies](../spec/aerial-enemies.md) is the settled description this slice implements.
- Transfer class: Behavioral port (instruction-derived control flow and numeric constants; no source text,
  ROM, or media copied). The slot layout, shadow-MSB compare and craft-death path are the derived data of
  records [023](023-aiming-and-slot-positions.md), [025](025-blaster-to-air-hit.md) and
  [026](026-enemy-bullets-and-collision-death.md).
- Scratch interpretation: The Bacura occupies its **own reserved band**, `BACURA_SLOTS` = slots 17–32 (arcade
  `0x10`–`0x1F`), never the 6-slot flying pool. Axes follow the family convention: `slot x` is the
  scroll/forward row (arcade `_X`), `slot y` the lateral column (arcade `_Y`). Two facts make it distinct from
  every flying family, and both are structural:
  - **Dispatched by band, not by type.** The ordered walk `advance slots` (SYS-04) routes a Bacura slot by a
    **slot-index range test** (`17 ≤ cursor ≤ 32`) to the warp proc `update bacura`, *not* by a
    `walk type == 1` equality. This is mandatory: `SHOT_TYPE` is also `1`, so a type-1 branch would run the
    Bacura handler over live shot slots. The band only ever holds Bacura, so the range test is unambiguous.
  - **No shot hit-test = indestructible.** `update bacura` deliberately **omits** the `check air shot hit`
    call that every flying family makes. That omission *is* the shot-invulnerability: no shot ever hit-tests
    a Bacura, so there is no `SLOT_HIT`, no `explode toroid tick`, and no score. (It does run the WPN-01
    shot-*bounce* detector, [record 038](038-bacura-bounce.md), which marks the shot and never touches the
    slab.) The bomb sweep covers the ground band and the score sweep the flying band, so the Bacura band is
    excluded from both by construction.

  `install_init_bacura` stamps a slab: `slot type = BACURA_TYPE` (1), `slot state = SLOT_ACTIVE` (1 — the port
  image of the arcade's active `_STATE = 2`, **not** the port's `SLOT_HIT = 2`), enters at the **top row**
  (`slot x = 0`, since the arcade never writes `_X`), `slot dx = BACURA_DRIFT_DX` (16), `slot dy = 0`, and —
  the distinctive contract — writes **no** `slot pts` (a Bacura is never scored). `install_update_bacura`
  gates the craft-death `player hit` write behind an overlap reporter carrying the **wider** `HIT_WINDOW_BACURA`
  (28, 40, 8, 16) — recognisable by its unique dy-high bound of 11 — then drifts (`slot x += 4 × slot dx`) and
  culls off the bottom (`row ≥ CULL_ROW_MAX`). The live population is the per-tick pump `install_pump_bacura`,
  which flattens the two arcade coroutines into one atomic pass: the INC half counts `one second cntr` down
  `TICK_TIMER_STEP` per tick from `BACURA_INC_PERIOD_FRAMES` (60) and, on zero, admits one slab (clamped to the
  16-slot band as a defensive guard) spending one `bacura inc cnt`; the INIT half stamps a fresh slab into any
  **empty** band slot for the first `num bacura` slots, leaving live slabs untouched, so the band refills as
  slabs cull. The schedule wires `set_bacura_count` (op `0x22`, loads `bacura inc cnt` + `one second cntr`) and
  `reset_bacura_count` (op `0x23`, `num bacura = 0`) into `_consume_schedule` beside the ground branch. A T-key
  debug direct-stamp seeds one slab into `BACURA_SLOTS[0]` for isolated playtesting, with the debug cursor's
  field-empty gate extended to wait on the Bacura band clearing too.
- Scratch evidence: `install_init_bacura`, `install_update_bacura` and `install_pump_bacura` (the lifecycle +
  live pump), the **band-membership** Bacura branch in `install_advance_slots` (a slot-range test, not a type
  equality), the `set_bacura_count` / `reset_bacura_count` schedule branches in `_consume_schedule` with the
  `_schedule_arg` count decode, the `num bacura` / `bacura inc cnt` / `one second cntr` / `bacura seed slot`
  spawn variables re-topped per area in `_enter_area_top`, the Bacura entry in `DEBUG_SPAWN_FAMILIES` plus its
  dedicated direct-stamp branch, `bacura_blocks` for the single-costume render, and the `BACURA_*` tuning
  constants in `tools/game_director.py`; the structural contract `_air11_failures` and its per-clause negatives
  (`test_bacura_slice_authoring_present` / `test_bacura_slice_negative_fixtures`) in
  `tests/test_scratch_project.py`, whose clauses pin the warp lifecycle procs, the **band-keyed** dispatch (the
  gate carrying bounds 17 and 32, with a corrupter that strips the lower bound), that init stamps an
  **ACTIVE** slab entering at the top and drifting at `BACURA_DRIFT_DX` with **no** points, that the update
  **omits** the shot detector and runs **no** explosion (corrupters graft each back in), that craft-death
  routes through the wider window's distinctive dy-high bound, and that the renderer draws exactly one slab
  costume.
- Acceptance criteria: In areas 3/4/7/11/14 (and via the T-key debug spawn) Bacura slabs enter from the top of
  their own band and drift steadily downward; a player shot **cannot destroy** one (it bounces off, WPN-01);
  a bomb cannot reach one; no Bacura ever scores; and touching a Bacura kills the craft through the wider
  contact box. The operator playtest confirms the felt behavior — indestructible bars sliding down the field
  that you must dodge or shoot *around*, not through.
- Fidelity status: Verified line-by-line against the pinned reference this slice (`handle_01_Bacura`,
  `check_bacura_hit_solvalou`, `main_fn_3__init_bacura`, `main_fn_5__inc_num_bacura`, the
  `set_bacura_inc_cnt` / `reset_num_bacura` schedule handlers, and the area-3 schedule counts were read at the
  pin). The behavior matches the reference within the recorded deviations below.
- License status: The reference states no reusable license; only instruction-derived behavior and numeric
  constants are transferred (recorded in [the index](../spec/index.md) and the data files). No source text is
  reproduced. The single Bacura slab frame is the Aerial Enemies rip credited in
  `src/xevious/assets/provenance.json` (`https://www.spriters-resource.com/arcade/xevious/`, sheet author
  "CrazyCarl").
- Known deviations or uncertainty: (1) **Two arcade frames per tick (tick scaling).** The per-frame reference
  rates are doubled for the port's two-frame tick — the drift is `slot x += 4 × slot dx` (arcade `_dX = 16`
  doubled by `move_object_dX` = 32 sub-px/frame = 1 px/frame, applied per two-frame tick), the one-second
  admit counts down `TICK_TIMER_STEP = 2` per tick from 60, and the cull uses the shared `CULL_ROW_MAX` — the
  same tick scaling every family uses. (2) **State overloading expressed as an explicit port state.** The
  arcade's active `_STATE = 2` maps to the port's `SLOT_ACTIVE = 1`, **not** the port's `SLOT_HIT = 2`; the
  numeric coincidence is not carried across. (3) **Invulnerability via routing, not a flag.** The arcade omits
  the Bacura from the shot/bomb sweeps and the Bacura handler never calls the flying hit test; the port
  reproduces this exactly — the `update bacura` chain has no `check air shot hit` call and the Bacura band is
  in no score/bomb sweep — rather than inventing an "invulnerable" state bit. (4) **Coroutine spawn flattened
  to a per-tick pump.** The arcade runs `init_bacura` and `inc_num_bacura` as cooperatively-yielding
  coroutines; Scratch has no coroutine yield, so the port flattens them into one atomic `pump bacura` pass per
  tick (inc → init) driven from the walk after the schedule has loaded this tick's counts. (5) **Defensive
  band clamp.** The arcade sets no explicit `num_bacura ≤ 16`; the port clamps the admit to the 16-slot band
  as a guard that never bites under the committed schedules but keeps the init loop inside the reserved band.
  (6) **Entry column not re-scattered.** The arcade `gen_random_Y_store_obj` picks a random entry column; the
  port enters from the shared top-row spawn column like every family — a cosmetic deviation, the drift and
  lifecycle are unaffected. (7) **No enemy scroll term.** As with every flying family, the port has no
  background-scroll term; the Bacura moves purely by `slot dx`, which already carries the full downward drift.
  (8) **Super Xevious content excluded.** Super-Solvalou area 6 has one extra Bacura record in the arcade; the
  port's extract is normal-only by construction (mechanics 017) and area 6 is correctly empty — the exclusion
  is intended, not a gap ([aerial enemies](../spec/aerial-enemies.md) catalogs Super Xevious as excluded).
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
