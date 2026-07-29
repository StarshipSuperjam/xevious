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
three Solvalou frames and seven Toroid frames from the credited sheets. The
standard-library generator removes only edge-connected `(0, 128, 0)` matte,
places every frame on a native 16×16 RGBA canvas, and records the exact source
hash, rectangle, canvas, anchor, credit, and license status in
`assets/sprite-extraction/provenance.json`. Scratch copies of the same records
live in `src/xevious/assets/provenance.json`.

The generated review contact sheet at
`docs/images/sprite-extraction-proof.png` is also a derivative of the credited
artwork. It exists for crop, transparency, and anchor review and carries the
same no-reusable-license-specified status as its sources.
