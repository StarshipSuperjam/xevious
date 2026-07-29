"""embed.py — turns text into a vector using the vendored word table.

The table is not a neural network. It is one row of numbers per vocabulary token, and embedding a passage
is: split into token ids, gather those rows, average them, scale to unit length. There is no attention, no
matrix-multiply chain, and no runtime beyond numpy — which is why the whole capability costs one dependency
and runs identically with no network.

WHY THIS TABLE AND NOT A SMALLER ONE. Table width is what decides whether two passages about different
things can be told apart, and the smallest table in this family was measured against this engine's own
history and could not do it: its best answers to real questions were wrong, and an unrelated question about
a broken coffee machine scored as high as a relevant one. A 512-wide table trained for retrieval answers
the same questions correctly and separates the unrelated one. The width is the capability; the 32 MB is
what it costs.

WHAT THE TABLE KNOWS, AND WHAT IT DOES NOT. The geometry it carries is general English: it places "cron"
near "scheduled" and "copy" near "text" because that is a property of the language, the same in every
repository. It has never seen a particular project's own vocabulary. Those terms are not lost — the
tokenizer splits an unknown word into known word-pieces, so `eADR` still lands somewhere — but their
placement is approximate. Meaning-based recall is therefore strongest on ordinary phrasing and weakest on
coined jargon, which is the opposite of the keyword path's bias and the reason both are offered.

WHY THE BYTES ARE CHECKED. The table is data, not code: numpy reads it as numbers and never executes it, so
a corrupted file degrades recall rather than running anything. But a silently wrong table would return
plausible nonsense, and the engine has no other way to notice. So both vendored files are verified against
recorded hashes at load and a mismatch refuses loudly, per the engine's rule that a half-working store is
worse than an honestly absent one.

STORED VECTORS ARE COMPARABLE BECAUSE THE TABLE IS FIXED. Every vector — for a stored record and for a
live question alike — comes from these same committed bytes. Nothing derives weights from the local
conversation, so a record embedded today and a question asked in a year are measured in the same space.
"""

import hashlib
import json
import os

from memory.semantic import wordpiece

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE_FILE = os.path.join(HERE, "potion-retrieval-32m-int8.npz")
VOCAB_FILE = os.path.join(HERE, "vocab.txt")
CHECKSUMS_FILE = os.path.join(HERE, "checksums.json")

# Set once by _load(); a process embeds many passages and must not re-read 2.2 MB for each one.
_CACHE = None


class TableUnavailable(RuntimeError):
    """The word table could not be loaded and trusted. Carries the plain-language reason."""


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify(path: str, expected: dict) -> None:
    name = os.path.basename(path)
    record = (expected.get("files") or {}).get(name)
    if not record:
        raise TableUnavailable(f"{name} has no recorded checksum, so its contents cannot be trusted.")
    actual = _sha256(path)
    if actual != record.get("sha256"):
        raise TableUnavailable(
            f"{name} does not match its recorded checksum — the file has changed since it was vendored. "
            "Meaning-based recall stays off rather than answering from a table that may be wrong."
        )


def _load():
    """The dequantized word table and its vocabulary, verified and cached. Raises TableUnavailable."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        import numpy
    except ImportError as exc:                       # the module's one dependency, absent
        raise TableUnavailable(
            "numpy is not installed, so meaning-based recall cannot run."
        ) from exc
    for path in (TABLE_FILE, VOCAB_FILE, CHECKSUMS_FILE):
        if not os.path.exists(path):
            raise TableUnavailable(f"{os.path.basename(path)} is missing from the semantic memory module.")
    with open(CHECKSUMS_FILE, encoding="utf-8") as fh:
        expected = json.load(fh)
    _verify(TABLE_FILE, expected)
    _verify(VOCAB_FILE, expected)

    # The table ships as int8 rows plus one float scale per row — a quarter of the size, and measured at
    # under 0.002 cosine drift against full precision. Restore it once, here, so the hot path is a gather.
    with numpy.load(TABLE_FILE, allow_pickle=False) as bundle:
        table = bundle["q"].astype(numpy.float32) * (bundle["scale"].astype(numpy.float32)[:, None] / 127.0)
    _CACHE = (table, wordpiece.load_vocab(VOCAB_FILE))
    return _CACHE


def available() -> bool:
    """True when the table can be loaded and trusted. Never raises — this answers a presence question."""
    try:
        _load()
        return True
    except Exception:
        return False


def unavailable_reason() -> str:
    """The plain-language reason the table cannot be used, or an empty string when it can."""
    try:
        _load()
        return ""
    except TableUnavailable as exc:
        return str(exc)
    except Exception as exc:                          # an unforeseen fault still has to be sayable
        return f"the word table could not be read ({exc})."


def dimensions() -> int:
    """The width of a vector from this table."""
    return int(_load()[0].shape[1])


def embed_many(texts) -> "list":
    """Unit-length vectors for `texts`, in order, as one float32 array of shape (len(texts), dimensions).

    A passage with no recognizable tokens (empty, or pure punctuation) yields a zero vector, which scores
    zero against every question — absent from results rather than spuriously close to them.
    """
    import numpy

    table, vocab = _load()
    width = table.shape[1]
    out = numpy.zeros((len(texts), width), dtype=numpy.float32)
    for row, text in enumerate(texts):
        ids = wordpiece.encode(text or "", vocab)
        if not ids:
            continue
        out[row] = table[ids].mean(axis=0)
    lengths = numpy.linalg.norm(out, axis=1, keepdims=True)
    lengths[lengths == 0] = 1.0                       # leave an all-zero row at zero, never divide by it
    return out / lengths


def embed(text: str):
    """The unit-length vector for one passage."""
    return embed_many([text])[0]
