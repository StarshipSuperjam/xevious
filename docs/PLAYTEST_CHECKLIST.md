# Operator playtest checklist

The operator's playtest is the only gameplay verification this project has — no automated check can
observe the running game. This is the short, ordered regression sweep to run on **every** pull request
that can affect gameplay, before approving it (the playtest gate in
[the principles](principles.md)). It probes the failure modes that have actually bitten this project;
expect it to take about fifteen minutes. Deeper per-capability checks live in each spec document's
acceptance table and are run when their capability changes.

Load the PR's built `.sb3` in Scratch 3, then:

1. **Cold start.** Green flag: one title presentation (logo entering as recorded), music once, no
   stray sprites. Press the start key: one READY presentation, then play.
2. **Held fire while moving.** Hold fire for five seconds while flying circles: the cadence stays
   steady the whole time — no stutter, no silencing when a movement key joins, and never more than the
   capacity of shots on screen. Shots vanish at the top edge, never parking there.
3. **Bomb hammering.** Hammer the bomb key: bombs never overlap — a new bomb only after the previous
   resolves. The crosshair signals over a targetable ground object; the release and impact
   presentations play.
4. **Terrain endurance.** Fly through at least two full terrain cycles (a minute or more): no black
   gap, no frozen strip, no drift.
5. **Repeated deaths.** Die several ways in a row (bullet, enemy, and Bacura when scheduled): the full
   death presentation and sound complete uncut, respawn follows the recorded area rule, the craft is
   immediately vulnerable, and nothing from the previous life lingers.
6. **Layering.** During busy play: shots and the craft render above terrain and enemies; the frame
   borders never hide the ship.
7. **Stop and reload.** Stop, green-flag again: identical cold start, no accumulated state.
8. **The PR's own changes.** Walk the list of behavior added or changed that the PR declares, one item
   at a time, against the spec sections it cites.

Anything that fails: the PR stays draft, the failure goes back with the item number, and the fix comes
back through this same list.
