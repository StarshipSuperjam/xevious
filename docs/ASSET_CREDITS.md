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
backgrounds and embedded credit panels. Four of them remain available on the
hidden `sprite_sheets` target. The fifth, `42386.png` (Andor Genesis), no longer
ships: it shows only assembled octagons, which cannot be cut into the boss's
separable parts, so the parts are rendered from the arcade sprite data instead
(below). It stays credited here as supplied source art.

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
this same CC BY 3.0 attribution. The same glyphs, composited by `tools/hud_glyphs.py`,
also draw the title and attract-screen text, the best-five table, initials entry,
the two-player banner and the hidden credit (SEC-03); each of those costumes
carries the same attribution in its provenance record.

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

The arcade's gameplay sounds, committed here byte-for-byte. Each is attached
unmodified as a Stage sound by `tools/hud_glyphs.py` under its content-hash
filename in `src/xevious/assets/`, and played at the arcade play point cited in
`docs/mechanics/040-arcade-sound-cues.md` (the first six),
`docs/mechanics/044-secrets.md` (the Bonus Flag) and
`docs/mechanics/055-presentation-fidelity.md` (the slice-20 CAB-05 cues). The
provenance manifest is `assets/game-sounds/manifest.json`. All but
`bonus_flag.wav` were supplied by the repository operator from local staging;
`bonus_flag.wav` is the pinned reference's own named source sound
(`jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`,
`assets/sounds/bonus_flag.wav`).

| Supplied file | Cue (arcade sound) | Plays when | License | SHA-256 |
| --- | --- | --- | --- | --- |
| `air_destroy.wav` | Flying-enemy hit (`FLYING_ENEMY_HIT_SND`, 0x05) | A shot destroys a flying enemy | No reusable license specified by source; third-party copyrighted material | `148f712ea61692a6feb45b20e25ca20a5416c5328a83e1e58245a1621c3765e9` |
| `ground_destroy.wav` | Ground explosion (`GROUND_EXPLOSION_SND`, 0x11) | A ground target is destroyed | No reusable license specified by source; third-party copyrighted material | `e2058ecc5e0ba28f7893b1c4fb8128853e1308e6820b3b0f75ba98ce4d53924a` |
| `zakato.wav` | Teleport (`TELEPORT_SND`, 0x09) | A Zakato / Brag Zakato teleports in | No reusable license specified by source; third-party copyrighted material | `e3fc46a9810c18dfc8a7162b5495d88a77bbdc50ce195d418e45d598f60d2035` |
| `garu_zakato.wav` | Garu Zakato (`GARU_ZAKATO_SND`, 0x06) | A Garu Zakato detonates | No reusable license specified by source; third-party copyrighted material | `782ba3a40112a60c55999d1dacfcd4ec014b964208c9d66bf9c7b7830e6b8a35` |
| `bacura.wav` | Bacura hit (`BACURA_HIT_SND`, 0x0a) | A shot bounces off a Bacura slab | No reusable license specified by source; third-party copyrighted material | `87ec0bcf2b770f94de03c7dc90e375bf20318d3abab9f868df842777627badcd` |
| `sheonite.wav` | Sheonite retreat (`SHEONITE_SND`, 0x08) | The right Sheonite peels off and retreats | No reusable license specified by source; third-party copyrighted material | `e78543787183a8d7b34740255b7a02c08b319a954e9240acab6273c50a6f7bd4` |
| `bonus_flag.wav` | Bonus Flag (`BONUS_FLAG_SND`, 0x0d) | A revealed Bonus Flag is collected | No reusable license specified by source; third-party copyrighted material | `9840624569987af8022e8fb09c73de63298c7dfe6f97d4dee5767445975678f2` |
| `credit.wav` | Coin (`COIN_SND`, 0x10) | A coin adds a credit (never at the 99 cap) | No reusable license specified by source; third-party copyrighted material | `92f8e4ac27118cd71e83b052ea32f9ef7b9ce772134a880fb658855317e525d5` |
| `name_entry_top.wav` | Highest score (`HIGHEST_SCORE_SND`, 0x02) | Initials entry opens for a new first place (loops) | No reusable license specified by source; third-party copyrighted material | `e95e0990f536743356f421b2a27148789e4063f858ebe5bf12c45dae852310ea` |
| `name_entry.wav` | High score (`HIGH_SCORE_SND`, 0x03) | Initials entry opens for a lower rank (loops) | No reusable license specified by source; third-party copyrighted material | `c0f855a8fe1220a0dd1d4ac268a78f794028c574a2be16f3c9735a68cee19eb2` |
| `andor_genesis.wav` | Andor Genesis (`ANDOR_GENESIS_SND`, 0x07) | The Andor Genesis descends, hovers or leaves | No reusable license specified by source; third-party copyrighted material | `1fb855ba2ffec4a3a5979b8a88c9880656858a6f5e836251ae48b31f55392104` |
| `start.wav` | Main theme (`MAIN_THEME_SND`, 0x01) | Every life starts | No reusable license specified by source; third-party copyrighted material | `2e5bff2e4c3c8bb64450775188b412847d793f76be53d28939bf958ca18d5fe0` |
| `bgm.wav` | Flight tune (`SOLVALOU_SND`, 0x0e) | Loops in play after the theme; stops at death | No reusable license specified by source; third-party copyrighted material | `cef95196fda166d09cbafc52f48eb8bf509a1fbaa889579d2a44d99f0435ef10` |
| `solvalou_explode.wav` | Solvalou explosion (`SOLVALOU_EXPLOSION_SND`, 0x12) | The Solvalou is destroyed | No reusable license specified by source; third-party copyrighted material | `ae5fdf7442b6e70603277d84e5d8ab013358d6ec03186061a22c663cb8c959ff` |
| `zapper_fire.wav` | Shot (`SHOT_SND`, 0x0b) | A Zapper shot is fired | No reusable license specified by source; third-party copyrighted material | `b04dcbaf56b3956332d052dfffb097d2265f5403323fcef1f1c38bc2ed9504fe` |
| `blaster_fire.wav` | Bomb (`BOMB_SND`, 0x0c) | A Blaster bomb is dropped | No reusable license specified by source; third-party copyrighted material | `c6c88e4b3c4d0ff939748a775119334621b92f080babdfb50027a74ad04edef6` |

