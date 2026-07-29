"""store.py — the vector store behind meaning-based recall, and the search over it.

WHY THIS IS ITS OWN FILE. The vectors live in `vectors.sqlite3`, beside the ledger and the keyword index but
separate from both. The keyword index belongs to the always-present memory substrate and its rebuild is
strict: any fault there propagates into consolidation, compaction and restore, which is why an optional
capability must not be able to reach it. Keeping the vectors apart means this module can be absent, stale,
or broken without the keyword floor noticing — and means installing it needs no rebuild of anything else.

WHY PASSAGES AND NOT WHOLE RECORDS. A vector for a passage is the average of its word vectors, so averaging
a long record pulls every distinct thing it says toward one blurred middle. Measured on real captured
conversation, whole-record vectors ranked plainly unrelated records above the right ones. Records are
therefore split into short passages and each is embedded on its own; a record scores as well as its best
passage. This is why the store holds more rows than the ledger has records.

WHAT IT IS. A throwaway derivative, exactly like the keyword index: every vector is reproducible from the
ledger and the committed word table, deleting the file loses nothing, and it is never the only copy of
anything.

HOW IT STAYS CURRENT. There is no background job and no capture-time work. The store reconciles itself at
the moment a question is asked: records that have appeared since last time are embedded, and records that
have gone are dropped, deletions first.

WHAT ERASURE GUARANTEES HERE, EXACTLY. An erased record can never be returned by this operation, from the
moment its text leaves the ledger — the answer is assembled only from records read live in that same pass,
so a stale row cannot reach a caller even before it is deleted. What is NOT instant is the row itself: the
vectors of an erased record stay in this local file until the next meaning-based question, which is when the
reconcile runs. What lingers is a lossy numeric derivative, not the wording — this store holds no text at
all — it lives only in the gitignored memory directory, and it is unreachable by every read path. Said
plainly rather than claimed away: the erasure paths rebuild the keyword index in the same pass, and they do
not know this file exists.

WHY BRUTE FORCE. Similarity here is one matrix multiply against a few tens of thousands of rows — a handful
of milliseconds, scored in blocks so peak memory stays flat however large the store grows. A vector index
would add a compiled SQLite extension, which has no source fallback on several platforms this engine
supports, to save time that is not being spent.
"""

import os
import re
import sqlite3
import sys

# Make the package parent (.engine/tools) importable so `from memory import …` resolves when this file is run
# directly as a script (the demo). Imported as `memory.semantic.store`, the parent is already on sys.path, so
# this is a guarded no-op. Three levels up rather than two: this package is nested inside `memory`.
_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import forget, index, ledger  # noqa: E402
from memory.semantic import embed  # noqa: E402

STORE_FILENAME = "vectors.sqlite3"

# Bumped whenever what the store holds, or how a vector is derived, changes — so an older store is rebuilt
# rather than silently mixed with vectors made a different way.
SCHEMA_VERSION = 2

DEFAULT_LIMIT = 10

# Passage length, in characters. Short enough that one passage is about one thing, long enough to carry a
# whole thought — sentence boundaries are preferred and this is the cap when a sentence runs long.
PASSAGE_CHARS = 220

# The most passages taken from any one record, so a single enormous record cannot dominate the store.
MAX_PASSAGES = 24

# Rows scored per block. Keeps peak memory flat: the stored rows are one byte per dimension and only a
# block is widened to float at a time.
SCORE_BLOCK = 8192

