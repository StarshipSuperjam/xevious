"""Meaning-based recall — finding a past conversation that means the same thing in different words.

The keyword path answers "what exactly did we say about X": it matches words, so when a word is absent it
correctly returns nothing. That strictness is the reason an irrelevant question gets an empty answer instead
of a plausible wrong one, and it is why this package does not touch it. Meaning-based recall answers the
other question — "have we hit this before?" — where the wording has drifted and a keyword match was never
going to land.

They are two operations a session chooses between, not two bidders for one ranking. Nothing here alters,
wraps, or falls back to the keyword path.

The whole capability is a committed word table, numpy, and the three modules here: `wordpiece` splits text
the way the table expects, `embed` turns text into a vector, and `store` keeps those vectors beside the
ledger and searches them.
"""
