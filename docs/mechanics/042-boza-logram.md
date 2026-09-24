# Boza Logram — GND-05, the five-part composite dome

- Mechanic: the Boza Logram (`0x2D`), the first multi-slot *composite* ground family, built on the slice-9
  ground pipeline ([033](033-ground-bombing-pipeline.md)) and the slice-12 dome/turret wave
  ([041](041-ground-domes-and-turrets.md)). One schedule record stamps five slots — four outer domes in a
  diamond around one centre — that share the object type and one update handler but split into two roles by a
  cross-slot link. It reuses the shared ground scroller, the type-agnostic bomb-hit/award sweep, the Logram
  open/close/fire cycle, and the shared fire-permission gate the slice-9/10 families already established, so it
  adds object behaviour, not new pipeline. This record covers the single slice-13 leaf `ground.boza-logram`
  (#87, GND-05).

- Derived behavior: a five-slot composite whose halves score asymmetrically.
  - **The four outer domes are lone Lograms with one extra behaviour.** Each outer runs the same arm- and
    cadence-gated open/close/fire cycle as a lone Logram on the Boza fire mask, firing one aimed shot at the
    full-open midpoint, and craters PERSISTENTLY when bombed (scrolls 32/tick, crater clock climbs 2/tick,
    never freed on the clock — removed only when it culls off the field). Each outer scores **300**. Its ONE
    addition: on being hit it rewrites its linked centre's value down to **600**.
  - **The centre never fires and scores 2,000 — until an outer is hit.** The centre only scrolls while active;
    on a bomb hit it craters persistently like a Barra AND **cascades** — it sets every surviving outer dome
    directly to the hit state. It scores **2,000** at full value; once any outer has been hit, a later bomb on
    the centre scores the downgraded **600** instead.
  - **The scoring asymmetry falls out of the shared award sweep — no per-family scoring code.** The
    type-agnostic bomb-hit sweep credits an object's value only when it is ACTIVE on the bomb target. A
    directly-bombed outer is ACTIVE, so it scores 300 and downgrades the centre. But the centre-first cascade
    writes each surviving outer's hit state *directly*, bypassing the award sweep — so a cascaded outer is
    already hit and can never be credited. The result: **only a directly-bombed outer scores (and downgrades
    the centre); bombing the centre first scores the centre alone and clears the four outers for free.** This
    needs no scoring change — each slot only carries the right value and state.

- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`, `src/xevious_main.68k`.
  The composite spawn — four outer domes in a diamond (row offsets 0/+12/+12/+24 px, lateral 0/+12/−12/0)
  around a centre, with the object table wiring four `handle_boza_logram_outer` slots and one
  `handle_boza_logram_centre` slot — is `handle_2D_Boza_Logram` (2861–2919). The outer dome's Logram fire
  cycle plus its hit-time centre downgrade is `handle_boza_logram_outer` (2944–2985); the downgrade itself,
  `update_centre_points_value` (2986–2989), writes `_PTS=36` (600 pts) through the outer's `_EXTRA` pointer to
  the centre object, then falls to `handle_bomb_explosion`. The centre handler `handle_boza_logram_centre`
  (2921–2930) routes a hit to `destroy_all_outer_lograms` (2931–2942), which walks the four adjacent outer
  objects and writes each `_STATE=3` (hit) directly, then falls to `handle_bomb_explosion`. Point crediting is
  the shared `handle_bombed_obj_and_award_points` (2597–2623), which awards an object's value only when it is
  active (`_STATE=2`) on the bomb target — the path the cascade bypasses. Placements and fire masks per area
  are the committed [schedule data](../spec/data/area-schedules.json); the settled behaviour is
  [ground objects](../spec/ground-objects.md); point values are
  [scoring, lives, and game over](../spec/scoring-lives-and-game-over.md).

- Transfer class: General behavior and numeric constants (instruction-derived control flow — the outer Logram
  fire cycle, the cross-slot centre-value downgrade to 600, the four-outer cascade, and the active-only award
  gate — over the committed, hash-pinned schedule and scoring tables; no source text or media copied).

- Scratch interpretation: the family is added to the `.sb3` generator `tools/game_director.py` along the same
  seams the slice-9/12 families use — a type code (`BOZA_LOGRAM_TYPE`) in `GROUND_HANDLED_TYPES`, a spawn
  branch that stamps all five slots (each outer's value, captured Boza fire mask, masked initial reload, and a
  link to the centre slot; the centre's full value and a zero link), a walk-dispatch entry routing the type to
  `install_update_boza`, and a renderer reading the parallel slot lists (the four outers reuse the Logram open
  crops; only the centre core adds a proof crop). Bomb-hit and scoring need no per-family change — the shared
  `install_check_ground_hit` scores any ACTIVE ground slot by its `slot pts`, so the asymmetry falls out for
  free once each slot carries the right value and state. `install_update_boza` clones the Logram open/close/
  fire machine for the outers and the Barra crater wrapper for both roles, and branches on `slot link`.
  - **Port necessity — one update proc branching on `slot link`, not two patched handlers.** The arcade patches
    two *separate* handlers into its object table (`handle_boza_logram_outer` for the four outer slots,
    `handle_boza_logram_centre` for the centre). The Scratch walk dispatches by object *type*, and all five
    slots share `BOZA_LOGRAM_TYPE`, so the port cannot patch two handlers; instead one `update boza` proc
    branches on `slot link` (0 = centre, > 0 = outer) to run the outer's arm/animate/fire-and-crater or the
    centre's scroll-and-cascade. This is a structural consolidation of two handlers, not a behavioural change —
    the same consolidation record [041](041-ground-domes-and-turrets.md) made for the Garu Derota base/node.
  - **Port necessity — the centre-value downgrade addresses the centre slot by index, not a pointer.** The
    arcade `update_centre_points_value` follows the outer object's `_EXTRA` pointer to its centre object and
    writes `_PTS`. Scratch has no pointers, so at spawn each outer captures the centre's *slot index* in its
    `slot link` list, and the outer's hit branch does `replace item (slot link) of "slot pts" with the 600
    position`. Like the arcade (which rewrites `_PTS` every hit frame), this idempotent per-tick write is
    faithful: the centre was already scored at its then-current value when a bomb resolved, so the write only
    lowers what a *later* centre bomb would award.
  - **Port necessity — the cascade addresses the four outer slots by offset, not by walking objects.** The
    arcade `destroy_all_outer_lograms` walks the four objects adjacent to the centre (`lea (-_OBJSIZE,a1)`) and
    writes each `_STATE=3`. The port stamps the outers at base+0..3 and the centre at base+4, so the cascade
    writes `slot state` at centre-index − 1..−4 directly to `SLOT_HIT`. It is idempotent (an already-hit outer
    keeps its own crater clock — no timer touch), so it runs every centre-hit tick with no guard, matching the
    arcade. The walk sweeps ascending slot index, so the outers update before the centre: a centre-first
    cascade marks the outers this same tick and their crater clocks begin cleanly next tick.
  - **State mapping.** The arcade's active `_STATE=2` and hit `_STATE=3` map to the port's `SLOT_ACTIVE=1` and
    `SLOT_HIT=2` — the same mapping the shared ground detector already uses; the centre value positions 19
    (2,000) and 13 (600) are the port's 1-based indices into the shared value table for the arcade `_PTS` bytes
    54 and 36.

- Scratch evidence: harness scenarios with biting negatives, structural guards each paired with a severing
  negative, and fresh roadmap-evidence markers.
  - Harness (`harness/lib/catalog.js`, negatives in `harness/lib/mutate.js`):
    `boza-outer-scores-300-and-craters` (an outer scores 300 through the shared detector and craters
    persistently), `boza-outer-hit-downgrades-centre-value` (a hit outer rewrites its linked centre to the
    600 position, so a later centre bomb scores 600 not 2,000), and
    `boza-centre-first-cascade-clears-outers-for-free` (a centre-first bomb scores 2,000, cascades all four
    outers to hit awarding nothing, and a later bomb on a cascaded outer scores nothing). Each is proven
    against the real build and a mutated build that fails the same assertion. `ground-dispatch-spawns-scoped`
    is widened so the now-built Boza type (`0x2D`) counts as an in-scope handled family.
  - Structural (`tests/test_scratch_project.py`): `_gnd05_failures` — present-and-negative guards pinning the
    Boza composite (five slots stamped ACTIVE under the Boza type; the outer/centre branch on `slot link`;
    outers fire on the Logram cycle, are arm-gated on the stop-firing row, and crater persistently; an outer
    hit downgrades the linked centre to the 600 position; the centre never fires; a centre hit cascades all
    four outers to hit and craters; outers award 300, the centre 2,000; exactly one of the five links is the
    centre's own zero). Each clause is paired with a biting negative.
  - Records / roadmap: fresh `roadmap-evidence: GND-05 success|failure` markers on the added tests, plus the
    deterministic-build gates `test_two_clean_processes_build_identical_bytes` and
    `test_game_director_generator_is_current`.
  - Operator playtest: the on-screen feel — bombing an outer dome craters it, scores 300, and drops the
    centre's value to 600; bombing the centre first scores 2,000 and clears the four outers for free.

- Acceptance criteria: Engine — the three harness scenarios pass with their biting negatives; the
  `_gnd05_failures` structural guard holds and every negative bites; the build is byte-identical across two
  clean processes and the generator is current; every ground and mechanics citation resolves at the pin.
  Operator — bombing an outer Boza dome craters it, scores 300, and drops the centre's value to 600; bombing
  the centre first scores 2,000 and clears the surviving outers for no extra score.

- Fidelity status: GND-05 (Boza Logram) is **built** — live and proven in the harness and the structural
  guard, with the on-screen feel to be confirmed by the operator playtest. The remaining ground families
  (`ground.grobda`, `ground.domogram` of slice 13) stay `provisional` under the locked ground spec and are not
  built here.

- License status: The pinned reference states no reusable license; only instruction-derived behaviour and the
  committed, hash-pinned tables (the [schedule data](../spec/data/area-schedules.json) and the scoring values)
  are used, cited to the settled spec and the data files, and no reference source text or media was reproduced.
  Ground sprite art is credited in `src/xevious/assets/provenance.json`
  (https://www.spriters-resource.com/arcade/xevious/); the four outer domes reuse the Logram crops and only
  the Boza centre core adds a new proof crop.

- Known deviations or uncertainty: no locked-spec correction to the Boza *behaviour* this leaf — the source
  and the settled [ground objects](../spec/ground-objects.md) spec already agree on the Boza composite. This PR
  does, however, carry a `guardrail-ack`, for a separate reason: to make this and the other ground families
  reachable for the operator playtest, it adds a **temporary `G` ground-debug key** (the ground analog of the
  `T` aerial-debug key — while held it cycles one built ground family at a time into the band from the top of
  the field), which amends the LOCKED control mapping in
  [core-game-systems.md](../spec/core-game-systems.md); that amendment is the guardrail-ack surface. The key is
  a dev tool tracked for removal once every ground family is built and playtested (issue #119), not a GND-05
  behaviour or a change to the Boza. The three port necessities above (one update proc branching on `slot link`; the centre
  downgrade and the cascade addressing slots by index/offset rather than following the arcade `_EXTRA`
  pointer / object walk) are structural translations of pointer-based code into Scratch's flat slot lists, not
  behavioural changes. The exact on-screen rhythm of the outer fire cycle relative to a lone Logram, and the
  visual read of the diamond composite, are points for the operator playtest to confirm.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