# The floor below which a passage is not offered at all. It cuts obvious noise; it does NOT decide relevance,
# and no number could.
#
# THE SCORE RANKS, IT DOES NOT VERDICT — measured, not assumed. On real captured history a question whose
# answer was genuinely present scored 0.478, while a deliberately irrelevant question about a broken coffee
# machine still peaked at 0.402 by sharing the word "broken". Meanwhile a true zero-overlap paraphrase —
# "did we consider running it on a timer" against "we ruled out a cron job and hooked the calendar" — scored
# only 0.244, because sharing no vocabulary is exactly the case this exists for and exactly the case that
# scores low. The relevant and irrelevant distributions therefore OVERLAP: any threshold high enough to
# exclude the coffee machine also excludes the cron job.
#
# So the floor is set low, where plainly unrelated text sits (sourdough bread against an engine ledger scores
# 0.077), and the judgement is left where it can actually be made: the caller receives the passage that
# matched, and reads it. That is not a shortcut — it is the same division of labour the rest of recall uses,
# where meaning is supplied by the reading model rather than by the retrieval process.
#
# The figure is computed here, used to order results and to apply this floor, and deliberately NOT relayed to
# the caller by the transport above this module. It ranks within one answer; it does not compare across
# questions, and a number printed beside a result is read as confidence no matter what the words around it
# say. The ordering and the passage carry everything a caller can actually act on.
MIN_SIMILARITY = 0.15

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def store_path(path: "str | None" = None) -> str:
    """Where the vectors live — beside the ledger, in the gitignored memory directory."""
    if path is not None:
        return path
    return os.path.join(ledger.ledger_dir(), STORE_FILENAME)


def passages(text: str) -> list:
    """`text` split into short passages on sentence and paragraph boundaries.

    Sentences are packed together up to the length cap rather than emitted one at a time, so a passage
    carries enough context to mean something. A record with no sentence punctuation still yields its
    opening — never an empty list, which would make the record unreachable.
    """
    out, current = [], ""
    for piece in _SENTENCE_SPLIT.split(text):
        piece = piece.strip()
        if not piece:
            continue
        # A single sentence can exceed the cap on its own — minified code, a pasted table, prose with no
        # terminator at all. Splitting it is what keeps the cap real: an un-split run averages into the same
        # blurred middle that passages exist to avoid, and it would silently be the whole record.
        while len(piece) > PASSAGE_CHARS:
            if current:
                out.append(current)
                current = ""
            out.append(piece[:PASSAGE_CHARS])
            piece = piece[PASSAGE_CHARS:]
        if not piece:
            continue
        if current and len(current) + 1 + len(piece) > PASSAGE_CHARS:
            out.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        out.append(current)
    return out[:MAX_PASSAGES]


def _table_fingerprint() -> str:
    """Identifies the word table the stored vectors were made with.

    Vectors from two different tables are not comparable, and comparing them degrades ranking silently
    rather than raising. Recording which table produced the store lets a change discard it wholesale.
    """
    import json

    with open(embed.CHECKSUMS_FILE, encoding="utf-8") as fh:
        recorded = json.load(fh)
    name = os.path.basename(embed.TABLE_FILE)
    return ((recorded.get("files") or {}).get(name) or {}).get("sha256", "")


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS passages ("
                 "  record_id TEXT NOT NULL,"
                 "  ordinal INTEGER NOT NULL,"
                 "  vec BLOB NOT NULL,"
                 "  scale REAL NOT NULL,"
                 # A digest of the text these vectors were made from. A record can be rewritten in place
                 # keeping its id — a ledger migration does exactly that — and without this the old vectors
                 # survive, so the record answers for wording it no longer contains and the passage recovered
                 # for the caller comes back EMPTY. That would strip the one piece of evidence this whole
                 # design asks the caller to judge on.
                 "  text_digest TEXT NOT NULL,"
                 "  PRIMARY KEY (record_id, ordinal))")
    conn.execute("CREATE TABLE IF NOT EXISTS meta ("
                 "  rowid INTEGER PRIMARY KEY CHECK (rowid = 1),"
                 "  schema_version INTEGER NOT NULL,"
                 "  table_fingerprint TEXT NOT NULL)")
    row = conn.execute("SELECT schema_version, table_fingerprint FROM meta WHERE rowid = 1").fetchone()
    current = (SCHEMA_VERSION, _table_fingerprint())
    if row is None:
        conn.execute("INSERT INTO meta (rowid, schema_version, table_fingerprint) VALUES (1, ?, ?)", current)
        conn.commit()
    elif tuple(row) != current:
        # A different table, or a different way of deriving a vector: every stored row is unusable.
        #
        # DROP, never DELETE. A version bump can change the table's SHAPE, and `CREATE TABLE IF NOT EXISTS`
        # is a no-op against a table that already exists — so emptying the rows would leave an OLD-shaped
        # table stamped as current, and the next query would fail on a missing column, for good, with no
        # path back. Dropping it forces the create above to run for real on the next connect.
        conn.execute("DROP TABLE IF EXISTS passages")
        conn.execute("UPDATE meta SET schema_version = ?, table_fingerprint = ? WHERE rowid = 1", current)
        conn.commit()
        conn.close()
        return _connect(path)
    return conn


