# Gameplay sprite extraction proof

- Mechanic: Deterministic conversion of credited sprite-sheet regions into gameplay-ready Scratch costumes.
- Derived behavior: Three Solvalou flight frames and seven Toroid turn frames become transparent, native-resolution costumes with stable centers; this slice does not add enemy behavior.
- Reference provenance: `docs/SPRITE_EXTRACTION.md`, `assets/sprite-extraction/manifest.json`, and the credited Solvalou and Aerial Enemies sheets listed in `docs/ASSET_CREDITS.md`.
- Transfer class: Media and non-gameplay migration.
- Scratch interpretation: The existing `solvalou` target gains three flight costumes, and a hidden scriptless `toroid_sprite_proof` family target holds seven turn costumes for the later air-combat slice.
- Scratch evidence: `solvalou` and `toroid_sprite_proof` in `src/xevious/project.json`; generated PNGs and records in `src/xevious/assets/`; `tests/test_sprite_extractor.py`.
- Acceptance criteria: Two generator runs are byte-identical; source sheets retain their hashes; each derivative is transparent RGBA on a 16×16 canvas at anchor 8,8; no crop touches a declared label or credit region; and the canonical Scratch source validates.
- Fidelity status: Non-gameplay extraction proof.
- License status: No reusable license was supplied or stated for the source sheets; derivatives retain that status.
- Known deviations or uncertainty: The costumes are integrated but not selected by gameplay scripts in this slice; Toroid timing and behavior belong to the later air-combat slice.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
