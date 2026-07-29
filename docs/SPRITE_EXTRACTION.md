# Gameplay sprite extraction design

## Goal

Convert the five credited, unchanged sprite sheets already stored in the
Scratch project into deterministic gameplay-ready RGBA costumes while
retaining exact source provenance.

The sheets are opaque RGB PNGs with an exact `(0, 128, 0)` green matte.
Disconnected colored regions are not reliable sprite boundaries: bullets,
multipart enemies, lettering, examples, and credit panels also appear as
separate islands. Extraction is manifest-driven, not automatic
connected-component slicing.

## Inputs and manifest

Committed inputs are the original sheet bytes and hashes, a versioned
manifest and schema, the extractor, and source-to-derivative provenance.
Generated outputs are PNG costumes and a review contact sheet. The original
sheets stay byte-for-byte unchanged.

Each frame records:

```json
{
  "name": "toroid/turn/01",
  "sheet": "SOURCE_MD5.png",
  "sheet_sha256": "PINNED_SOURCE_HASH",
  "rect": [0, 0, 16, 16],
  "canvas": [16, 16],
  "anchor": [8, 8],
  "family": "toroid",
  "animation": "turn",
  "duration_frames": 1
}
```

Coordinates above are illustrative. The implementation PR measures real
coordinates. Names are unique, compatible animation frames share a canvas and
anchor, and no crop may include labels, examples, or credit panels.

## Deterministic algorithm

1. Verify every source-sheet SHA-256.
2. Validate the complete manifest before producing output.
3. Crop the exact rectangle without scaling or filtering.
4. From crop edges only, flood-fill pixels exactly equal to `(0, 128, 0)` and
   make that connected matte transparent.
5. Preserve enclosed green pixels not connected to an edge.
6. Place the crop on its transparent canvas at the declared anchor.
7. Encode RGBA PNG at native 1× with fixed settings.
8. Use the MD5 of final PNG bytes as the Scratch `MD5.png` filename.
9. Emit provenance with source sheet/hash, frame name, crop, and generator
   version.
10. Generate a contact sheet showing name, bounds, canvas, and anchor.

## Scratch organization

- Use costumes per frame, not one Scratch target per image.
- Prefer one clone target per behavior family; share a target only if the
  entity spike proves it maintainable and responsive.
- Keep native pixels and scale targets by integer percentages.
- Keep timing and hitboxes in behavior data, not PNG metadata.
- Split Andor only where independently targetable gameplay parts require it.
- Keep the source-sheet library hidden and scriptless.

Sheet routing:

- **Solvalou:** craft orientations, blaster, bomb, crosshair, explosions, and
  weapon effects are separate families.
- **Aerial Enemies:** slice by labeled family; explicitly group Sheonite
  pairs, multipart enemies, and bullet arrangements.
- **Ground Enemies:** separate states, bullets, explosions, crater, Sol Tower,
  and Bonus Flag; exclude illustrated example combinations.
- **Andor Genesis:** define complete ship states and independently targetable
  parts from gameplay needs, not pixel connectivity.
- **Logo & Title:** crop complete logo/text elements rather than letters.

## Rejection and verification

Reject unexpected source hashes, duplicate names, invalid rectangles, crops
touching excluded labels/credits, incompatible animation anchors/canvases,
nondeterministic output, missing provenance, and asset names that do not match
their byte MD5.

The proof slice uses Solvalou and Toroid. Visual review checks transparency,
native scale, stable anchors, continuity, and absence of labels/credits.
Automated tests check hashes, schema failures, determinism, provenance, and
Scratch validation.

Derivatives retain the same third-party and no-reusable-license-specified
status as their source sheets. Extraction and attribution do not grant
permission.
