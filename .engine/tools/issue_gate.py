#!/usr/bin/env python3
"""The engine-Issue conformance reroute gate — the matcher (modes registers it; this holds the logic).

WHAT THIS IS. A pure-logic matcher the Explore/Build PreToolUse hook (modes.handler) consults on every tool
call: when a session types a Bash command that files an `engine`-labelled GitHub Issue whose body is NOT in
the control-plane body contract's shape, this returns a plain redirect reason; modes wraps it in
hooks.decide("deny", reason) so the platform blocks the call and feeds the reason back to the session, which
re-files through the issue-authoring helper. A conforming body, an unlabelled or non-engine Issue, every read /
list / view / comment / close, and anything the matcher cannot parse all return None → the call proceeds.

This routes the engine-labelled channel from posture to a channel-scoped
reroute gate. It is the engine-side counterpart of the proven workspace reference
— the matcher logic is mined from it, with three
deliberate engine-side changes:
  • The deny rides modes' hooks.decide channel (exit 0 + hookSpecificOutput), NOT the reference's exit-2 — the
    platform reads exit-2 as a crash and DROPS the reason, and the reason IS the redirect (it names the three
    required parts, the in-repo helper, and the --body-file fallback), so it must survive.
  • Label detection is PRECISE — only a real `--label`/`-l`/`--label=`/`labels[]=` field carrying `engine`,
    never the reference's loose "any token containing both 'label' and 'engine'" (which false-denies an
    innocent Issue whose body merely says e.g. "relabel the engine room").
  • A heredoc body is recovered from the raw command string, because a cold session commonly files via
    `gh issue create --body-file - <<'EOF' … EOF` (the body on stdin) — which the token path cannot see.

KEYS ON BODY SHAPE, NEVER PROVENANCE. The gate checks for the contract's structural MARKERS, so a body
written by hand passes exactly as one rendered by the helper. Body TRUTHFULNESS stays posture (a less-truthful
body costs legibility, never a guardrail; the weakening guard is untouched) — the gate guarantees shape, not truthfulness.

A NUDGE, NOT A WALL — best-effort and fail-open, stated honestly. The shell-string check is incomplete: an
alias / eval / substitution / a piped (non-heredoc) stdin / a temp-file written in the SAME chained command
(not yet on disk when the gate fires) all evade it and resolve to None → ALLOW. The heredoc recovery is one
more best-effort form recovered, never a closing of the hole. (Conversely a body passed as an unexpanded shell
variable — `-b "$BODY"` — cannot be read, so it reroutes even if its value would conform; the redirect's
`--body-file` path is the clean way through.) The catch-all for everything the gate misses is the `on:issues`
CI backstop (a later slice); the only unbypassable guarantee is the protected-branch merge.

SELF-CONTAINED RUNTIME. No network, no label application, no import of the helper at runtime (it holds no
producer roster). The markers are pinned here as the SINGLE SOURCE the CI backstop also imports; a test
(test_issue_gate) couples them to issue_author's actual output so an operator-facing copy change to the framing
or the headers breaks the test, never the gate silently.

CLI (operator-runnable demo; the live gate is what modes' wired hook invokes):
  uv run --directory .engine -- python tools/issue_gate.py demo   # a scripted allow/deny demonstration
"""
from __future__ import annotations

import os
import re
import shlex
import sys

# The engine-domain label marking the channel the body contract governs (telemetry.ENGINE_DOMAIN_LABEL). An
# Issue without it is ordinary backlog or a human/operator Issue, and is never gated.
ENGINE_LABEL = "engine"

# The body-contract markers the issue-authoring helper always emits (issue_author.py: the framing floor + the
# two required section headers). A conforming body carries all three; a free-text body carries none. The
# framing floor is matched as an ASCII substring (it stops before the em-dash / curly apostrophe in the helper's
# _FRAMING) so a hand-written body using straight punctuation still passes. SINGLE SOURCE: the on:issues CI
# backstop imports these same constants, and test_issue_gate pins them to issue_author's real output.
CONTRACT_MARKERS = (
    "The engine opened this item",
    "**What this is.**",
    "**What happens next.**",
)

# The in-repo helper the redirect points at (NOT the workspace reference's cross-repo "../engine-template/…").
HELPER = ".engine/tools/issue_author.py"

# The redirect reason, surfaced to the session by modes.handler via hooks.decide. Names what is wrong, the three
# parts the format guarantees, the helper to render them, the manual --body-file fallback, AND the escape hatch
# (drop the label) — so a legitimate non-engine note that tripped the gate is never stranded.
DENY_REASON = (
    f"This looks like an engine Issue — it carries the `{ENGINE_LABEL}` label — but its body isn't "
    "in the engine's Issue format, so it would read as raw text when the operator reviews it. "
    "Re-file it so the body carries the three parts the format guarantees:\n\n"
    "    *The engine opened this item itself — you didn't create it.*\n"
    "    **What this is.** <what this item is and why it's here>\n"
    "    **What happens next.** <what the operator must decide, or what happens next>\n\n"
    f"Render the body with the helper ({HELPER} — call render_engine_issue_body), or write those "
    "three parts directly, then file with `--body-file`. If you actually meant a plain personal "
    f"note rather than an engine Issue, drop the `{ENGINE_LABEL}` label and re-run."
)


