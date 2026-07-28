"""The engine's memory substrate package (SQLite + FTS5).

The public import surface the rest of the engine binds to as ``memory`` — e.g. the close turn-hook's
ambient-capture relay does ``import memory; memory.capture_turn_delta(payload)``. As of the capture
slice that relay is LIVE: ``capture_turn_delta`` is exposed here, so the previously-dormant seam now
appends the completed turn's delta to the ledger instead of degrading to a no-op. The function is
fail-soft (any fault is a clean no-op return, never a raise), so close is still never gated by capture.

Importing ``memory`` binds the ``capture`` + ``ledger`` submodules but does **no filesystem work** —
all reads/writes happen inside the called functions — so the import itself cannot fail or do work on a
live session's turn. Callers reach the ledger/index primitives explicitly with ``from memory import
ledger`` / ``from memory import index``.

Shipped: the ledger (``memory.ledger``), the derived index + plain-scan fallback (``memory.index``),
turn-delta capture (``memory.capture`` / ``memory.capture_turn_delta``), the operator's own controls — pins
(``memory.pins``), reversible withhold/restore and the readout (``memory.forget``), export (``memory.export``),
the operator-asked erasure request (``memory.erase``) and the operator-asked secret re-scrub
(``memory.rescrub``) — crash-safe ledger compaction (``memory.compact``), the public search interface + MCP server, the
backup/restore vault with its resurrection-surfacing (``memory.backup_vault`` / ``memory.restore_vault``),
the pre-migration backup seam the module manager consumes (``memory.snapshot_for_migration``) and the
migration-revert restore that brings a pre-migration snapshot tag back (``memory.restore_pre_migration``). Layer-2
audit-gated physical erasure has shipped its enactment core (the gated removal + sole minter in
``memory.compact``) and its cross-session observer (``memory.erasure_observer``).
"""

from memory.capture import capture_turn_delta  # noqa: F401 — the public capture entry close's relay calls
from memory.backup_vault import migration_backup_available  # noqa: F401 — the migration pre-flight readiness probe
from memory.backup_vault import snapshot_for_migration  # noqa: F401 — the pre-migration backup seam module_manager calls
from memory.restore_vault import restore_pre_migration  # noqa: F401 — the migration-revert restore detector calls

__all__ = ["capture_turn_delta", "migration_backup_available", "snapshot_for_migration", "restore_pre_migration"]