# The last derivation, keyed by everything that can invalidate it. `search` is the only caller and this
# module is loaded inside the long-lived recall server, so a session that asks several meaning-based questions
# in a row re-derives once instead of once per question.
#
# WHAT IT COSTS, stated because a comment further down in `search` warns against exactly this shape: it says
# materialising the row set whole "would make peak memory linear in store size". The distinction that makes
# this a different trade is PEAK versus RESIDENT. `_live_text` is derived on every query regardless and held
# for that query's duration, so the peak is unchanged; what changes is that the derivation now survives
# BETWEEN queries in the long-lived recall server — roughly one ledger's worth, about 30 MB on a 30 MB store,
# never released. That is a real cost and it is the one being bought: 334 ms to 118 ms on a repeat question.
#
# It is also NOT the durable byte-cursor the design review named. A cursor kept in the vector store would
# survive a restart; this does not, so the first query in each process still pays full price, and `_reconcile`
# still re-checks its passages per query. What is fixed is the repeat cost inside one session, which is where
# a recall workflow actually asks several questions in a row.
#
# WHY THESE FOUR KEYS ARE SUFFICIENT, which is the whole safety argument. A record can only enter the live set
# by being APPENDED (the file grows, so size changes) and can only leave it by being withheld (which bumps the
# index epoch), by an erasure compaction, or by the operator's re-scrub (both of which rewrite the file, and
# the first bumps the generation). Size plus mtime catches every append including a same-size rewrite; the two
# counters catch every removal. Miss any of them and this would serve a withheld record from cache, so the
# cache is deliberately dropped on ANY doubt rather than refreshed cleverly.
_LIVE_CACHE: dict = {}


def _live_key(path: "str | None") -> tuple:
    """The identity of the ledger's current content, or a never-matching key when it cannot be read."""
    from memory import ledger as _ledger
    target = path or _ledger.ledger_path()
    try:
        stat = os.stat(target)
        return (target, stat.st_size, stat.st_mtime_ns,
                _ledger.generation(for_path=target), _ledger.index_epoch(for_path=target))
    except Exception:  # noqa: BLE001 — unreadable: never reuse a cache against a ledger we cannot identify
        return (target, None, None, None, None)


def _live_text(path: "str | None" = None) -> dict:
    """{record_id: (record, searchable_text)} for everything recall is allowed to surface.

    The text is `index._record_text` — the same projection the keyword path uses, so both paths see one
    record the same way, and a harness-inserted block is excluded from meaning-based reach exactly as it is
    excluded from keyword reach. A record whose projection is empty is skipped outright: it has no meaning
    to match, and an all-zero vector would otherwise be a row that matches everything equally.

    CACHED on the ledger's identity (see `_live_key`), because this is a full pass over the whole store and it
    ran on EVERY meaning-based question — measured at 215 ms of a 334 ms query on a 30 MB store, with a further
    48 ms re-hashing every record behind it. A miss re-derives from scratch; there is no partial update, so a
    stale cache cannot survive any change that could remove a record from recall.
    """
    key = _live_key(path)
    if key[1] is not None and _LIVE_CACHE.get("key") == key:
        return _LIVE_CACHE["value"]
    out = {}
    for record in forget.live_records(path):
        if not isinstance(record, dict):
            continue
        rid = record.get("id")
        if not rid:
            continue                          # a legacy record with no id cannot be tracked or re-found
        text = index._record_text(record)
        if text.strip():
            out[rid] = (record, text)
    if key[1] is not None:
        _LIVE_CACHE.clear()
        _LIVE_CACHE.update({"key": key, "value": out})
    return out