# Shell command separators (as shlex emits them) after which a NEW command begins — so a verb counts only at
# the start of the command or just after one of these, never inside an echoed / grepped argument.
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "("})


def _find_command(tokens: list[str], seq: tuple[str, ...]) -> bool:
    """True if `seq` appears as consecutive tokens AT COMMAND POSITION — the first token, or just after a
    shell separator — so `cd x && gh issue create …` matches but `echo gh issue create …` (the verb inside an
    argument) does not. Mirrors the write-gate's command-position discipline (modes._CMD_START)."""
    n = len(seq)
    for i in range(len(tokens) - n + 1):
        if tuple(tokens[i:i + n]) == seq and (i == 0 or tokens[i - 1] in _SEPARATORS):
            return True
    return False


def _is_issue_creation(tokens: list[str]) -> bool:
    """`gh issue create …`, or `gh api …/issues` with a write method or fields (an Issue body write). The
    `gh api` arm also matches a PATCH that SETS a body on an existing engine Issue — also a non-conforming-body
    write worth rerouting — so it is intentionally not POST-only."""
    if _find_command(tokens, ("gh", "issue", "create")):
        return True
    if _find_command(tokens, ("gh", "api")):
        joined = " ".join(tokens)
        if "/issues" in joined and re.search(
            r"(-X\s+POST|--method\s+POST|(?:^|\s)-[fF](?:\s|$)|--field|--raw-field|--input)", joined
        ):
            return True
    return False


def _label_value_carries_engine(value: str) -> bool:
    """A `--label`/field value is a single label or a comma-separated list; the engine label must be one of its
    members (so `--label engine` and `--label engine,bug` match, but `--label engineering` does not)."""
    return ENGINE_LABEL in [part.strip() for part in value.split(",")]


# The `gh api` field form, e.g. `-f 'labels[]=engine'` (shlex yields the token `labels[]=engine`) or
# `-f labels=engine`. Matches `label=`/`labels=`/`label[]=`/`labels[]=` and captures the value list. It does NOT
# match `--label=…` (that starts with `--`, handled by its own branch).
_API_LABEL_FIELD = re.compile(r"^labels?(\[\])?=(.*)$")


def _has_engine_label(tokens: list[str]) -> bool:
    """True iff the command carries the engine-domain label at a REAL label flag/field — never a loose substring
    match on body/title text (the reference's `"label" in tok and "engine" in tok` clause false-denied an
    innocent Issue whose prose merely mentioned both words)."""
    for i, tok in enumerate(tokens):
        if tok in ("--label", "-l") and i + 1 < len(tokens) and _label_value_carries_engine(tokens[i + 1]):
            return True
        if tok.startswith("--label=") and _label_value_carries_engine(tok.split("=", 1)[1]):
            return True
        m = _API_LABEL_FIELD.match(tok)
        if m and _label_value_carries_engine(m.group(2)):
            return True
    return False


def _read_file(path: str) -> str | None:
    """The contents of a --body-file path, or None when not inspectable (stdin `-`, or an unreadable file)."""
    if path == "-":  # stdin — not inspectable from the token path (a heredoc is recovered separately, below)
        return None
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _extract_body(tokens: list[str]) -> str | None:
    """The Issue body from the tokens, or None when the token form gives no inspectable body.

    Covers `gh issue create` inline (-b/--body/--body=) and --body-file/-F <path>, plus the `gh api` field form
    (-f/-F/--field/--raw-field body=<value>). `-F` is overloaded — body-file for `gh issue create`, a typed
    field for `gh api` — so a `body=`-prefixed value is read as an inline field and a bare path as a file."""
    for i, tok in enumerate(tokens):
        if tok in ("-b", "--body") and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--body="):
            return tok.split("=", 1)[1]
        if tok.startswith("body="):  # gh api field form: -f/-F body=<value>
            return tok.split("=", 1)[1]
    for i, tok in enumerate(tokens):
        if tok in ("-F", "--body-file") and i + 1 < len(tokens) and "=" not in tokens[i + 1]:
            return _read_file(tokens[i + 1])
        if tok.startswith("--body-file="):
            return _read_file(tok.split("=", 1)[1])
    return None


