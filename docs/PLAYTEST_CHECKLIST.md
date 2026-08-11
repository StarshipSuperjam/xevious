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
(there is no separate crosshair control); **D** and **G** are the temporary death fixtures — until
enemies exist, pressing **D** is how you trigger a death, and **G** a terminal death (both recorded in
the spec's core game systems document).

**Applicability.** A step that names something not yet built (enemies, ground objects, scoring) is
skipped, not failed — the mechanics catalog says what exists. **Dispositions are three,** not two: a
step passes; or it fails (the PR stays draft, the failure goes back with the item number); or it shows
a **known recorded divergence** — behavior the spec explicitly records as an interim fixture (today:
terrain preserved across death, pending the life-economy work) — which is noted, not failed.

1. **Cold start.** Green flag: one title presentation (the logo entering as the spec's presentation
   document records), music once, no stray sprites. Press Space: one READY presentation, then play.
2. **Held fire while moving.** Hold Space for five seconds while flying circles: the cadence stays
   steady the whole time — no stutter, no silencing when an arrow key joins, and never more than 3
   shots on screen. Shots vanish at the top edge, never parking there.
3. **Bomb hammering.** Hammer B: bombs never overlap — a new bomb only after the previous resolves.
   The release and impact presentations play. (Crosshair lock-over-target applies once ground objects
   exist.)
4. **Terrain endurance.** Fly through at least two full terrain cycles (a minute or more): no black
   gap, no frozen strip, no drift.
5. **Repeated deaths.** Die several times in a row (today: press D; once killers exist, die to a
   bullet, an enemy, and a Bacura): the full death presentation and sound complete uncut, the craft
   respawns immediately vulnerable, area position follows the spec's recorded rule (or its recorded
   interim divergence, noted above), and nothing from the previous life lingers.
6. **Layering.** During busy play: shots and the craft render above the terrain (and enemies, once
   they exist); the frame borders never hide the ship.
7. **Movement and weapon feel — the restored prototype.** This build restores the movement, shot speed,
   and crosshair behavior of the recovery build (#13/#14) you approved — a single spatial factor was tried
   and rejected, and this build tunes those quantities as port constants instead. Confirm the feel is back
   to what you validated: the craft moves at its familiar speed and reaches every edge; the shot speed
   reads well; and the crosshair **leads the ship, tracks it, and — this is the key fix — cannot leave the
   top of the screen**: when it reaches the top border it stops there and the ship stops with it (the
   crosshair also marks the bomb-drop point). If any of movement, bounds, shot, or the crosshair top-stop
   feels wrong, that is a bug to report, not a decision to revisit.
8. **Stop and reload.** Stop, green-flag again: identical cold start, no accumulated state.
9. **The PR's own changes.** Walk the list of behavior added or changed that the PR declares, one item
   at a time, against the spec sections it cites.
