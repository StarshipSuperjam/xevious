#!/usr/bin/env python3
"""scent.py — the per-prompt recall cue (boot/orientation; core-owned).

The per-prompt member of the orientation family. It is "metacognition as a push": on every prompt
(`UserPromptSubmit`) it injects one short, constant, AI-facing line asking whether this project has already
settled the thing at hand — and, when it may have, to run the recall workflow instead of answering from a
recollection that does not survive between sessions. It pushes the CUE; the model pulls the content.

WHY A CUE AND NOT A POINTER. This seam used to run a fast keyword lookup over memory and inject pointers to
whichever stored records matched the prompt's words. That fired only when the prompt happened to share
vocabulary with the original conversation — silent on exactly the reworded questions recall keeps failing, and
silent on the case that costs most: a prompt that names no past at all while the project has already decided
the question, already tried and rejected the approach, or already stated a preference against it. The reflex
that catches those cannot be a word-match; it has to be a standing question the model asks itself. So the
payload is a constant, the firing is unconditional, and the intelligence lives in the workflow the cue names.

OWNERSHIP — the close-relay twin (NOT memory-owned). `UserPromptSubmit` is a single-owner `boot/orientation`
event (hooks.py EVENT_INVENTORY `("boot",)`; the locked hooks owner table), so this is a boot/core-owned tool,
wired in core's manifest. Its ONLY reach toward memory is asking whether the module is installed at all — it
reads no memory, opens no store, and resolves no data path. On a repo without the memory module the seam is
inert (silent), never a fault — the close-relay degrade-clean precedent.

THE LAWS (all load-bearing here and pinned by tests):
  - EVERY PROMPT. The reflex is the deliverable: a per-prompt event that fires only sometimes teaches the model
    that silence means "no memory", and it may not thin to no per-prompt event at all (eADR-0018). The same
    prompt twice, and two prompts sharing no vocabulary, all get the identical cue.
  - NEAR-ZERO. Fires every prompt, so it imports only `hooks` (+ `validate`) and asks `importlib` one question;
    it starts no subprocess, opens no database, and reads no file that grows with memory. Its cost does not
    grow with the store. The injected text is bounded by `_CUE_MAX_CHARS` and that bound is tested, because
    `additionalContext` persists in history and an unbounded cue would accrue every turn.
  - CONTENT-FREE. The cue names no record, no role, no tag and no stored text — there is nothing to leak,
    because nothing is read. What it carries is a question to ask and where to go to answer it.
  - POINTS AT ONE PROCEDURE. The workflow depth lives in `.engine/operations/memory-recall.md`, never copied
    here; the cue and the `engine-recall` skill are two doors into the same room. A test pins that the named
    operation exists, since a rename would otherwise leave the cue firing at nothing.
  - WRITES NOTHING ITSELF. The hook appends no record and keeps no session state. (What the workflow it
    provokes does write is memory's own business — the `search` tool records usage on every call.)
  - DEGRADE / FAIL-OPEN. No memory module -> silent. Any crash injects nothing (the hooks harness fail-opens),
    never stalling the turn.

CLI:  python tools/scent.py            # hook mode: run the UserPromptSubmit handler over stdin (what the
                                       #   wired hook invokes; injects additionalContext, fail-open)
      python tools/scent.py demo       # an operator-runnable fail-then-pass demonstration
"""
from __future__ import annotations

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks     # noqa: E402  (run_hook + inject/proceed: the fail-open harness this rides)
import validate  # noqa: E402  (ENGINE_DIR: resolving the operation the cue names, for the demo and its test)


# ---- the cue (a recorded build-spec leaf) --------------------------------------------------
# The one procedure the cue points at. Repo-relative and identical on both runtimes — deliberately not a
# runtime-specific command form (`/engine-recall` on Claude, `$engine-recall` on Codex), so one string serves
# both. `_OPERATION_FILE` is the same path resolved on disk, which the demo and a test check actually exists.
_OPERATION = ".engine/operations/memory-recall.md"
_OPERATION_FILE = os.path.join(validate.ENGINE_DIR, "operations", "memory-recall.md")

