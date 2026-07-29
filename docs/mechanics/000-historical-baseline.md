# Historical Scratch baseline

- Mechanic: Infrastructure-only migration of the 2017 historical baseline.
- Observable arcade behavior: No arcade behavior is added in this migration; it preserves the 2017 Scratch project as found.
- Independent evidence: The guarded `assets/original/Xevious.sb3` archive at SHA-256 `3a870e4402d18027d26daa06c006be7ab9973f594558a282ac14b7ee032a274e`.
- Observation date: 2026-07-28.
- Scratch interpretation: `src/xevious/project.json` preserves every parsed value and object-key order from the historical archive.
- Known deviations or uncertainty: Interactive parity remains to be checked in Scratch 3 and TurboWarp; this record proves structural preservation only.
- [x] No external code, ROM data, lookup tables, graphics, or audio were transferred.

This record establishes the starting point. Later gameplay changes need their
own changed record describing the observed Namco arcade mechanic and the
independent evidence used.
