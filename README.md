# Xevious

An unfinished 2017 Scratch interpretation of Namco's arcade game Xevious,
being restored through reviewable, reproducible changes.

Original public project: <https://scratch.mit.edu/projects/195680409/>

## Current controls

- Green flag: load the title screen
- Space: start, then fire the blaster
- Arrow keys: move the Solvalou
- B: drop a bomb
- D: trigger the existing death/restart demonstration

The current project is a proof of concept. It has scrolling terrain, movement,
weapons, music, a death animation, and restart behavior, but no enemies,
scoring, lives, progression, or win condition yet.

## Repository layout

- `assets/original/Xevious.sb3` — immutable historical archive and baseline
  asset store
- `src/xevious/project.json` — canonical, order-preserving Scratch structure
- `src/xevious/assets/` — only new or modified asset overlays, each with
  provenance
- `dist/Xevious.sb3` — generated playable build; ignored by Git
- `tools/scratch_project.py` — import, build, validation, and reproducibility
  boundary

## Build and validate

Python 3.12 is used in CI and no third-party Python packages are required.

```sh
python3 tools/scratch_project.py verify
python3 tools/scratch_project.py build
```

The build uses stored ZIP entries with fixed metadata, so identical source
produces identical output across supported systems.

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
the mechanic, evidence, and no-transfer attestation are present. Copy the
[mechanics record template](docs/mechanics/README.md), then check it locally:

```sh
python3 tools/check_mechanics_record.py origin/main
```

## Runtime comparison

For infrastructure changes, load both the preserved original and rebuilt
archive in Scratch 3 and TurboWarp and compare:

1. Green flag shows the title.
2. Space starts the level; music and terrain scrolling begin.
3. Arrow keys move within the frame.
4. Space fires the animated blaster.
5. B animates and sounds the bomb.
6. D plays the death animation and restarts.
7. Stop halts the project.
8. Reloading and pressing the green flag returns to the title state.

Record the tested commit, archive SHA-256 values, date, runtime versions or
dated web URLs, and each result in the pull request.

## Arcade reference boundary

The restoration targets the Namco arcade behavior. External reverse-engineered
implementations may identify mechanics to investigate, but their code, ROM
data, tables, graphics, audio, and converted assets are not copied. See
[the reference policy](docs/REFERENCE_POLICY.md).
