# What this engine is made of

> **Generated file — do not edit by hand.** This map is derived from the engine's surface
> catalog and module manifests, so it always matches them. To update it, change those and
> regenerate with `uv run --directory .engine -- python tools/self_map.py generate`, then commit the result.

> **What this shows — and what it does not.** This map shows your engine's structural makeup:
> the kinds of file it governs and the packages it is built from, derived to match those sources.
> It does not show whether each part *works* or is well designed — that is your review and each
> module's own checks, never something this map attests.

Engine release `0.5.0` · identity `solo`

## Surfaces

Every kind of file the engine governs — its home and authority, and the schema and template that govern it (13 surfaces).

| surface | purpose | home | authority | lifecycle | class | governing schema | template |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `agent` | Personas the engine runs for a trigger (review, worker, and audit roles), routed by role, lens, model tier, permissions, and output contract. | `.claude/agents/` | mechanics-and-guidance | artifact | prose | `agent.v1.json` | `../templates/agent.md` |
| `check` | Declarative validation rules the validator dispatches (target, kind, params, tier, suites, message) — authored as data, never as validator code. | `.engine/check/` | mechanics-and-guidance | artifact | structured | `check.v1.json` | (none) |
| `codex-agent` | The Codex-native render of an engine review persona (a TOML agent Codex spawns; generated from the canonical Claude persona, never hand-authored). | `.codex/agents/` | mechanics-and-guidance | artifact | structured | `codex-agent.v1.json` | (none) |
| `codex-skill` | The Codex-native render of an engine typed command (a SKILL.md Codex discovers; generated from the canonical Claude skill, never hand-authored). | `.agents/skills/` | mechanics-and-guidance | artifact | prose | `codex-skill.v1.json` | `../templates/codex-skill.md` |
| `conduct` | Codes of conduct — the operator's standing behavioral stance for how the AI engages (plain language, provenance, push-back, and the like); tier-3 guidance, pure posture, never an enforcement gate. Two committed layers (engine defaults plus operator override) composed by rule id, loaded at the grounding floor. | `.engine/conduct/` | mechanics-and-guidance | artifact | prose | `conduct.v1.json` | `../templates/conduct.md` |
| `contract` | Architecturally significant decision records — one decision each, with rationale and the rejected alternative; the top authority tier, file-per-decision, append-only. | `.engine/contracts/` | decisions | decision | prose | `contract.v1.json` | `../templates/contract.md` |
| `doc` | Operator-facing, hand-authored plain-language explanations of the engine — written for the human, not the AI. | `.engine/docs/` | mechanics-and-guidance | artifact | prose | `doc.v1.json` | `../templates/doc.md` |
| `interface` | Protocol contracts — a stable callable boundary a swappable implementation satisfies; implementations bind by presence, resolve single-active, and name a fallback. | `.engine/interfaces/` | mechanics-and-guidance | artifact | structured | `interface.v1.json` | (none) |
| `operation` | The authoritative steps of a multi-step engine procedure performed by reading-and-following; one procedure, one home, referenced by its invokers rather than restated. | `.engine/operations/` | mechanics-and-guidance | artifact | prose | `operation.v1.json` | `../templates/operation.md` |
| `policy` | Standing rules — ongoing directives that govern behavior across sessions; the second authority tier. | `.engine/policies/` | standing-rules | decision | prose | `policy.v1.json` | `../templates/policy.md` |
| `schema` | Structural contracts — JSON Schema (2020-12) declaring the shape of structured files and of prose frontmatter. | `.engine/schemas/` | mechanics-and-guidance | artifact | structured | `https://json-schema.org/draft/2020-12/schema` | (none) |
| `skill` | In-session procedures (Claude Code SKILL.md, progressive disclosure), engine-prefixed; invoked per the model-auto / operator-typed / model-only axis. | `.claude/skills/` | mechanics-and-guidance | artifact | prose | `skill.v1.json` | `../templates/skill.md` |
| `tool` | The engine's executable machinery — the validator, hooks, MCP servers, the wiring library, and interface implementations. | `.engine/tools/` | mechanics-and-guidance | artifact | code | (none) | (none) |