# AI-FACING text (this reaches the model via additionalContext, never the operator's screen).
#
# The trigger is deliberately "may this project have already settled this", NOT "does this prompt mention an
# earlier session". Every backward-referencing shape — "what did we decide", "why did we do it that way" —
# already reaches recall through the `engine-recall` skill's own description, on both runtimes. What nothing
# else catches is the prompt that announces no past while the project has already answered it: "should we use
# a cron job or hook the calendar?" when that was tried and rejected, or "make the onboarding copy longer"
# when a stated preference says keep it short. Those are the expensive misses, and they are why the wording
# leads with the three record kinds a forward-looking prompt can collide with rather than with the past tense.
_CUE = (
    "You do not remember this project's earlier sessions; its saved memory does. Before proposing an "
    "approach, making a call, or acting on an instruction, ask whether it was already decided, already tried "
    f"and rejected, or covered by a stated preference — and when it may have been, follow `{_OPERATION}` "
    "first."
)

# The near-zero bound, tested rather than asserted in prose. The cue is injected on EVERY prompt and
# `additionalContext` persists in history, so its length is a standing per-turn cost: this ceiling is what
# stops a later edit growing that cost quietly. Roughly a hundred tokens at the ceiling.
_CUE_MAX_CHARS = 400


# ---- the one question this seam asks about memory --------------------------------------------

def _memory_installed() -> bool:
    """Whether the memory module is present at all — the inert-seam gate, and the ONLY reach toward memory on
    this hot path. `find_spec` locates the package without importing or executing it, so the cost is a handful
    of path stats and nothing in memory's own import chain (sqlite3, the ledger, capture) is paid per prompt.

    Deliberately NOT a check on whether the store holds anything: resolving the ledger's path forks a
    `git rev-parse` subprocess (the ledger is shared across every worktree of a clone, so its location cannot
    be derived from this file's own), which is a process spawn per prompt to decide whether to print a
    constant. A store with nothing in it costs the model one search that finds nothing, once, since it then
    knows for the rest of the session — a far cheaper wrong answer than a subprocess on every turn, and one
    that cannot silence the cue outright the way a mis-resolved path would."""
    try:
        return importlib.util.find_spec("memory") is not None
    except Exception:  # noqa: BLE001 — an unimportable/odd path entry reads as absent (inert, never a fault)
        return False


# ---- the UserPromptSubmit handler -----------------------------------------------------------

def handler(payload: dict) -> dict:
    """The per-prompt recall cue. Rides `hooks.run_hook` (fail-open: any crash injects nothing).

      no prompt                 -> proceed (nothing to consult memory about)
      no memory module          -> proceed (silent; the seam is inert without memory)
      else                      -> inject the cue, unconditionally and identically
    """
    payload = payload if isinstance(payload, dict) else {}
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return hooks.proceed()
    if not _memory_installed():
        return hooks.proceed()
    return hooks.inject(_CUE)


# ---- the operator-runnable demo --------------------------------------------------------------
# Run it:
#     uv run --directory .engine --frozen -- python tools/scent.py demo
# It exercises the REAL handler, so a real regression flips a `!!!` and returns non-zero. Plain words only.

# Two prompts that share no words with each other. Neither mentions the past; one is the shape the old
# keyword-matching pointer was blind to. Vary them and re-run — the cue does not move, which is the point.
_DEMO_PROMPT_A = "should we use a nightly cron job for this?"
_DEMO_PROMPT_B = "make the welcome copy longer"


def _inject_text(decision) -> "str | None":
    """The text a handler decision would inject, or None when it stays silent."""
    if isinstance(decision, dict) and decision.get("action") == "inject":
        return decision.get("context", "")
    return None


