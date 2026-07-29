# Self-map (deliberately stale fixture — NOT the real map)

This is a negative fixture for `engine/check/self-map-drift`. It is a stale self-map: its content
does not match the canonical map the check derives from the live surface catalog + module manifests,
so running the drift gate against it (the committed side) MUST produce the "out of date" hard finding.

The check re-derives the canonical map from the real repo and compares it to this seeded file; any
mismatch is the bite. This file is intentionally a single stale stanza and will never match canon.

- stale-entry: this-surface-no-longer-exists
