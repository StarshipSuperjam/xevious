---
spec_depth: full
reference_pin: 71473685a8c7856c8401c8519276cd97a38d4183
note: >-
  The operator chose the full write-up but deliberately declined the user
  guides; principles and architecture are authored, tutorials and how-tos are
  not.
---

# Product spec — normal arcade Xevious in Scratch 3

## Why this spec exists

A build session merged a change (pull request #9) that invented presentation elements with no basis in the
arcade game and silently removed or degraded ten working behaviors — the operator caught four on sight
(held-fire cadence, the single-bomb lockout, terrain wrapping, the title glide), and the committed
fidelity audit found six more (the crosshair release animation, the bomb impact marker, shot expiry at
the screen edge, layer ordering, the post-death pause, and the baseline's terrain restart on death) —
because no written description of the product existed for the build or its review to check against. This spec is that description: every gameplay behavior in these documents is
traceable to the pinned arcade reference, marked as a recorded deviation, or marked as a Scratch port
necessity, so no future build can improvise gameplay and no review has to rely on memory.

## Ground rules

The [product principles](../principles.md) and [architecture overview](../architecture.md) govern every
document below. The normal Namco arcade release is authoritative; Super Xevious content is excluded; area 16
loops back to area 7 with no win screen. Bulk numeric data lives once, in the
[generated data files](data/) produced by `tools/reference_extract.py`, and each capability document
describes its meaning.

## Capabilities

| Capability | Status | Doc |
| --- | --- | --- |
| Core game systems | in progress | [Core game systems](core-game-systems.md) |
| Player craft and weapons | in progress | [Player craft and weapons](player-craft-and-weapons.md) |
| Scoring, lives, and game over | in progress | [Scoring, lives, and game over](scoring-lives-and-game-over.md) |
| Area progression and terrain | in progress | [Area progression and terrain](area-progression-and-terrain.md) |
| Difficulty and formations | in progress | [Difficulty and formations](difficulty-and-formations.md) |
| Aerial enemies | in progress | [Aerial enemies](aerial-enemies.md) |
| Ground objects | in progress | [Ground objects](ground-objects.md) |
| Secrets | in progress | [Secrets](secrets.md) |
| Andor Genesis | in progress | [Andor Genesis](andor-genesis.md) |
| Cabinet flow | in progress | [Cabinet flow](cabinet-flow.md) |
| Audio and presentation | in progress | [Audio and presentation](audio-and-presentation.md) |

Deliberate exclusions (recorded so no build re-invents them): Super Xevious content — Galaxian, jet with its
score-reset trap, helicopter, tank, bridge, Super formations and schedules; port conveniences — pause and
persistent high-score storage; a conventional win screen. Sources: the mechanics catalog rows EX-01 through
EX-06.

## Provenance

Every reference-derived value in these documents and in `data/` derives from the public
[`jotd666/xevious`](https://github.com/jotd666/xevious) repository at the pinned commit named in this file's
frontmatter, a reverse-engineered transcode of the arcade ROM code that states no reusable license. Input
classes used: player-visible mechanics and rules; numeric constants, timing, scores, and difficulty values;
structured formations, schedules, and lookup tables; normal-versus-Super branch information. Citations name
file, label, and line range at the pinned commit; re-cloning the repository at that commit is the expected
verification path (no copy is kept here). The transcode acknowledges one unidentified remaining gameplay
divergence from the arcade, so no value here is claimed as arcade-confirmed.

SHA-256 of the source files read at extraction (2026-08-09):

| File | SHA-256 |
| --- | --- |
| `src/xevious_sub.68k` | `e2d8a77e1c9b6190949aa00ae86fc1398022c90e285d7fda38920d1d17c77e4a` |
| `src/xevious_main.68k` | `bd23912e5cc25dfe7ebb69c043ab098fead300949b066ece972d5ddda29de77e` |
| `src/map_rom.68k` | `f96a17e75caa788589755bb39fb0097a17a51d957663a54885f97b7835a7d7d6` |
| `src/xevious_ram.68k` | `fc40e0e8b939f665d38812e62f6dbcc994606ff0afaf34e6d9e585ae424a4773` |
| `src/xevious.inc` | `56b9b0e22d77c53bed7a8b31c2d8c38e5e68f94434319bfa5f524210df01ab66` |

Attestations, carried by this specification as a whole: no assembly or other source-code text from the
reference is reproduced in this repository; no arcade ROM file was acquired, opened, extracted, or
distributed in producing it. Attribution is not permission: this project claims no rights in Namco's
trademarks, artwork, or audio, and a rights review is required before broader distribution or promotion.
