# Bacura bounce (the player shot reflects off the slab)

- Mechanic: A player **shot** that strikes a Bacura is **reflected, not consumed** (WPN-01). Because the
  Bacura is indestructible and worth nothing ([record 037](037-bacura-slab.md)), the collision has no effect
  on the slab at all — it keeps drifting. The only thing that changes is the **shot itself**: it stops, flips
  to travel **backward** (down the field, away from where it was going) at a fraction of its forward speed,
  plays a short reflection animation in place, and then disappears. Visually the shot appears to **bounce off**
  the bar. This is the player-facing complement of the Bacura's shot-invulnerability: the shot detector for a
  Bacura exists only to route the *shot* to its rebound, never to resolve a hit. It shares the shot clone, the
  3-slot shot pool and the shadow-MSB collision compare with the blaster ([record 025](025-blaster-to-air-hit.md)).
- Derived behavior: The shot-fire routine, after testing shots against ground objects and flying enemies,
  sweeps the 16-object Bacura band: for each active Bacura, `check_shot_hit_bacura` tests the shot's
  shadow-MSB position against the box `(_Y: −24..+7, _X: −8..+7)`, and on a hit calls `deactivate_shot`, which
  sets the shot's `_STATE = 3` and plays `BACURA_HIT_SND`. The Bacura object is **never written**. On the next
  shot pass the `_STATE == 3` shot runs `shot_destroyed` instead of `move_shot`: it sets the reflected
  velocity `_dX = 24` (a quarter of the normal shot speed, reversed in sign — the normal shot travels up at
  `_dX = −96`, so `move_shot`'s doubling gives −192/frame forward vs +48/frame on the rebound), and animates an
  8-frame burst — `_TIMER` counts 0→7 with sprite code `0x18 + ((TIMER>>1) & 3)` (four codes) and a yflip
  toggle, deleting the shot when `_TIMER` reaches 8. So a bounced shot travels backward a short way over eight
  frames, then vanishes; it does not resume forward flight and cannot hit anything on the way out.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. Line citations are
  `src/xevious_main.68k`. The Bacura sweep is at 2547–2555 (`obj_tbl + _OBJSIZE*0x10` = 1st Bacura, 16 to
  check, `check_shot_hit_bacura` → `jcs deactivate_shot`); the overlap box is `check_shot_hit_bacura`
  2583–2595 (`_STATE == 2` gate, `_Y: sub #24 / add #32`, `_X: subq #8 / add #16`); the shot-kill is
  `deactivate_shot` 2557–2561 (`_STATE = 3`, `BACURA_HIT_SND`); the reflection + animation is `shot_destroyed`
  2400–2417 (`_dX = 24` reflected, `_TIMER` 0xff→…, delete at `_TIMER == 8`, sprite code
  `0x18 + ((TIMER>>1) & 3)`), reached from the shot handler's `_STATE == 3` test at 2389–2390, with the
  forward step `move_shot` 2419–2424 (`_X += 2 × _dX`). The behavior is a port mapping of that logic, not
  copied text; the Bacura/weapon paragraphs of [aerial enemies](../spec/aerial-enemies.md) and
  [player craft and weapons](../spec/player-craft-and-weapons.md) are the settled descriptions this leaf
  implements.
- Transfer class: Behavioral port (instruction-derived control flow and numeric constants; no source text,
  ROM, or media copied). The shot clone, shot pool and shadow-MSB compare are the derived data of record
  [025](025-blaster-to-air-hit.md).
- Scratch interpretation: The port adds a **dedicated** shot×Bacura detector, `check shot bacura`, a warp proc
  called **per live slab** from `update bacura`. It is a deliberate **sibling** of `check air shot hit`, never
  a reuse: the air detector resolves a hit (score + explosion + spend), which is exactly wrong for a Bacura.
  `check shot bacura` sweeps the three shot slots (`SHOT_SLOTS` 37–39), and on the first overlapping shot that
  is `type == SHOT_TYPE` and `state == SLOT_ACTIVE` — against a live (`SLOT_ACTIVE`) Bacura — it stamps **only**
  that shot slot's state to the new sentinel `SHOT_BOUNCE` (6). It writes no `hit slot`, no `award value`, makes
  no `resolve hit` call, and never touches the Bacura's own slot. `SHOT_BOUNCE` is distinct from `SHOT_SPENT`
  (3) precisely so the shot clone can tell a bounce (reverse + animate) from an ordinary air-kill spend (delete
  at once). The port **inverts the sweep direction** relative to the arcade (it tests shots per Bacura, where
  the arcade tests Bacura per shot) — functionally identical, and it matches the per-slot warp-proc shape of the
  other detectors. The blaster clone's travel loop already exits the instant its slot leaves `SLOT_ACTIVE`; the
  port adds a branch after that exit: when the slot's state is `SHOT_BOUNCE`, it **broadcasts** the Bacura hit
  cue `sfx bacura` — a blaster clone cannot play a Stage-owned sound, so a Stage receiver plays the real
  `bacura` sound (the audio pass, [record 040](040-arcade-sound-cues.md)) — and runs a
  `control_repeat` of `BACURA_BOUNCE_FRAMES` (8) that reverses the shot `BACURA_BOUNCE_DY` (−5 stage-px/frame)
  and steps the costume each frame, then falls through to the shared free+delete. Top-expiry (state still
  `SLOT_ACTIVE`) and air-kill spend (`SHOT_SPENT`) skip the branch and delete at once, exactly as before.
- Scratch evidence: `install_check_shot_bacura` (the warp detector), its call in `install_update_bacura`'s
  chain, its registration in `stage_blocks` before `install_update_bacura`, the `SHOT_BOUNCE` state constant,
  the `HIT_WINDOW_SHOT_BACURA` window and the `BACURA_BOUNCE_DY` / `BACURA_BOUNCE_FRAMES` constants in
  `tools/game_director.py`, and the bounce branch added to `blaster_blocks`; the structural contract
  `_wpn01_failures` and its per-clause negatives (`test_bacura_bounce_authoring_present` /
  `test_bacura_bounce_negative_fixtures`) in `tests/test_scratch_project.py`, whose clauses pin that the
  detector is warp and **called from** `update bacura`, that it **marks** an overlapping shot `SHOT_BOUNCE`
  **through the window** (its distinctive low bound), that it **never scores or touches the slab** (no
  `resolve hit`, no `hit slot` / `award value` write — corrupter grafts a `resolve hit` back in), and that the
  blaster clone **reverses** at `BACURA_BOUNCE_DY` for `BACURA_BOUNCE_FRAMES` (corrupter flips the step
  forward).
- Acceptance criteria: Firing into a Bacura leaves the slab **alive and drifting** and sends the **shot**
  visibly **backward** for a short animated burst before it disappears — no explosion, no score, and the shot
  does not pass through to hit anything behind the slab. Confirmed by the harness shot-bounce scenario (a shot
  overlapping a Bacura marks the shot for the bounce while the Bacura slot is untouched) and the operator
  playtest (shots visibly rebounding off the bars).
- Fidelity status: Verified line-by-line against the pinned reference this slice (`check_shot_hit_bacura`, the
  Bacura sweep at 2547–2555, `deactivate_shot`, and `shot_destroyed` / `move_shot` were read at the pin). The
  behavior matches the reference within the recorded deviations below.
- License status: The reference states no reusable license; only instruction-derived behavior and numeric
  constants are transferred (recorded in [the index](../spec/index.md) and the data files). No source text is
  reproduced. No new art is added: the bounce reuses the existing shot costume's `nextcostume` cadence. The
  `BACURA_HIT_SND` cue now plays the real `bacura` sound committed by the audio pass
  ([record 040](040-arcade-sound-cues.md)); the audio itself carries the same no-reusable-license rights status
  as the other ripped Xevious assets and is recorded in `src/xevious/assets/provenance.json`.
- Known deviations or uncertainty: (1) **Reflected speed derived from the arcade ratio, not copied literally.**
  The arcade reflects at `_dX = 24` = ¼ of the normal `96`, reversed; the port applies the same ¼-and-reverse
  to its own forward step (`changeyby 20` → `changeyby −5`), rather than importing the raw arcade velocity —
  the spatial factor between arcade and port shot speeds is otherwise unratified. (2) **Detector window doubled
  (anti-tunneling + sprite-match).** `check_shot_hit_bacura`'s box is `(_Y: 24/32, _X: 8/16)` half-px shadow
  units; the port uses `HIT_WINDOW_SHOT_BACURA = (48, 64, 16, 32)`, doubled on both axes, for the **same
  ratified reasons** recorded for `HIT_WINDOW_SHOT_FLYING` (mechanics 008): the shot is the same fast mover
  (`changeyby 20` ≈ 2.5 cells/frame), so the un-doubled scroll-axis window would let a shot tunnel clean over
  the thin one-row slab between frames, and the doubled box matches the rendered slab body. This is a
  consistent application of an existing deviation, not a new one. (3) **Sweep direction inverted.** The arcade
  sweeps the 16 Bacura per shot; the port sweeps the 3 shots per Bacura (called from `update bacura`) — the
  overlap set is identical, and the per-slot warp-proc shape matches the other detectors. (4) **Two arcade
  frames per tick.** The 8-frame arcade animation is expressed as `BACURA_BOUNCE_FRAMES = 8` repeat iterations
  of the clone's own animation loop; as with every family the clock is the port's, driven by the clone rather
  than a global two-frame tick. (5) **Bounce animation is a stand-in.** The arcade reflection uses a dedicated
  4-code sprite sequence (`0x18 + ((TIMER>>1) & 3)`); no such rip exists, so the port reuses the shot costume's
  `nextcostume` cadence over the 8 frames — the same stand-in precedent as the Zakato/Spario bursts. (6)
  **`BACURA_HIT_SND` now uses the real cue.** The earlier "blaster" stand-in has been replaced: the audio pass
  ([record 040](040-arcade-sound-cues.md)) commits the real `bacura` sound and the bounce broadcasts `sfx
  bacura` to a Stage receiver that plays it (a blaster clone cannot play a Stage-owned sound directly).
  Acceptance is "the shot visibly bounces back," met by the reversal + visible travel.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
