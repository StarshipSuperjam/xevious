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
the screen edge, layer ordering, the post-death pause, and the truncated death cue) —
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

Each document's frontmatter `status` is authoritative: `draft` renders as *in progress* (the content is
complete or filling, not yet binding), `locked` renders as *settled* — the ground a build adapts to,
changeable afterwards only with the operator's guardrail acknowledgement — the `guardrail-ack` label you
yourself apply on the changing pull request, a deliberate confirmation separate from the merge click.
The table below mirrors the frontmatter. `reference_verified_at` in each document records the reference
commit its values were last verified against and must equal this file's `reference_pin` (a pin bump
re-verifies each document and updates its stamp deliberately). **Precedence:** where a settled document
and an in-progress document disagree, the settled one wins, and a change to an in-progress document that
would contradict a settled one is a spec amendment to the settled document, not a quiet roster edit.
Where a settled document delegates a detail to an in-progress one, the settled document's own statements
bind now; the delegated detail becomes binding when its document settles.

| Capability | Status | Doc |
| --- | --- | --- |
| Core game systems | settled | [Core game systems](core-game-systems.md) |
| Player craft and weapons | settled | [Player craft and weapons](player-craft-and-weapons.md) |
| Scoring, lives, and game over | settled | [Scoring, lives, and game over](scoring-lives-and-game-over.md) |
| Area progression and terrain | settled | [Area progression and terrain](area-progression-and-terrain.md) |
| Difficulty and formations | settled | [Difficulty and formations](difficulty-and-formations.md) |
| Aerial enemies | in progress | [Aerial enemies](aerial-enemies.md) |
| Ground objects | in progress | [Ground objects](ground-objects.md) |
| Secrets | in progress | [Secrets](secrets.md) |
| Andor Genesis | in progress | [Andor Genesis](andor-genesis.md) |
| Cabinet flow | in progress | [Cabinet flow](cabinet-flow.md) |
| Audio and presentation | in progress | [Audio and presentation](audio-and-presentation.md) |

The order this work gets built in is the [build order](build-plan.md) (a living document with no stage).

Deliberate exclusions (recorded so no build re-invents them): Super Xevious content — Galaxian, jet with its
score-reset trap, helicopter, tank, bridge, Super formations and schedules; port conveniences — pause and
persistent high-score storage; a conventional win screen. Sources: the mechanics catalog rows EX-01 through
EX-06.

## Generated data

Every file under [data/](data/) is emitted by `tools/reference_extract.py` from the pinned reference
commit (provenance, hashes, and attestations below). **None of these files is ever hand-edited** — to change one, change the extractor and regenerate; to verify them all, run:

```bash
python3 tools/reference_extract.py --verify --checkout <path-to-local-clone-at-the-pin>
```

To make the clone (substitute the pin from this file's frontmatter):

```bash
git clone https://github.com/jotd666/xevious.git /tmp/xevious-reference
git -C /tmp/xevious-reference checkout 71473685a8c7856c8401c8519276cd97a38d4183
```

The command needs that local clone, so it is run by a person, not CI; the
CI-runnable structural guards live in `tests/test_spec_docs.py`.

One file per concern:

| File | Owns | Described by |
| --- | --- | --- |
| `area-schedules.json` | All 16 normal area schedules, record by record, with the exact-consumption proof | [Area progression and terrain](area-progression-and-terrain.md) |
| `formations.json` | The flying-formation table, including its negative-index half | [Difficulty and formations](difficulty-and-formations.md) |
| `difficulty.json` | The four cabinet difficulty increments | [Difficulty and formations](difficulty-and-formations.md) |
| `terrain.json` | Per-area terrain start columns | [Area progression and terrain](area-progression-and-terrain.md) |
| `scores.json` | Master value table, starting lives, bonus thresholds and increments, high-score defaults | [Scoring, lives, and game over](scoring-lives-and-game-over.md) |
| `object-types.json` | The 93-code registry (handlers, names, schedule actions, Super flags) and the flying-enemy type table | [Core game systems](core-game-systems.md) |
| `andor-genesis.json` | The boss's fifteen-part layout list | [Andor Genesis](andor-genesis.md) |
| `domogram.json` | Domogram's 32-entry movement vector table | [Ground objects](ground-objects.md) |
| `rng.json` | The random generator's update rule and golden fixture sequences | [Core game systems](core-game-systems.md) |

New reference tables get their own file (or join the file whose concern they belong to) — never a
second copy of an existing one.

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

Attestations, carried by this specification as a whole: reference symbol names and line numbers appear
in this repository as citation locators and, in the object registry, as derived handler identifiers; no
assembly instructions, comments, or prose from the reference are reproduced; and no arcade ROM file was acquired, opened, extracted, or distributed in
producing it. The generated data files are documented in the Generated data section above. Attribution is not permission: this project claims no rights in Namco's
trademarks, artwork, or audio, and a rights review is required before broader distribution or promotion.