def _digest(text: str) -> str:
    """A short fingerprint of a record's searchable text, so a rewrite in place is noticed."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


def _quantize(vectors):
    """Unit vectors as int8 rows plus a per-row scale — a quarter of the size, no measurable ranking cost."""
    import numpy

    scales = numpy.abs(vectors).max(axis=1)
    scales[scales == 0] = 1.0
    rows = numpy.round(vectors / scales[:, None] * 127).astype(numpy.int8)
    return rows, scales


def _reconcile(conn: sqlite3.Connection, live: dict) -> dict:
    """Bring the open store in line with `live`: drop departed records, embed newly-arrived ones.

    The one place reconciliation happens, so the answer a question gets and the answer a maintenance run
    reports can never be computed from differently-reconciled stores. Deletions are applied and committed
    before any embedding, so a record that left is gone even if embedding then fails.
    """
    stored = {}
    for rid, digest in conn.execute("SELECT DISTINCT record_id, text_digest FROM passages"):
        stored[rid] = digest
    gone = set(stored) - live.keys()
    # A record whose text changed under the same id is as stale as one that left: its rows are dropped and
    # rebuilt rather than kept, so no vector ever outlives the wording it was made from.
    changed = {rid for rid in live if rid in stored and stored[rid] != _digest(live[rid][1])}
    stale = gone | changed
    if stale:
        conn.executemany("DELETE FROM passages WHERE record_id = ?", [(rid,) for rid in stale])
        conn.commit()
    fresh = [rid for rid in live if rid not in stored or rid in changed]
    if fresh:
        texts, owners, ordinals, digests = [], [], [], []
        for rid in fresh:
            digest = _digest(live[rid][1])
            for ordinal, passage in enumerate(passages(live[rid][1])):
                texts.append(passage)
                owners.append(rid)
                ordinals.append(ordinal)
                digests.append(digest)
        if texts:
            rows, scales = _quantize(embed.embed_many(texts))
            conn.executemany(
                "INSERT OR REPLACE INTO passages (record_id, ordinal, vec, scale, text_digest) "
                "VALUES (?, ?, ?, ?, ?)",
                [(owners[i], ordinals[i], rows[i].tobytes(), float(scales[i]), digests[i])
                 for i in range(len(texts))])
    conn.commit()
    return {"embedded": len(fresh), "dropped": len(gone), "total": len(live)}


def sync(*, ledger_file: "str | None" = None, store_file: "str | None" = None) -> dict:
    """Reconcile the store against the ledger: embed what is new, drop what is gone.

    Returns plain counts. Raises `embed.TableUnavailable` when the word table cannot be trusted — the
    caller turns that into a stated reason rather than an empty answer.
    """
    live = _live_text(ledger_file)
    conn = _connect(store_path(store_file))
    try:
        return _reconcile(conn, live)
    finally:
        conn.close()


def _best_by_record(cursor, live: dict, question):
    """(best, scanned): the closest passage of each live record, and how many passages were compared.

    Streams the cursor in blocks so peak memory is bounded by SCORE_BLOCK rather than by the store's size,
    and considers only rows still present in the live read — the second erasure guarantee, applied before a
    departed record's vector is ever scored.
    """
    import numpy

    best: dict = {}
    scanned = 0
    width = question.shape[0]
    while True:
        rows = cursor.fetchmany(SCORE_BLOCK)
        if not rows:
            break                       # only an exhausted cursor returns nothing
        block = [row for row in rows if row[0] in live]
        if not block:
            continue                    # a block that was entirely departed records; more may follow
        matrix = numpy.frombuffer(b"".join(row[1] for row in block), dtype=numpy.int8)
        matrix = matrix.reshape(len(block), width).astype(numpy.float32)
        scales = numpy.fromiter((row[2] for row in block), dtype=numpy.float32, count=len(block))
        # The stored row is a unit vector scaled into int8; restoring the scale restores the cosine.
        scores = (matrix @ question) * (scales / 127.0)
        for offset, score in enumerate(scores):
            rid, ordinal = block[offset][0], block[offset][3]
            if score > best.get(rid, (-2.0, 0))[0]:
                best[rid] = (float(score), int(ordinal))
        scanned += len(block)
    return best, scanned


def search(query: str, *, limit: int = DEFAULT_LIMIT, ledger_file: "str | None" = None,
           store_file: "str | None" = None) -> dict:
    """The records closest in meaning to `query`, best first, each with how close it was.

    Reconciles first, so the answer covers everything currently in the ledger and nothing that has left it.
    Every returned record comes from the live read of the ledger performed in that same pass, and a record
    scores as well as its best passage.
    """
    live = _live_text(ledger_file)
    conn = _connect(store_path(store_file))
    try:
        reconciled = _reconcile(conn, live)
        # Streamed in blocks, never fetchall(): the row set grows with the store, and materialising it whole
        # would make peak memory linear in store size — the same unbounded read the keyword path already fixed.
        best, scanned = _best_by_record(
            conn.execute("SELECT record_id, vec, scale, ordinal FROM passages"), live, embed.embed(query))
    finally:
        conn.close()

    if not best:
        # Every key the populated return carries must be present here too. A caller unpacks this dict
        # positionally, and a deployed repo starts with an empty ledger — so the shape a fresh project sees
        # FIRST is this one, and an omitted key is a crash on the first question ever asked.
        return {"records": [], "scores": [], "passages": [], "searched": scanned,
                "embedded": reconciled["embedded"]}

    ranked = sorted(best.items(), key=lambda pair: -pair[1][0])[:max(int(limit), 1)]
    records, scores_out, matched = [], [], []
    for rid, (score, ordinal) in ranked:
        if score < MIN_SIMILARITY:
            break                              # sorted best-first, so everything after this is further away
        # The passage that actually matched — recomputed from the same text, never stored twice. Without it
        # a caller sees a record's opening and judges relevance on words that had nothing to do with the hit.
        found = passages(live[rid][1])
        if ordinal >= len(found):
            continue        # no recoverable passage means no evidence, and evidence is the whole offer
        records.append(live[rid][0])
        scores_out.append(round(score, 4))
        matched.append(found[ordinal])
    return {"records": records, "scores": scores_out, "passages": matched,
            "searched": scanned, "embedded": reconciled["embedded"]}


# --- Operator demonstration -------------------------------------------------------------------------------
# A runnable proof, on a throwaway practice store in a temp folder — never the real one:
#     uv run --directory .engine --frozen -- python tools/memory/semantic/store.py demo
#
# It plants a note whose wording deliberately shares nothing with the question that should find it, then
# checks BOTH halves. Either half failing exits non-zero: if keyword search finds the note, the case was not
# genuinely word-free and proves nothing; if meaning-based recall misses it, the capability does not work.

_DEMO_NOTE = "We ruled out a cron job and hooked the calendar instead, so nothing polls on a schedule."
_DEMO_QUESTION = "did we consider having it run automatically on a timer"
_DEMO_UNRELATED = "sourdough bread proofs better with rye flour and a longer cold rest"


def _demo() -> int:
    import shutil
    import tempfile
    import time

    from memory import index as _index
    from memory import records as _records
    from memory import recall as _recall

    scratch = tempfile.mkdtemp(prefix="engine-semantic-demo-")
    ledger_file = os.path.join(scratch, "ledger.ndjson")
    index_file = os.path.join(scratch, "index.sqlite3")
    store_file = os.path.join(scratch, "vectors.sqlite3")
    # The same refusal the window reader carries: this writes and prints, so it must never touch the real store.
    _recall.assert_not_live_store(scratch, ledger_file)

    previous = os.environ.get("ENGINE_MEMORY_DIR")
    os.environ["ENGINE_MEMORY_DIR"] = scratch
    try:
        print("Meaning-based recall — finding a note by what it means, not the words you use")
        print("=" * 78)
        print()
        for text in (_DEMO_NOTE, _DEMO_UNRELATED):
            ledger.append({_records.RECORD_ID_KEY: _records.new_record_id(), "ts": int(time.time()),
                           "role": "decision", "tags": [], "text": text}, path=ledger_file)
        _index.rebuild(ledger_file=ledger_file, index_file=index_file)
        print(f'Saved into a practice store:\n  "{_DEMO_NOTE}"\n  "{_DEMO_UNRELATED}"')
        print(f'\nThe question asked later:\n  "{_DEMO_QUESTION}"')
        print("\nIt shares no words with the note — no 'cron', no 'calendar', no 'schedule'.")
        print()

        by_word = _index.search(_DEMO_QUESTION, ledger_file=ledger_file, index_file=index_file)
        word_missed = not by_word.records
        print(f"  looking it up by the WORDS in the question .......... "
              f"{'found nothing' if word_missed else 'FOUND IT — the question was not word-free'}")

        found = search(_DEMO_QUESTION, ledger_file=ledger_file, store_file=store_file)
        hit = bool(found["records"]) and "cron job" in (found["records"][0].get("text") or "")
        print(f"  looking it up by MEANING ............................ "
              f"{'found the note' if hit else 'MISSED IT'}")
        if hit:
            print(f'      it returned the sentence that matched: "{found["passages"][0][:70]}…"')

        noise = search("what is the best way to renew a passport",
                       ledger_file=ledger_file, store_file=store_file)
        kept_out = all("cron job" not in (r.get("text") or "") for r in noise["records"])
        print(f"  an unrelated question does not return the note ...... {'correct' if kept_out else 'WRONG'}")
        print()
        print("  One honest limit, worth seeing before you rely on this. Only two notes are saved here, so")
        print("  the unrelated question above had almost nothing to reach for. On a real store of thousands,")
        print("  an unrelated question often DOES come back with something — the nearest thing present, which")
        print("  is not the same as an answer. That is why every result carries the sentence that matched:")
        print("  reading it is how you tell a real hit from the nearest miss, and there is no score that")
        print("  would tell you instead.")

        ok = word_missed and hit and kept_out
        print()
        if ok:
            print("What this changes for you: until now, asking about something recorded in different words")
            print("found nothing at all — the lookup matched words, so a question phrased your way missed a")
            print("note phrased another way. It can now also search by meaning, and it shows you the sentence")
            print("that matched, so you can see for yourself why it was offered. It shows the sentence rather")
            print("than a confidence figure on purpose: how near two pieces of text are does not reliably say")
            print("whether one answers a question about the other, and a number would suggest otherwise.")
            print("Word-based lookup is unchanged and still the right tool for an exact phrase.")
        else:
            print("Meaning-based recall is NOT working as described above.")
        return 0 if ok else 1
    finally:
        if previous is None:
            os.environ.pop("ENGINE_MEMORY_DIR", None)
        else:
            os.environ["ENGINE_MEMORY_DIR"] = previous
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "demo":
        return _demo()
    print("usage: store.py demo")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
