"""Capture-time secret redaction — the one sanctioned mutation of an otherwise-verbatim archive.

Memory is a transcript-first archive whose whole value is the EXACT conversation (eADR-0038); recall
depends on it. So this redactor is deliberately PRECISION-BIASED, not recall-biased: it redacts only
credential shapes that anchor on a structural constant a normal sentence essentially never contains —
a vendor prefix, a fixed delimiter, a rigid multi-segment grammar. It never uses an entropy heuristic,
because a bare high-entropy string is indistinguishable from a git SHA, a UUID, a checksum, or a nonce
we legitimately discuss, and redacting those would shred everyday engineering prose. A false positive
here PERMANENTLY destroys unrecoverable memory (capture is the only record), so under-redacting a rare
exotic token is the correct trade against corrupting real conversation.

Deliberately OUT of scope (a surfaced boundary, not an oversight): bare high-entropy strings; a generic
`password=`/`secret=` sitting in prose (no vendor anchor — "the database password lives in the vault"
must survive verbatim); personal names; and PII — EMAILS and PHONE NUMBERS are intentionally left
intact. The operator asked for password/key/token-shaped content; eADR-0038 says "secret-shaped." An
email is not a credential, is extremely common in legitimate conversation, and is often the very anchor
a later session searches on — redacting it trades a large recall loss for a benefit no one requested.
PII redaction, if ever wanted, is a separate opt-in decision with its own record, never folded in here.

This module is pure, offline, and standard-library-only (eADR-0004: no outbound calls, no third-party
deps). `scrub_text` is deterministic, idempotent (`scrub_text(scrub_text(t)) == scrub_text(t)`), and
NEVER raises (capture is fail-soft, capture.py:756) — on any internal fault it returns the input
unchanged, biasing to under-redaction over corruption or a crash. It is defense-in-depth, not a wall:
a novel secret shape can pass un-redacted, and the real protection stays the gitignored local store and
the private backup vault.

That last sentence is about where the bytes LIVE, and the retrieval boundary moved out from under it. Recall
now reaches the recorded conversation by keyword, so text this module chose not to redact — names, email
addresses, phone numbers, bare high-entropy strings, ordinary `password=` prose — and everything captured
before this module existed is reachable from any prompt, not only by naming a session. The store is still
private and gitignored; what changed is how easily a search pulls a piece of it into a session's context. The
search answer carries a standing disclosure saying so. Rewriting the already-stored history is what
`rescrub.py` does — an operator-asked verb, because it rewrites their own conversation and requires a backup.
"""

import re

