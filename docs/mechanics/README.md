# Mechanics record template

Add or update one record whenever `src/xevious/project.json` changes, including
infrastructure migrations. Copy this exact field structure; every value must be
non-empty and the attestation must remain checked.

```md
# Short mechanic name

- Mechanic: Name the behavior or non-gameplay migration.
- Observable arcade behavior: Describe what the Namco arcade game visibly does.
- Independent evidence: Cite observation or published documentation, with enough detail to repeat it.
- Observation date: YYYY-MM-DD.
- Scratch interpretation: Describe the original Scratch structure used.
- Known deviations or uncertainty: State deviations, unknowns, or “None known.”
- [x] No external code, ROM data, or lookup tables were transferred.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
```

Check the record locally against the pull request’s target branch:

```sh
python3 tools/check_mechanics_record.py origin/main
```

Fetch `origin/main` first if the local reference is stale. The check only
requires a changed record when `src/xevious/project.json` changed.
