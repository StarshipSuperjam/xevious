"""ledger_health.py — memory's boot-surfaced health detectors.

`ledger.py` is a leaf: it reads the ledger and RETURNS a read-health report, but never surfaces
anything itself. This module is the concern layer that turns that report into a boot signal — the
same detect→relay split the other memory→boot detectors use (restore_vault's restore/migration
offers). Boot lazily imports and gathers it; render decides the operator-facing line.

`detect_ledger_malformed` reports a ROTTING ledger — one or more complete-but-unparseable lines that
the resilient read skips (and compaction skips-and-reports). Left unsurfaced, a ledger could lose
recall line by line with no signal; this is that signal, at cold start.

It deliberately does NOT report a torn TRAILING line: that is the normal, self-healing state after any
crash mid-append (the very next append heals it, ledger.append), so surfacing it would be a standing
false alarm rather than a real degradation.

`detect_stalled_migration` reports an in-flight-migration marker whose migration didn't finish (its
process died, or it is far past any real migration's span) — automatic tidying (compaction) is paused
until it clears. The clear itself is compaction's job (it self-heals a stale marker under the lock); this
detector is read-only, for boot's heads-up. A LIVE marker (a migration genuinely in progress) is normal
and draws no signal.

`detect_recall_offline` reports the AVAILABILITY floor: the saved-memory ledger present-but-unreadable, so
recall genuinely cannot answer (even the plain scan the index falls back to has nothing to read from). This
is the local, boot-detectable instance of the spec's "memory offline" — distinct from the LATENCY axis (a
missing/broken derived index self-heals via that scan, so it is NOT offline) and from `detect_ledger_malformed`
(some lines unreadable, the rest intact). A MISSING/empty ledger is the normal "no memories yet" state, never
offline. The live MCP server being down is a separate axis boot cannot read (it reads committed files only);
that case is surfaced by the model's own live-helper check, not here.

`detect_fast_search_unavailable` reports the LATENCY axis its sibling above deliberately excludes: this
machine's SQLite has no FTS5, so every search falls back to a full scan of the store. Recall still answers —
availability holds, latency does not (`index.fts5_available`, whose own contract names boot as the renderer of
this disclosure). It lived on the per-prompt seam while that seam ran a keyword lookup of its own; once the
per-prompt event became a constant cue that queries nothing, cold start is the only place left that can say it
unconditionally, which is also where the orientation contracts put every other degraded-substrate line.
"""

from __future__ import annotations

import os


def detect_ledger_malformed(cwd: "str | None" = None) -> "int | None":
    """The count of unreadable (malformed) lines in the live ledger, or None on any read fault.

    0 on a clean (or empty, or torn-only) ledger — a falsy no-signal boot treats as nothing to say.
    A positive count is a genuine-corruption signal boot surfaces. None (a read/import fault) also
    surfaces nothing: this is a best-effort health readout, never a gate, so it degrades to silence.
    """
    try:
        from memory import ledger
        report = ledger.read(path=ledger.ledger_path(cwd))
        return report.malformed
    except Exception:  # noqa: BLE001 — a health readout degrades to no-signal, never breaks boot
        return None


def detect_stalled_migration(cwd: "str | None" = None) -> bool:
    """True iff a memory migration didn't finish and left an ORPHANED in-flight marker (dead process / past the
    wall-clock ceiling), so compaction is paused until the marker clears. False on a clean state, a genuinely
    LIVE migration (normal, not a stall), or any read/import fault — a best-effort readout that degrades to
    silence, never a gate.
    """
    try:
        from memory import capture, ledger
        return capture.detect_orphaned_migration(ledger.ledger_dir(cwd)) is not None
    except Exception:  # noqa: BLE001 — a health readout degrades to no-signal, never breaks boot
        return False


def detect_recall_offline(cwd: "str | None" = None) -> bool:
    """True iff the saved-memory ledger is PRESENT but structurally unreadable — recall genuinely cannot answer.

    A MISSING ledger is empty ("no memories yet" — the shipped-empty normal state) and draws no signal; only a
    present ledger file that cannot be OPENED (a permission change, a directory where the file should be, a
    filesystem fault) is "offline". The probe is a cheap OPEN — not a full parse — because the only question is
    "does the store open?"; the same open failure is what makes `detect_ledger_malformed` return None, so the two
    signals are mutually exclusive by construction. Any other fault degrades to False (no-signal): a best-effort
    readout, never a gate — it must never break boot nor false-alarm a healthy or empty store.
    """
    try:
        from memory import ledger
        target = ledger.ledger_path(cwd)
        if not os.path.exists(target):
            return False                     # no ledger yet = "no memories yet", the normal empty state
        with open(target, "rb"):             # a present-but-unopenable ledger raises the OSError family here
            pass
        return False
    except OSError:
        return True                          # present but unopenable = offline
    except Exception:  # noqa: BLE001 — any other fault degrades to no-signal, never breaks boot
        return False


def detect_fast_search_unavailable(cwd: "str | None" = None) -> bool:
    """True iff there is saved memory to search AND this machine's SQLite lacks FTS5, so every search falls
    back to a full scan of the store.

    The LATENCY axis, distinct from `detect_recall_offline`'s availability floor: recall still answers, it just
    answers by reading everything. Gated on a ledger actually existing, because on the shipped-empty store a
    full scan is instant and there is no consequence to disclose — the same "no memories yet is not a fault"
    rule the sibling detectors keep. Absence is decided by `index.fts5_available`'s probe, never by catching a
    query-time error. Any fault degrades to False (no-signal): best-effort, never a gate.
    """
    try:
        from memory import index, ledger
        if not os.path.exists(ledger.ledger_path(cwd)):
            return False                     # nothing stored yet — a slow scan of nothing costs nothing
        return not index.fts5_available()
    except Exception:  # noqa: BLE001 — any fault degrades to no-signal, never breaks boot
        return False
