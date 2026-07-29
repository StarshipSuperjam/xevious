# Historical Scratch baseline

- Mechanic: Infrastructure-only migration of the 2017 historical baseline.
- Derived behavior: No arcade behavior is added in this migration; it preserves the 2017 Scratch project as found.
- Reference provenance: The guarded `assets/original/Xevious.sb3` archive at SHA-256 `3a870e4402d18027d26daa06c006be7ab9973f594558a282ac14b7ee032a274e`.
- Transfer class: Historical baseline.
- Scratch interpretation: `src/xevious/project.json` preserves every parsed value and object-key order from the historical archive.
- Scratch evidence: The canonical `src/xevious/project.json`, its referenced baseline assets, and `tools/scratch_project.py verify`.
- Acceptance criteria: Building the preserved source is deterministic and retains the baseline Scratch structure.
- Fidelity status: Non-gameplay structural baseline.
- License status: The project makes no reusable-license claim for the historical Namco-derived media.
- Known deviations or uncertainty: Interactive parity remains to be checked in Scratch 3 and TurboWarp; this record proves structural preservation only.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.

This record establishes the starting point. Later gameplay changes need their
own changed record describing the mechanic and its reference provenance.