Each raw wav is committed byte-for-byte to `src/xevious/assets/` under its
content-hash filename and to `assets/game-sounds/` under the readable name above.
For the operator-supplied files no individual contributor credit was listed,
and the exact upstream page was not recorded, so no per-file URL is asserted;
the family matches the Sounds Spriters Resource Xevious audio (asset 449687).
These carry the same rights-status caveat as the material above: recording
provenance is not a claim that credit grants permission, this is Namco
copyrighted audio, and no ownership is claimed. Slice 20 (CAB-05) replaced the
base project's own music, game-start, player-death, shot and bomb sounds with
`start`, `bgm`, `solvalou_explode`, `zapper_fire` and `blaster_fire`; the
replaced baseline sounds stay in the project unplayed, as preserved history.

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

## Sol Tower rendered from the arcade sprite data

The Sol Tower's seven rise frames (slice 20, `docs/mechanics/044-secrets.md`)
are decoded from the same pinned reference graphics
(`assets/amiga/xevious_gfx.c`: sprite tiles 0xA8–0xAB and the 2×2 groups
0xAC–0xB7, sprite colour table 7, palette) by `tools/sol_tower_render.py` into
one 224×32 sheet, `src/xevious/assets/7f90e2226e98c3d79f046846412b0fbd.png`,
on the hidden `sprite_sheets` target; `--verify` re-derives it byte-for-byte at
the pin. The `sol_tower` entry in `assets/sprite-extraction/manifest.json` crops
the seven frames from it. They replace the earlier Spriters Resource crops,
which held the tower's dome but not its shadow.