## Modules

The packages your engine is assembled from, and how they wire together (9 installed).

The dependency graph — each module is listed after the ones it builds on (`→` means "depends on"):

- `core` (no dependencies)
- `design-review` → `core`
- `memory-substrate-sqlite-fts5` → `core`
- `memory-semantic-recall` → `core`, `memory-substrate-sqlite-fts5`
- `product-design` → `core`
- `qa-review` → `core`
- `routine-mode` → `core`
- `validators-core` → `core`
- `audit-library` → `validators-core`

### `core` — version `0.5.0` (required)

- depends on: nothing
- provides:
  - check: `.engine/check/guardrail-weakening.json`, `.engine/check/protection.json`
  - codex-skill: `.agents/skills/engine-conduct/SKILL.md`, `.agents/skills/engine-conduct/agents/openai.yaml`, `.agents/skills/engine-help/SKILL.md`, `.agents/skills/engine-help/agents/openai.yaml`, `.agents/skills/engine-parts/SKILL.md`, `.agents/skills/engine-parts/agents/openai.yaml`, `.agents/skills/engine-recall/SKILL.md`, `.agents/skills/engine-recall/agents/openai.yaml`, `.agents/skills/engine-start/SKILL.md`, `.agents/skills/engine-start/agents/openai.yaml`, `.agents/skills/engine-status/SKILL.md`, `.agents/skills/engine-status/agents/openai.yaml`, `.agents/skills/engine-tune/SKILL.md`, `.agents/skills/engine-tune/agents/openai.yaml`, `.agents/skills/engine-upgrade/SKILL.md`, `.agents/skills/engine-upgrade/agents/openai.yaml`
  - conduct: `.engine/conduct/defaults.md`
  - contract: `.engine/contracts/*.md`
  - doc: `.engine/docs/getting-started.md`
  - foundation: `.engine/self-map.md`, `.engine/suites.json`
  - interface: `.engine/interfaces/*.json`
  - knowledge: `.engine/knowledge/*.json`
  - migration: `.engine/modules/core/migrations/*.py`
  - operation: `.engine/operations/boot-session-start.md`, `.engine/operations/build-orchestration.md`, `.engine/operations/close-turn.md`, `.engine/operations/codex-validation.md`, `.engine/operations/conduct-author.md`, `.engine/operations/control-plane-bootstrap.md`, `.engine/operations/engine-arrival.md`, `.engine/operations/engine-remove.md`, `.engine/operations/engine-team-switch.md`, `.engine/operations/engine-upgrade.md`, `.engine/operations/knowledge-impact-check.md`, `.engine/operations/memory-recall.md`, `.engine/operations/module-add.md`, `.engine/operations/module-remove.md`, `.engine/operations/onboarding-read.md`, `.engine/operations/operating-modes.md`, `.engine/operations/tune-policy.md`
  - policy: `.engine/policies/attention.md`, `.engine/policies/briefing-budget.md`, `.engine/policies/contract-threshold.md`, `.engine/policies/escalation.md`, `.engine/policies/finding-disposition.md`, `.engine/policies/model-bindings.json`, `.engine/policies/model-routing.md`, `.engine/policies/supported-upgrade-matrix.md`, `.engine/policies/triage-threshold.md`
  - provisioning: `.engine/provisioning/first-run-assets.json`, `.engine/provisioning/module-catalog.json`, `.engine/provisioning/module-surfaces.json`
  - schema: `.engine/schemas/*.json`
  - skill: `.claude/skills/engine-conduct/SKILL.md`, `.claude/skills/engine-help/SKILL.md`, `.claude/skills/engine-parts/SKILL.md`, `.claude/skills/engine-recall/SKILL.md`, `.claude/skills/engine-start/SKILL.md`, `.claude/skills/engine-status/SKILL.md`, `.claude/skills/engine-tune/SKILL.md`, `.claude/skills/engine-upgrade/SKILL.md`
  - state: `.engine/state/*.json`
  - template: `.engine/templates/*.md`
  - tool: `.engine/tools/*.py`, `.engine/tools/*.sh`
