"""ledger.py — the engine's memory ledger: the canonical, append-only, plain-text store.

This is the foundation of the memory substrate (SQLite + FTS5).
The locked design makes this NDJSON ledger the ONE
source of truth; the SQLite/FTS5 index built later is a throwaway accelerator rebuilt from here,
and backup is "copy this file". So this file's integrity is a law, not a leaf:

  - Writes are SERIALIZED. A bare append is atomic only for writes under the platform's PIPE_BUF
    bound (~4 KB); an episodic memory record routinely exceeds that, so two live sessions appending
    at once could tear a line. Every append takes an exclusive advisory lock (`fcntl.flock`) so the
    writes never interleave. (The serialization requirement is the law; flock is the build-spec leaf.)

  - Reads are LINE-RESILIENT. The reader skips and counts a malformed line rather than halting (one
    bad line never costs the records after it), and tolerates a torn TRAILING line from a crash
    mid-append by rejecting it structurally: a record is complete only if it is newline-terminated.
    A final line without its terminating newline is a half-written record and is dropped, never read
    back as if real.

  - Each record is one JSON object per line, terminated by a single "\n" (the record terminator).
    `json.dumps` escapes any newline inside the data, so the trailing "\n" is unambiguous.

This module's scope: the read/write primitives only. They are RECORD-AGNOSTIC — they store and return any
JSON-serializable dict. The closed memory *record shape* (the role vocabulary) and the per-record
ledger-version envelope belong to `records.py`, not here; this module never inspects what it is handed.

This module is a leaf: it never logs findings or writes telemetry of its own. It RETURNS its
read-health report to the caller; surfacing degraded recall is boot/telemetry's job.

Path resolution shares the ledger across every git worktree of one clone (the engine's AI sessions
each work in their own worktree under .claude/worktrees/, but the project's memory is one store): it
resolves to the shared clone root's `.engine/memory/`, overridable via the ENGINE_MEMORY_DIR env var.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

try:
    import fcntl  # POSIX advisory file locking (macOS dev + ubuntu CI); absent on Windows.
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - the engine targets POSIX; degrade rather than crash.
    _HAVE_FCNTL = False

# The on-disk framing version (newline-terminated NDJSON) — the FILE format, not the record shape. This is the
# version a restore routes a migration on: `restore_vault` compares the backup manifest's recorded version
# against this one and asks `ledger_migrations` for the chain between them. Exposed so the backup
# snapshot-manifest has a legible version source. (Each record also carries its own `v` shape stamp, written by
# every producer; nothing reads it while only one record shape has ever existed — it is what would let a reader
# tell two shapes apart if a second ever landed.)
LEDGER_FORMAT_VERSION = 1

ENV_DIR = "ENGINE_MEMORY_DIR"
DATA_SUBDIR = os.path.join(".engine", "memory")
LEDGER_FILENAME = "ledger.ndjson"
META_FILENAME = "ledger-meta.json"   # the monotonic ledger-generation sidecar; gitignored sibling

# The platform durability barrier. On Darwin a bare os.fsync does NOT guarantee bytes reached the platter;
# fcntl.F_FULLFSYNC does (the locked crash-safe-swap law names it). Absent elsewhere — then os.fsync is the floor.
_F_FULLFSYNC = getattr(fcntl, "F_FULLFSYNC", None) if _HAVE_FCNTL else None


@dataclass
class LedgerRead:
    """The result of reading the ledger: the intact records plus an honest read-health report.

    `malformed` counts complete lines that failed to parse (each skipped, never halting the read).
    `torn_trailing` is True when the final line lacked its terminating newline — a half-written
    record from a crash mid-append, dropped rather than read back as real.
    `torn_raw` carries that trailing fragment's EXACT bytes when `torn_trailing` (else None), so a
    caller that rewrites the ledger (compaction) can re-emit it verbatim and preserve it for a later
    append to heal — never silently erasing recoverable recall. Raw bytes, not decoded text, so a
    fragment that split a multibyte character at the crash point round-trips unchanged.
    """

    records: list = field(default_factory=list)
    malformed: int = 0
    torn_trailing: bool = False
    torn_raw: "bytes | None" = None


def _git_common_root(cwd: str | None = None) -> str | None:
    """The shared clone root (parent of the common `.git` dir), so every worktree shares one ledger.

    Returns None for a bare repo, an unusual layout, or when git is unavailable — the caller then
    falls back to a CWD-relative path. (Mirrors checkout_health.py's `.git`-name guard: only resolve
    the parent when the common dir is actually named `.git`; never guess otherwise.)
    """
    base = cwd or os.getcwd()
    try:
        out = subprocess.run(
            ["git", "-C", base, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    if not out:
        return None
    common = out if os.path.isabs(out) else os.path.join(base, out)
    common = os.path.normpath(os.path.abspath(common))
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return None


def ledger_dir(cwd: str | None = None) -> str:
    """Resolve the directory holding the ledger: ENGINE_MEMORY_DIR override, else the shared clone
    root's `.engine/memory/`, else a CWD-relative fallback."""
    env = os.environ.get(ENV_DIR)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    base_cwd = cwd or os.getcwd()
    root = _git_common_root(base_cwd)
    base = root if root is not None else base_cwd
    return os.path.join(base, DATA_SUBDIR)