| Output | Description | Credit | Source | License |
| --- | --- | --- | --- | --- |
| Sol Tower sheet and seven `sol-tower/rise` costumes | Sol Tower rise frames | Namco (Xevious, 1983); decoded from the arcade sprite ROM in the pinned reference by jotd666 | [jotd666/xevious](https://github.com/jotd666/xevious) at the pin | No reusable license specified by source; third-party copyrighted material |

The same rights caveat as the terrain and the Andor parts applies.

## Explosions, bomb and crosshair rendered from the arcade sprite data

The player, air and ground explosions, the bomb crater, the crosshair, the bomb
target, the bomb and the enemy bullet (slice 20, CAB-05,
`docs/mechanics/055-presentation-fidelity.md`) are decoded from the same pinned
reference graphics (`assets/amiga/xevious_gfx.c`: the explosion sprite codes,
the bank-1 crosshair and bomb tiles, their colour tables and the palette) by
`tools/effects_sprite_render.py` into one 224×128 sheet,
`src/xevious/assets/92f9eb9bdf3015dee68c2169b50c2b8a.png`, on the hidden
`sprite_sheets` target; `--verify` re-derives it byte-for-byte at the pin. The
`effects` entries in `assets/sprite-extraction/manifest.json` crop the frames
from it, flip-expanding the player and air explosions. They replace the
Spriters Resource stand-ins (the shared eight-frame burst, the crater crops and
the Toroid frames the enemy bullet borrowed); the replaced baseline costumes stay
in the project unreferenced.

| Output | Description | Credit | Source | License |
| --- | --- | --- | --- | --- |
| Effects sheet and its explosion, crater, crosshair, bomb-target, bomb and bullet costumes | Arcade effects sprites at their colour steps | Namco (Xevious, 1983); decoded from the arcade sprite ROM in the pinned reference by jotd666 | [jotd666/xevious](https://github.com/jotd666/xevious) at the pin | No reusable license specified by source; third-party copyrighted material |

The same rights caveat as the terrain and the Andor parts applies.

## Enemy, shot and sparkle sprites rendered from the arcade sprite data

The Giddo Spario's flight and hit frames at its four colours, the Zakato, Brag
Zakato and Garu Zakato bodies at the pulsing colour, the Zakato self-destruct and
teleport frames, the Brag Spario, the player's shot and its rebound off a Bacura,
the title-screen sparkle, and the title logo (slice 21, `presentation.reference-art`)
are decoded from the same pinned reference graphics (`assets/amiga/xevious_gfx.c`:
the bank-1 sprite codes, the tile bank, their colour tables and the palette) by
`tools/reference_art_render.py` into one 400×448 sheet,
`src/xevious/assets/c8028a15c77fd2068aa107476ead3bcc.png`, on the hidden
`sprite_sheets` target; `--verify` re-derives it byte-for-byte at the pin. The
`reference_art` entries in `assets/sprite-extraction/manifest.json` crop the
frames from it, flip-expanding the Brag Spario, the shot, its rebound and the
teleport sparkle. The logo is drawn from the two tile layers the arcade's
`display_xevious_logo_flashing` and `display_xevious_logo_yellow` write: one
background cell, the outline at the eight flash colours, and the demo's yellow
logo. The fan-rip logo from asset 168901 stays only as the `start_screen`
target's preserved baseline costume; the build no longer shows it.

| Output | Description | Credit | Source | License |
| --- | --- | --- | --- | --- |
| Reference-art sheet and its Giddo Spario, Zakato body, self-destruct, teleport, Brag Spario, shot, rebound, title-sparkle and title-logo costumes | Arcade enemy, shot and sparkle sprites at their colour steps and flips, and the title logo's tile art | Namco (Xevious, 1983); decoded from the arcade sprite ROM in the pinned reference by jotd666 | [jotd666/xevious](https://github.com/jotd666/xevious) at the pin | No reusable license specified by source; third-party copyrighted material |

The same rights caveat as the terrain and the Andor parts applies.

## Andor Genesis parts rendered from the arcade sprite data

The Andor Genesis boss's fourteen part cells (nine armour plates, four gun ports
and the core) and the four Bragza fly-up cells are decoded from the same pinned
reference graphics (`assets/amiga/xevious_gfx.c`: each part's sprite code read
at the pin in `xevious_main.68k`, sprite colour table 3 for the parts and 0x15
for the Bragza, and the palette) by `tools/andor_sprite_render.py` into one
sheet, `src/xevious/assets/233c35c614f27343780ebf4967597aab.png`, on the hidden
`sprite_sheets` target; `--verify` re-derives it at the pin. The
`andor_genesis` entries in `assets/sprite-extraction/manifest.json` crop the
part costumes from it. They replace the crops of the Spriters Resource sheet
42386, which is no longer shipped.

| Output | Description | Credit | Source | License |
| --- | --- | --- | --- | --- |
| Andor Genesis sheet and its part and Bragza costumes | Arcade Andor Genesis parts and Bragza | Namco (Xevious, 1983); decoded from the arcade sprite ROM in the pinned reference by jotd666 | [jotd666/xevious](https://github.com/jotd666/xevious) at the pin | No reusable license specified by source; third-party copyrighted material |

The same rights caveat as the terrain applies.

## Bonus Flag rendered from the arcade sprite data

No Spriters Resource sheet breaks out the hidden Special Flag (SEC-02,
`docs/mechanics/044-secrets.md`), so its sprite is decoded from the pinned
reference graphics (`assets/amiga/xevious_gfx.c` sprite index 287, labelled
`flag` in the reference's `sprite_config.json`) at sprite colour table 0x0E, the
colour `reveal_bonus_flag` writes (`xevious_main.68k` 3148), as one 16×16 cell:
`src/xevious/assets/6ca6cf679d389290d64bd6417973df2c.png` on the hidden
`sprite_sheets` target. The `bonus_flag` entry in
`assets/sprite-extraction/manifest.json` crops the flag costume from it.

| Output | Description | Credit | Source | License |
| --- | --- | --- | --- | --- |
| Bonus Flag sheet and its flag costume | The Special Flag bonus item | Namco (Xevious, 1983); decoded from the arcade sprite ROM in the pinned reference by jotd666 | [jotd666/xevious](https://github.com/jotd666/xevious) at the pin | No reusable license specified by source; third-party copyrighted material |

The same rights caveat as the terrain applies.

## The base Scratch project (2017)

The build starts from a historical Scratch project, preserved byte-for-byte at
`assets/original/Xevious.sb3` with its record in `assets/original/provenance.json`:
[Scratch project 195680409](https://scratch.mit.edu/projects/195680409/), created
by StarshipSuperjam (2017-12-31, last publicly modified 2018-01-11). The media
the build still references from it — its baseline costumes and sounds, most kept
unplayed and unshown as preserved history — ship under their original
content-hash names and are not repeated in `src/xevious/assets/provenance.json`.
That project contains media recognizable as Namco Xevious material; preserving it
here asserts no ownership of, and grants no license to, third-party material.

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
