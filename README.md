# Xevious

A Scratch 3 port of Namco's 1983 arcade game Xevious, restored from an unfinished
2017 Scratch project through reviewable, reproducible changes. Its behaviour follows
a pinned public reconstruction of the arcade program (`jotd666/xevious`), cited
line by line; see [the reference policy](docs/REFERENCE_POLICY.md).

Original public project: <https://scratch.mit.edu/projects/195680409/>

## Controls

- Green flag: power on — the title, then the attract cycle (a demonstration
  game and the best-five scores)
- C: insert a coin (one credit, up to 99)
- Up / down arrows at the title: choose one or two players; Space: start
  (a two-player game takes two credits)
- Arrow keys: move the Solvalou
- Space: fire the Zapper at flying enemies
- B: drop a Blaster bomb on the ground target under the bomb sight
- Initials entry: up / down arrows change the letter, Space accepts it

The coin key, the two-player selector and the initials controls are this
port's stand-ins for cabinet hardware. READY, the player's explosion, the forest
after a death and GAME OVER accept no gameplay input. The green flag returns to
the title from any state; Stop halts everything.

## Release notes — release candidate (slice 21)

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

- `assets/original/Xevious.sb3` — immutable historical archive and baseline
  asset store
- `src/xevious/project.json` — canonical, order-preserving Scratch structure
- `src/xevious/assets/` — only new or modified asset overlays, each with
  provenance
- `docs/ASSET_CREDITS.md` — sources, credits, and license status for imported
  third-party media
- `docs/BUILD_PLAN.md` — ordered implementation slices and acceptance gates
- `docs/MECHANICS_CATALOG.md` — normal-game mechanic inventory and source
  locators
- `docs/SPRITE_EXTRACTION.md` — deterministic sprite-derivative design
- `assets/sprite-extraction/` — versioned crop manifest, schema, and generated
  source-to-derivative provenance
- `dist/Xevious.sb3` — generated playable build; ignored by Git
- `tools/scratch_project.py` — import, build, validation, and reproducibility
  boundary
- `tools/game_director.py` — deterministic slice-2 state/reset block generator
  and drift check
- `tools/sprite_extractor.py` — deterministic sprite extraction and generated
  output check
- `harness/` — headless scratch-vm runtime regression net; a pre-playtest
  tripwire with its own pinned Node/JS toolchain (not a gameplay gate)

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
Its npm packages are not watched by Dependabot (an accepted gap: the harness never
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

## After an Engine update

An Engine update replaces the files under `.engine/`, including one this project has
edited locally, so expect one Engine self-test to go red afterwards until the edit is
re-applied (StarshipSuperjam/xevious#164; it was handled once before in StarshipSuperjam/xevious#113).

- **What breaks:** `.engine/tools/test_conduct.py`, test
  `test_operator_override_ships_empty`. The stock test asserts that the operator's
  conduct file (`.engine/conduct/operator.md`) ships with no codes; this project adds
  its own code (check the arcade reference before trusting a claim about the game),
  so the stock assertion fails.
- **The fix to re-apply:** in that test, replace the stock line
  `self.assertEqual(validate.frontmatter(_OPERATOR).get("codes"), [])` with a check
  that the codes list is well-formed:

  ```python
  codes = validate.frontmatter(_OPERATOR).get("codes")
  self.assertIsInstance(codes, list)
  ```

  and keep the comment that points at the upstream issue.
- **The real fix** belongs in the Engine's home repository,
  StarshipSuperjam/engine-template#1200. Engine faults filed in this repository do not
  reach the Engine's home, so the edit stays a per-update chore until that issue ships.

## Arcade reference boundary

The restoration targets normal Namco arcade behavior. The pinned public
`jotd666/xevious` source snapshot may provide mechanics, constants, timing,
scores, formations, schedules, collision rules, and tables with exact
commit/file/label provenance. Scratch code is independently expressed; arcade
ROM files are not acquired, opened, extracted, or distributed. Separately
supplied third-party media requires per-file provenance and honest license
status. See [the reference policy](docs/REFERENCE_POLICY.md) and
[asset credits](docs/ASSET_CREDITS.md).
