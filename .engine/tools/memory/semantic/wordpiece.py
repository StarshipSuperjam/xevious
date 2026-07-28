"""wordpiece.py — the BERT WordPiece tokenizer the vendored word table was built against.

The table is keyed by token id, so text must be split into exactly the token ids its publisher used or
every lookup is wrong. That pipeline is fully specified — BertNormalizer, BertPreTokenizer, WordPiece with
a `##` continuation prefix and an `[UNK]` fallback — so it is implemented here in the standard library
rather than taken as a dependency.

WHY IT IS HERE RATHER THAN INSTALLED. The reference implementation is the `tokenizers` package, which
declares an unconditional dependency on `huggingface-hub` and so pulls an HTTP client and a remote-artifact
fetcher into the virtual environment that the validator and every hook execute in. This engine reaches only
two hosts by design, and a recall tool has no business widening that. The whole pipeline is the ~90 lines
below.

FIDELITY IS PROVEN, NOT ASSUMED. `test_wordpiece.py` pins the output of this module against a frozen corpus
of real captured conversation, and the vendoring pass verified it token-for-token against the reference
tokenizer over 3,012 strings — the whole live ledger sampled, plus accents, CJK, emoji, URLs, code
identifiers and control characters — with zero divergence.
"""

import unicodedata

# WordPiece's own bound: a longer run of non-space characters is emitted as one unknown token rather than
# scanned, which keeps a pathological input (a base64 blob, a minified line) from costing quadratic time.
MAX_WORD_CHARS = 100

UNK_TOKEN = "[UNK]"
SUBWORD_PREFIX = "##"


def _is_control(ch: str) -> bool:
    """True for a control character. Tab, newline and carriage return are whitespace here, never control."""
    if ch in ("\t", "\n", "\r"):
        return False
    return unicodedata.category(ch).startswith("C")


def _is_whitespace(ch: str) -> bool:
    if ch in (" ", "\t", "\n", "\r"):
        return True
    return unicodedata.category(ch) == "Zs"


def _is_punctuation(ch: str) -> bool:
    """BERT's definition: every ASCII non-alphanumeric printable, plus anything Unicode calls punctuation.

    The ASCII ranges are listed explicitly because Unicode classes `$`, `+`, `^` and `` ` `` as symbols
    rather than punctuation, and BERT splits on them anyway.
    """
    cp = ord(ch)
    if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126):
        return True
    return unicodedata.category(ch).startswith("P")


def _is_cjk(cp: int) -> bool:
    """True inside the CJK ideograph blocks, which BERT surrounds with spaces so each becomes its own word."""
    return (
        0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0x20000 <= cp <= 0x2A6DF
        or 0x2A700 <= cp <= 0x2B73F or 0x2B740 <= cp <= 0x2B81F or 0x2B820 <= cp <= 0x2CEAF
        or 0xF900 <= cp <= 0xFAFF or 0x2F800 <= cp <= 0x2FA1F
    )


def normalize(text: str) -> str:
    """BertNormalizer as the vendored table's config declares it: clean, space CJK, lowercase, strip accents.

    Control characters and the replacement character are dropped rather than mapped, and every whitespace
    run becomes a plain space, so a tab-indented code block and the same line re-indented tokenize alike.
    """
    out = []
    for ch in text:
        cp = ord(ch)
        if cp == 0 or cp == 0xFFFD or _is_control(ch):
            continue
        if _is_whitespace(ch):
            out.append(" ")
        elif _is_cjk(cp):
            out.append(" " + ch + " ")
        else:
            out.append(ch)
    lowered = "".join(out).lower()
    return "".join(c for c in unicodedata.normalize("NFD", lowered)
                   if unicodedata.category(c) != "Mn")


def pre_tokenize(text: str) -> list:
    """BertPreTokenizer: split on whitespace, then split each punctuation character into its own word.

    This is what makes `eADR-0038` three words and `__init__.py` five — the reason a project's own
    identifiers survive as searchable pieces instead of collapsing to a single unknown token.
    """
    words = []
    for chunk in text.split():
        current = ""
        for ch in chunk:
            if _is_punctuation(ch):
                if current:
                    words.append(current)
                    current = ""
                words.append(ch)
            else:
                current += ch
        if current:
            words.append(current)
    return words


def encode(text: str, vocab: dict) -> list:
    """The token ids for `text`, greedy longest-match-first.

    A word with any unmatchable piece becomes a single `[UNK]` — WordPiece's rule, not a shortcut: a
    partially-matched word would otherwise contribute fragments that mean nothing.
    """
    unk = vocab[UNK_TOKEN]
    ids = []
    for word in pre_tokenize(normalize(text)):
        if len(word) > MAX_WORD_CHARS:
            ids.append(unk)
            continue
        start, pieces, unmatchable = 0, [], False
        while start < len(word):
            end = len(word)
            matched = None
            while start < end:
                piece = word[start:end]
                if start > 0:
                    piece = SUBWORD_PREFIX + piece
                if piece in vocab:
                    matched = piece
                    break
                end -= 1
            if matched is None:
                unmatchable = True
                break
            pieces.append(vocab[matched])
            start = end
        ids.extend([unk] if unmatchable else pieces)
    return ids


def load_vocab(path: str) -> dict:
    """The token -> id map, read from the vendored `vocab.txt` (one token per line, id is the line number)."""
    vocab = {}
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh):
            vocab[line.rstrip("\n")] = line_no
    return vocab