# Each entry: (compiled pattern, replacement). Order matters where one shape is a prefix of another —
# the more specific pattern (Anthropic sk-ant-, Stripe sk_live_) is listed BEFORE the generic sk-, so
# it wins the first redaction and the generic pattern then finds nothing to match. The PEM multiline
# block is applied first so its inner base64 can never be half-caught by a single-line pattern.
#
# Every variable-length run is UPPER-BOUNDED (a real credential has a bounded length), and the scheme
# run in the URL pattern is capped — this keeps each pattern linear on large pathological input (a long
# dotted/hex paste, or repeated BEGIN markers with no END) instead of re-scanning per start position.
# Two patterns carry a precision guard against destroying ordinary technical prose: the generic `sk-`
# body is hyphen-free AND must contain a digit (so a `sk-`-prefixed slug like a CSS class or a branch
# name — `sk-chasing-dots`, `sk-refactor-the-memory` — is left intact), and the Authorization credential
# must contain a digit (so a plain word after "bearer" — "credentials", "authentication" — is not
# redacted). A false positive here is unrecoverable, so these lean to under-redaction, by design.
_PATTERNS = [
    # PEM private-key armor — redact the WHOLE block as one unit (the paired BEGIN/END lines are a
    # globally reserved format). The body is upper-bounded so a BEGIN with no matching END fails fast.
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]{0,8192}?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
     "[redacted:private-key]"),
    # JWT — anchored on TWO `eyJ` segments (base64url of `{"`), which a random dotted token won't have.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,2000}\.eyJ[A-Za-z0-9_-]{8,4000}\.[A-Za-z0-9_-]{8,2000}\b"),
     "[redacted:jwt]"),
    # AWS access key id — the AKIA-family prefix + exactly 16 more upper/digit chars = a minted shape.
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}\b"), "[redacted:aws-key]"),
    # GitHub fine-grained PAT (literal prefix) — before the generic ghp_ family for clarity.
    (re.compile(r"\bgithub_pat_[0-9A-Za-z_]{22,255}\b"), "[redacted:github-token]"),
    # GitHub tokens — vendor prefix + underscore + long base62.
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{36,255}\b"), "[redacted:github-token]"),
    # Anthropic — literal sk-ant-, BEFORE the generic sk- so it wins (its strong prefix makes a hyphen/
    # underscore in the body low-risk, so no digit guard is needed here).
    (re.compile(r"\bsk-ant-[0-9A-Za-z_-]{20,255}\b"), "[redacted:anthropic-key]"),
    # Stripe — the _live_/_test_ infix is unmistakable; BEFORE the generic sk-.
    (re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,255}\b"), "[redacted:stripe-key]"),
    # OpenAI / generic sk- — the weakest-anchored vendor shape, so it is guarded: a hyphen-free body of
    # >=20 token chars that CONTAINS A DIGIT. This redacts real keys (base62 with digits) while leaving a
    # `sk-`-prefixed hyphenated slug or a digit-free word untouched (a false positive is unrecoverable).
    (re.compile(r"\bsk-(?:proj-)?(?=[0-9A-Za-z]{0,80}[0-9])[0-9A-Za-z]{20,80}\b"), "[redacted:openai-key]"),
    # Slack — xox + kind letter + dashes.
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,255}\b"), "[redacted:slack-token]"),
    # Google OAuth client id — literal domain suffix (before the AIza key so both are distinct).
    (re.compile(r"\b[0-9]{1,30}-[0-9a-z]{20,64}\.apps\.googleusercontent\.com\b"), "[redacted:google-oauth]"),
    # Google API key — AIza + exactly 35 chars = Google's fixed shape.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[redacted:google-key]"),
    # Authorization header — keep the scheme scaffold, redact only the credential. The credential must
    # contain a digit (a real opaque/base64 token does; a plain word like "credentials" does not), so
    # ordinary prose "Authorization: Bearer authentication is standard" is left intact. Case-insensitive
    # on the header/scheme keywords only — the digit guard stays case-sensitive.
    (re.compile(r"([Aa]uthorization\s*:\s*(?:[Bb]earer|[Bb]asic|[Tt]oken)\s+)"
                r"(?=[0-9A-Za-z._+/=~-]{0,200}[0-9])[0-9A-Za-z._+/=~-]{8,200}"),
     r"\1[redacted:auth-credential]"),
    # Credentials embedded in a URL authority — redact only the `user:pass@` userinfo, keep proto+host.
    # The scheme run is capped (a real scheme is short) so a long dotted paste can't drive re-scanning.
    (re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]{0,30}://)[^\s/:@]{1,128}:[^\s/:@]{1,128}@"),
     r"\1[redacted:url-credential]@"),
]


def scrub_text(text):
    """Redact high-confidence secret/credential shapes from a captured turn's text, replacing each with
    a typed, idempotent placeholder (`[redacted:<kind>]`).

    PRECISION-BIASED: only anchored, vendor/format-specific shapes are redacted; bare high-entropy
    strings, generic `password=` in prose, names, emails, and phone numbers are DELIBERATELY left intact
    — a false positive permanently destroys unrecoverable verbatim memory (eADR-0038). Pure, offline,
    stdlib-only, no I/O, deterministic. Idempotent: `scrub_text(scrub_text(t)) == scrub_text(t)` (the
    `[redacted:...]` placeholder matches none of the patterns). NEVER raises — on any internal fault the
    input is returned unchanged, biasing to under-redaction over corruption or a crash."""
    if not text:
        return text
    try:
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text
    except Exception:  # noqa: BLE001 — capture is fail-soft; never corrupt or crash on a redaction fault
        return text
