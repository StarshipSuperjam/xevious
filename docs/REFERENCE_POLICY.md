# Xevious reference policy

## Target

This project is an independently written Scratch interpretation of the Namco
arcade release of Xevious. The arcade behavior is authoritative when later
ports differ.

## Reference boundary

The public [`jotd666/xevious`](https://github.com/jotd666/xevious) repository
is the primary mechanics reference. It describes itself as a line-by-line 68K
transcode of reverse-engineered arcade ROM code and states no reusable
repository license in the pinned snapshot.

The project uses commit
[`71473685a8c7856c8401c8519276cd97a38d4183`](https://github.com/jotd666/xevious/tree/71473685a8c7856c8401c8519276cd97a38d4183)
unless a later pull request deliberately updates the pin.

Allowed inputs from that public source repository include:

- player-visible mechanics and rules;
- numeric constants, timing, scores, hit behavior, and difficulty values;
- structured formations, schedules, and lookup tables; and
- normal-versus-Super branch information.

Every mechanics record cites the exact commit, file, and source label, states
which input classes were used, and re-expresses the result with original
Scratch blocks, structure, and naming. Assembly or other source-code text is
not copied into the Scratch project.

The project does not acquire, distribute, open, or extract arcade ROM files.
That prohibition is about handling ROM files; it does not turn the already
published GitHub repository into a ROM file.

Arcade observation is selective fidelity QA, not a prerequisite for each
repository-derived mechanic. Use it to resolve the reference's acknowledged
remaining gameplay bug, ambiguous behavior, normal-versus-Super differences,
platform additions, and final feel.

This is a documented provenance and implementation boundary, not a claim that
attribution grants permission or legal clearance.

## Media and provenance

The historical `.sb3` remains intact instead of exposing each embedded media
file as a separately tracked repository asset. New or modified overlay assets
must record a non-empty origin and an honest license status in
`src/xevious/assets/provenance.json`; validation fails otherwise. A source that
states no reusable license is recorded as such rather than being treated as
licensed merely because attribution is available.

Third-party media may be imported only when the operator deliberately supplies
or approves it and every file is covered by the provenance record. Attribution
does not itself grant permission. Imported media credits and source links are
summarized in `docs/ASSET_CREDITS.md`.

The repository does not claim ownership of or grant a license to Namco's
trademarks, artwork, audio, or other third-party material. A rights review is
needed before broader distribution or promotion.

## Mechanics record

Every pull request that changes `src/xevious/project.json` must add or update a
record under `docs/mechanics/`, including a non-gameplay migration. The
required project check enforces this. Each record includes:

- the mechanic and its plain-language derived behavior;
- exact reference provenance and transferred input class;
- the Scratch interpretation and implementation evidence;
- an acceptance criterion, fidelity status, license status, and known
  deviations or uncertainty;
- confirmation that no assembly or other source code was copied;
- confirmation that no arcade ROM file was acquired, opened, extracted, or
  distributed; and
- confirmation that transferred graphics or audio are covered by the
  repository's provenance record.