- wires: codex-hook, codex-mcp, gitignore, hook, mcp

### `design-review` — version `0.2.0` (optional)

- depends on: `core`
- provides:
  - agent: `.claude/agents/engine-design-review-architecture.md`, `.claude/agents/engine-design-review-feasibility.md`, `.claude/agents/engine-design-review-product-intent.md`, `.claude/agents/engine-design-review-risk-governance.md`
  - codex-agent: `.codex/agents/engine-design-review-architecture.toml`, `.codex/agents/engine-design-review-feasibility.toml`, `.codex/agents/engine-design-review-product-intent.toml`, `.codex/agents/engine-design-review-risk-governance.toml`
- wires: none (this module adds no shared-state edits)

### `memory-substrate-sqlite-fts5` — version `0.2.0` (required)

- depends on: `core`
- provides:
  - backup: `.engine/memory-backup/pointer.json`
  - erasures: `.engine/erasures/proposal.json`
  - tool: `.engine/tools/memory/*.py`
- wires: codex-hook, codex-mcp, gitignore, hook, mcp

### `memory-semantic-recall` — version `0.1.0` (default-on)

- depends on: `core`, `memory-substrate-sqlite-fts5`
- provides:
  - asset: `.engine/tools/memory/semantic/NOTICE.txt`, `.engine/tools/memory/semantic/checksums.json`, `.engine/tools/memory/semantic/potion-retrieval-32m-int8.npz`, `.engine/tools/memory/semantic/vocab.txt`
  - tool: `.engine/tools/memory/semantic/*.py`
- wires: none (this module adds no shared-state edits)

### `product-design` — version `0.2.0` (optional)

- depends on: `core`
- provides:
  - check: `.engine/check/product-adr-form.json`, `.engine/check/product-design-form.json`, `.engine/check/product-lock-integrity.json`, `.engine/check/product-spec-coverage.json`, `.engine/check/product-spec-form.json`, `.engine/check/product-spec-matrix.json`
  - codex-skill: `.agents/skills/engine-design/SKILL.md`, `.agents/skills/engine-design/agents/openai.yaml`
  - doc: `.engine/docs/product-design.md`
  - foundation: `.engine/product-spec-matrix.json`
  - operation: `.engine/operations/product-intake.md`
  - policy: `.engine/policies/spec-structure-integrity.md`
  - scaffold: `.engine/modules/product-design/scaffold/*.md`
  - skill: `.claude/skills/engine-design/SKILL.md`
  - tool: `.engine/tools/product_design/*.py`
- wires: codex-hook, hook

### `qa-review` — version `0.2.0` (optional)

- depends on: `core`
- provides:
  - agent: `.claude/agents/engine-qa-review-divergence-hunter.md`, `.claude/agents/engine-qa-review-security-governance.md`, `.claude/agents/engine-qa-review-spec-conformance.md`, `.claude/agents/engine-qa-review-technical-integrity.md`, `.claude/agents/engine-qa-review-usability.md`
  - codex-agent: `.codex/agents/engine-qa-review-divergence-hunter.toml`, `.codex/agents/engine-qa-review-security-governance.toml`, `.codex/agents/engine-qa-review-spec-conformance.toml`, `.codex/agents/engine-qa-review-technical-integrity.toml`, `.codex/agents/engine-qa-review-usability.toml`
- wires: none (this module adds no shared-state edits)

### `routine-mode` — version `0.1.0` (required)

