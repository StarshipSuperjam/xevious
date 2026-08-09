---
status: draft
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Cabinet flow

Covers mechanics catalog rows CAB-01 through CAB-04. Values cite the pinned reference
(`reference_pin` in [the index](index.md)) as `file label lines`; citations are `src/xevious_main.68k`
unless noted. Timings are frames at 60 per second.

License status of extracted values: the reference states no reusable license (recorded in [the index](index.md) and every data file).

## Summary

Everything around the game itself: the attract cycle an idle cabinet plays, coins and starts, how two
players alternate, and how a good score enters the best-five table. These flows are what make it a
*cabinet* rather than a demo, and their rhythm — title, demonstration, scores, repeat — is part of the
game's identity.

## Behavior

**Attract cycle (CAB-01).** An idle cabinet loops: title → demonstration play → best-five table →
demonstration play → title … (`attract_mode_jump_tbl` and stage handlers 1211–1350; the demonstration
stage appears twice per cycle because two table slots share its handler). The title stage runs ~744
frames (~12.4 s: an initial hold, a sparkle appear/move/disappear sequence, then a flashing-logo phase —
derived by tracing the stage's frame gating, recorded medium-confidence). The demonstration stage has no
timer: the attract pilot (a random walk drawing from the shared stream, with a 1-in-16 chance of a
simulated fire press per frame — `gen_rnd_dir` 2156–2165, `gen_rnd_shot` 2351–2354) plays until the
demonstration craft is destroyed. The best-five stage holds 512 frames (~8.5 s, 1338–1344). No stage
scores or consumes lives; coin-up resets the attract state completely (377–384, 1292–1294).

**Credits and starts (CAB-02).** Credits cap at 99; each coin adds one with the coin sound. A 1-player
start costs 1 credit and requires at least 1; a 2-player start costs 2 and requires at least 2
(`src/xevious_sub.68k` `sub_fn_4__handle_credits_and_start` 171–206). Starts below the cost are ignored.

**Two-player alternation (CAB-03).** Players alternate by whole lives: when the active player loses a
craft and both players still have craft remaining, the machine swaps to the other player; if the other
player is out, the same player continues (549–591). The swap exchanges each player's complete game state
block — score, craft remaining, area number, difficulty level, bonus-life progress, and fire-mask state
(`swap_curr_other_player` 671–679; state layout `src/xevious_ram.68k` 161–218). Each turn begins at the
top of that player's current area (the death-restart rule in
[Area progression and terrain](area-progression-and-terrain.md)). When one player's game ends, their
high-score check runs; the survivor plays on. When both are out, the combined GAME OVER shows, state
returns to player 1, and the cabinet resumes attract (556–591).

**High-score entry (CAB-04).** A score beating fifth place in the best-five table enters initials: ten
characters from a 27-symbol set (A–Z and space, wrapping both directions; a lowercase variant exists
behind a DIP bit recorded as uncertain in practical reach), with entry auto-completing on the tenth
character or on an idle timeout of roughly 68 seconds (derived from the compound frame gating,
medium-confidence) (`check_for_high_score` through name entry 1618–1793). Insertion shifts lower entries
down; the table always holds exactly five. Table values and defaults are owned by
[Scoring, lives, and game over](scoring-lives-and-game-over.md). Pause and persistent high-score storage are port
conveniences in the reference and are excluded (catalog EX-05) — a power cycle starts fresh, like the cabinet.

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| An idle cabinet cycles title → demo → best five → demo → title without scoring or losing lives | Watch the built `.sb3` idle through two full cycles | operator |
| The demonstration plays itself and ends by dying, not by a timer | Watch a demo stage to its end | operator |
| Credits cap and start costs behave as recorded (1 credit/1P, 2 credits/2P, cap 99) | Play: insert coins, try starts with 0, 1, and 2 credits | operator |
| Two players alternate on life loss with fully independent state, each resuming at the top of their own area | Two-player game: verify score/lives/area separation across several swaps | operator |
| A qualifying score enters ten-character initials that auto-complete on the tenth character or timeout | Play: qualify, enter initials both ways | operator |
| Attract stage durations and the credit rules match this document in the build's data | Data/structural fixture over the built project's director states and constants | engine |
| The attract pilot draws from the shared random stream (seeded attract runs repeat) | Seeded fixture: two attract runs from one seed produce identical demonstrations | engine |