# A heredoc in the RAW command string: `<<['"]?DELIM['"]? …rest of line\n …body… \n[ \t]*DELIM` (the `<<-`
# form's indented terminator allowed). Matched on the raw string, NOT shlex tokens — shlex silently shreds a
# heredoc (it strips the quotes and word-splits the body), so the token path is blind to it.
_HEREDOC_RE = re.compile(
    r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\r?\n"  # opening: <<['"]?DELIM['"]? + rest of the line
    r"(.*?)"                                                  # the heredoc body (non-greedy)
    r"\r?\n[ \t]*\2[ \t]*(?:\r?\n|$)",                        # terminator: DELIM alone on its own line (CRLF ok)
    re.DOTALL,
)


def _extract_heredoc_body(command: str) -> str | None:
    """The body of the FIRST here-document in the raw command string, or None when there is none / it is
    unterminated. Used ONLY when the token path found no body (the `--body-file -` stdin case), so an inline
    `--body "… a << b …"` is always checked as its own inline body, never mis-read as a heredoc. Best-effort:
    if an earlier, unrelated heredoc precedes the `gh`-bound one, this reads that earlier body and may
    fail-open — the CI backstop is the catch-all."""
    m = _HEREDOC_RE.search(command)
    return m.group(3) if m else None


def _is_conforming(body: str) -> bool:
    return all(marker in body for marker in CONTRACT_MARKERS)


def non_conforming_reason(tool_name: str, tool_input) -> str | None:
    """The reroute decision for one tool call. Returns the redirect REASON string when the call is an
    engine-labelled issue-creation with an inspectable, NON-conforming body; otherwise None (out of scope, or
    conforming, or not inspectable → fail-open ALLOW). Pure and side-effect-free; modes.handler wraps a returned
    reason in hooks.decide("deny", reason)."""
    if tool_name != "Bash":
        return None
    command = ""
    if isinstance(tool_input, dict):
        command = tool_input.get("command") or ""
    if not isinstance(command, str) or not command:  # a non-str / absent command is not inspectable → allow
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None  # unparseable shell string (unbalanced quotes, etc.) — fail open
    if not _is_issue_creation(tokens) or not _has_engine_label(tokens):
        return None
    body = _extract_body(tokens)
    if body is None:
        body = _extract_heredoc_body(command)  # recover a heredoc body the token path could not see
    if body is None:
        return None  # no inspectable body (piped stdin / editor prompt / unreadable file) — fail open
    if _is_conforming(body):
        return None
    return DENY_REASON


# ---- the operator-runnable demo (the live gate is the wired modes hook) ----------------------

_CONFORMING_BODY = (
    "*The engine opened this item itself — you didn't create it.*\n\n"
    "**What this is.** A demo item.\n\n"
    "**What happens next.** Nothing — this is a demonstration."
)


def _demo() -> int:
    """A scripted demonstration over the REAL non_conforming_reason: a labelled free-text Issue is rerouted; a
    labelled conforming Issue, an unlabelled free-text Issue, and a labelled free-text Issue passed by heredoc
    are all decided as designed. Self-checks and returns 1 on any unexpected verdict (the failure path)."""
    def verdict(command: str) -> str:
        reason = non_conforming_reason("Bash", {"command": command})
        return "REROUTE" if reason else "ALLOW"

    heredoc = "gh issue create --label engine --body-file - <<'EOF'\njust some free text\nEOF"
    conforming_heredoc = f"gh issue create --label engine --body-file - <<'EOF'\n{_CONFORMING_BODY}\nEOF"
    cases = [
        ("engine label + free-text body (inline)", 'gh issue create --label engine -b "just some free text"', "REROUTE"),
        ("engine label + conforming body (inline)", f'gh issue create --label engine -b {shlex.quote(_CONFORMING_BODY)}', "ALLOW"),
        ("NO engine label + free-text body", 'gh issue create -b "just some free text"', "ALLOW"),
        ("a different label + free-text body", 'gh issue create --label bug -b "just some free text"', "ALLOW"),
        ("body merely MENTIONS engine + label", 'gh issue create -b "please relabel the engine room"', "ALLOW"),
        ("engine label + free-text body (heredoc on stdin)", heredoc, "REROUTE"),
        ("engine label + conforming body (heredoc on stdin)", conforming_heredoc, "ALLOW"),
        ("not a creation (gh issue comment)", "gh issue comment 5 --body whatever", "ALLOW"),
    ]
    print("The engine-Issue reroute gate — what it decides for each command (this runs the real matcher):\n")
    ok = True
    for label, command, expected in cases:
        got = verdict(command)
        flag = "" if got == expected else "  <- UNEXPECTED"
        if got != expected:
            ok = False
        print(f"  {label:52} -> {got}{flag}")
    print("\nA REROUTE feeds the session this redirect (it is NOT shown to the operator):\n")
    print("    " + DENY_REASON.replace("\n", "\n    "))
    if not ok:
        print("\nDEMO UNEXPECTED: a command did not get the verdict the gate's contract promises.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