def ledger_path(cwd: str | None = None) -> str:
    """The resolved path to the ledger file."""
    return os.path.join(ledger_dir(cwd), LEDGER_FILENAME)


def append(record: dict, *, path: str | None = None) -> None:
    """Append one record to the ledger as a single newline-terminated JSON line, under an exclusive
    advisory lock so concurrent writers never tear a line. Creates the data directory on first write.

    Before writing, HEALS a torn trailing line: if a prior append crashed mid-write the file ends
    without its terminating newline, and a bare O_APPEND would weld the new record onto those torn
    bytes — fusing them into one unparseable line and silently losing the new record. So when the file
    does not end in a newline we first append one, isolating the torn fragment on its own (rejected,
    counted-as-malformed) line, so the new record lands clean and is never lost. This upholds the read
    law above — *one bad line never costs the records after it* — on the write side. (A prior record
    that was complete JSON but lost only its terminating newline is thereby recovered; a truly
    half-written fragment stays invalid JSON and is still read back as malformed, never resurrected.)

    `record` may be any JSON-serializable dict (record shape is `records.py`'s concern, never this module's).
    """
    target = path or ledger_path()
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    data = line.encode("utf-8")
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_EX)
        # Heal a torn trailing line from a crashed prior append BEFORE writing (under the lock, so two
        # writers can never both heal): if the file is non-empty and its last byte is not the record
        # terminator, the previous write died mid-record — start ours on a clean line so O_APPEND cannot
        # fuse them. os.pread reads the last byte without moving the write position; O_APPEND keeps the
        # write at EOF regardless.
        size = os.fstat(fd).st_size
        if size > 0 and os.pread(fd, 1, size - 1) != b"\n":
            os.write(fd, b"\n")
        # Write the whole framed record while holding the lock (O_APPEND keeps every write at EOF;
        # the lock keeps a second writer out until we have written all bytes and flushed them).
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read(*, path: str | None = None) -> LedgerRead:
    """Read every intact record from the ledger, line-resilient, returning a read-health report.

    Skips and counts a malformed (complete-but-unparseable) line; skips blank lines without counting
    them; drops a torn trailing line (a record whose terminating newline never landed), flags it, and
    captures its exact bytes in `torn_raw` so a rewriter can preserve it (a later append heals it).
    A missing ledger file reads as empty — the substrate ships empty.
    """
    target = path or ledger_path()
    result = LedgerRead()
    if not os.path.exists(target):
        return result
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if not raw.endswith("\n"):
                # Only the final line of a file can lack its newline: a half-written record from a
                # crash mid-append. Reject it structurally (the missing terminator) and stop.
                result.torn_trailing = True
                break
            stripped = raw.strip()
            if not stripped:
                continue  # a blank line is not a record and is not corruption
            try:
                result.records.append(json.loads(stripped))
            except ValueError:
                result.malformed += 1
    if result.torn_trailing:
        # Capture the trailing fragment's EXACT bytes (everything after the last record terminator) —
        # not the errors="replace" decoding above, which would mangle a fragment that split a multibyte
        # character at the crash point. A cheap tail-read, only on the rare torn path.
        with open(target, "rb") as fb:
            blob = fb.read()
        cut = blob.rfind(b"\n")
        result.torn_raw = blob[cut + 1:] if cut != -1 else blob
    return result


