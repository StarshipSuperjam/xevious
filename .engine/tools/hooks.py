#!/usr/bin/env python3
"""The hooks contract substrate — the cross-cutting hook LAWS every local gate
is built on. Where the control-plane is the GitHub-side substrate, hooks is the in-session
substrate the boot pack, the close ritual, the local nudges, and the experiential capture all
fire through. It is foundational: boot, close, telemetry, validation, and memory all presuppose
it.

THIS MODULE OWNS THE LAWS, NOT THE BEHAVIORS. It ships:
  - the closed EVENT INVENTORY (the engine's chosen subset of the platform's larger event set),
  - the BLOCK BUDGET (which events may hard-block — only PreToolUse and Stop) + the block cap,
  - the FAIL-OPEN-AND-FLAG harness (a crashing gate never strands the operator and never fails
    silently),
  - the HOOK-SCRIPT CONTRACT translation (stdin event JSON; exit code / structured stdout),
  - the per-OS INTERPRETER-PATH resolver.
It ships NO hook behavior and NO registered hook: the boot SessionStart pack, the close
Stop ritual, the modes explore write-gate, and validation/telemetry's
PostToolUse each supply their own handler and ride this harness. The committed `.claude/settings.json`
that registers a hook is born at the first hook-wiring step; the keyed, reversible registration
MECHANISM is the wiring library's (wiring.py), applied by provisioning — hooks fixes only that
registration must be keyed and reversible, never the mechanism.

The STATIC half of the block-budget law — the pure `block_budget_findings` block-registry coherence leg,
which flags a block declared on a non-eligible event AND a block that does not declare the stances it is
active in (the mode dimension) — lives in validate.py beside its sibling
legs (agent/skill/interface/policy), fixture-tested and wrapped by the first-class engine/check/
block-coherence check (the agent/skill-coherence precedent). This module owns the RUNTIME half: the
harness enforces the same budget at the moment a handler asks to block.

BOUNDARY: a hook script is a `tool` instance — deterministic engine code
homed at `.engine/tools/`, not a dedicated surface. Hook registrations and the settings file are
wiring, not surfaces.

CLI (the operator-runnable demo on a throwaway fixture — no registered hook exists yet):
  uv run --directory .engine -- python tools/hooks.py demo
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import providers  # noqa: E402  (stdlib-only; the provider-normalization seam run_hook applies)
import moment  # noqa: E402  (stdlib-only; the trailing-Z time seam — kept off no happy path, imports nothing back)
import validate  # noqa: E402


# ---- the closed event inventory -------------------------
# The Engine governs a SUBSET of the Claude Code hook events: the platform exposes many more
# (SubagentStart/Stop, PostCompact, PermissionRequest, StopFailure, Notification, ...) — the set
# the Engine binds is an end-state decision, not the platform's full list, and it grows additively
# like the surface catalog. This is NOT a claim that only seven events exist.
#   blocks  — may this event HARD-BLOCK? Only PreToolUse and Stop (the block-budget law).
#   injects — may this event inject `additionalContext`?
#   owners  — the system(s) that own the behavior on this event. PostToolUse has THREE owners
#             (validation's local nudge + telemetry's ambient capture + modes' plan-acceptance
#             Build-entry trigger coexist on one event); it MAY inject — modes' acceptance
#             trigger injects an assistant-internal stance directive (additionalContext) on Build entry
#             still non-blocking; SessionEnd is hooks-owned
#             (cleanup/flush, cannot block); UserPromptSubmit is boot/orientation's per-prompt scent.
#             SessionStart has THREE owners: boot's orientation pack + memory's own session-start work
#             (the cross-session erasure observer and the backup push)
#             + the optional github-projects-sync board refresh, which coexist on one event by keyed
#             registration (the PostToolUse multi-owner precedent). The board-sync owner is present only
#             while that optional module is installed; the entry names every system that may own the event.
EVENT_INVENTORY = {
    "SessionStart":     {"owners": ("boot", "memory", "github-projects-sync"), "blocks": False, "injects": True},
    "PreToolUse":       {"owners": ("invariant-owner",),         "blocks": True,  "injects": True},
    "PostToolUse":      {"owners": ("validation", "telemetry", "modes"), "blocks": False, "injects": True},
    "PreCompact":       {"owners": ("memory",),                  "blocks": False, "injects": False},
    "Stop":             {"owners": ("close",),                   "blocks": True,  "injects": False},
    "SessionEnd":       {"owners": ("hooks",),                   "blocks": False, "injects": False},
    "UserPromptSubmit": {"owners": ("boot",),                    "blocks": False, "injects": True},
}
EVENTS = frozenset(EVENT_INVENTORY)
# The block budget: the closed set of events that MAY hard-block. The platform would let PreCompact,
# UserPromptSubmit, and SubagentStop block too; the Engine declines — a local hard-block buys
# friction without proportional trust. The one unbypassable gate stays the
# protected-branch review.
BLOCK_ELIGIBLE_EVENTS = frozenset(e for e, m in EVENT_INVENTORY.items() if m["blocks"])  # {PreToolUse, Stop}

# The block-eligible INVARIANT set starts EMPTY: owning
# systems register their block into it additively when designed — close's findings-disposition Stop
# block and modes' explore write-gate PreToolUse block. Hooks names no
# invariant itself, so it presupposes none of the systems that will populate the set. A registration
# is {event, name, owner, modes}; the block-registry leg (validate.block_budget_findings) reads `event`
# (only PreToolUse/Stop may block) AND `modes` (the stances the block is active in, declared as data —
# not code-only).
BLOCK_ELIGIBLE_INVARIANTS: tuple = ()


# ---- the Stop-hook block cap (verified on the live platform) ---
# A Stop or PreToolUse block is a STRONG local block, not an absolute wall: Claude Code force-ends the
# turn after this many consecutive Stop blocks (it sets `stop_hook_active` on the forced continuation),
# so a local gate makes evasion take deliberate effort while the durable backstop stays the merge gate.
STOP_HOOK_BLOCK_CAP = 8
STOP_HOOK_BLOCK_CAP_ENV = "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"   # the operator override (raise the cap)


# ---- exit codes (the platform contract) -------------------
# Exit 2 is the ONLY blocking exit (and only PreToolUse/Stop may use it); 2 also feeds stderr back to
# Claude. Any OTHER non-zero exit is a NON-BLOCKING error — the tool runs. A gate must never be wrapped
# to deny-on-error: that would make a bug fail closed and strand a non-engineer who cannot debug it.
EXIT_PROCEED = 0       # allow / no-op / injection
EXIT_NONBLOCKING = 1   # the fail-open exit: a non-blocking error; the guarded action proceeds
EXIT_BLOCK = 2         # the single blocking exit (PreToolUse/Stop only)

# The PreToolUse structured-stdout permission decision values the Engine uses (the platform also
# offers `defer`, which the Engine does not need).
PERMISSION_DECISIONS = frozenset({"allow", "deny", "ask"})


# ---- the per-OS interpreter-path resolver ----
# A hook command names the engine tool-runtime interpreter EXPLICITLY and ${CLAUDE_PROJECT_DIR}-rooted,
# so it is portable (resolves on any operator's machine after the template is generated) and independent
# of any PATH the non-interactive hook shell may lack. A bare `python`/`uv`, or `uv run` with its
# implicit re-sync, is NEVER used on a hot path (the re-sync adds latency at a latency-sensitive moment).
PROJECT_DIR_VAR = "${CLAUDE_PROJECT_DIR}"


def interpreter_path(os_name: str | None = None) -> str:
    """The ${CLAUDE_PROJECT_DIR}-rooted engine venv interpreter, resolved per-OS: POSIX `bin/python`,
    Windows `Scripts/python.exe` (the standard venv layout). `os_name` defaults to os.name; pass it
    explicitly to render the other OS's form (fixture-testable).

    This is the SINGLE definition of the two per-OS layouts. Committed hook commands are always rendered
    in the POSIX form (`hook_command` below); the actual per-OS choice is made at FIRE TIME by the launcher
    (`hook-runner.sh`), which falls back from bin/python to Scripts/python.exe under the same venv root when
    the POSIX layout is absent — so one committed repo runs on every OS (StarshipSuperjam/engine-template#407 per-OS build-spec
    leaf). A drift test pins the launcher's bin/python + Scripts/python.exe literals back to this function so
    the two homes never diverge."""
    name = os.name if os_name is None else os_name
    sub = "Scripts/python.exe" if name == "nt" else "bin/python"
    return f"{PROJECT_DIR_VAR}/.engine/.venv/{sub}"


# The hook launcher (.engine/tools/hook-runner.sh) holds the bounded wait that lets a hook survive the
# fresh-worktree race (issue StarshipSuperjam/engine-template#83): the gitignored `.engine/.venv` is provisioned a beat AFTER a checkout,
# so a hook that fires in that window finds no interpreter and exits 127 — a SessionStart hook cannot
# block, so the failure is silent and boot never runs. The launcher polls for either OS's venv layout (the
# named POSIX bin/python, or the Windows Scripts/python.exe sibling under the same venv root) and runs the
# one present — so one committed repo boots on every OS; the ceiling is ~5 s (the observed provisioning gap
# is well under 1 s), overridable for tests via
# ENGINE_HOOK_WAIT_POLLS / ENGINE_HOOK_WAIT_INTERVAL. It is NOT extended to cover a cold multi-second
# runtime build, and NEVER falls back to the operator's system Python. The wait/exec preamble used to be
# inlined in every hook command; it moved into this one launcher so the command Claude Code DISPLAYS after
# a hook fires stays short and legible to the non-engineer operator (it otherwise reads as a wall of code).
HOOK_RUNNER = f"{PROJECT_DIR_VAR}/.engine/tools/hook-runner.sh"


def hook_command(script_relpath: str, os_name: str | None = None, provider: str = "claude") -> str:
    """The full hook `command` string a settings.json registration carries: a call to the hook launcher
    (`.engine/tools/hook-runner.sh`) passing the explicit ${CLAUDE_PROJECT_DIR}-rooted venv interpreter and
    the ${CLAUDE_PROJECT_DIR}-rooted script. The launcher does the bounded wait that closes the
    fresh-worktree race (issue StarshipSuperjam/engine-template#83) and then `exec`s the interpreter; if the interpreter never appears it
    runs NOTHING and NEVER falls back to the operator's system Python.
    The interpreter is still NAMED EXPLICITLY in the command (the launcher's first
    argument), so the rule that "the hook command names the interpreter explicitly and ${CLAUDE_PROJECT_DIR}-
    rooted" stays witnessable in the diff; only the wait/exec MECHANICS moved into the launcher — the
    invocation FORM is hooks'; the command's internal STRUCTURE (inline vs. launcher) is an
    unspecified build-spec leaf. Shell-form (no `args`), so Claude Code runs it under `sh -c` (macOS/Linux)
    / Git Bash (Windows); `${CLAUDE_PROJECT_DIR}` is substituted before the shell sees it. The script PATH
    is DOUBLE-QUOTED so a project directory whose path contains a space (a common iCloud/OneDrive/"My Drive"
    layout) resolves as a single token: unquoted, it word-splits under `sh -c`, the launcher forwards a
    truncated path, CPython exits 2, and the platform reads that exit-2 as a BLOCK — fail-CLOSED on every
    tool call and turn-end, the exact stranding the fail-open law forbids (StarshipSuperjam/engine-template#390). Any trailing args
    (`accept-hook`, `hook`, `session-start`, ...) stay OUTSIDE the quotes as the bare tail so they still
    word-split into the launcher's positional params. `${CLAUDE_PROJECT_DIR}` expanding to a spaced path
    inside the double quotes does NOT re-split (a parameter expansion in double quotes is field-split-exempt),
    which is why the already-quoted interpreter token has always survived a spaced path and only the bare
    script tail did not. The settings.json registration itself is wiring's.

    `provider` selects the runtime's command form. "claude" (the default) renders the historical form
    byte-identically. "codex" renders the .codex/hooks.json form: Codex has NO project-directory token,
    so the command first resolves the project root itself (`cd` to `git rev-parse --show-toplevel`,
    falling back to the current directory) and then rides the Codex shim
    (.engine/tools/codex-hook-runner.sh), which tags ENGINE_PROVIDER=codex and execs the SAME shared
    launcher — the wait/exec mechanics and per-OS fallback are one implementation for both runtimes.
    The same quoting law applies: the path tokens are double-quoted, the args tail stays bare."""
    # `script_relpath` is the script PATH plus any trailing args, space-joined (e.g. "modes.py accept-hook").
    # Quote ONLY the path token; leave the args as the bare, still-word-splittable tail (see docstring, StarshipSuperjam/engine-template#390).
    script_path, _, script_args = script_relpath.partition(" ")
    args_tail = f" {script_args}" if script_args else ""
    if provider == "codex":
        return ('cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)" && '
                f'sh ".engine/tools/codex-hook-runner.sh" "{script_path}"{args_tail}')
    interp = interpreter_path(os_name)
    return f'sh "{HOOK_RUNNER}" "{interp}" "{PROJECT_DIR_VAR}/{script_path}"{args_tail}'


# ---- shared payload classifier: is this tool call a `git commit`? ------------------------------
# The one place the commit-boundary hooks agree on "is this a `git commit`". The knowledge-graph regen
# (knowledge_gen), the self-map regen (self_map), and validation's local pre-commit nudge all fire at the
# same boundary, so the classifier lives here in the harness they all import rather than in a copy per
# consumer. Matched at a COMMAND-START position (line start, or just after a shell separator) so an
# occurrence inside a quoted argument or an echoed/grepped string (`echo 'git commit'`) does not trip it.
# Best-effort by construction: a prefixed/aliased/substituted form (`git -c k=v commit`, `time git commit`)
# is missed — that only leaves a slightly stale derived file the CI gate catches, or a skipped local nudge;
# it never blocks and never mis-fires on echoed text. (modes.py keeps its OWN git-commit pattern: there it
# is one of several building-verb patterns in a different matcher, a distinct concern.)
_CMD_START = r"(?:^|[\n;&|])\s*"
_GIT_COMMIT_RE = re.compile(_CMD_START + r"git\s+commit\b")


def _is_git_commit(payload: dict) -> bool:
    """True iff this tool call is a `git commit` Bash command — the commit-boundary trigger. Degrades safe:
    a non-dict payload / non-Bash tool / absent or non-string command -> False (no fire)."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return False
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return isinstance(command, str) and bool(_GIT_COMMIT_RE.search(command))


# ---- the decision vocabulary a handler returns (the hook-script contract, normalized) ----------
# A handler is the OWNING SYSTEM'S behavior (boot/close/modes/...). The harness ships no handler and
# seeds no block: it is a pure MECHANICAL TRANSLATOR of a handler's returned decision into the platform
# contract, never deciding *when* to block. These constructors are the small, reviewed surface a
# consumer's handler returns.

def proceed() -> dict:
    """Allow the action / no-op (the default). → exit 0, no output."""
    return {"action": "proceed"}


def block(reason: str) -> dict:
    """Request a HARD block with a plain-language reason. Honored only on a block-eligible event
    (PreToolUse/Stop); on any other event it is a budget violation → fail open + flag."""
    return {"action": "block", "reason": reason}


def inject(context: str) -> dict:
    """Inject `additionalContext` back to Claude (SessionStart / UserPromptSubmit / PreToolUse /
    PostToolUse injectors — PostToolUse carries modes' Build-entry stance directive).
    → structured stdout, exit 0."""
    return {"action": "inject", "context": context}


# The platform's per-value output cap: past 10,000 characters the platform saves the full payload to a file
# and substitutes a preview of the first 2,000 characters (plus the file path) — both figures verified against
# the shipped Claude Code 2.1.185 binary. The boot pack's grounding marker sits near the top, so it survives
# inside that preview; what drops from the injected context is everything past it — the status
# headline, the write-gate summary, and the dashboard. So measure-before-inject sheds the lowest-value
# tiers to keep the essential content within the surviving preview window. The cap binds EACH output
# value, not the event total.
HOOK_OUTPUT_CAP = 10_000


def cap_shed(blocks: list, cap: "int | None" = None, notice=None,
             compact_notice=None) -> "tuple[str, list]":
    """Measure-before-inject with tiered shedding — the guard an injector composes BEFORE handing its
    payload to inject(), which stays a pure translator. `blocks` is an ordered list of (priority, name,
    text): priority 0 is PINNED (a governance alarm or grounding marker — never shed, even if the pinned
    text alone still exceeds the cap, because a truncated alarm is worse than an oversize one the
    platform previews); higher priorities shed FIRST, a whole priority class at a time, until the joined
    text fits. When anything is shed and `notice` is given, notice(shed_names) is appended to the kept
    text and counted against the cap — and CONTENT ALWAYS BEATS THE LABEL (StarshipSuperjam/engine-template#495 review): a further class
    is never shed just to make room for the notice. When the kept content fits but the full notice tips
    it over, the notice shrinks to `compact_notice(shed_names)`; if even that doesn't fit, the notice is
    dropped (the kept content, grounding marker included, matters more than the sentence about
    trimming). Returns (text, shed_names) — the order of `blocks` is preserved; priorities select what
    survives, never reorder it. `cap` defaults to HOOK_OUTPUT_CAP resolved at call time (so a test can
    patch the module constant)."""
    if cap is None:
        cap = HOOK_OUTPUT_CAP
    kept = list(blocks)
    shed: list = []

    def _render(current, shed_names, notice_fn):
        parts = [t for _p, _n, t in current if t]
        if shed_names and notice_fn is not None:
            parts.append(notice_fn(shed_names))
        return "\n".join(parts)

    for priority in sorted({p for p, _n, _t in kept if p > 0}, reverse=True):
        if len(_render(kept, shed, notice)) <= cap:
            break
        if shed and len(_render(kept, shed, None)) <= cap:
            break                       # only the notice is over — resolved below, never another shed
        shed.extend(n for p, n, _t in kept if p == priority)
        kept = [b for b in kept if b[0] != priority]
    text = _render(kept, shed, notice)
    if len(text) <= cap or not shed:
        return text, shed               # fits, or pinned-alone oversize (emitted whole, documented above)
    if len(_render(kept, shed, None)) <= cap:
        compact = _render(kept, shed, compact_notice) if compact_notice is not None else None
        if compact is not None and len(compact) <= cap:
            return compact, shed
        return _render(kept, shed, None), shed
    return text, shed


def decide(permission: str, reason: str | None = None) -> dict:
    """A PreToolUse structured permission decision (allow/deny/ask). → structured stdout, exit 0.
    `deny` is a block expressed through the structured channel rather than exit 2."""
    return {"action": "decide", "permissionDecision": permission, "reason": reason}


# ---- the fail-open-and-flag harness -----------------------

# The honest tail appended to a fail-open finding's in-session line — STRICTLY conditional on whether the
# durable promotion actually landed (StarshipSuperjam/engine-template#391). The old copy asserted "this was recorded as a problem to fix"
# unconditionally while nothing recorded anything; that was false. These say what is actually true.
_RECORDED_TAIL = " This was recorded as a tracked item you'll see at your next start."
_NOT_RECORDED_TAIL = (" I've noted it here, but could not file it as a tracked item yet — it is not durably "
                      "recorded until the engine next reaches GitHub.")


def _fail_open_source_id(event: str, kind: str) -> str:
    """The dedup key for a fail-open finding: COARSE (per event + failure-kind, NEVER per-occurrence), so a
    gate that keeps failing collapses onto ONE tracked Issue via telemetry's source-keyed dedup rather than
    spamming one per crash. Marker-safe by construction — fixed tokens and the platform event name, never
    operator input (so it can carry no forged `<!-- engine-signal -->` marker)."""
    return f"hooks/fail-open/{event}/{kind}"


def _promote_fail_open(event: str, kind: str, message: str) -> bool:
    """Best-effort DURABLE promotion of a fail-open finding to a tracked engine-labelled Issue, via
    telemetry's out-of-band `promote_finding` — the same "log it" relay `close.py` uses at cap-exhaustion
    (detection-vs-relay seam; this is the promotion StarshipSuperjam/engine-template#391 wires, retiring the old `owes → telemetry`).

    Two invariants make this safe to reach from the shared harness:
      - LAZY imports: `telemetry`/`boot` (and the network) load ONLY here, on a fail-open branch — never on
        the happy hot path every hook rides (the hot-path latency law).
      - FAIL-SAFE: ANY error is swallowed and returns False. Recording the crash must NEVER re-break the
        fail-open path into a block or an unhandled crash — that would re-create the exact fail-CLOSED
        stranding of a non-engineer the whole law (and StarshipSuperjam/engine-template#390) forbids.
    Returns True when the Issue was opened/updated; False when offline / unreachable / errored — in which
    case the finding was still surfaced in-session and the protected-branch merge is the durable backstop.
    The general triage LOOP that drains and reconciles at scale (auto-close, ambient capture, the refused-
    cursor and broken-runtime routing) is telemetry's live loop — issues StarshipSuperjam/engine-template#403 / StarshipSuperjam/engine-template#412, not here.

    The promoter is INJECTABLE into run_hook (default = this), which is how the demo and the promote/copy
    behaviour tests exercise it without a network. As a hard SAFETY BACKSTOP for a safety feature, this also
    refuses to touch live GitHub under a test harness: a fail-open firing in ANY test must never open a real
    engine Issue (boot.gh_token can resolve a logged-in `gh auth token` even locally). Production hook
    execution never imports `unittest`; the real wiring is tested directly via `_do_promote_fail_open`."""
    if "unittest" in sys.modules:   # backstop: never reach live GitHub from a test run
        return False
    return _do_promote_fail_open(event, kind, message)


def _do_promote_fail_open(event: str, kind: str, message: str) -> bool:
    """The real promotion wiring, split out so it is directly testable against a mocked telemetry without the
    test-harness backstop above. Emits the fail-open finding through telemetry's EMIT-AND-DONE seam
    (`telemetry.emit_finding`): the hook hands telemetry a TRUST_CRITICAL record and is
    done — telemetry now OWNS resolving the GitHub boundary (the repo slug + token it used to resolve here) and
    promoting it. Un-inverts the seam (the producer no longer holds telemetry's acting-mechanism) while
    preserving the exact fail-open behaviour: a trust-critical finding promotes immediately, returns the Issue
    number (truthy) when it lands and False offline / with no token (surfaced-not-durably-recorded, the honest
    tail StarshipSuperjam/engine-template#391 depends on)."""
    try:
        import telemetry  # lazy: keep telemetry's stack + the network off every hook's happy path
        now = moment.utc_now()
        record = {"source_id": _fail_open_source_id(event, kind), "severity": telemetry.TRUST_CRITICAL,
                  "message": message, "first_seen": now, "last_seen": now}
        return bool(telemetry.emit_finding(record))
    except Exception:  # noqa: BLE001 — recording the crash must NEVER fail-close the gate; degrade silently
        return False


def _emit_finding(err, severity: str, event: str, kind: str, message: str, promote) -> None:
    """Surface a fail-open finding in plain language on stderr (the channel the platform shows) AND promote
    it to a durable tracked engine Issue best-effort (StarshipSuperjam/engine-template#391). `message` is the base statement — what could
    not run, and that the action was allowed to proceed — carrying NO recording claim; this appends the
    honest tail conditional on `promote(event, kind, message)` actually landing, so the engine never again
    tells the operator something was "recorded" when it was not. `promote` is injected (default
    `_promote_fail_open`) so tests/the demo never reach live GitHub."""
    try:
        recorded = bool(promote(event, kind, message))
    except Exception:  # noqa: BLE001 — belt-and-suspenders: the fail-open guarantee must NOT depend on the
        recorded = False  #   promoter behaving. Even a misbehaving promoter degrades to surfaced-not-recorded
        #                     rather than propagating to re-break the fail-open path into a block or a crash.
    f = validate.finding(severity, message + (_RECORDED_TAIL if recorded else _NOT_RECORDED_TAIL))
    err.write(f["message"] + "\n")


def _record_crash_debug(event: str, exc: BaseException, path: str | None = None) -> None:
    """Append a compact DIAGNOSTIC entry for a fail-open handler crash to the engine-only debug FILE — for
    the engine's OWN later debugging, NEVER operator-facing. It captures the exception (type + message) and
    the last traceback frame (file:line), so a transient crash (e.g. a mid-edit NameError that fires the
    gate once and is then fixed) leaves a locatable trail instead of the anonymous type name the
    operator-facing finding carries.

    It goes to a gitignored file the operator never sees — deliberately NOT to stderr (which the platform
    DOES show the operator on the non-blocking fail-open exit — that is where the plain-language finding
    goes) and NOT to the promoted Issue body. So the raw message + code location (backstage detail an
    operator must never be shown) stay engine-only, while the operator-facing surfaces carry only the
    exception type. The sink is telemetry's gitignored cache (the crash is promoted as a telemetry
    finding, so its backstage detail belongs beside telemetry's cache); telemetry is lazy-imported here,
    exactly as the fail-open promotion path already does. Best-effort and fully swallowed by the caller:
    recording a crash must never re-break fail-open."""
    if "unittest" in sys.modules and path is None:
        # The hermetic backstop its siblings already carry (telemetry.emit_finding, providers'
        # live-session write): the test suite exercises crashing handlers by the hundred, and without
        # this guard every run appends test-harness noise to the PRODUCTION crash log — the pollution
        # that made tonight's real-crash archaeology harder (StarshipSuperjam/engine-template#520/StarshipSuperjam/engine-template#522 investigation). An explicit
        # `path` (the unit tests' own temp file) still writes.
        return
    if path is None:
        import telemetry  # noqa: E402 — lazy, on the fail-open branch only (as _do_promote_fail_open is)
        path = telemetry.HOOK_CRASH_DEBUG_PATH
    tb = getattr(exc, "__traceback__", None)
    frames = traceback.extract_tb(tb) if tb else []
    where = f" @ {os.path.basename(frames[-1].filename)}:{frames[-1].lineno}" if frames else ""
    stamp = moment.utc_now()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {event} handler crash: {type(exc).__name__}: {exc}{where}\n")


def run_hook(event: str, handler, *, stdin=None, stdout=None, stderr=None, promote=None,
             fail_open_notice: str | None = None) -> int:
    """Run one hook event under the fail-open-and-flag law and the platform contract. `event` is the
    Claude Code event name (the calling hook script declares it); `handler(payload) -> decision` is the
    owning system's behavior. Returns the process exit code (the caller does `sys.exit(run_hook(...))`).

    `fail_open_notice` lets the owning system supply the OPERATOR-facing sentence for a handler CRASH,
    instead of the generic "a safety check on the {event} step could not run" line — so a gate can speak in
    its own plain terms (close's disposition gate passes the spec's "I couldn't run the check that confirms
    nothing was dropped — review this turn's work with extra care" — it fails open, and says so).
    It replaces ONLY the operator message on the crash branch; the single promote path, the engine-only
    crash-debug trace (which still records the exception type + file:line), and the honest recorded/not-
    recorded tail are unchanged — the copy is owned by the caller, the acting-mechanism stays here.

    The law, in order:
      1. Read the event JSON from stdin (tolerant). A payload the platform cannot even deliver is the
         platform's contract breaking, not the operator's fault — FAIL OPEN (never block on it) + flag.
      2. A forced Stop continuation (`stop_hook_active` true: the platform is force-ending the turn
         after the block cap) STILL runs the handler — the owning system may need the give-up moment
         (close degrades a still-undispositioned finding to a logged tracked finding here, so the cap
         can never lose one) — but it MUST NOT re-block, or it loops until the cap, so ANY block it
         returns is downgraded to proceed (in run_hook, the budget law; `_translate` stays pure).
      3. Run the handler. ANY exception → the guarded action proceeds (non-blocking exit) and the
         failure becomes a plain-language finding: a crashing gate must never strand a non-engineer
         who cannot debug it, and must never fail silently.
      4. Translate the handler's decision to the platform contract — gating a block to the two
         block-eligible events (the budget); a block requested elsewhere is itself fail-open + flag."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    inp = sys.stdin if stdin is None else stdin
    # The fail-open promoter is injected (default = the real, lazy, fail-safe one) so tests and the demo
    # never reach live GitHub; production hook scripts call run_hook(event, handler) and get the real one.
    promote = _promote_fail_open if promote is None else promote

    try:
        raw = inp.read()
        payload = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        # Canonicalize the payload vocabulary at the boundary (eADR-0034): a Codex payload is
        # rewritten into the canonical tool names every handler is written against; a Claude
        # payload passes through as the SAME object (identity — providers' test pins it), so the
        # Claude path is byte-unchanged. A normalization fault is a payload fault: fail open.
        payload = providers.normalize(event, payload)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:  # noqa: BLE001 — reading the platform's event must NEVER block: any input the
        #   platform delivers (or fails to) is fail-open, never the operator's fault.
        _emit_finding(err, "hard", event, "input",
                      f"The {event} hook could not read its event input, so it could not run; the "
                      f"action was allowed to proceed.", promote)
        return EXIT_NONBLOCKING

    # A forced Stop continuation: the handler still runs (close needs the give-up moment to log a
    # still-undispositioned finding), but its block is downgraded to proceed below so it can NEVER
    # re-block and loop the cap. stop_hook_active is only ever set by the platform on a Stop.
    forced_stop = event == "Stop" and payload.get("stop_hook_active") is True

    try:
        decision = handler(payload) if handler is not None else proceed()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — fail-open is the whole point. SystemExit
        #   is included DELIBERATELY: a handler that reaches past the decision protocol and calls
        #   sys.exit() (e.g. exit 2 to force a block) must STILL fail open — the harness owns the exit
        #   code, so a handler bug can never fail-closed and strand a non-engineer. KeyboardInterrupt /
        #   GeneratorExit (not caught here) stay propagating so an operator can still interrupt.
        try:
            _record_crash_debug(event, exc)
        except Exception:  # noqa: BLE001 — a diagnostic aid must NEVER re-break fail-open into a block or a
            pass           #   crash: if recording the trace fails (disk, perms), drop it and still flag + proceed.
        crash_message = fail_open_notice if fail_open_notice else (
            f"A safety check on the {event} step could not run ({type(exc).__name__}); the "
            f"action was allowed to proceed. The work was not verified by that check.")
        _emit_finding(err, "hard", event, "crash", crash_message, promote)
        return EXIT_NONBLOCKING

    if forced_stop and isinstance(decision, dict) and decision.get("action") == "block":
        decision = proceed()   # no-re-block guarantee, by construction (the harness, not the handler)

    return _translate(event, decision or proceed(), out, err, promote)


def _translate(event: str, decision, out, err, promote) -> int:
    """Pure translation of a handler decision → (exit code, stdout/stderr writes). Enforces the block
    budget: a block is honored (exit 2) ONLY on a block-eligible event; anywhere else it is a misuse
    that fails open and flags rather than blocks."""
    action = decision.get("action") if isinstance(decision, dict) else None

    if action == "block":
        if event not in BLOCK_ELIGIBLE_EVENTS:
            _emit_finding(err, "hard", event, "block-misuse",
                          f"A {event} hook tried to hard-block, but only {sorted(BLOCK_ELIGIBLE_EVENTS)} "
                          f"may block; the action was allowed to proceed.", promote)
            return EXIT_NONBLOCKING
        err.write((decision.get("reason") or "") + "\n")
        return EXIT_BLOCK

    if action == "inject":
        out.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": decision.get("context", ""),
        }}) + "\n")
        return EXIT_PROCEED

    if action == "decide":
        perm = decision.get("permissionDecision")
        if event != "PreToolUse" or perm not in PERMISSION_DECISIONS:
            _emit_finding(err, "hard", event, "decide-misuse",
                          f"A {event} hook returned a permission decision {perm!r}, which is only valid "
                          f"as one of {sorted(PERMISSION_DECISIONS)} on a PreToolUse hook; the action was "
                          f"allowed to proceed.", promote)
            return EXIT_NONBLOCKING
        result = {"hookEventName": "PreToolUse", "permissionDecision": perm}
        if decision.get("reason"):
            result["permissionDecisionReason"] = decision["reason"]
        out.write(json.dumps({"hookSpecificOutput": result}) + "\n")
        return EXIT_PROCEED

    return EXIT_PROCEED


# ---- the operator-runnable demo (a throwaway fixture; no registered hook exists yet) ----

def _demo_promoter(event: str, kind: str, message: str):
    """The demo's fail-open promoter: runs the REAL `telemetry.emit_finding` seam against a FAKE GitHub
    transport injected as the boundary (only the network is faked — the demo-fidelity rule), so the demo
    exercises the SAME emit-and-done path production uses (not one layer below it) and shows the finding
    actually being promoted plus the honest "recorded" copy WITHOUT touching live GitHub. Returns the (fake)
    Issue number, so the demo renders the promoted case."""
    import telemetry
    fake = telemetry._FakeGitHub()
    gh = telemetry.GitHubIssues("you/your-project", "demo-token", transport=fake.transport)
    record = {"source_id": _fail_open_source_id(event, kind), "severity": telemetry.TRUST_CRITICAL,
              "message": message, "first_seen": moment.utc_now(), "last_seen": moment.utc_now()}
    return telemetry.emit_finding(record, gh=gh)


def _run_capture(event: str, handler, payload: dict, promote=None):
    """Run the REAL committed harness with a fixture handler over a synthetic payload, capturing its
    stdout/stderr — so the demo exercises the shipped run_hook, not a reimplementation. The fail-open
    promoter is the demo one (real relay, faked network) so the demo can never open a live Issue."""
    import io
    out, err = io.StringIO(), io.StringIO()
    code = run_hook(event, handler, stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=err,
                    promote=_demo_promoter if promote is None else promote)
    return code, out.getvalue(), err.getvalue()


def _demo(_argv: list) -> int:
    """A scripted, operator-runnable fail-open demonstration. It runs the REAL `run_hook` (only the
    misbehaving handler is a fixture) for the block and crash cases, and shells out to an absent
    interpreter for the missing-runtime case — proving the THREE fail-open behaviors without touching
    the real .venv. Vary it: change the handlers, or rename .engine/.venv and re-run the printed
    hook command line yourself."""
    print("The hooks fail-open contract — three behaviors (only the misbehaving handler is a fixture;")
    print("the harness run is the real committed .engine/tools/hooks.py):\n")

    print("(1) A gate that BLOCKS — a handler that returns block() on a block-eligible event (Stop):")
    code, _out, err = _run_capture("Stop", lambda p: block("not done yet — finish the disposition"),
                                   {"hook_event_name": "Stop"})
    print(f"    exit code = {code}  (2 = block; the platform feeds this back to Claude)")
    print(f"    stderr → Claude: {err.strip()!r}\n")
    c1 = code

    print("(2) A gate that CRASHES — a handler that raises; the action must still proceed:")
    code, _out, err = _run_capture("PreToolUse", lambda p: (_ for _ in ()).throw(RuntimeError("boom")),
                                   {"hook_event_name": "PreToolUse"})
    print(f"    exit code = {code}  (not 2, so NON-blocking — the tool runs)")
    print(f"    finding on stderr — now PROMOTED to a tracked Issue with the honest 'recorded' copy (#391): "
          f"{err.strip()!r}\n")
    c2, crash_err = code, err

    print("(2b) A block requested on a NON-eligible event (PostToolUse) — the budget rejects it, "
          "fail-open:")
    code, _out, err = _run_capture("PostToolUse", lambda p: block("I should not be able to block here"),
                                   {"hook_event_name": "PostToolUse"})
    print(f"    exit code = {code}  (not 2 — only PreToolUse/Stop may block)")
    print(f"    finding on stderr: {err.strip()!r}\n")
    c2b = code

    print("(2c) The SAME crash but with GitHub UNREACHABLE (offline) — the copy stays HONEST and never")
    print("     claims a record that did not happen (this is the exact state the old code lied about):")
    code, _out, err = _run_capture("PreToolUse", lambda p: (_ for _ in ()).throw(RuntimeError("boom")),
                                   {"hook_event_name": "PreToolUse"}, promote=lambda *a: False)
    print(f"    exit code = {code}  (still NON-blocking)")
    print(f"    honest offline copy on stderr: {err.strip()!r}\n")
    offline_err = err

    import tempfile
    print("(3) The hook LAUNCHER (.engine/tools/hook-runner.sh) — the wait/exec preamble lives here now,")
    print("    so the command Claude Code DISPLAYS after a hook fires is short instead of a wall of shell:")
    old = ('n=0; while [ ! -x "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python" ] && [ "$n" -lt 50 ]; '
           'do sleep 0.1; n=$((n+1)); done; [ -x "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python" ] && '
           'exec "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python" ${CLAUDE_PROJECT_DIR}/tools/boot.py')
    print(f"    BEFORE: {old}")
    print(f"    AFTER:  {hook_command('tools/boot.py')}")
    print("    (the AFTER line is exactly what the hook-display renders; the surrounding 'Pre-compact /")
    print("     completed successfully' chrome is the platform's and cannot be changed.)\n")

    runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-runner.sh")
    print("    The launcher still waits-then-execs the venv interpreter and NEVER falls back to system")
    print("    Python — proven against the REAL hook-runner.sh on throwaway interpreters:")
    with tempfile.TemporaryDirectory() as td:                         # (a) interpreter PRESENT -> execs it
        stub = os.path.join(td, "python")
        with open(stub, "w") as fh:
            fh.write('#!/bin/sh\necho "RAN $@"\n')
        os.chmod(stub, 0o755)
        r = subprocess.run(["sh", runner, stub, os.path.join(td, "hook.py"), "demo-arg"],
                           capture_output=True, text=True, timeout=10)
        print(f"      present → {r.stdout.strip()!r}  (execs the interpreter; the script + args forward)")
        present_out = r.stdout
    with tempfile.TemporaryDirectory() as td:                         # (b) NEVER appears -> runs nothing
        r = subprocess.run(["sh", runner, os.path.join(td, "python"), os.path.join(td, "hook.py")],
                           capture_output=True, text=True, timeout=10,
                           env={**os.environ, "ENGINE_HOOK_WAIT_POLLS": "3",
                                "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})
        print(f"      never   → stdout={r.stdout.strip()!r} exit={r.returncode}  "
              f"(ran nothing; no system-Python fallback)")
        print(f"                #391: instead of exiting SILENTLY, the launcher now names the absent runtime "
              f"on stderr (NON-blocking): {r.stderr.strip()!r}\n")
        never_out = r.stdout
        never_err = r.stderr

    print("All three fail-open cases proceeded without a hard block except the one deliberate, eligible")
    print("block — and (#391) the crash/budget findings were PROMOTED to a tracked engine Issue (shown here")
    print("against a faked GitHub) and the missing-runtime case named its absent runtime. The fail-open")
    print("floor holds, and the operator is now told rather than left blind.")
    print("(#391 wired the promotion shown above; boot orientation carries any promoted finding via its "
          "open-findings register, and the PR Validation line is surfaced at submit per build-orchestration. "
          "The live triage LOOP that reconciles and auto-closes at scale is telemetry's — #403/#412.)")
    # Self-check (a demo that can FAIL): the eligible block returns exit 2; a crashing handler and a block on
    # a non-eligible event both proceed (not 2); the launcher execs a present interpreter but runs nothing on
    # an absent one (no fallback); AND StarshipSuperjam/engine-template#391 — the crash finding was promoted with the honest "recorded" copy,
    # and the missing-runtime case NAMED its absent runtime on stderr instead of exiting silently.
    ok = (c1 == 2 and c2 != 2 and c2b != 2 and "RAN" in present_out and not never_out.strip()
          and "recorded as a tracked item" in crash_err and "not durably" in offline_err
          and "runtime is not ready" in never_err)
    if not ok:
        print("\nDEMO UNEXPECTED: the hooks fail-open contract or the launcher's no-fallback behaviour did "
              "not hold.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "demo":
        return _demo(argv[1:])
    print(f"usage: hooks.py demo\nunknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
