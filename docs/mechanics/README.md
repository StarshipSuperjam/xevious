# Mechanics record template

Add or update one record whenever `src/xevious/project.json` changes, including
infrastructure migrations. Copy this exact field structure; every value must
be non-empty and each attestation must remain checked.

```md
# Short mechanic name

- Mechanic: Name the behavior or non-gameplay migration.
- Derived behavior: Describe the mechanic in plain language without copying source text.
- Reference provenance: Cite the exact commit, file, and source label, or the canonical project artifact for a non-gameplay migration.
- Transfer class: List each class used: general behavior, numeric constant, structured table or schedule, media, historical baseline, or non-gameplay migration.
- Scratch interpretation: Describe the original Scratch structure used.
- Scratch evidence: Name the targets, variables, lists, broadcasts, and test fixtures that implement the mechanic.
- Acceptance criteria: State a player-visible or mechanically falsifiable result.
- Fidelity status: State repo-derived, arcade-confirmed, uncertain, deliberate deviation, or non-gameplay.
- License status: State the reference or media license status without treating attribution as permission.
- Known deviations or uncertainty: State deviations, unknowns, or “None known.”
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
```

For a repository-derived mechanic, prefer this locator form:

```text
jotd666/xevious@FULL_COMMIT; `src/xevious_main.68k`: `label_name` (NNN-MMM),
`other_label` (NNN-MMM); `src/xevious_sub.68k`: `related_table_label` (NNN-MMM)
```

The commit must be the pinned one, and every line range on this line carries the
label it belongs to (`tools/reference_citations.py` resolves each against the pin;
an unlabelled or nonexistent citation fails). Record exact constants and structured data actually used. Arcade footage or
documentation may supplement provenance when it resolves an ambiguity, but it
is not required for an unambiguous repository-derived mechanic.

Check the record locally against the pull request’s target branch:

```sh
python3 tools/check_mechanics_record.py origin/main
```

Fetch `origin/main` first if the local reference is stale. The check only
requires a changed record when `src/xevious/project.json` changed.
