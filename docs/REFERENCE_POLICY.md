# Xevious reference policy

## Target

This project is an independently written Scratch interpretation of the Namco
arcade release of Xevious. The arcade behavior is authoritative when later
ports differ.

## Reference boundary

The public [`jotd666/xevious`](https://github.com/jotd666/xevious) repository
is a useful index of mechanics to investigate. It describes itself as a
line-by-line 68K transcode of reverse-engineered arcade ROM code and has no
visible repository license as of 2026-07-28.

It is a reference, not an input:

- Do not copy or translate its source code.
- Do not copy ROM data, timing or lookup tables, graphics, audio, generated
  assets, or binary files.
- Use it to identify questions about observable behavior.
- Record the independent evidence used to answer each mechanics question,
  such as observation of legally accessed gameplay or published documentation.
- Re-express confirmed behavior using original Scratch structure and naming.

This is a documented reference boundary, not a claim of clean-room
development or legal clearance.

## Media and provenance

The historical `.sb3` remains intact instead of exposing each embedded media
file as a separately tracked repository asset. New or modified overlay assets
must record a non-empty origin and license in
`src/xevious/assets/provenance.json`; validation fails otherwise.

The repository does not claim ownership of or grant a license to Namco's
trademarks, artwork, audio, or other third-party material. A rights review is
needed before broader distribution or promotion.

## Mechanics record

Every pull request that changes `src/xevious/project.json` must add or update a
record under `docs/mechanics/`, including a non-gameplay migration. The
required project check enforces this. Each record includes:

- the mechanic being implemented;
- the observable arcade behavior;
- the independent evidence and observation date;
- the Scratch interpretation chosen;
- known deviations or uncertainty;
- confirmation that no external code, ROM data, tables, graphics, or audio
  were transferred.