def _demo() -> int:
    import shutil
    import tempfile
    try:
        # Demo-only (never the hot path). Guarded because this file is CORE-owned while memory is an optional
        # module: on a repo without it, PART 5's whole point is that the cue stays silent, and dying with an
        # import traceback before reaching that part would be the one shape this demo must not take.
        from memory import index, ledger, records
    except ImportError:
        print("This project does not have the memory feature installed, so there is nothing for the per-prompt")
        print("reminder to point at — and it stays silent. That is the correct behaviour, and it is all there")
        print("is to show here. Install the memory feature and run this again to see the reminder itself.")
        return 0 if _inject_text(handler({"prompt": _DEMO_PROMPT_A, "session_id": "demo"})) is None else 1

    def run(prompt):
        return _inject_text(handler({"prompt": prompt, "session_id": "demo"}))

    results: list = []

    # PART 1 — it speaks on EVERY prompt, not only when your words happen to match
    print("=" * 80)
    print("PART 1 — the reminder arrives on every message, including the same one twice")
    print("=" * 80)
    first, second, third = run(_DEMO_PROMPT_A), run(_DEMO_PROMPT_A), run(_DEMO_PROMPT_B)
    # `bool(first)`, not `first is not None`: an injection carrying an EMPTY payload is the thinning this
    # demonstration exists to rule out, and it would satisfy an is-not-None check while the narrative below
    # printed "(silent)" beside a passing verdict.
    ok1 = bool(first) and first == second == third
    print(f'\n  "{_DEMO_PROMPT_A}"           -> {"a reminder" if first else "(silent)"}')
    print(f'  the very same message again           -> {"a reminder" if second else "(silent)"}')
    print(f'  "{_DEMO_PROMPT_B}"                 -> {"a reminder" if third else "(silent)"}')
    print("\n  what it says, every time:")
    for line in (first or "(nothing)").splitlines():
        print(f"      {line}")
    print(f'\n  The first and third messages above ("{_DEMO_PROMPT_A}" and "{_DEMO_PROMPT_B}") share no words')
    print("  with each other, and neither mentions anything from the past.")
    print("  The old version looked for words matching something stored and stayed quiet otherwise — so it was")
    print("  silent exactly when you asked in different words, or asked about something already settled without")
    print(f"  knowing it was. => {'Same reminder every message.' if ok1 else '!!! it varied or went silent'}")
    results.append(ok1)

    # PART 2 — it never carries anything you stored
    print("\n" + "=" * 80)
    print("PART 2 — it never repeats anything from your saved memory")
    print("=" * 80)
    tmp = tempfile.mkdtemp(prefix="engine-scent-demo-")
    prev = os.environ.get(ledger.ENV_DIR)
    os.environ[ledger.ENV_DIR] = tmp     # a PRACTICE cabinet; the real store is never touched
    try:
        # Belt on that brace. Every worktree of this clone SHARES one real ledger, so a mis-resolved path here
        # would write demo records into the operator's actual memory. Assert the resolved path really is the
        # throwaway one before the first append, rather than trusting the environment variable took effect.
        resolved = ledger.ledger_path()
        if not resolved.startswith(tmp):
            print(f"  !!! refusing to run: the practice cabinet did not take effect ({resolved}).")
            return 1
        empty = run(_DEMO_PROMPT_A)
        secrets = [
            {"role": "decision", "tags": ["scheduling"], "text": "we rejected the nightly cron job outright"},
            {"role": "preference", "tags": ["onboarding"], "text": "keep the welcome copy short"},
        ]
        now = int(__import__("time").time())
        for m in secrets:
            ledger.append({**m, "ts": now, records.RECORD_ID_KEY: records.new_record_id()},
                          path=ledger.ledger_path())
        index.rebuild()
        filled = run(_DEMO_PROMPT_A)
        leaked = [m["text"] for m in secrets if m["text"] in (filled or "")]
        ok2 = bool(filled) and filled == empty and not leaked
        print("\n  Two memories were filed in the practice cabinet, one of which answers the message above.")
        print(f"  reminder with memory EMPTY  -> {len(empty or '')} characters")
        print(f"  reminder with memory FILLED -> {len(filled or '')} characters, identical: "
              f"{'yes' if filled == empty else 'NO'}")
        print(f"  repeats anything you stored? {'NO' if not leaked else 'YES — ' + str(leaked)}")
        print("\n  It is a reminder to go and look, never a peek at what is in there. Nothing you stored travels")
        print("  into the conversation unless the assistant deliberately looks it up.")
        print(f"  => {'Identical, and quoted nothing.' if ok2 else '!!! it varied with your data or leaked it'}")
        results.append(ok2)
    finally:
        if prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = prev
        shutil.rmtree(tmp, ignore_errors=True)

    # PART 3 — it stays small, and there is a ceiling holding it there
    print("\n" + "=" * 80)
    print("PART 3 — it stays short, because it is added to every single message")
    print("=" * 80)
    # Measure what the handler ACTUALLY injected, never the module's constant — a demonstration that reads the
    # constant is measuring the intention rather than the behaviour.
    size = len(first or "")
    ok3 = 0 < size <= _CUE_MAX_CHARS
    print(f"\n  the reminder is {size} characters; the limit it is held to is {_CUE_MAX_CHARS}")
    print("  A test holds the reminder to 400 characters — written out in the test itself, so raising the")
    print("  limit is a visible change someone has to approve, not something that slips through. It is added")
    print("  to every message and stays in the conversation, so its length is a running cost, not a one-off.")
    print(f"  => {'Within its limit.' if ok3 else '!!! over the limit'}")
    results.append(ok3)

    # PART 4 — it points at a procedure that actually exists
    print("\n" + "=" * 80)
    print("PART 4 — the instructions it points to are really there")
    print("=" * 80)
    ok4 = _OPERATION in _CUE and os.path.isfile(_OPERATION_FILE)
    print(f"\n  it points to: {_OPERATION}")
    print(f"  that file exists: {'yes' if os.path.isfile(_OPERATION_FILE) else 'NO'}")
    print("  A reminder pointing at a moved or renamed file would still look fine — it would arrive every")
    print("  message and say the right words — while quietly leading nowhere. So this is checked.")
    print(f"  => {'Points somewhere real.' if ok4 else '!!! it points at nothing'}")
    results.append(ok4)

    # PART 5 — no memory module, no reminder
    print("\n" + "=" * 80)
    print("PART 5 — on a project without the memory feature it says nothing at all")
    print("=" * 80)
    # Swap the module-level function the REAL handler resolves at call time, so this exercises the real
    # branch rather than a demo-only stand-in (restored in `finally`, the save/restore pattern).
    original = globals()["_memory_installed"]
    globals()["_memory_installed"] = lambda: False
    try:
        absent = run(_DEMO_PROMPT_A)
    finally:
        globals()["_memory_installed"] = original
    ok5 = absent is None
    print(f"\n  with the memory feature removed -> {'(silent)' if absent is None else 'STILL SPOKE'}")
    print("  Pointing at instructions for a feature that is not installed would be worse than saying nothing.")
    print(f"  => {'Silent without memory.' if ok5 else '!!! it spoke anyway'}")
    results.append(ok5)

    print("\n" + "-" * 80)
    print("What this changes for you: from now on, every message you send quietly carries a short note asking")
    print("the assistant to check whether this project already decided the thing, already tried and rejected")
    print("it, or already stated a preference about it — before it answers from its own recollection, which")
    print("does not carry over between sessions. The note itself contains none of your saved memory. When the")
    print("assistant judges it relevant it then goes and searches, which takes a moment and reads back real")
    print("past conversation; when it judges it irrelevant, nothing happens. It deletes nothing, and whether")
    print("it looks is its judgement, not a guarantee.")
    return 0 if all(results) else 1


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if not argv or argv[0] == "hook":
        # Hook mode: what the wired UserPromptSubmit hook invokes. run_hook reads the event JSON from stdin,
        # runs the handler, translates inject -> structured stdout (additionalContext), fail-open on any error.
        return hooks.run_hook("UserPromptSubmit", handler)
    print("usage: scent.py [hook | demo]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
