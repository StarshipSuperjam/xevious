# Operator playtest checklist

The operator's playtest is the only gameplay verification this project has. A headless runtime tripwire
(`harness/`) now runs the game's logic in CI and can catch state/logic regressions early, but it observes
internal state only — never the running game on screen — so it covers **none** of the steps below and
reduces none of them: run the full sweep regardless. This is the short, ordered regression sweep to run on
**every** pull request that can affect gameplay, before approving it (the playtest gate in
[the principles](principles.md)). It probes the failure modes that have actually bitten this project;
expect about twenty to twenty-five minutes now that death and scoring are exercised through real combat
(the instant D/G/S debug keys are gone) — step 5 in particular needs a clean flight to the near-end
window before an intentional death, with no clock-acceleration key. Deeper per-capability checks live in
each spec document's acceptance table and are run when their capability changes.

**Before you start.** Build and load the PR's `.sb3` through the handover tool,
which verifies the build is faithful to the pinned reference (it re-derives the
generated data and resolves every citation) and refuses to build while any
citation is unresolved:

```bash
python3 tools/playtest_package.py
```

then open `dist/Xevious.sb3` in Scratch 3. (The raw `python3 tools/scratch_project.py build`
still exists for a non-gameplay build, but a playtest build goes through the tool
above so a build that adapted to a wrong spec never reaches the playtest.) Controls today: **arrow keys** move, **Space** fires (and
also starts from the title), **B** bombs; the crosshair leads the ship and tracks it automatically
(there is no separate crosshair control). The temporary **D**, **G**, and **S** debug keys are **gone** —
enemies now exist, so death, game over, and scoring are exercised by real combat: destroy Toroids to
score, let one (or its bullet) touch you to die. One new **temporary** key is present: holding **T**
during play brings in **Terrazis one at a time** (step 4b) so a family unreachable in early play can be
tested — a dev tool tracked for removal (issue #119), not part of the finished game.

**Applicability.** A step that names something not yet built (enemies, ground objects, scoring) is
skipped, not failed — the mechanics catalog says what exists. **Dispositions are three,** not two: a
step passes; or it fails (the PR stays draft, the failure goes back with the item number); or it shows
a **known recorded divergence** — behavior the spec explicitly records as an interim fixture — which is
noted, not failed. (The former terrain-preserved-on-death fixture is retired: a new life now restarts
the current area from its top, per the life-economy slice, and the near-end checkpoint exception to that
rule is now built in the area clock — a death in the final fifth of an area advances the area number
instead of restarting. The visual terrain stays decoupled from the area clock at this foundation stage,
so area position is read from the `area progress`/`area number` variable watchers, not the screen.)

1. **Cold start.** Green flag: one title presentation (the logo entering as the spec's presentation
   document records), music once, no stray sprites. Press Space: one READY presentation, then play.
2. **Held fire while moving.** Hold Space for five seconds while flying circles: the cadence stays
   steady the whole time — no stutter, no silencing when an arrow key joins, and never more than 3
   shots on screen. Shots vanish at the top edge, never parking there.
3. **Bomb hammering.** Hammer B: bombs never overlap — a new bomb only after the previous resolves.
   The release and impact presentations play. (Crosshair lock-over-target applies once ground objects
   exist.)
4. **Terrain endurance and the area clock.** Fly for **at least ~70 seconds** — long enough to cross
   at least one area boundary (one area-clock cycle is ≈68 s): no black gap, no frozen strip, no
   drift. Open the variable watchers for **`area progress`**, **`area number`**, **`scroll row`**, and
   **`schedule fired`** — in Scratch these are hidden by default, so **tick the checkbox beside each
   one in the Variables section of the blocks palette** to show its monitor on the stage. While you
   fly: `area progress` climbs steadily and resets to 0 **only in the
   same moment `area number` ticks up** — a paired reset-and-advance at an area boundary is correct,
   but an `area progress` drop that is *not* paired with `area number` changing is a bug; `scroll row`
   counts 13 down to 0 then wraps 255 down toward 14; and `schedule fired` climbs (once per schedule
   record as the area scrolls) and resets to 0 at each area boundary. Every area now carries its **own**
   schedule, so the peak `schedule fired` reaches **varies as the area changes** — a coarse sign that each
   area runs its own schedule. (Some areas legitimately share a record count, so two areas showing the
   *same* peak is **not** itself a bug; the exact per-area correctness is guaranteed by the build-time
   round-trip test, not the eye.) The visual terrain is not yet driven by the clock, so these are
   variable-watcher checks, not on-screen ones. **Also tick `ai level`, `formation count`, and
   `formation type offset`** (DIF-01 / FORM-01): as you fly, `ai level` climbs a little each time a raise
   record fires and stays below **128** (a raise folds it back — you should never see it reach 128); if you
   have been scoring (destroy Toroids to raise the score), it can also jump when a score-adjust record fires
   (DIF-02) — the *amount* is score/craft-dependent and its full visible effect on enemy pressure is deferred
   to slice 10; and `formation count` / `formation type offset` change to a new wave (count in **1–6**) when a
   raise or a set-formation record fires, and drop to 0 on a reset-formation. **Read the `ai level` growth
   *rate* as a placeholder, not fidelity:** the cabinet difficulty is a project-chosen default (increment +2),
   so how *fast* the level climbs is not meaningful yet — only that it climbs, folds, and drives a valid
   formation is. The formation now **spawns live** (see step 4a), so `formation count` and the enemies on
   screen should agree; the exact table correctness is still the build-time model fixture's, not the eye's.
   **You can also tick a `fire mask *` watcher** (e.g. `fire mask logram`) and **`ground stop firing row`**
   (DIF-03): each takes its scheduled byte value as the area scrolls (logram, for instance, is set near the top
   of area 1) and resets to 0 on a new game. No family's fire *rate* is gated by these yet — the shooting
   Toroid fires once without consuming any mask (slice 10 wires mask-gated firing) — so this only confirms the
   schedule sets them.
4a. **Toroid combat — the first live enemy.** Fly area 1 until the first formation record fires (watch
   `formation count` climb above 0): **Toroid waves appear** and keep replacing themselves — each slot
   re-spawns a fresh Toroid the moment its occupant leaves or dies — until a reset-formation record
   zeroes the count. Confirm: they enter aimed toward the craft, then as they draw nearly level they
   **swing** — *reversing* their lateral course and peeling **away** from the side they were closing on
   (the arcade bounce: a Toroid moving left toward you veers back to the right), flapping as they go —
   they do **not** home straight into you; the **shooting variant fires one aimed bullet** at that
   moment (a bullet streaks from the enemy toward where you were), never a continuous stream. **Shoot
   one:** it **explodes and the score rises by exactly 30** (watch the HUD digits — the score change is
   the definitive pass signal), and the wreck is gone — no lingering sprite. The enemy **scale and aspect**
   should look right against the craft (report if they look stretched or mis-sized). Finally, **stop and
   green-flag again while a wave is on screen:** no Toroid or bullet sprite should survive the reset (no
   orphan clones). **Two recorded art stand-ins — note, do not fail (they are interim, per records 025/026):**
   the enemy bullet is not a dedicated bullet sprite yet — it renders as a small stand-in costume (a shrunk
   Toroid frame), so a small enemy-looking dot flying straight *is* the bullet; and the kill explosion reuses
   the player craft's own death-burst frames as a placeholder. Both are deliberate deferrals to a later art
   pass, so judge the *behavior* (fires / flies / kills; explodes / scores / clears), not the placeholder art.
4b. **Terrazi combat — the first firing family (temporary debug spawn).** Terrazi only spawns at very high
   AI levels, unreachable in a normal area-1 flight, so this build carries a **temporary playtest key**:
   while playing, **hold `T`** to bring in Terrazis **one at a time** — a single Terrazi enters, and the
   next appears only after it leaves or dies, so you can watch each one's full lifecycle without a crowded
   wave. This key is a dev tool tracked for removal (issue #119) — it is not part of the finished game.
   Hold `T` and watch a single Terrazi through, confirming: it **enters aimed toward the craft** at a
   faster clip than Toroids (the 3 px/frame tier), **rolling** through its frames; while still distant it
   **fires aimed bullets on a timer** (a steady drip, not one-and-done like the Toroid — the rate tracks
   the scheduled mask); and as it draws **nearly level with you in the scroll direction** it **stops firing
   and glides** — decelerating and **reversing its sideways course** to peel away, rather than diving into
   you. **Shoot one:** it explodes and the score rises by **700** (the HUD digits are the definitive
   signal), the wreck clears. Check the **roll sprite** reads right (the small green banking-light on two
   of the frames is correct, not an artifact); the shared explosion is the same placeholder burst as the
   Toroid (note, do not fail). Release `T` and confirm normal Toroid waves resume.
5. **Repeated deaths and the near-end checkpoint.** Die several times in a row by letting a Toroid or its
   bullet touch the craft (once ground objects and Bacura exist, exercise those too): the full death
   presentation and sound complete uncut, the craft respawns **immediately vulnerable** (fly into an enemy
   right after respawn to confirm there is no invulnerability window), and nothing from the previous life
   lingers. Watch **`area number`** across each death, using **`area progress`** to place the death —
   and be sure to exercise **both** kinds, or the checkpoint path goes untested: an **ordinary** death
   (die while `area progress` is low, early in an area) restarts the area from its top and **keeps**
   `area number`; a **near-end** death (die while `area progress` is above roughly **52,000** — the
   final fifth before the ≈65,056 completion mark, i.e. `scroll row` in 14–67) **advances** `area
   number` by one instead (the checkpoint; completing area 16 rolls to 7). With area-1-only art the
   screen can't show the difference, so `area number` is the pass/fail signal. **You do not need to
   reach area 16 by play** — there is no clock-acceleration key, so confirm a **few** real area→area+1
   advances (with `schedule fired` climbing and resetting each boundary, its peak varying as areas
   change — the coarse sanity signal of step 4); the full
   **1–16→7 loop** and the 16→7 return with **no win screen** are proven by the engine's accelerated
   trace, not by playing to the end. Losing your last craft to enemy contact reaches GAME OVER, holds, and
   returns to the title; life icons in the HUD track the count.
6. **Life economy — score, cap, bonus, HUD.** Destroy Toroids while playing: the score climbs by 30 a
   kill, the HUD digits roll in sync (white), and the yellow **HIGH SCORE** value tracks it whenever the
   score passes it. Each digit shows leading zeros, arcade-style. Reaching 20,000 grants an extra craft
   with its **extend** sound and a life icon (the icon row is capped at 9 on screen — the true count keeps
   rising); reaching that at 30 points a kill is a long grind, so the **bonus-life award, the 9,999,990
   cap, and the at-cap extra-craft quirk are better confirmed by the build's fixtures and the earlier
   life-economy playtest than by grinding here** — spot-check that the score and high-score track a few
   kills correctly, and report any misbehavior in the digits or tracking.
7. **Layering.** During busy play: shots, the craft, Toroids, and enemy bullets all render above the
   terrain, and the craft and shots read clearly against the Toroids; the frame borders never hide the ship.
8. **Movement and weapon feel — the restored prototype.** This build restores the movement, shot speed,
   and crosshair behavior of the recovery build (#13/#14) you approved — a single spatial factor was tried
   and rejected, and this build tunes those quantities as port constants instead. Confirm the feel is back
   to what you validated: the craft moves at its familiar speed and reaches every edge; the shot speed
   reads well; and the crosshair **leads the ship, tracks it, and — this is the key fix — cannot leave the
   top of the screen**: when it reaches the top border it stops there and the ship stops with it (the
   crosshair also marks the bomb-drop point). If any of movement, bounds, shot, or the crosshair top-stop
   feels wrong, that is a bug to report, not a decision to revisit.
9. **Stop and reload.** Stop, green-flag again: identical cold start, no accumulated state.
10. **The PR's own changes.** Walk the list of behavior added or changed that the PR declares, one item
    at a time, against the spec sections it cites.
