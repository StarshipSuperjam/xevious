# Credited sprite-sheet library

- Mechanic: Non-gameplay import of five arcade Xevious sprite sheets as a hidden costume library.
- Observable arcade behavior: No arcade behavior changes; the imported sheets depict the title, Solvalou, ground enemies, Andor Genesis, and aerial enemies for later independently written Scratch work.
- Independent evidence: The five sheets and their credits are listed on https://www.spriters-resource.com/arcade/xevious/ and checksummed in `docs/ASSET_CREDITS.md`.
- Observation date: 2026-07-29.
- Scratch interpretation: A hidden, scriptless `sprite_sheets` target holds the five unchanged PNG files as named costumes without affecting the current game loop.
- Known deviations or uncertainty: The sheets remain unsliced with their green backgrounds and embedded credit panels; their source states no reusable license, so attribution is recorded without a permission claim.
- [x] No external code, ROM data, or lookup tables were transferred.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