def iter_records(*, path: str | None = None):
    """Stream the intact records, quietly skipping malformed and torn lines. The streaming form for
    a full scan (e.g. rebuilding the derived index); use read() when the read-health report is wanted.
    """
    target = path or ledger_path()
    if not os.path.exists(target):
        return
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if not raw.endswith("\n"):
                return  # torn trailing record — drop it
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError:
                continue
            yield record


# --- Generation stamp + the crash-safe swap primitive (compaction) -------------------
# The ledger-generation is a monotonic integer bumped once per compaction. It lets a derived index that was
# built against an OLDER ledger generation be DETECTED as stale (index.py gates its fast path on a match) and
# fully rebuilt — never incrementally patched — so a compaction-erased record can never resurface from a stale
# index (the locked law). It is carried in a tiny sidecar (NOT in the append-only ledger, so every
# existing reader is untouched), resolved to the SAME directory as the ledger it describes.

def meta_path(cwd: str | None = None, *, for_path: str | None = None) -> str:
    """The generation-sidecar path, in the SAME directory as its ledger. When `for_path` is given (an explicit
    ledger file — what index.rebuild/query pass), the sidecar is its sibling, so a query over a specific store
    reads THAT store's generation, never the default dir's. Otherwise the resolved `ledger_dir(cwd)`."""
    if for_path is not None:
        return os.path.join(os.path.dirname(os.path.abspath(for_path)), META_FILENAME)
    return os.path.join(ledger_dir(cwd), META_FILENAME)


