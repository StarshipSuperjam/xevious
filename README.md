# Xevious

A Scratch 3 port of Namco's 1983 arcade game Xevious, restored from an unfinished
2017 Scratch project through reviewable, reproducible changes. Its behaviour follows
a pinned public reconstruction of the arcade program (`jotd666/xevious`), cited
line by line; see [the reference policy](docs/REFERENCE_POLICY.md).

**Status: complete.** The whole normal arcade game is built (slice 21 of the
[build plan](docs/BUILD_PLAN.md) was the last). The project is finished and
archived as a release: nothing is planned beyond it.

## Play it

- **Online:** <https://scratch.mit.edu/projects/195680409/> (the Scratch project page the
  2017 original was first shared on).
- **From this repository:** [`release/Xevious.sb3`](release/Xevious.sb3) is the finished
  game, built from the source here. Open it in
  [Scratch 3](https://scratch.mit.edu/projects/editor/) (File ▸ Load from your computer)
  or [TurboWarp](https://turbowarp.org/). It is also attached to the GitHub release.

  SHA-256: `8003492e3d4ef783610bd0280d37ab76ce48e6bb5117018e43b9327693fc58e3`

The committed file is checked against the source by a test (`tests/test_release_build.py`),
so it cannot drift. To rebuild it yourself (Python 3.12, nothing else needed):

```sh
python3 tools/scratch_project.py build                                  # writes dist/Xevious.sb3
python3 tools/playtest_package.py --output release/Xevious.sb3          # same, after the reference checks
```

The build is deterministic: identical source gives a byte-identical file.

## For a reviewer: where to look

| To see… | Start at |
| --- | --- |
| What the game is, and how it was proved faithful | [`docs/spec/index.md`](docs/spec/index.md), [`docs/REFERENCE_POLICY.md`](docs/REFERENCE_POLICY.md) |
| Every deliberate departure from the arcade, and why | [`docs/MECHANICS_CATALOG.md`](docs/MECHANICS_CATALOG.md) → [`docs/mechanics/`](docs/mechanics/) |
| How the build is structured and verified | [`docs/architecture.md`](docs/architecture.md), [`docs/principles.md`](docs/principles.md) |
| Where every sprite, sound and image came from | [`docs/ASSET_CREDITS.md`](docs/ASSET_CREDITS.md) |
| How to check it by playing | [`docs/PLAYTEST_CHECKLIST.md`](docs/PLAYTEST_CHECKLIST.md) |
| How the work was sequenced | [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md), [`docs/roadmap/`](docs/roadmap/) |
| The pre-release fidelity audit | [`docs/audit/`](docs/audit/) |

## Controls

- Green flag: power on — the title, then the attract cycle (a demonstration
  game and the best-five scores)
- C: insert a coin (one credit, up to 99)
- Up / down arrows at the title: choose one or two players; Space: start
  (a two-player game takes two credits)
- Arrow keys: move the Solvalou
- Space: fire the Zapper at flying enemies
- B: drop a Blaster bomb on the ground target under the bomb sight
- Initials entry: up / down arrows change the letter, Space accepts it; hold B
  for lowercase (a space entered with B held becomes a full stop)

The coin key, the two-player selector and the initials controls are this
port's stand-ins for cabinet hardware. READY, the player's explosion, the forest
after a death and GAME OVER accept no gameplay input. The green flag returns to
the title from any state; Stop halts everything.

## Release notes — final release

The whole normal arcade game is built:

- **The campaign.** Areas 1 to 16 from the arcade's own map and object
  schedules, then the loop back to area 7; there is no win screen, as in the
  arcade.
- **Enemies.** Every normal flying enemy (Toroid, Torkan, Zoshi, Jara, Kapi,
  Terrazi, the Zakato line, the Sparios, Bacura, Sheonite) and every ground
  object (Barra, Zolbak, Logram, Derota, Boza Logram, Grobda, Domogram), with
  the arcade's formations, adaptive difficulty and fire timing.
- **Andor Genesis**, with its gun ports, Bragza and core.
- **Secrets.** Sol Tower, the Special Flag and the hidden credit.
- **Cabinet.** Coins and credits, one and two players, scoring and bonus
  craft, the best-five table with initials entry, the attract cycle, and the
  arcade's sounds.
- **Presentation.** Sprites, terrain, title logo and HUD rendered from the
  arcade's own graphics data at the pin, framed by the cabinet bezel.

What this release adds over slice 20: the area schedules' `add_object` records
now place their objects (the Special Flag in areas 1, 3, 5 and 7, and the Garu
Zakato in areas 9, 10 and 14 among them); the world keeps running through the
player's explosion; the forest wait after the last death; the arcade's own
difficulty divisor and bonus-craft thresholds; the Brag Spario surviving shots;
the Zakato teleport scatter; the enemy, shot, sparkle and title-logo art; and
the HUD's blank leading zeros, 1UP flash and two-row banner. A whole-campaign
soak and a media audit now run in CI.

Every deliberate departure from the arcade, and why, is recorded under
[`docs/mechanics/`](docs/mechanics/), indexed by the
[mechanics catalog](docs/MECHANICS_CATALOG.md). Media sources, credits and
rights status are in [the asset credits](docs/ASSET_CREDITS.md): the arcade
graphics and sounds are Namco's, and no reusable license is claimed for them.

The dependency-ordered work is in the [build plan](docs/BUILD_PLAN.md).

## Repository layout

```
src/xevious/            canonical, order-preserving Scratch source
  project.json            the game's blocks, sprites and variables
  assets/                 only new or modified media, each with provenance
  runtime_identifiers.json  stable Scratch ids used by the generators
assets/
  original/               immutable 2017 archive (Xevious.sb3) and its provenance
  sprite-extraction/      crop manifest and generated source-to-derivative provenance
  hud-font/  bezel/       HUD font sheet and cabinet bezel, with provenance
  game-sounds/ hud-sounds/ sound cues, with manifests
  terrain/                area-map and terrain images, with provenance
tools/                  Python build, generation and verification tools (see below)
tests/                  unittest suite for the tools and the built project
harness/                headless scratch-vm runtime regression net (Node)
docs/
  spec/                   the product spec, one document per capability
  mechanics/              one record per behaviour change, and the mechanics README
  roadmap/                the dependency-ordered plan as data (historical)
  adr/  audit/  images/   decision record, fidelity audit, figures
  *.md                    architecture, principles, policy, credits, checklists
release/                the finished game, release/Xevious.sb3
dist/                   scratch build output; ignored by Git
```

The main tools, all under `tools/`:

- `scratch_project.py` — import, build, validation and reproducibility boundary
- `game_director.py` — deterministic block generator for the state/reset logic, with a drift check
- `sprite_extractor.py`, `andor_sprite_render.py`, `effects_sprite_render.py`,
  `reference_art_render.py`, `sol_tower_render.py`, `terrain_render.py`, `hud_glyphs.py`,
  `bezel_panels.py` — deterministic art generators that render from the arcade's own graphics data
- `reference_checkout.py`, `reference_extract.py`, `reference_citations.py` — fetch the pinned
  arcade source, re-derive the generated data from it, and resolve every citation
- `playtest_package.py` — the one way to produce a playtest build (runs the reference checks first)
- `check_mechanics_record.py`, `script_layout.py` — repository checks used by CI
- `roadmap.py`, `check_roadmap_closures.py` — the historical roadmap tooling (see `docs/roadmap/`)

## Build and validate

Python 3.12 is used in CI and no third-party Python packages are required.

```sh
python3 tools/sprite_extractor.py check
python3 tools/game_director.py check
python3 tools/scratch_project.py verify
python3 tools/scratch_project.py build
```

The pinned arcade reference is verified against a local clone (fetched once,
outside the repository):

```sh
REF="$(python3 tools/reference_checkout.py ensure)"
python3 tools/reference_extract.py --verify --checkout "$REF"
python3 tools/reference_citations.py --checkout "$REF"
```

A build for the operator's playtest is produced only through the handover tool,
which runs those reference checks first and refuses to emit a build while any
citation fails to resolve:

```sh
python3 tools/playtest_package.py
```

The build uses stored ZIP entries with fixed metadata, so identical source
produces identical output across supported systems.

The runtime harness under `harness/` is a separate Node/JavaScript toolchain —
its one dependency is a pinned `scratch-vm` with a committed lockfile. It builds
the `.sb3` and runs headless scenarios (and their negative fixtures) against the
game's logic layer:

```sh
harness/run.sh   # or: cd harness && npm ci --ignore-scripts && node --test
```

It is a pre-playtest regression tripwire that observes internal state only, never
the game on screen — not a gameplay gate. See [`harness/README.md`](harness/README.md).
No update bot watches its npm packages (an accepted gap: the harness never
ships in the `.sb3`); a `scratch-vm` bump is made by hand and needs the full harness
run, since the harness's results reflect the VM it pins. The reasoning is in
[`docs/architecture.md`](docs/architecture.md).

When the sprite manifest changes, regenerate its costumes, provenance, Scratch
costume records, and review contact sheet, then run both checks:

```sh
python3 tools/sprite_extractor.py generate
python3 tools/sprite_extractor.py check
python3 tools/scratch_project.py verify
```

## Bring visual-editor changes back into Git

Always start the editor from the current generated build, not the historical
archive or public Scratch project:

1. Confirm `src/xevious/` has no uncommitted work.
2. Run `python3 tools/scratch_project.py build`.
3. Load `dist/Xevious.sb3` in Scratch 3 or TurboWarp.
4. Edit, then export a new `.sb3`.
5. Import that export through the guarded boundary.
6. Add or update its mechanics record.
7. Run `python3 tools/scratch_project.py verify` and review the Git diff.

```sh
python3 tools/scratch_project.py import path/to/edited.sb3 --force
```

`--force` authorizes replacing the existing canonical source, but the importer
still refuses when `src/xevious/` has visible uncommitted work. Commit or stash
first. Every successful replacement also retains the complete prior source
tree under ignored `dist/import-backups/` and prints its path, covering local
files Git may not report.

For slice 2, `tools/game_director.py` is the exclusive source of truth for the
block maps of Stage, Solvalou, blaster, bomb, both terrain sprites, both target
sprites, the title sprite, and the death sprite. After importing an editor
export, commit the recoverable import before doing anything else, inspect its
diff, and port intended block changes for those targets into the generator.
`generate` refuses to run while `src/xevious/project.json` has staged or
unstaged edits, so it cannot silently erase a fresh editor import. Costumes,
sounds, target properties, and blocks on other sprites remain editor-owned.

If the export adds or changes media, also provide its origin and license:

```sh
python3 tools/scratch_project.py import path/to/edited.sb3 --force \
  --asset-origin "Created for this project" \
  --asset-license "CC0-1.0"
```

That shorthand applies one origin and license to every new media file. For
mixed sources or licenses, pass `--asset-provenance path/to/provenance.json`
instead. The file uses the same version 1 shape as
`src/xevious/assets/provenance.json`, with one `origin` and `license` record
for each asset filename the import reports.

The importer accepts PNG, WAV, MP3, and sanitized SVG media. SVG scripts,
event handlers, embedded content, and external references are rejected.

Importing preserves the relative order of existing block-map entries because
Scratch uses that order when scheduling top-level scripts. New blocks are
appended in the editor's order.

Any change to `src/xevious/project.json` must also add or update a structured
record under `docs/mechanics/`. The required project check enforces that
the mechanic, provenance, implementation evidence, and attestations are
present. Copy the
[mechanics record template](docs/mechanics/README.md), then check it locally:

```sh
python3 tools/check_mechanics_record.py origin/main
```

## Playtest

Every change that can affect gameplay is played in Scratch 3 before it merges,
through the ordered sweep in [the playtest checklist](docs/PLAYTEST_CHECKLIST.md),
on a build made by `python3 tools/playtest_package.py`. Record the tested commit,
the build's SHA-256, the date, and each result in the pull request.

## Arcade reference boundary

The restoration targets normal Namco arcade behavior. The pinned public
`jotd666/xevious` source snapshot may provide mechanics, constants, timing,
scores, formations, schedules, collision rules, and tables with exact
commit/file/label provenance. Scratch code is independently expressed; arcade
ROM files are not acquired, opened, extracted, or distributed. Separately
supplied third-party media requires per-file provenance and honest license
status. See [the reference policy](docs/REFERENCE_POLICY.md) and
[asset credits](docs/ASSET_CREDITS.md).
