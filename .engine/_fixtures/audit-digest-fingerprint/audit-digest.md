---
schema_version: 1
generated: 2026-06-23
fingerprint: sha256:0000000000000000000000000000000000000000000000000000000000000000
---

This is a negative fixture for `engine/check/audit-digest-fingerprint`. The header is well-formed
(it carries a run-date and a check-value), but the check-value is a deliberately wrong seal, so the
recomputed seal over (generated + this body) will not match it — the silent-hand-edit bite. The check
must report that this self-review file no longer matches the value the audit recorded.
