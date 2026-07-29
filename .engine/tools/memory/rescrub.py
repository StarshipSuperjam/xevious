"""rescrub.py — run every stored conversation back through the secret masking, when the operator asks.

WHAT THIS IS FOR. Capture masks secret-shaped text before anything is written — but only for what was captured
after that masking existed. Everything stored before it went in raw, and it is still there. `scrub.py` has
recorded that debt since the day it landed: "rewriting the already-stored history is owed." This is the verb
that pays it.

WHY A VERB AND NOT A MIGRATION. The obvious home was a `kind: "data"` module migration that runs on upgrade.
It cannot be. That kind is pre-flighted before ANY part of an upgrade is applied, and it refuses the WHOLE
upgrade when no memory backup is configured — and the shipped pointer is unconfigured. Every repository the
engine deploys into would have been unable to take this release, or any release after it, until its operator
set up a backup vault: including a brand-new repository with an empty store and nothing to scrub. A shipped
migration key can never be removed, so that would have been permanent.

The verb form is also the better shape on its own merits. A migration rewrites the operator's own conversation
silently, during something they asked for for other reasons. This asks.

IT RE-SCRUBS EVERYTHING, NOT "EVERYTHING BEFORE A DATE". There is no marker of when masking began — no record
version was bumped, no field records it, and the only ground truth is a commit date, which does not travel to a
deployed repository. So there is no boundary to read, and inventing one would be guesswork about the operator's
own data. `scrub.scrub_text` is deterministic and idempotent, so running it over already-clean text changes
nothing. Scrubbing everything is both the honest design and the simpler one.

THE LOCK IS HELD ACROSS THE WHOLE REWRITE, AND THAT IS THE POINT. `ledger.replace_ledger` swaps the entire file
by rename, so any turn appended between the read and the swap is unlinked with the old file. The
migration-window marker does NOT prevent this: it is a file that only compaction consults, and it deliberately
releases the lock. So this holds the single-writer lock itself, across read -> write -> swap, exactly as
compaction does — and takes its backup snapshot BEFORE acquiring it, because that snapshot goes over the
network and no capture should wait on it.

WHAT IT MOVES AND WHAT IT DOES NOT. It bumps `index_epoch`, the counter meaning "what the derived stores hold
is out of date", and drops the semantic store's passages so their digests are recomputed. It deliberately does
NOT bump `generation`, which means "content was rewritten or REMOVED" and is what the restore guard reads: no
record is removed here, and bumping it would make the restore guard refuse every backup taken before this ran,
telling the operator that notes had been deliberately removed. That is false, and it would be refused on the
day they needed it. The consequence to state plainly instead: a pre-scrub backup stays restorable, and
restoring one brings the unmasked text back — this verb says so, and can simply be run again.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import ledger, scrub  # noqa: E402

_TEMP_PREFIX = ".rescrub-"
_TEMP_SUFFIX = ".ndjson"
# Which fields are scrubbed. `text` is the human content; nothing else in a record holds free prose, and
# running the masker over an id or a session key would only risk mangling one.
_TEXT_KEY = "text"


class RescrubRefused(RuntimeError):
    """The rescrub did not happen, with the plain-language reason. Raised rather than returned so no caller can
    report a store as cleaned when nothing ran."""


def _digest_of(records_in: list) -> str:
    """A content checksum over the whole projection, in order — what the round-trip is verified against."""
    h = hashlib.sha256()
    for record in records_in:
        h.update(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _scrubbed(record):
    """`record` with its text masked, or unchanged when it has none. Returns `(record, changed)`."""
    if not isinstance(record, dict):
        return record, False
    text = record.get(_TEXT_KEY)
    if not isinstance(text, str) or not text:
        return record, False
    cleaned = scrub.scrub_text(text)
    if cleaned == text:
        return record, False
    out = dict(record)
    out[_TEXT_KEY] = cleaned
    return out, True


def plan(*, path: "str | None" = None) -> dict:
    """What a rescrub WOULD change, without changing anything. Reads only."""
    src = path or ledger.ledger_path()
    total = changed = 0
    for record in ledger.iter_records(path=src):
        total += 1
        _, did = _scrubbed(record)
        changed += 1 if did else 0
    return {"records": total, "would_change": changed}


def _require_backup() -> None:
    """Refuse unless a backup destination is configured. The engine does not rewrite stored data it cannot
    first copy somewhere else, and this rewrite is no exception to that."""
    from memory import backup_vault
    if not backup_vault.migration_backup_available():
        raise RescrubRefused(
            "no backup is set up yet, and the engine never rewrites your stored memory without first copying "
            "it somewhere safe. Nothing was changed. Ask me to set up the memory backup, then run this again."
        )


def run(*, path: "str | None" = None, snapshot=True, engine_version: str = "rescrub") -> dict:
    """Re-scrub every stored record. Returns a report; raises `RescrubRefused` rather than half-running.

    Order is the whole safety argument: back up first (over the network, no lock held), then take the
    single-writer lock and hold it across read -> write -> verify -> swap, so no turn captured meanwhile is
    lost with the old file."""
    from memory import capture, index
    src = path or ledger.ledger_path()
    data_dir = os.path.dirname(src) or "."
    if not os.path.exists(src):
        return {"status": "empty", "records": 0, "changed": 0,
                "message": "There is no saved memory yet, so there was nothing to clean."}
    # The backup REQUIREMENT is unconditional — it is the whole safety argument, and a keyword argument that
    # could switch it off would be a public seam straight past it. `snapshot=False` skips only the network
    # push, which is what a test needs; it never skips the check that a destination exists.
    _require_backup()
    if snapshot:
        from memory import backup_vault
        if backup_vault.snapshot_for_migration(None, engine_version, migration_id="rescrub") is None:
            raise RescrubRefused(
                "the engine could not save a copy of your memory before changing it, so it changed nothing. "
                "This is usually a network problem — try again when you are online."
            )
    # READ RAW, REFUSE WHAT CANNOT BE PARSED. `ledger.read` skips a malformed line, counts it, and does NOT
    # keep its bytes — so writing back only what it returned would delete that line permanently while this
    # reported a clean sweep over "all N records". A writer must not read through a lossy reader. The count is
    # the reader's own, so this refuses on exactly what it could not see.
    probe = ledger.read(path=src)
    if probe.malformed:
        raise RescrubRefused(
            f"{probe.malformed} line{'' if probe.malformed == 1 else 's'} of your saved memory could not be "
            "read, and rewriting the file would delete "
            f"{'it' if probe.malformed == 1 else 'them'} for good. Nothing was changed. This usually means a "
            "damaged file — ask me to look at your memory's health first."
        )
    lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
    if lock_fd is None:
        raise RescrubRefused("another memory write is in progress. Nothing was changed; try again in a moment.")
    tmp = os.path.join(data_dir, _TEMP_PREFIX + uuid.uuid4().hex + _TEMP_SUFFIX)
    try:
        health = ledger.read(path=src)
        before = health.records
        after, changed = [], 0
        for record in before:
            out, did = _scrubbed(record)
            after.append(out)
            changed += 1 if did else 0
        # VERIFY BEFORE SWAPPING, not after. A count that does not match, or a record that changed in any way
        # other than its masked text, means the projection is wrong — and the moment to find that out is while
        # the original file is still the canonical one.
        if len(after) != len(before):
            raise RescrubRefused(f"internal check failed: {len(before)} records went in and {len(after)} came "
                                 "out. Nothing was changed.")
        for original, cleaned in zip(before, after):
            if {k: v for k, v in original.items() if k != _TEXT_KEY} != \
               {k: v for k, v in cleaned.items() if k != _TEXT_KEY}:
                raise RescrubRefused("internal check failed: a record changed in more than its text. "
                                     "Nothing was changed.")
        expected = _digest_of(after)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            for record in after:
                os.write(fd, (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            if health.torn_raw:
                os.write(fd, health.torn_raw)   # a crash-torn tail is preserved, exactly as a normal read leaves it
            os.fsync(fd)
        finally:
            os.close(fd)
        # ROUND-TRIP: read the temp back through the same reader and compare the checksum. This is what catches
        # an encoding fault or a truncated write before the swap, rather than after it.
        if _digest_of(ledger.read(path=tmp).records) != expected:
            raise RescrubRefused("internal check failed: the cleaned copy did not read back identically. "
                                 "Nothing was changed.")
        # BEFORE the swap, never after — the ordering compaction uses for the same reason. Bumped after, a
        # failed bump would leave the index stamped current over the OLD text, so every search would keep
        # serving back the very secrets this just masked, silently, behind a success message. Bumped first, a
        # failed swap costs one unnecessary rebuild and nothing else.
        ledger.bump_index_epoch(for_path=src)
        ledger.replace_ledger(tmp, path=src)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
        capture._release_lock(lock_fd)
    # The derived stores are rebuilt AFTER the swap and outside the lock: both are throwaway, and a failure
    # here costs a slow first search, never the cleaned ledger.
    try:
        index.rebuild(ledger_file=src)
    except Exception:  # noqa: BLE001 — derived and rebuildable; never let it undo a successful clean
        pass
    _drop_semantic_passages(data_dir)
    return {"status": "ok", "records": len(before), "changed": changed,
            "message": (f"Cleaned {changed} of {len(before)} saved records." if changed
                        else f"Checked all {len(before)} saved records; none needed cleaning.")}


def _drop_semantic_passages(data_dir: str) -> None:
    """Drop the meaning-based store's passages so they are recomputed from the cleaned text.

    Best-effort by design: the semantic module is optional and its store is derived. A machine without it has
    nothing to drop, and a fault here must never undo a clean that already succeeded."""
    try:
        from memory.semantic import store as vstore
    except Exception:  # noqa: BLE001 — the optional module is simply not installed
        return
    try:
        import sqlite3
        path = vstore.store_path()
        if not os.path.exists(path):
            return
        conn = sqlite3.connect(path)
        try:
            conn.execute("DELETE FROM passages")
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return


def main(argv: list) -> int:
    cmd = argv[0] if argv else "plan"
    if cmd == "plan":
        report = plan()
        print(f"{report['records']} saved records; {report['would_change']} contain text that would be masked.")
        print("Run `rescrub.py run` to clean them. A copy of your memory is saved first.")
        return 0
    if cmd == "run":
        try:
            report = run()
        except RescrubRefused as exc:
            print(f"Not cleaned: {exc}")
            return 1
        print(report["message"])
        if report.get("changed"):
            print("A copy of your memory from before this ran is saved in your backup. Restoring it would "
                  "bring the unmasked text back — you can simply run this again if that ever happens.")
        return 0
    print(f"usage: rescrub.py [plan|run]\nunknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
