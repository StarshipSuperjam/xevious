# Operator playtest checklist

The operator's playtest is the only gameplay verification this project has — no automated check can
observe the running game. This is the short, ordered regression sweep to run on **every** pull request
that can affect gameplay, before approving it (the playtest gate in
[the principles](principles.md)). It probes the failure modes that have actually bitten this project;
expect about fifteen minutes. Deeper per-capability checks live in each spec document's acceptance
table and are run when their capability changes.

**Before you start.** Build and load the PR's `.sb3`:

```bash
python3 tools/scratch_project.py build
```

then open `dist/Xevious.sb3` in Scratch 3. Controls today: **arrow keys** move, **Space** fires (and
also starts from the title), **B** bombs; **D** and **G** are the temporary death fixtures — until
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
7. **Stop and reload.** Stop, green-flag again: identical cold start, no accumulated state.
8. **The PR's own changes.** Walk the list of behavior added or changed that the PR declares, one item
   at a time, against the spec sections it cites.
