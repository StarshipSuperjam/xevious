---
schema_version: 2
reviewed_at: 2026-06-23
content_modified_at: 2026-06-23
fingerprint: sha256:0000000000000000000000000000000000000000000000000000000000000000
---

This is a negative fixture for `engine/check/audit-digest-fingerprint`. The header is well-formed
(a v2 header carrying a run-date, a prose-modified date, and a check-value), but the check-value is a
deliberately wrong seal, so the recomputed seal over (the header fields + this body) will not match it —
the silent-hand-edit bite on the live v2 format. The check must report that this self-review file no
longer matches the value the audit recorded.