- depends on: `core`
- provides:
  - codex-skill: `.agents/skills/engine-routine/SKILL.md`, `.agents/skills/engine-routine/agents/openai.yaml`
  - operation: `.engine/operations/routine-entry.md`
  - skill: `.claude/skills/engine-routine/SKILL.md`
- wires: none (this module adds no shared-state edits)

### `validators-core` — version `0.2.0` (required)

- depends on: `core`
- provides:
  - check: `.engine/check/agent-coherence.json`, `.engine/check/agent-frontmatter.json`, `.engine/check/agent-shape.json`, `.engine/check/audit-concern-list.json`, `.engine/check/audit-digest-fingerprint.json`, `.engine/check/audit-digest-staleness.json`, `.engine/check/block-coherence.json`, `.engine/check/catalog-completeness.json`, `.engine/check/catalog-coverage.json`, `.engine/check/census-completeness.json`, `.engine/check/codex-agent-coherence.json`, `.engine/check/codex-agent-schema.json`, `.engine/check/codex-hooks-schema.json`, `.engine/check/codex-provider-parity.json`, `.engine/check/codex-skill-coherence.json`, `.engine/check/codex-skill-frontmatter.json`, `.engine/check/codex-skill-shape.json`, `.engine/check/conduct-frontmatter.json`, `.engine/check/conduct-shape.json`, `.engine/check/conduct-weakening-guard.json`, `.engine/check/contract-frontmatter.json`, `.engine/check/contract-shape.json`, `.engine/check/contract-threshold.json`, `.engine/check/doc-frontmatter.json`, `.engine/check/doc-shape.json`, `.engine/check/engine-manifest.json`, `.engine/check/engine-todo-form.json`, `.engine/check/execution-state.json`, `.engine/check/first-run-assets.json`, `.engine/check/first-run-reference-closure.json`, `.engine/check/hard-check-bite.json`, `.engine/check/in-tool-demo-failure-path.json`, `.engine/check/interface-coherence.json`, `.engine/check/interface-declaration.json`, `.engine/check/knowledge-coverage.json`, `.engine/check/knowledge-vocabulary.json`, `.engine/check/lens-consumption.json`, `.engine/check/link-integrity.json`, `.engine/check/manifest-write-funnel.json`, `.engine/check/memory-pointer-public-safety.json`, `.engine/check/model-bindings-schema.json`, `.engine/check/module-manifest.json`, `.engine/check/ontology-authority-reservation.json`, `.engine/check/operation-frontmatter.json`, `.engine/check/operation-shape.json`, `.engine/check/operator-guarded-paths.json`, `.engine/check/operator-local-references.json`, `.engine/check/policy-frontmatter.json`, `.engine/check/policy-override-stale.json`, `.engine/check/policy-shape.json`, `.engine/check/pr-behaviors-declared.json`, `.engine/check/pr-body-completeness.json`, `.engine/check/provider-exceptions-schema.json`, `.engine/check/provider-vocabulary-confinement.json`, `.engine/check/provisioning-catalog.json`, `.engine/check/release-integrity.json`, `.engine/check/self-map-drift.json`, `.engine/check/shipped-issue-references.json`, `.engine/check/skill-coherence.json`, `.engine/check/skill-frontmatter.json`, `.engine/check/skill-shape.json`, `.engine/check/state-cursor.json`, `.engine/check/template-shape-spec.json`, `.engine/check/untracked-surface.json`, `.engine/check/uv-group-drift.json`
  - policy: `.engine/policies/provider-exceptions.json`
- wires: none (this module adds no shared-state edits)

### `audit-library` — version `0.2.0` (required)

- depends on: `validators-core`
- provides:
  - agent: `.claude/agents/engine-audit.md`
  - audits: `.engine/audits/concern-list.json`, `.engine/audits/self-review-setup.md`
  - codex-agent: `.codex/agents/engine-audit.toml`
- wires: none (this module adds no shared-state edits)
