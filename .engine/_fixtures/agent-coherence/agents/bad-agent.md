---
name: bad-agent
role: pre-submission-review
lens: spec-conformance
model-tier: judgment
permissions: read-only
output-contract: pre-submission-review-finding.v1
---

# bad-agent (negative fixture)

A negative fixture for `engine/check/agent-coherence`: this persona declares `permissions: read-only`
but carries neither a `tools` allowlist nor a `disallowedTools` denylist, so it inherits every tool —
including the authoritative-write tools (Edit, Write, NotebookEdit). The coherence gate must catch it.
