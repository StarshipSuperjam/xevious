# Operator playtest checklist

The operator's playtest is the only gameplay verification this project has. A headless runtime tripwire
(`harness/`) now runs the game's logic in CI and can catch state/logic regressions early, but it observes
internal state only — never the running game on screen — so it covers **none** of the steps below and
reduces none of them: run the full sweep regardless. This is the short, ordered regression sweep to run on
**every** pull request that can affect gameplay, before approving it (the playtest gate in
[the principles](principles.md)). It probes the failure modes that have actually bitten this project;
expect about fifteen minutes. Deeper per-capability checks live in each spec document's acceptance
table and are run when their capability changes.

**Before you start.** Build and load the PR's `.sb3`:

```bash
python3 tools/scratch_project.py build
```

then open `dist/Xevious.sb3` in Scratch 3. Controls today: **arrow keys** move, **Space** fires (and
also starts from the title), **B** bombs; the crosshair leads the ship and tracks it automatically
(there is no separate crosshair control); **D**, **G**, and **S** are the temporary debug fixtures —
until enemies exist, **D** triggers one death, **G** a terminal death (drains to the last craft), and
**S** awards points (each press adds the top value-table entry, 10,000) so scoring, the cap, and bonus
lives can be exercised. All three are recorded in the mechanics records and are removed when the real
collision trigger and enemy scoring land (slice 8).

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
   record as the area scrolls) and resets to 0 at each area boundary. The visual terrain is not yet
   driven by the clock, so these are variable-watcher checks, not on-screen ones.
5. **Repeated deaths and the near-end checkpoint.** Die several times in a row (today: press D; once
   killers exist, die to a bullet, an enemy, and a Bacura): the full death presentation and sound
   complete uncut, the craft respawns immediately vulnerable, and nothing from the previous life
   lingers. Watch **`area number`** across each death, using **`area progress`** to place the death —
   and be sure to exercise **both** kinds, or the checkpoint path goes untested: an **ordinary** death
   (die while `area progress` is low, early in an area) restarts the area from its top and **keeps**
   `area number`; a **near-end** death (die while `area progress` is above roughly **52,000** — the
   final fifth before the ≈65,056 completion mark, i.e. `scroll row` in 14–67) **advances** `area
   number` by one instead (the checkpoint; completing area 16 rolls to 7). With area-1-only art the
   screen can't show the difference, so `area number` is the pass/fail signal. Draining the craft
   (repeated D, or G) reaches GAME OVER, holds, and returns to the title; life icons in the HUD track
   the count.
6. **Life economy — score, cap, bonus, HUD.** Press **S** repeatedly (or hold it) while playing: the
   score climbs, the HUD digits roll in sync (white), and the yellow **HIGH SCORE** value tracks it
   whenever the score passes it. Reach 20,000 → an extra craft is granted with its **extend** sound and
   a life icon appears (the icon row is capped at 9 on screen — the true count keeps rising). Hold **S**
   to the **9,999,990** cap: the score pins there and, at the cap, every further press grants a craft
   (the recorded arcade quirk). Each digit shows leading zeros, arcade-style. If the score, cap,
   high-score tracking, or bonus award misbehaves, report it.
7. **Layering.** During busy play: shots and the craft render above the terrain (and enemies, once
   they exist); the frame borders never hide the ship.
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
