# Third-party asset credits

The repository operator did not create the five sprite sheets imported in this
change. Credit for the collection belongs with
[The Spriters Resource Xevious page](https://www.spriters-resource.com/arcade/xevious/).
The individual sheets retain their embedded credit panels.

| Supplied file | Sheet | Sheet credit | Source | SHA-256 |
| --- | --- | --- | --- | --- |
| `168901.png` | Logo & Title Screen | StarmanElite | [Asset 168901](https://www.spriters-resource.com/arcade/xevious/asset/168901/) | `c8b88f131701e4db2d79284eafda2f5fea7589b412ed47a3373b3e78811c42a0` |
| `42384.png` | Solvalou | CrazyCarl | [Asset 42384](https://www.spriters-resource.com/arcade/xevious/asset/42384/) | `0c88cd5cb440bebcc59aeeb20d8e141f62a5be4f4ff607be06a72ae1b8afdeaf` |
| `42385.png` | Ground Enemies | CrazyCarl | [Asset 42385](https://www.spriters-resource.com/arcade/xevious/asset/42385/) | `bfcb48cb942c959bfcf482f86dca7c9a98f36d58913fb09133ee6529f0c566cf` |
| `42386.png` | Andor Genesis | CrazyCarl | [Asset 42386](https://www.spriters-resource.com/arcade/xevious/asset/42386/) | `4ca80d9f5d8894c86d5557cafaf8b5fb8dff368c69ec36f16cbde69dd3891d68` |
| `42387.png` | Aerial Enemies | CrazyCarl | [Asset 42387](https://www.spriters-resource.com/arcade/xevious/asset/42387/) | `0cd8361108354d74c2ea9bfa9e22836acc66158c963eafdc5a02c9021f5b9da8` |

## Rights status

No reusable license was supplied with these files or stated on their source
pages. This project records attribution and provenance without claiming that
credit alone grants permission. It does not claim ownership of, or grant
rights to, the Xevious artwork or trademarks. A rights review is needed before
broader distribution or promotion.

The sheets are stored byte-for-byte as supplied, including their green
backgrounds and embedded credit panels. They remain available on the hidden
`sprite_sheets` target.

## Gameplay-ready derivatives

The versioned manifest in `assets/sprite-extraction/manifest.json` measures
three Solvalou frames, seven Toroid frames, and seven Terrazi roll frames from
the credited sheets (the Terrazi frames from the same Aerial Enemies sheet as
the Toroid). The standard-library generator removes only edge-connected
`(0, 128, 0)` matte,
places every frame on a native 16×16 RGBA canvas, and records the exact source
hash, rectangle, canvas, anchor, credit, and license status in
`assets/sprite-extraction/provenance.json`. Scratch copies of the same records
live in `src/xevious/assets/provenance.json`.

The generated review contact sheet at
`docs/images/sprite-extraction-proof.png` is also a derivative of the credited
artwork. It exists for crop, transparency, and anchor review and carries the
same no-reusable-license-specified status as its sources.

## HUD font (Creative Commons Attribution 3.0)

Unlike the five Spriters Resource sheets above, the HUD digit/letter glyphs
are sourced from a font released under a stated reusable license:

| Supplied file | Description | Credit | Source | License | SHA-256 |
| --- | --- | --- | --- | --- | --- |
| `xevious_hud_font.png` | Xevious HUD font recreation, digit/letter sheet | Patrick H. Lauke (FontStruct), reshared by AnthonyCassimiro | [FontStruct project](https://fontstruct.com/), reshared on [DeviantArt](https://www.deviantart.com/anthonycassimiro/art/Xevious-HUD-font-recreation-1345048685) | Creative Commons Attribution 3.0 (CC BY 3.0) | `87095dc731a54115850ce3509de70380f7707dbc977d0efa0df08b89a057da56` |

The raw sheet is committed byte-for-byte at `assets/hud-font/xevious_hud_font.png`.
The versioned manifest at `assets/hud-font/manifest.json` measures the digit
and uppercase-letter crop rectangles used by `tools/hud_glyphs.py`, which
removes the white background and recolors the ink — to **white** for the score,
high-score, 1UP, and GAME OVER glyphs, and to **yellow** (RGB 255,255,0) for the
`hs/*` glyphs of the arcade's yellow **HIGH SCORE** label — then centers each
glyph on a fixed monospace cell and downscales it with nearest-neighbor sampling.
Both colour variants are recorded in `src/xevious/assets/provenance.json` with
this same CC BY 3.0 attribution.

The license deed is [Creative Commons Attribution 3.0](https://creativecommons.org/licenses/by/3.0/).
Attribution is given per its terms. Two provenance caveats, both for the
standing rights review before any broader distribution (`docs/REFERENCE_POLICY.md`):
the CC BY 3.0 grant is read from the DeviantArt re-share and the FontStruct
project home, not the specific FontStruct fontstruction page that states it — the
exact page and license should be confirmed at that review; and the CC grant covers
the FontStruct author's recreation as an expression, while the depicted HUD glyph
*design* derives from Namco's arcade game and remains Namco's. This project does
not otherwise claim rights to the font.

## Extend / 1UP sound (Sounds Spriters Resource)

| Supplied file | Description | Source | License | SHA-256 |
| --- | --- | --- | --- | --- |
| `extend.wav` | Extend (1UP / bonus life) cue | [Sounds Spriters Resource, Xevious (Arcade), asset 449687](https://sounds.spriters-resource.com/arcade/xevious/asset/449687/) | No reusable license specified by source; third-party copyrighted material | `ab3ff92caa592770628efa30d415d4c68e0153a6617d0a209b6179502c9930a4` |

The raw wav is committed byte-for-byte to `src/xevious/assets/` under its
content-hash filename and attached as a new Stage sound named `extend`
(`tools/hud_glyphs.py`). No individual contributor credit was listed on the
asset's source page. This carries the same rights-status caveat as the
Spriters Resource sprite sheets above: recording provenance is not a claim
that credit grants permission, and no ownership of the Xevious audio is
claimed.

## Terrain area map (reference source, not yet ingested)

Operator-supplied source art for the upcoming terrain/area slice (Part of #17).
It is committed as a source only — no generator ingests it yet, so it produces
no `src/xevious/assets/` overlay costume and no `project.json` change. When the
terrain extractor lands it will read this source and record its extracted
outputs, mirroring the sprite-extraction and HUD-font pipelines above.

| Supplied file | Description | Credit | Source | License | SHA-256 |
| --- | --- | --- | --- | --- | --- |
| `xevious_area_map.png` | Xevious area map (terrain reference source) | ringostarr39 (DeviantArt) | [DeviantArt](https://www.deviantart.com/ringostarr39/art/Xevious-area-map-626303469) | No reusable license specified by source; third-party copyrighted material | `4d5b5270f171053c5e88be3f0c1a9cf7933e819f3e883b1660ad783bb54f3c5f` |

The raw image is committed byte-for-byte at `assets/terrain/xevious_area_map.png`
with its provenance in `assets/terrain/provenance.json`. It carries the same
rights-status caveat as the material above: recorded attribution is not a claim
that credit grants permission, this is a fan-made map of Namco's Xevious, and a
rights review is needed before broader distribution (`docs/REFERENCE_POLICY.md`).

**Cited but not vendored.** A related fan-annotated slicing guide (the 16-area
breakdown and some hidden-target locations,
[arcadeblogger.com Xevious journey map](https://i0.wp.com/arcadeblogger.com/wp-content/uploads/2022/12/xevious-journey-map.jpeg))
is *reference*, not a build input, so it is cited here and deliberately kept
local (git-ignored) rather than committed.
