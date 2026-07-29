# Credited sprite-sheet library

- Mechanic: Non-gameplay import of five arcade Xevious sprite sheets as a hidden costume library.
- Derived behavior: No arcade behavior changes; the imported sheets depict the title, Solvalou, ground enemies, Andor Genesis, and aerial enemies for later independently written Scratch work.
- Reference provenance: The five sheets and their credits are listed on https://www.spriters-resource.com/arcade/xevious/ and checksummed in `docs/ASSET_CREDITS.md`.
- Transfer class: Media and non-gameplay migration.
- Scratch interpretation: A hidden, scriptless `sprite_sheets` target holds the five unchanged PNG files as named costumes without affecting the current game loop.
- Scratch evidence: The hidden `sprite_sheets` target and the per-file records in `src/xevious/assets/provenance.json`.
- Acceptance criteria: The five supplied PNGs remain byte-identical, hidden, scriptless, credited, and behavior-neutral.
- Fidelity status: Non-gameplay media library.
- License status: No reusable license was supplied or stated on the source pages.
- Known deviations or uncertainty: The sheets remain unsliced with their green backgrounds and embedded credit panels; their source states no reusable license, so attribution is recorded without a permission claim.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
