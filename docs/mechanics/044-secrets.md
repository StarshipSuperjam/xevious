# Secrets — SEC-01 / SEC-02 / SEC-03 + ECO-03, the three hidden ground objects

- Mechanic: the three "secret" objects of slice 14 — hidden things a player finds by bombing or overflying a
  spot of empty-looking ground. The **Sol Tower** (`0x1D`) is an invisible citadel that a bomb reveals and
  raises through a seven-frame growth, then a second bomb destroys — two scoring stages at 2,000 each. The
  **Bonus Flag** (`0x54`) is an invisible flag that a bomb reveals (scoring 1,000) and that the craft then
  **collects by flying over it, not by shooting**, for a cabinet-selected award — an extra craft or 10,000
  points (ECO-03). The **Hidden Credit** (`0x53`) is an invisible object that a bomb reveals into a ~2-second
  on-screen credit, then self-removes. All three build on the slice-9 ground bombing pipeline
  ([033](033-ground-bombing-pipeline.md)) and the shared ground detector and terrain scroll it established;
  their new seams are per-family reveal state machines, a proximity (fly-over) collection test, a
  cabinet-award selector, and a one-shot credit overlay. This record covers the leaves `secrets.sol-tower`
  (#90, SEC-01), `secrets.bonus-flag` (#91, SEC-02), `economy.bonus-flag-award` (#92, ECO-03) and
  `secrets.hidden-credit` (#93, SEC-03).

- Derived behavior: three hidden objects over the one shared ground seam.
  - **The Sol Tower — invisible, revealed, raised over two scoring stages.** A Sol Tower is scheduled as a
    single-slot ground object that is ALWAYS invisible while hidden and just scrolls with the terrain. A bomb
    flows through the shared ground detector, scores the citadel's 2,000 points, and marks the slot hit; the
    reveal then flips the object into its **rising** phase and raises it through a seven-step growth clock —
    step `(timer >> 4) & 7`, one animation frame per step, growing 1×1 → 2×2 at step 4 — and on reaching step 7
    returns the slot to **active** as a fully-**risen** citadel. A second bomb on the risen citadel scores
    **again** (the same 2,000) and craters it persistently like a Barra. The detector only scores an active
    slot, so the object is never re-scored mid-rise: two bombs, two awards, one destroyed tower. (The
    Super-Xevious 1,000-point variant is excluded — this is the normal cabinet.)
  - **The Bonus Flag — bomb to reveal and score, fly over to collect.** A Bonus Flag is scheduled invisible at
    a bounded-random lateral position and scrolls with the terrain. A bomb reveals it (through the shared
    detector, which scores its 1,000 points and marks it hit) and flips it to its **revealed** phase, in which
    it stays hit so the active-only detector never re-scores it. While revealed it runs a per-tick
    **fly-over** test — a proximity check of the craft's cell against the flag's, exactly the arcade's
    collection window, **not a weapon hit** — and on overlap it awards, plays the flag sound, and removes
    itself.
  - **The bonus-flag award (ECO-03) — a cabinet choice of a craft or 10,000.** Collection awards by a cabinet
    DIP selection: with the "bonus life" option set, an **extra craft** (craft +1); otherwise **10,000
    points**. The extra-craft path plays no "extend" jingle — that jingle belongs only to the score-threshold
    bonus life ([011](011-lives-and-bonus-economy.md)). The reveal's own 1,000 (above) is separate from and in
    addition to this collection award.
  - **The Hidden Credit — bomb to reveal a ~2-second credit, then gone.** A Hidden Credit is scheduled
    invisible and scrolls with the terrain. A bomb reveals it (through the shared detector, scoring the minimum
    10 points) and flips it to its **showing** phase, in which it **freezes in place** (it no longer scrolls),
    raises an on-screen credit, and counts a display clock up to a ~2-second window, after which it clears the
    credit and removes itself. The credit shows this project's own placeholder wording (see the deviations
    below), never the arcade's own credit strings.

- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`; citations are
  `src/xevious_main.68k` unless noted. **Sol Tower** — `handle_1D_Sol_Tower` (3013–3039) inits the object
  (`_STATE=2`, `_ATTR` size 1×1, `_CODE=0` invisible, `_PTS=54` = 2,000 on the normal cabinet), and on a hit
  (`_STATE==3`) branches to `handle_sol_tower_rising` (3040–3057), which counts the rise clock, derives the
  step `(_TIMER >> 4) & 7`, grows to size 2×2 (`_ATTR=3`) at step 4, and selects the frame from
  `sol_tower_animation_tbl` (3068–3078); on step 7 it falls to `sol_tower_risen` (3058–3067), which returns the
  object to `_STATE=2` so a second bomb routes to `handle_bomb_explosion`. **Bonus Flag** —
  `handle_54_Bonus_Flag` (3131–3147) inits the object (`_STATE=2`, size 1×1, `gen_rnd_spriteY` for a bounded
  random Y, `_CODE=0` invisible, `_PTS=48` = 1,000), and on a hit branches to `reveal_bonus_flag` (3148–3157),
  which makes the flag visible (`_CODE=0x1f`), runs `check_flag_collected` (3178–3191) each tick, and on a
  collected result (carry) falls to `score_bonus_flag` (3158–3160); `check_flag_collected` compares the craft's
  shadow position (`sprite_shadow_msb+2*0x23`) against the flag's own with a `−0x0a/+0x14` window on Y and a
  `−5/+0x0a` window on X (a proximity match, not a projectile). **The award** — `score_bonus_flag` (3158–3160)
  tests DIP bit 1 (`btst #1,(dswb)`): set routes to `inc_num_solvalou` (3166–3170), which adds a craft and
  redraws the craft count; clear takes `pts_10000` (`score_10000_for_bonus_flag_tank`, 3161) through
  `add_to_score`; both fall to `remove_bonus_flag` (3171–3177), which clears the object and plays
  `BONUS_FLAG_SND`. **Hidden Credit** — `handle_53_Easter_Egg` (5989–6000) removes the object in attract mode
  (`attract_mode_stage`) and otherwise inits it (`_STATE=2`, size 1×1, `_CODE=0` invisible), and on a hit
  branches to `check_copyright_strings` (6001–6012), which sets a `0x80`-frame display timer, calls
  `display_easter_egg` (6018–6104), and counts the timer down to `wipe_easter_egg`; `remove_easter_egg`
  (6013–6017) clears the object. The disassembler stubbed the original copyright-tamper trigger
  (`check_copyright_strings` 6003–6005) and `display_easter_egg` shows only the porter's own credit strings.
  Placements per area are the committed [schedule data](../spec/data/area-schedules.json); the object registry
  is [object-types.json](../spec/data/object-types.json); point values are
  [scoring, lives, and game over](../spec/scoring-lives-and-game-over.md); the settled behaviour is
  [secrets](../spec/secrets.md).

- Transfer class: General behavior and numeric constants (instruction-derived control flow — the three reveal
  state machines, the seven-step rise clock and its `(timer >> 4) & 7` step, the two-stage scoring, the
  fly-over proximity window, the DIP award selector, and the 128-frame credit display — over the committed,
  hash-pinned schedule, registry, and scoring tables; no source text or media copied, and the credit wording is
  the port's own, not the arcade's).

- Scratch interpretation: the three families are added to the `.sb3` generator `tools/game_director.py` along
  the same seams the slice-9/12/13 ground families use — type codes handled by the shared detector and terrain
  scroll, single-slot spawn init in the ground seed, walk-dispatch entries routing each type to
  `update sol tower` / `update bonus flag` / `update easter egg`, and per-family renderers. Each update proc is
  a `warp` custom block split on `slot state` (active idle vs hit) and then on `slot flag` (the per-family
  phase).
  - **Port necessity — the Sol Tower's rise size growth is baked into the artwork, not a runtime resize.** The
    arcade grows the object 1×1 → 2×2 by writing `_ATTR=3` at rise step 4. The port renders the seven rise
    frames as pre-scaled costumes, so the growth is inherent to the frame the renderer shows for each step and
    there is no runtime `setsizeto`. Behaviour (which frame at which step, the destroy-stage hit window) is
    unchanged.
  - **Port necessity — the display and rise clocks are arcade-frame counts, stepped by the frame convention.**
    The Sol Tower rise (`SOL_RISE_PHASE_FRAMES = 16` per step, `SOL_RISE_STEP_COUNT = 7`) and the credit
    display (`EASTER_EGG_DISPLAY_FRAMES = 128`) are counted by `TICK_TIMER_STEP` (2 arcade frames per tick),
    like every other ground/air timer. The arcade counts the credit timer DOWN from `0x80`; the port counts it
    UP to 128 — the same ~2-second window.
  - **Port necessity — the fly-over collection reuses the shared craft-overlap reporter.** The arcade's
    `check_flag_collected` proximity window (`−0x0a/+0x14` on Y, `−5/+0x0a` on X, against the craft's shadow
    slot) is exactly the port's `HIT_WINDOW_BOMB_GROUND` (10, 20, 5, 10) craft-overlap reporter, so the flag's
    per-tick collection test reuses that reporter reading the cached player cell — proximity, not a new
    collision group and not a weapon.
  - **Port necessity — the DIP award selection is a project config marker.** The port has no raw cabinet-DIP
    model, so the arcade `btst #1,(dswb)` selection is a project config flag `flag awards craft`
    (`FLAG_AWARDS_CRAFT_ID`), in the same four-marker placeholder convention as the other cabinet options. True
    → the extra-craft arm (craft +1, "craft changed", no "extend"); false → `award value = 10,000` through the
    single `score` proc.
  - **Port necessity — the credit is a single generated bitmap in a port-generated font, and attract removal
    is unreachable.** No ground object before this drew text. The credit is a dedicated `easter-egg` render
    target whose ORIGINAL (zero clones) shows one pre-composed two-line costume at a fixed screen position
    while the `easter egg showing` signal is 1 — keeping the port at its existing 300-clone ceiling untouched.
    The wording is this project's own placeholder ("XEVIOUS PORT / BY STARSHIP SUPERJAM"), rendered in a
    port-generated 5×7 pixel font (`tools/hud_glyphs.py`), because the arcade HUD font manifest crops only the
    letters the HUD readouts use (it lacks X/T/Y/J/B) and the credit is original content. The arcade
    attract-mode silent-removal branch has no analog — the port has no attract/demo mode — so only the
    real-play reveal path is built.
  - **State mapping.** The arcade's active `_STATE=2` and hit `_STATE=3` map to the port's `SLOT_ACTIVE=1` and
    `SLOT_HIT=2`, the mapping the shared ground detector already uses. Each family's phase is carried in the
    port's `slot flag`: the Sol Tower's hidden / rising / risen (0 / 1 / 2), the flag's hidden / revealed
    (0 / 1), and the credit's hidden / showing (0 / 1). A revealed flag and a showing credit keep the slot HIT
    so the active-only detector never re-scores them (mirroring the arcade `_STATE=3` re-bomb dodge); the
    showing credit also stops scrolling (the arcade scrolls only on the non-hit branch). The `easter egg
    showing` signal is cleared on `stage_reset`, so a death, transition, or new game never leaves the credit
    lingering.

- Scratch evidence: harness scenarios with biting negatives, structural guards each paired with a severing
  negative, and fresh roadmap-evidence markers.
  - Harness (`harness/lib/catalog.js`, negatives reusing `harness/lib/mutate.js`):
    `sol-tower-reveals-rises-then-a-second-bomb-craters-scoring-both-stages` (a bomb reveals and scores the
    hidden tower, it raises through its steps back to active — and is not re-scoreable while rising — and a
    second bomb scores again and craters it persistently),
    `bonus-flag-revealed-by-bomb-scores-once-then-collected-by-flyover-not-a-weapon` (a bomb reveals and scores
    the flag once — a second bomb never re-scores it — and flying the craft over the revealed flag collects it
    while a flag the craft is not over is not),
    `bonus-flag-collection-award-honours-the-dip-choice` (the craft-award and the 10,000-point arms are each
    taken under the matching config, and the craft arm plays no "extend"), and
    `hidden-credit-bomb-reveals-a-held-overlay-then-self-removes-for-the-min-score` (a bomb reveals the credit
    for the minimum score, the `easter egg showing` signal holds for the display window and then clears, the
    slot removes itself, and an un-bombed egg raises nothing). `ground-dispatch-spawns-scoped` additionally
    treats the Sol Tower (`0x1D`) and Hidden Credit (`0x53`) as in-scope handled families rather than unhandled
    leakage (the Bonus Flag rides the `add_object` path, not the ground schedule that scenario consumes, so it
    does not appear there). Each scenario is proven against the real build and a mutated build that fails the
    same assertion.
  - Structural (`tests/test_scratch_project.py`): `_sec01_failures` — guards pinning the Sol Tower (warp; the
    state split; the active-idle scroll; the hit split into destroy vs rise; the persistent crater; the
    HIDDEN→RISING reveal guarded on the hidden phase; the rise clock; the two-stage return to active+risen
    gated on the rise-step test; the spawn seed under the `type == SOL_TOWER_TYPE` gate; the dispatch).
    `_sec02_failures` — guards pinning the Bonus Flag and its award (warp; the state split; the active-idle
    scroll; the hit split; the reveal that keeps the slot hit; the fly-over gate reading the player cell; the
    DIP if/else on `flag awards craft`; the craft arm with no "extend"; the points arm through `score`; the
    sound; the cull; the dispatch; the debug spawn seed; the single-clone renderer). `_sec03_failures` —
    guards pinning the Hidden Credit (warp; the state split; the active-idle scroll; the hit split; the reveal
    that raises the signal, keeps the slot hit, and freezes scrolling; the display clock; the expiry that
    clears the signal and culls; the spawn seed; the dispatch; the overlay gated on the signal; no clone band).
    Each clause is paired with a biting negative. The `area-schedules.json` round-trip golden in
    `tests/test_spec_docs.py` is widened so the three secret object types (`0x1D` / `0x53` / `0x54`) are
    covered with their expected handlers.
  - Records / roadmap: fresh `roadmap-evidence: SEC-01|SEC-02|SEC-03|ECO-03 success|failure` markers on the
    added tests, plus the deterministic-build gates `test_two_clean_processes_build_identical_bytes` and
    `test_game_director_generator_is_current`.
  - Operator playtest: bomb a hidden Sol Tower to reveal it — it rises in stages, and a second bomb destroys
    it, scoring both times; bomb a Bonus Flag to reveal it, then collect it by flying over (not shooting) for
    the configured award; bomb the hidden-credit spot to see the ~2-second original-text credit, which then
    clears itself.

- Acceptance criteria: Engine — the four harness scenarios pass with their biting negatives; the
  `_sec01_failures`, `_sec02_failures`, and `_sec03_failures` structural guards hold and every negative bites;
  the widened `area-schedules.json` round-trip golden holds; the build is byte-identical across two clean
  processes and the generator is current; every schedule, registry, scoring, and mechanics citation resolves at
  the pin. Operator — a hidden Sol Tower reveals, rises, and a second bomb destroys it (scoring both times); a
  Bonus Flag reveals on a bomb and is collected by flying over it for the cabinet-selected award; the
  hidden-credit spot reveals the ~2-second original-text credit and then clears.

- Fidelity status: SEC-01 (Sol Tower), SEC-02 (Bonus Flag), ECO-03 (bonus-flag award), and SEC-03
  (Hidden Credit) are **built** — live and proven in the harness and the structural guards, with the on-screen
  feel to be confirmed by the operator playtest. With these leaves the slice-14 secrets are complete; the
  Andor Genesis boss (`andor.lifecycle`, slice 15) stays out of scope here.

- License status: The pinned reference states no reusable license; only instruction-derived behaviour and the
  committed, hash-pinned tables (the [schedule data](../spec/data/area-schedules.json), the
  [object registry](../spec/data/object-types.json), and the scoring values) are used, cited to the settled
  spec and the data files, and no reference source text, credit strings, or media were reproduced. The Sol
  Tower and Bonus Flag sprite crops are credited in `src/xevious/assets/provenance.json`
  (https://www.spriters-resource.com/arcade/xevious/); the bonus-flag pickup sound is credited in the same
  provenance; the hidden-credit overlay is fully port-original content (a generated bitmap in a port-generated
  font) and carries project-original provenance with no third-party source.

- Known deviations or uncertainty: this leaf **corrects the locked `secrets.md`** where its prose diverged from
  the source (the source wins), so this PR carries a **`guardrail-ack`**: the spec had described the bonus-flag
  reveal-bomb as scoring nothing, but `handle_54_Bonus_Flag` / the shared detector score its 1,000-point
  `_PTS=48` value on the reveal bomb (and the fly-over collection then awards the craft-or-10,000 separately),
  so the spec is corrected to match. The four port necessities above (the artwork-baked Sol Tower growth; the
  frame-stepped clocks; the DIP award as a config marker; and the single-costume credit in a port-generated
  font with attract removal unreachable) are structural translations into Scratch's flat slot lists, tick
  convention, and clone budget, not behavioural changes. Two source features are inherently absent in the port:
  the arcade **attract-mode** silent-removal branch (`handle_53_Easter_Egg` 5990–5992) has no analog because
  the port has no attract/demo mode, and the original **copyright-tamper trigger** was already stubbed in the
  reference itself (`check_copyright_strings` 6003–6005, "no point doing that now"). The credit **wording** is
  this project's own placeholder, never the arcade's credit strings, per `docs/REFERENCE_POLICY.md`. The exact
  on-screen rhythm of the Sol Tower rise, the fly-over collection feel, and the credit's readability remain for
  the operator playtest to confirm, along with the operator's pixel-verification of the Sol Tower rise frames
  and the flag crop.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
