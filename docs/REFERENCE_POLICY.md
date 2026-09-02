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

Reference-derived numeric data may also be committed to this repository as
part of the product specification: derived values, orderings, and structured
tables recorded in the project's own notation (prose or generated JSON under
`docs/spec/`), each carrying its citation (commit, file, label, line range),
the SHA-256 hashes of the source files it was derived from, and an honest
license status. This covers derived numbers and their arrangement only.
Reference symbol names and line numbers appear as citation locators and,
in the object registry, as derived handler identifiers;
assembly instructions, comments, prose, and media from the reference are
never reproduced. This paragraph is a deliberate widening of the
recorded boundary, made through the product-spec intake with the operator's
acknowledgement on the pull request that introduced it.

The project does not acquire, distribute, open, or extract arcade ROM files.
That prohibition is about handling ROM files; it does not turn the already
published GitHub repository into a ROM file.

In-game display text — from the reference or from arcade observation alike —
is never transcribed into this project: the game's hidden credit event shows
this project's own original wording, and the default high-score table ships
with this project's own placeholder initials. The mechanic is in scope; the
arcade's strings are not.

Verifiability of every committed derived value rests on the public reference
remaining reachable at the pinned commit; this project keeps no copy in the
repository. A regenerable local clone at the pin, created outside the repository
by `tools/reference_checkout.py`, is the archive this policy permits — a
throwaway cache, not a vendored copy. Tooling does depend on *obtaining* that
clone: `tools/reference_citations.py` resolves each citation against it,
`tools/playtest_package.py` refuses to produce a playtest build while any
citation is unresolved, and the reference-fidelity review reads it. This is an
operational dependency on the reference being obtainable at the pin, not on a
stored copy. Accepted risk, recorded: if the upstream repository disappears, the
committed data freezes as-is (its hashes prove integrity, not re-derivability),
the fidelity tooling can no longer ground a new change, and arcade observation
becomes the only confirmation path.

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
