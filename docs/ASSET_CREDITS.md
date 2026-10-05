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
three Solvalou frames and, from the same Aerial Enemies sheet, seven Toroid
frames, seven Terrazi roll frames, seven Kapi dive frames, six Torkan roll
frames, and four Zoshi spin frames. The standard-library generator removes
every `(0, 128, 0)` matte pixel (enclosed regions included — interior negative
space that shows the game background through),
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

## Arcade gameplay sound effects (Sounds Spriters Resource)

Six of the arcade's gameplay sound effects, supplied by the repository operator
from local staging and committed here byte-for-byte. Each is attached unmodified
as a Stage sound by `tools/hud_glyphs.py` under its content-hash filename in
`src/xevious/assets/`, and played at the arcade play point cited in
`docs/mechanics/040-arcade-sound-cues.md`. The provenance manifest is
`assets/game-sounds/manifest.json`.

| Supplied file | Cue (arcade sound) | Plays when | License | SHA-256 |
| --- | --- | --- | --- | --- |
| `air_destroy.wav` | Flying-enemy hit (`FLYING_ENEMY_HIT_SND`, 0x05) | A shot destroys a flying enemy | No reusable license specified by source; third-party copyrighted material | `148f712ea61692a6feb45b20e25ca20a5416c5328a83e1e58245a1621c3765e9` |
| `ground_destroy.wav` | Ground explosion (`GROUND_EXPLOSION_SND`, 0x11) | A ground target is destroyed | No reusable license specified by source; third-party copyrighted material | `e2058ecc5e0ba28f7893b1c4fb8128853e1308e6820b3b0f75ba98ce4d53924a` |
| `zakato.wav` | Teleport (`TELEPORT_SND`, 0x09) | A Zakato / Brag Zakato teleports in | No reusable license specified by source; third-party copyrighted material | `e3fc46a9810c18dfc8a7162b5495d88a77bbdc50ce195d418e45d598f60d2035` |
| `garu_zakato.wav` | Garu Zakato (`GARU_ZAKATO_SND`, 0x06) | A Garu Zakato detonates | No reusable license specified by source; third-party copyrighted material | `782ba3a40112a60c55999d1dacfcd4ec014b964208c9d66bf9c7b7830e6b8a35` |
| `bacura.wav` | Bacura hit (`BACURA_HIT_SND`, 0x0a) | A shot bounces off a Bacura slab | No reusable license specified by source; third-party copyrighted material | `87ec0bcf2b770f94de03c7dc90e375bf20318d3abab9f868df842777627badcd` |
| `sheonite.wav` | Sheonite retreat (`SHEONITE_SND`, 0x08) | The right Sheonite peels off and retreats | No reusable license specified by source; third-party copyrighted material | `e78543787183a8d7b34740255b7a02c08b319a954e9240acab6273c50a6f7bd4` |

Each raw wav is committed byte-for-byte to `src/xevious/assets/` under its
content-hash filename and to `assets/game-sounds/` under the readable name above.
No individual contributor credit was listed, and the exact upstream page was not
recorded, so no per-file URL is asserted; the family matches the Sounds Spriters
Resource Xevious audio (asset 449687). These carry the same rights-status caveat
as the material above: recording provenance is not a claim that credit grants
permission, this is Namco copyrighted audio, and no ownership is claimed. Several
other staged wavs duplicate sounds already in the base project (music,
game-start, player-death, blaster fire) and are intentionally left on those base
sounds rather than re-committed.

## Terrain rendered from the arcade map data

The in-game terrain (slice 20, AREA-01, `docs/mechanics/015-area-clock.md`) is
rendered by `tools/terrain_render.py` from the pinned arcade reference
(`jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`): the background
map ROMs transcribed in `src/map_rom.68k` and the background tiles, colour
tables and palette in `assets/amiga/xevious_gfx.c`. The tool writes two
committed source pictures, `assets/terrain/arcade_map.png` (the whole map, one
pixel per arcade pixel) and `assets/terrain/forest_filler.png` (the forest
pattern a life begins over); `render --verify` re-derives both byte-for-byte.
`terrain_render.py generate` slices them into the six terrain costumes on the
`area_01a` / `area_01b` strips, written under their content-hash filenames in
`src/xevious/assets/`. Input hashes, output hashes and the credit are in
`assets/terrain/provenance.json` and `src/xevious/assets/provenance.json`.

| Output | Description | Credit | Source | License |
| --- | --- | --- | --- | --- |
| `arcade_map.png`, `forest_filler.png`, six strip costumes | Xevious background map and forest filler | Namco (Xevious, 1983); transcription and tile conversion by jotd666 | [jotd666/xevious](https://github.com/jotd666/xevious) at the pin | No reusable license specified by source; third-party copyrighted material |

This is the same class as the arcade sprites rendered from the reference for
the Andor parts: recording provenance is not a claim that credit grants
permission, this is Namco's copyrighted game content, no ownership is claimed,
and a rights review is needed before broader distribution
(`docs/REFERENCE_POLICY.md`).

## Terrain area map (fan map, cross-check only — not used by the build)

Operator-supplied source art, committed earlier for the terrain slice. The build
renders the terrain from the arcade's own map data instead (above), so no
generator reads this image: it produces no `src/xevious/assets/` costume and has
no effect on `project.json`. It is kept as a visual cross-check of the rendered
map; removing it is the operator's call.

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

## Cabinet bezel artwork (MAME Realistic Bezel Artwork)

Operator-approved cabinet bezel art for the arcade-screen framing (slice 20,
PRES-01, `docs/mechanics/054-arcade-screen-proportions.md`). The source image is
committed byte-for-byte at `assets/bezel/xevious_bezel.png`, with its source
record in `assets/bezel/manifest.json`. `tools/bezel_panels.py` box-averages
its two side columns into opaque panels and writes one full-stage costume (a
panel either side of the transparent arcade-screen window) under its
content-hash filename in `src/xevious/assets/`. Its generated outputs are
recorded in `assets/bezel/provenance.json`.

| Supplied file | Description | Credit | Source | License | SHA-256 |
| --- | --- | --- | --- | --- | --- |
| `xevious_bezel.png` | Xevious cabinet bezel (side panels used) | estefan3112, adapting bezel artwork by John Merrit and Orionsangel, derived from Namco's Xevious cabinet art | [estefan3112/MAME-Realistic-Bezel-Artwork](https://github.com/estefan3112/MAME-Realistic-Bezel-Artwork) (`mame/xevious/xevious_bezel.png`) | No reusable license for the artwork (the repository's GPL-3.0 label cannot license third-party fan art of Namco's copyrighted cabinet art); third-party copyrighted material | `dca729dee359ea454b03b064d5202b3203559d6403e132921adf292a1c2142d6` |

This carries the same rights-status caveat as the material above. Recording the
attribution is not a claim that credit grants permission. This is fan artwork
derived from Namco's copyrighted cabinet art, and it needs a rights review
before broader distribution (`docs/REFERENCE_POLICY.md`).
