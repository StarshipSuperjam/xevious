#!/usr/bin/env python3
"""Provider normalization — the ONE seam where AI-runtime differences are absorbed (eADR-0034).

The engine's gates, hooks, and tools are written against one canonical payload vocabulary (the
Claude Code hook shapes: tool_name Edit/Write/Bash, tool_input.file_path/command, session_id).
This module translates every other runtime's payloads INTO that vocabulary at the hook boundary
(hooks.run_hook calls normalize() immediately after reading the event), so everything downstream
stays provider-blind. Provider-specific names — the env vars, the Codex tool names, the patch
envelope — live HERE and in the narrow adapter code (hooks.py's command renderer, memory/capture.py's
transcript recognizer), never scattered through gate logic; a standing check holds that confinement.

Three laws:
  1. NORMALIZE IS THE IDENTITY FOR CLAUDE. A payload that carries no Codex tool name is returned
     as the SAME object, untouched — the Claude path's byte-stability is pinned by test.
  2. REWRITES ARE NAME-KEYED, NOT DETECTION-KEYED. `apply_patch` and the Codex shell tools are
     rewritten wherever they appear, because Claude Code never emits those names — so an edit is
     recognized even if provider detection itself fails (defense in depth; modes.py additionally
     carries `apply_patch` in its own denied set as the second belt).
  3. SESSION RESOLUTION IS PAYLOAD-FIRST, FAIL-SAFE. The hook payload's session_id always wins;
     the env chain is the CLI fallback; the live-session marker is the last resort for a typed
     Codex verb — and on any ambiguity (stale, foreign-owned, unreadable) it resolves NOTHING, so
     a stance change degrades to "could not identify the session" and the stance stays Explore,
     never a silent flip of the wrong session (eADR-0034).
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import time

CLAUDE = "claude"
CODEX = "codex"

# The engine's own provider tag, exported by each runtime's hook launcher (codex-hook-runner.sh sets
# codex; the Claude launcher sets nothing and claude is the default). Env-first so detection never
# depends on payload heuristics on the live path.
PROVIDER_ENV = "ENGINE_PROVIDER"

# The session-id env fallback chain, in order. ENGINE_SESSION_ID is DELIBERATELY first: it is the
# engine's own neutral, explicit override knob (an operator or a test sets it on purpose to pin the
# session identity), so when set it outranks the platform vars — the same explicit-beats-ambient rule
# as a --session flag. The platform vars follow; a runtime that exports no session var falls through
# to the live-session marker (typed-verb path only).
SESSION_ENV_CHAIN = ("ENGINE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")

# Codex's canonical edit tool (its payloads report apply_patch even when a matcher aliases Edit/Write)
# and its shell tool names. "Bash" itself needs no entry — Codex reports simple shell as Bash; these
# are the sibling names that may appear on other shell paths, mapped defensively.
CODEX_EDIT_TOOL = "apply_patch"
CODEX_SHELL_TOOLS = frozenset({"shell", "local_shell", "unified_exec"})

# The apply_patch envelope: one call may create/edit/delete MANY files, each named on a marker line.
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", re.MULTILINE)
_PATCH_MARKERS = ("*** Begin Patch", "*** Update File:", "*** Add File:", "*** Delete File:")
_PATCH_INPUT_KEYS = ("patch", "input", "content", "changes")   # likeliest field names, tried first


def detect(payload: dict | None = None) -> str:
    """Which runtime this process is serving: the launcher-exported ENGINE_PROVIDER wins; a payload
    that carries a Codex-only shape (turn_id, or a Codex tool name) reads as codex; default claude."""
    env = (os.environ.get(PROVIDER_ENV) or "").strip().lower()
    if env in (CLAUDE, CODEX):
        return env
    if isinstance(payload, dict):
        if "turn_id" in payload:
            return CODEX
        tool = payload.get("tool_name")
        if tool == CODEX_EDIT_TOOL or tool in CODEX_SHELL_TOOLS:
            return CODEX
    return CLAUDE


def _patch_file_paths(tool_input) -> list:
    """Every file path named in an apply_patch envelope, in order, de-duplicated. The payload field
    carrying the envelope is not a stable contract, so the envelope is FOUND by its own markers: the
    likeliest keys are tried first, then any string value. No envelope found → [] (the deny still
    fires on the tool name; only the per-file message/relay refinement is lost)."""
    candidates = []
    if isinstance(tool_input, str):
        candidates = [tool_input]
    elif isinstance(tool_input, dict):
        candidates = [tool_input[k] for k in _PATCH_INPUT_KEYS
                      if isinstance(tool_input.get(k), str)]
        candidates += [v for k, v in sorted(tool_input.items())
                       if isinstance(v, str) and k not in _PATCH_INPUT_KEYS]
    for text in candidates:
        if any(marker in text for marker in _PATCH_MARKERS):
            seen, out = set(), []
            for p in _PATCH_FILE_RE.findall(text):
                if p not in seen:
                    seen.add(p)
                    out.append(p)
            return out
    return []


def _shell_command(tool_input) -> str:
    """The one command string a Codex shell payload carries — joined shell-safely when the runtime
    reports an argv list instead of a string."""
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command")
        if isinstance(cmd, str):
            return cmd
        if isinstance(cmd, list):
            try:
                return shlex.join(str(c) for c in cmd)
            except (TypeError, ValueError):
                return ""
    return ""


def normalize(event: str, payload):
    """Canonicalize a hook payload. Claude payloads pass through as the SAME object (identity —
    test-pinned); a Codex edit becomes tool_name "Edit" with tool_input.file_paths = EVERY path the
    patch envelope names (+ file_path = the first, for single-path readers), and a Codex shell tool
    becomes tool_name "Bash" with tool_input.command. The raw payload fields are preserved under
    provider_raw for diagnostics; nothing else in the payload is touched."""
    if not isinstance(payload, dict):
        return payload
    tool = payload.get("tool_name")
    if tool == CODEX_EDIT_TOOL:
        raw = payload.get("tool_input")
        paths = _patch_file_paths(raw)
        tool_input = {"file_paths": paths}
        if paths:
            tool_input["file_path"] = paths[0]
        out = dict(payload)
        out["tool_name"] = "Edit"
        out["tool_input"] = tool_input
        out["provider_raw"] = {"tool_name": tool, "tool_input": raw}
        return out
    if tool in CODEX_SHELL_TOOLS:
        out = dict(payload)
        out["tool_name"] = "Bash"
        out["tool_input"] = {"command": _shell_command(payload.get("tool_input"))}
        out["provider_raw"] = {"tool_name": tool, "tool_input": payload.get("tool_input")}
        return out
    return payload


def session_from_env() -> str | None:
    """The first present, non-empty, actually-expanded session id in the env chain (an unexpanded
    `${...}` literal — a shell that passed the token through — is skipped, mirroring the CLI flag
    guard)."""
    for var in SESSION_ENV_CHAIN:
        value = os.environ.get(var) or ""
        if value and "${" not in value:
            return value
    return None


# ---- the live-session marker (the typed-verb fallback for a runtime with no session env var) ----
# boot writes it at every SessionStart; a typed `$engine-start` on Codex resolves through it when the
# payloadless CLI has no env var to read. FAIL-SAFE BY CONSTRUCTION: per-user temp scope, owner-only
# permissions, owner + freshness checked on read, and any ambiguity resolves to None — the caller's
# stance change then reports failure instead of flipping an unidentified session. KNOWN LIMIT
# (disclosed, eADR-0034): two CONCURRENT sessions of the same user in one repo share the marker
# last-writer-wins, so the typed verb can only be trusted to address the most recently started
# session; the stance readout (`$engine-status`) is the check.
_MARKER_MAX_AGE = 24 * 3600      # a marker older than one session-day is stale — refuse, never guess
_MARKER_FUTURE_SKEW = 300        # a timestamp from the future beyond clock skew is forged/broken — refuse


MARKER_ENV = "ENGINE_LIVE_SESSION_MARKER"   # explicit path override (tests / unusual temp setups)


def live_session_path() -> str:
    override = os.environ.get(MARKER_ENV)
    if override:
        return override
    if "unittest" in sys.modules:
        # Hermetic under a test harness (the emit_finding precedent): a test that exercises boot's
        # heartbeat must NEVER write the developer's real per-user marker — a stale test session id
        # there would leak into real session resolution.
        return os.path.join(tempfile.gettempdir(), f"engine-live-session-test-{os.getpid()}.json")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:16]
    # The uid in the name scopes the marker per-user even on a SHARED system temp dir (a pre-created
    # same-name file by another user then simply isn't ours — the owner check refuses it — and can no
    # longer squat the one name every user would compute).
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return os.path.join(tempfile.gettempdir(), f"engine-live-session-{uid}-{digest}.json")


def write_live_session(session_id, provider: str | None = None) -> bool:
    """Record the live session (called by boot). Owner-only permissions; best-effort — a failure
    never disturbs the hook that called it."""
    if not isinstance(session_id, str) or not session_id:
        return False
    record = {"session_id": session_id, "provider": provider or detect(), "ts": time.time()}
    path = live_session_path()
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW      # a planted symlink must never make this write land elsewhere
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record))
        try:
            os.chmod(path, 0o600)     # O_CREAT's mode only applies to a NEW file; re-assert on reuse
        except OSError:
            pass
        return True
    except OSError:
        return False


def read_live_session(max_age: float = _MARKER_MAX_AGE) -> dict | None:
    """The live-session record, or None on ANY ambiguity: absent, unreadable, malformed, owned by
    another user, stale, or timestamped in the future. Refusal is the safety property — a caller
    must treat None as 'could not identify the session', never guess."""
    path = live_session_path()
    try:
        st = os.stat(path)
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return None
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
        if not isinstance(record, dict):
            return None
        sid, ts = record.get("session_id"), record.get("ts")
        if not isinstance(sid, str) or not sid or not isinstance(ts, (int, float)):
            return None
        age = time.time() - ts
        if age > max_age or age < -_MARKER_FUTURE_SKEW:
            return None
        return record
    except (OSError, ValueError):
        return None


def resolve_session(payload: dict | None = None, explicit: str | None = None) -> str | None:
    """The session id for the current action: an explicit value (a --session flag) wins; then the
    hook payload's session_id; then the env chain; then the live-session marker. None means 'could
    not identify the session' — every caller degrades safe on it (a stance change reports failure
    and the stance stays Explore)."""
    if isinstance(explicit, str) and explicit and "${" not in explicit:
        return explicit
    if isinstance(payload, dict):
        sid = payload.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
    sid = session_from_env()
    if sid:
        return sid
    record = read_live_session()
    # PROVIDER-CONFINED: the marker resolves a session ONLY when the session it records is a Codex
    # one — the runtime with no session env var, the fallback's whole reason to exist. A Claude
    # session always exports its env var, so reaching this point on Claude means something is off,
    # and the safe answer is the historical one: resolve nothing (stance changes report failure)
    # rather than adopt whichever session most recently booted.
    if record and record.get("provider") == CODEX:
        return record["session_id"]
    return None
