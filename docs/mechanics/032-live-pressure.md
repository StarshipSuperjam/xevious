# Live difficulty pressure — the `.play` halves of DIF-01, DIF-02, DIF-03, FORM-01

- Mechanic: the in-game "pressure" loop — DIF-01, DIF-02, DIF-03, FORM-01 — that the difficulty
  foundation records ([019](019-difficulty-ai-level.md), [020](020-normal-flying-formations.md),
  [021](021-score-adaptive-ai-level.md), [022](022-fire-permission-masks.md)) built and left
  half-proven. Those records proved the *models* live (leaf #56): the AI level accumulates and folds,
  the formation table is selected, the score re-tune runs, and the per-family masks are set. This record
  closes the *playable* halves (leaf #60, `difficulty.live-pressure`) — that the selected pressure is
  actually felt in play: denser waves as the AI level climbs, harder waves when the player scores well,
  and per-family fire whose cadence the mask controls. It is a **proof + fidelity** record: the pressure
  chain was already wired by the enemy slices (Toroid [024](024-toroid-vertical-slice.md), Terrazi
  [027](027-terrazi-and-fire-permission.md), and the slice-10 families), so this adds live proofs, not
  new game logic — and corrects spec-prose fidelity errors the proofs surfaced.

- Derived behavior: the pressure the difficulty models select is now observable in play across three paths.
  - **DIF-01 / FORM-01 — density tracks the table, live.** Each `raise_ai_level_and_set_formation`
    record adds the cabinet increment to the AI level, folds it back once at ≥ 0x80, and re-selects the
    formation with the folded level as the table index; the flying-slot refill then fills the first
    `formation count` empty slots. The harness scenario `live-pressure-density` proves the live
    `formation count` equals the committed count-table entry at the *live* formation index on every
    pump, that the wave size **varies** across pumps, and that it stays within the recorded 1..6 band.
    The structural guard `test_live_pressure_contract` (`_live_pressure_failures`) pins the wiring: the
    refill loop repeats `formation count` times and fills only empty slots.
  - **DIF-02 — scoring well raises pressure past the raise ceiling.** The `adjust_ai_level_from_score`
    record adds `floor(floor(score / 1000) / craft)` (capped 16, guarded on craft > 0) to the AI level
    and — unlike a raise — does **not** fold it back. Because raises fold at 0x80, the AI level from
    raises alone can never be observed ≥ 128; the score adjust is the only path across that ceiling. The
    `live-pressure-adaptive` scenario injects a heavy score with craft in reserve, pumps area 1, and
    asserts the live AI level crosses 128 — the adjust's unique signature.
  - **DIF-03 — the mask sets fire *frequency*, not permission.** Every firing family, at spawn, captures
    its per-family fire-permission byte into the slot's fire mask, then seeds its fire countdown to
    `(random & mask) + 1`; the shared gate reloads the countdown the same way each time it fires. The
    mask **caps the reload interval** — mask 0 reloads to 1 (fastest), a larger mask draws a rarer
    reload. The `fire-reload-reads-mask` structural guard pins that the reload reads the captured mask,
    and the `terrazi-fires-under-mask` scenario proves a masked family fires through the shared gate.

- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. `src/xevious_sub.68k`: `sub_2_fn_3__inc_enemy_AI_and_flying_enemies` (317–329) raises the AI level, folds at 0x80, and re-selects the formation (DIF-01); `sub_2_fn_23__adjust_AI_level_based_on_score` through `avg_score_per_solvalou` (344–372) the un-folded score re-tune (DIF-02); `sub_2_fn_2__set_flying_enemies` (300–311) and `sub_2_fn_5__reset_flying_enemies` (331–335) the set/reset formation paths (FORM-01); `sub_2_fn_8__fire_freq_mask_derota` through `sub_2_fn_22__fire_freq_mask_andor_genesis` (375–419) the per-family fire-*frequency* mask setters and `sub_2_fn_10__gnd_stop_firing_row` (416–417) the separate ground permission-row setter (DIF-03); `flying_enemy_type_offset_tbl_normal` (432–437) the formation count column (a sawtooth). `src/xevious_main.68k`: `handle_logram_init` (2722–2732) scales the fire timer by the mask (frequency, not permission) and `handle_logram_main` (2733–2740) gates ground firing on `gnd_stop_firing_row`. The committed data is the hash-pinned [formations.json](../spec/data/formations.json), [difficulty.json](../spec/data/difficulty.json), and [area-schedules.json](../spec/data/area-schedules.json); the settled behaviour is [difficulty and formations](../spec/difficulty-and-formations.md).

- Transfer class: General behavior and numeric constants (instruction-derived control flow — the AI-level fold, the score-re-tune cap, the mask→reload scaling — over the committed, hash-pinned formation/difficulty/mask tables; no source text or media copied). This is a proof-and-verification record over behaviour built in the earlier difficulty and enemy slices.

- Scratch interpretation: no new generator logic is introduced this leaf. The pressure chain already lives in `tools/game_director.py` from the difficulty and enemy slices: `raise_ai_level_and_set_formation` folds the AI level and calls `_select_formation(ai level)`, which sets `formation count` / `formation type offset` from the two baked tables; `install_spawn_flying` refills that many *empty* flying slots by type; `adjust_ai_level_from_score` adds the un-folded score re-tune; and each firing family captures its `fire mask <family>` at spawn and reloads `slot fire timer` to `(random & mask) + 1` through the shared fire-permission gate. This record adds live proofs and structural guards over that existing wiring, and reshapes the spec/records prose to the verified fidelity.

- Scratch evidence: the live proofs and guards that pin the chain end-to-end.
  - Harness (`harness/lib/catalog.js`): `live-pressure-density` (DIF-01/FORM-01), `live-pressure-adaptive`
    (DIF-02), and `terrazi-fires-under-mask` (DIF-03) — each with a biting negative
    (`pinVariableSet` on `formation count`, severing the score-adjust equality, and breaking the shared
    fire gate, respectively).
  - Structural (`tests/test_scratch_project.py`): `test_live_pressure_contract` /
    `test_live_pressure_negative_fixtures` — `spawn-loop-times-formation-count`, `spawn-gates-empty-slot`,
    `spawn-advances-cursor`, and `fire-reload-reads-mask`, each paired with a negative that severs it.
  - Model (`tests/test_spec_docs.py::DifficultyAndFormations`): the committed-table and rule fixtures
    (leaf #56) the live proofs build on, and the fresh `.play` `roadmap-evidence` markers for all four
    obligations.
  - Operator playtest: the visible density climb, the score-driven difficulty difference, and the
    per-family fire cadence — the parts the settling harness cannot rate-measure.

- Acceptance criteria: Engine — the three harness scenarios pass with their biting negatives; the four
  structural guards hold and their negatives bite; the leaf-#56 model fixtures stay green. Operator —
  flying several areas at one setting shows the wave size vary (never above six) as difficulty climbs;
  scoring heavily versus minimally in one area produces visibly harder waves; a Terrazi family fires on
  its scheduled per-family cadence (denser under a smaller mask). Fire *rate* is confirmed by the operator
  playtest: the settling harness proves the mechanism (mask → reload) and that a masked family fires, but
  has no per-tick fire counter to measure a rate.

- Fidelity status: DIF-01, DIF-02, DIF-03, and FORM-01 are **built** — the models are live (leaf #56)
  and the playable behavior is proven here (leaf #60), with the one ground-only permission demonstration
  (`gnd_stop_firing_row`) tracked to the ground slice. Fire *rate* is not measurable in the settling
  harness (no per-tick hook, no fire counter), so DIF-03's live cadence is confirmed by the operator
  playtest; the harness proves the mechanism (mask → reload) and that a masked family fires.

- License status: The pinned reference states no reusable license; only instruction-derived behaviour and
  the committed, hash-pinned tables (`formations.json`, `difficulty.json`, `area-schedules.json`) are
  used, cited to the settled spec and the data files, and no reference source text or media was reproduced.

- Known deviations or uncertainty: the proofs surfaced three spec-prose fidelity errors, all corrected in
  [the spec](../spec/difficulty-and-formations.md) with no build change (the source wins over prose).
  - **The `_FFREQ` masks are fire *frequency* (cadence) masks, not on/off permission.** The schedule
    setters are named `sub_2_fn_8__fire_freq_mask_derota` through
    `sub_2_fn_22__fire_freq_mask_andor_genesis`. Each family consumes its mask by scaling a random draw
    into its fire timer — in `handle_logram_init`: `pseudo_random_gen`, then `and.b (_FFREQ,a5),d0`
    (commented `scale with mask`), then a `+1`, then `move.b d0,(_TIMER,a5)` — and the same reload runs
    in every firing family. There is no "mask ⇒ don't fire" branch. The build's cadence-cap treatment is
    faithful; the spec's older "control which families may fire" / "fire only when permitted" prose was
    a description error.
  - **The schedule *permission* / "not from the start" behavior is a separate, ground-only gate:**
    `gnd_stop_firing_row`. `handle_logram_main` reads it and refuses to fire until the object has
    scrolled past the scheduled row — `move.b (gnd_stop_firing_row),d0`, `cmp.b (_X,a5),d0` (commented
    `too low to fire?`), `jcs handle_logram_exit`. Its four consumers are all ground families (Logram,
    Derota, Boza Logram, Domogram); **no flying family uses it** (a Terrazi gates on proximity instead).
    So the DIF-03 "families don't fire until their scheduled point" demonstration belongs with the
    **ground slice** (slice 9), where a ground family that reads `gnd_stop_firing_row` ships. It is
    tracked there ([ground objects](../spec/ground-objects.md)), not descoped.
  - **The formation count column is a sawtooth, not monotonic growth.** The count bytes of
    `flying_enemy_type_offset_tbl_normal` run `3, 3, 3, 4, 4, 4, 5, 5, 5, 2, 2, 2, 4, 4, 4, …` — rising
    then dropping, bounded 1–6. The AI level walks this table, so a higher AI level yields a *different*
    wave, not a strictly denser one. The committed `formations.json` matches the ROM byte-for-byte; the
    spec's "waves grow denser" acceptance prose was corrected to "varied, bounded 1–6" (the same class of
    error as the earlier Toroid-swing spec incident — trust the ROM bytes, not the summarizing prose).
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
