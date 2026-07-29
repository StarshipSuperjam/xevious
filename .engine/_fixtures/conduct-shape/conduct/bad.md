## Orphan Section

This is a negative fixture for `engine/check/conduct-shape`. It carries a `## ` section that has no
matching entry in the settings block at the top of the file, so the shape gate must bite: a body
section and the settings block have drifted out of sync.