def generation(cwd: str | None = None, *, for_path: str | None = None) -> int:
    """The ledger's monotonic generation: 0 for a never-compacted ledger, +1 per compaction. A missing /
    unreadable / non-int / negative sidecar reads as 0 (a fresh store) — fail-safe: an unreadable stamp makes a
    real index look in-generation rather than wrongly forcing a scan storm; correctness still holds because a
    genuinely older index carries an older stamp."""
    path = meta_path(cwd, for_path=for_path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    val = data.get("generation") if isinstance(data, dict) else None
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        return 0
    return val


def _read_sidecar(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sidecar_with(path: str, **updates) -> dict:
    """The sidecar's current contents with `updates` applied — so a writer of one counter never silently
    discards the other. The file holds two independent numbers now, and each has exactly one writer."""
    data = _read_sidecar(path)
    data.update(updates)
    return data


def index_epoch(cwd: str | None = None, *, for_path: str | None = None) -> int:
    """A monotonic counter meaning "what the derived index is allowed to CONTAIN has changed".

    DELIBERATELY NOT `generation`, and the distinction is load-bearing. `generation` means "content was
    rewritten or removed", and a second reader depends on exactly that meaning: `restore_vault` refuses a
    backup whose generation is BEHIND the local store, on the reasoning that restoring it would undo
    deliberate removals, and raises a trust-critical finding saying so. Bumping `generation` to invalidate the
    index would therefore have told an operator who merely pinned a note that their backup could not be
    restored because notes had been deliberately removed — false, and refused on the day they needed it.

    So membership changes that remove nothing — a withhold, which hides without deleting — move this instead.
    Missing or malformed reads as 0, the same fail-safe `generation` uses."""
    data = _read_sidecar(meta_path(cwd, for_path=for_path))
    val = data.get("index_epoch")
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        return 0
    return val


def bump_index_epoch(cwd: str | None = None, *, for_path: str | None = None) -> int:
    """Increment and durably persist the index epoch (temp + fsync + atomic os.replace). Returns the new value.
    The CALLER must hold the single-writer lock, as with `bump_generation`."""
    path = meta_path(cwd, for_path=for_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    new_val = index_epoch(cwd, for_path=for_path) + 1
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, json.dumps(_sidecar_with(path, index_epoch=new_val),
                                separators=(",", ":")).encode("utf-8"))
        _durable_fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return new_val


def bump_generation(cwd: str | None = None, *, for_path: str | None = None) -> int:
    """Increment and durably persist the ledger generation (temp + fsync + atomic os.replace). Returns the new
    value. The CALLER must hold the single-writer lock (compaction does); this is not itself serialized. Bumped
    BEFORE the ledger swap so every crash window leaves the index's stamp != the sidecar (=> scan the current
    ledger, always correct), never a stale index trusted against a swapped ledger."""
    path = meta_path(cwd, for_path=for_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    new_val = generation(cwd, for_path=for_path) + 1
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, json.dumps(_sidecar_with(path, generation=new_val),
                                separators=(",", ":")).encode("utf-8"))
        _durable_fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return new_val


def set_generation(value: int, cwd: str | None = None, *, for_path: str | None = None) -> int:
    """Durably SET the generation to an explicit `value` (temp + fsync + atomic os.replace) — the sibling of
    bump_generation that RESTORE uses: a restored ledger must carry the BACKUP's true generation, not a
    blind +1, so the lineage and the *next* restore's resurrection compare stay faithful. A non-int/negative value
    clamps to 0 (the same fail-safe floor `generation` reads). The CALLER must hold the single-writer lock (restore
    does); this is not itself serialized. Returns the value written."""
    new_val = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    path = meta_path(cwd, for_path=for_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, json.dumps(_sidecar_with(path, generation=new_val),
                                separators=(",", ":")).encode("utf-8"))
        _durable_fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return new_val


def _durable_fsync(fd) -> None:
    """Flush `fd` to stable storage as durably as the platform allows: F_FULLFSYNC on Darwin (a true barrier),
    else os.fsync. A fsync fault must NEVER crash past the caller's lock-release, so every call is guarded —
    degrade rather than abort a critical section that still leaves an intact ledger."""
    if _F_FULLFSYNC is not None:
        try:
            fcntl.fcntl(fd, _F_FULLFSYNC)
            return
        except OSError:
            pass
    try:
        os.fsync(fd)
    except OSError:
        pass


def _fsync_dir(path: str) -> None:
    """fsync a directory so a rename within it survives a crash (rename atomicity is ordering, not durability).
    Best-effort: some platforms/filesystems refuse to fsync a directory fd — degrade, never crash."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def replace_ledger(temp: str, *, path: str | None = None) -> None:
    """Memory's own restore primitive: durably replace the canonical ledger with a COMPLETE `temp` written in
    the SAME directory — fsync the temp, atomic os.replace over the canonical name, fsync the directory. A
    cross-filesystem rename is not atomic, so `temp` MUST be a sibling of the ledger. The CALLER holds the
    single-writer lock across this. (Backup/restore reuses this exact mechanism; compaction is the
    same restore primitive applied internally.) Recovery binds to the fixed canonical name: a temp left
    by a crash before this rename is a complete same-schema file but is NEVER the canonical name, so it is
    ignored-and-reaped, never promoted."""
    target = path or ledger_path()
    fd = os.open(temp, os.O_RDONLY)
    try:
        _durable_fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, target)
    _fsync_dir(os.path.dirname(os.path.abspath(target)) or ".")


# --- Operator demonstration -------------------------------------------------------------------
# An operator-runnable fail-then-pass walkthrough (the same pattern other engine tools use, e.g.
# checkout_health.py's _demo). It exercises the REAL append/read above; only the file location is a
# throwaway temp folder, so it never touches any real data. Run it and vary the numbers yourself:
#     uv run --directory .engine --frozen -- python tools/memory/ledger.py demo
# The module-level helpers below exist only for this demo (multiprocessing under "spawn" needs the
# worker to be importable by name); normal use never imports multiprocessing.

_DEMO_WRITERS = 8
_DEMO_PER_WRITER = 15
_DEMO_BODY_LEN = 9000  # > the ~4 KB the OS appends in one shot — the size that tears if unprotected


def _demo_entry(writer: int, seq: int) -> dict:
    body = f"WRITER-{writer}-START " + ("." * _DEMO_BODY_LEN) + f" END-WRITER-{writer}"
    return {"writer": writer, "seq": seq, "body": body}


def _demo_naive_append(record: dict, path: str) -> None:
    """What a naive writer does: NO lock, and it writes the entry in two pieces, so concurrent writers
    scramble each other. This is the hazard we protect against — NOT the engine's real code."""
    line = json.dumps(record) + "\n"
    half = len(line) // 2
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line[:half].encode("utf-8"))
        os.write(fd, line[half:].encode("utf-8"))
    finally:
        os.close(fd)


def _demo_worker(mode: str, path: str, writer_id: int) -> None:
    writer = _demo_naive_append if mode == "naive" else (lambda r, p: append(r, path=p))
    for seq in range(_DEMO_PER_WRITER):
        writer(_demo_entry(writer_id, seq), path)


def _demo_run_writers(mode: str, path: str) -> None:
    import multiprocessing as mp

    procs = [mp.Process(target=_demo_worker, args=(mode, path, w)) for w in range(_DEMO_WRITERS)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()


def _demo() -> int:
    import tempfile

    expected = _DEMO_WRITERS * _DEMO_PER_WRITER
    print("=" * 78)
    print(f"TEST 1 — {_DEMO_WRITERS} sessions filing {_DEMO_PER_WRITER} large entries each, at the same moment")
    print(f"         (expecting {expected} whole entries)")
    print("=" * 78)
    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "naive.txt")
        _demo_run_writers("naive", bad)
        good = scrambled = 0
        sample = None
        for raw in open(bad, "r", encoding="utf-8", errors="replace"):
            if not raw.endswith("\n"):
                continue
            try:
                rec = json.loads(raw)
                body = rec["body"]
                if body.startswith(f"WRITER-{rec['writer']}-START") and body.endswith(f"END-WRITER-{rec['writer']}"):
                    good += 1
                    continue
            except Exception:
                pass
            scrambled += 1
            sample = sample or raw.strip()
        print("\n  WITHOUT protection (a naive writer):")
        print(f"    whole entries: {good} / {expected}     SCRAMBLED entries: {scrambled}")
        if sample:
            print(f"    e.g. a scrambled entry begins:  {sample[:88]}…")
            print("    ^ notice a START with the wrong writer's number, or two entries mashed together.")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ledger.ndjson")
        _demo_run_writers("real", path)  # the REAL engine code
        result = read(path=path)
        intact = sum(
            1 for r in result.records
            if r["body"].startswith(f"WRITER-{r['writer']}-START")
            and r["body"].endswith(f"END-WRITER-{r['writer']}")
        )
        print("\n  WITH the engine's memory file (the real code):")
        print(f"    whole entries: {intact} / {expected}     scrambled: {len(result.records) - intact}     corrupted on read: {result.malformed}")
        t1 = intact == expected and result.malformed == 0
        print(f"    => {'EVERY entry survived whole.' if t1 else '!!! something was lost'}")

    print("\n" + "=" * 78)
    print("TEST 2 — a crash leaves a half-written entry; does the good entry before it survive?")
    print("=" * 78)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ledger.ndjson")
        append({"note": "DO NOT LOSE THIS"}, path=path)
        with open(path, "a", encoding="utf-8") as fh:  # crash mid-write: no terminating newline
            fh.write('{"note":"half-written when the power went ou')
        result = read(path=path)
        print(f"\n  entries read back: {[r['note'] for r in result.records]}")
        print(f"  half-written entry dropped: {result.torn_trailing}")
        t2 = [r["note"] for r in result.records] == ["DO NOT LOSE THIS"] and result.torn_trailing
        print(f"  => {'The good entry survived; the half-written one was discarded.' if t2 else '!!! unexpected'}")

    print("\n" + "=" * 78)
    print("TEST 3 — a single corrupted entry in the middle; do the entries around it survive?")
    print("=" * 78)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ledger.ndjson")
        append({"note": "entry before the corruption"}, path=path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("@@@ this line is corrupted garbage @@@\n")
        append({"note": "entry after the corruption"}, path=path)
        result = read(path=path)
        print(f"\n  entries read back: {[r['note'] for r in result.records]}")
        print(f"  corrupted entries skipped (and counted): {result.malformed}")
        t3 = [r["note"] for r in result.records] == ["entry before the corruption", "entry after the corruption"]
        print(f"  => {'Both good entries survived; only the corrupted one was skipped.' if t3 else '!!! unexpected'}")

    print("\n" + "=" * 78)
    print("TEST 4 — a crash tears one entry; does the NEXT entry filed after it survive?")
    print("         (the loss this fix closes — an earlier build let the entry filed after a tear vanish)")
    print("=" * 78)
    with tempfile.TemporaryDirectory() as tmp:
        # BEFORE — the old behavior (a bare append with NO heal), on a real file, for contrast:
        old = os.path.join(tmp, "old.ndjson")
        append({"note": "KEEP-A"}, path=old)
        with open(old, "a", encoding="utf-8") as fh:                 # a crash leaves a torn half-entry
            fh.write('{"note":"half-written when the power went ou')
        with open(old, "a", encoding="utf-8") as fh:                 # OLD behavior: a bare append, no heal
            fh.write(json.dumps({"note": "KEEP-B-AFTER-CRASH"}) + "\n")
        before = read(path=old)
        print("\n  BEFORE this fix (a bare append with no heal):")
        print(f"    entries read back: {[r['note'] for r in before.records]}")
        print("    ^ KEEP-B-AFTER-CRASH was swallowed by the torn line and silently lost.")
        # AFTER — the real engine code (the healed append):
        new = os.path.join(tmp, "new.ndjson")
        append({"note": "KEEP-A"}, path=new)
        with open(new, "a", encoding="utf-8") as fh:                 # the same crash
            fh.write('{"note":"half-written when the power went ou')
        append({"note": "KEEP-B-AFTER-CRASH"}, path=new)             # the REAL healed append
        after = read(path=new)
        print("\n  AFTER this fix (the real engine code):")
        print(f"    entries read back: {[r['note'] for r in after.records]}")
        note = "   <- the torn half-entry, still rejected" if after.malformed else ""
        print(f"    corrupted entries skipped (and counted): {after.malformed}{note}")
        ok4 = "KEEP-B-AFTER-CRASH" in [r["note"] for r in after.records] and after.malformed == 1
        print(f"  => {'The entry filed AFTER the crash survived; the torn half-entry is still rejected.' if ok4 else '!!! unexpected'}")

    print("\n" + "-" * 78)
    print("Reminder: this demo proves only the filing cabinet's INTEGRITY — that it keeps what's filed,")
    print("whole, and never scrambles or silently loses it. Saving each turn's notes, tidying them into")
    print("summaries, and searching them are the capture / consolidation / search steps built on top.")
    ok_all = t1 and t2 and t3 and ok4
    if not ok_all:
        print("\nDEMO UNEXPECTED: a ledger-integrity guarantee did not hold (whole concurrent entries, "
              "torn-tail rejection, mid-file corruption skipped, or post-crash survival).", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    print("usage: ledger.py demo")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
