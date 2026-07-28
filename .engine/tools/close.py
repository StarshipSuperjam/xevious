#!/usr/bin/env python3
"""close: the turn-close `Stop` hook (the finding-disposition gate + ambient-capture trigger).

"Close" has two senses; this tool owns the TURN close. SESSION close —
the submitted pull request — is build-orchestration's and is not here. The `Stop` hook does
exactly two things at the end of every turn:

  1. AMBIENT CAPTURE — relay the turn's delta to MEMORY's ledger. This is memory's mechanism; close only
     TRIGGERS it and never gates it. Memory-substrate has shipped, so this relay is now LIVE:
     it appends the completed turn's delta to memory's ledger. Still best-effort and fail-soft — any fault
     (including the import, on a repo without the memory module) is a silent no-op, never raising into the
     handler, so capture never gates close.

  2. THE FINDING-DISPOSITION GATE — the trust spine. Under the standing pushback habit every concern the
     session raises takes exactly one durable disposition (fix in line / log a tracked issue / escalate;
     finding-disposition policy). While the session's ephemeral, session-scoped findings record holds an
     UNDISPOSITIONED entry, the `Stop` hook HARD-BLOCKS the turn (looping the model back to disposition it),
     then proceeds. This is the second member of the hooks block budget (the first is modes' explore
     write-gate); Stop is block-eligible, so the block-budget coherence leg stays green over it.

THE HONEST TIER — posture plus a STRONG LOCAL BLOCK over the RECORDED SUBSET, never an absolute wall. It is mechanical only on what was recorded (writing a raised
concern into the record is the AI's discipline — posture); the durable, unbypassable backstop is the
protected-branch merge. Cap-exhaustion degrades a recorded finding to LOGGED, never lost. The gate fails
open (a crash lets the turn END — the inverse direction of modes' write-gate, both "fail toward
not-stranding"). Routine is satisfiable non-interactively (log-it discharges without a human). It never
deadlocks.

THE CHANNELS (stated honestly, never overstated). The RELIABLE surface is the PUSHBACK: a `Stop` block is
exit-2 + stderr (hooks.block), and the platform feeds that reason back to Claude, who relays it. The
legibility of a disposition LOOP rides this same reliable channel: on the 2nd+ consecutive block the pushback
collapses to one calm line (`_LOOP_LINE`) instead of re-listing, and on the approach to the block cap it
folds in a PRE-ANNOUNCEMENT (`_CAP_APPROACH`) that the open items will be saved and the turn finished — so a
non-engineer is told before an unexplained hang, not after. A clean (exit-0) `Stop` has no quiet
read-and-stop channel: its stdout is debug-log only, and a `Stop` hook's one inject channel
(`hookSpecificOutput.additionalContext`) CONTINUES the turn rather than ending it — so the engine declines
it (EVENT_INVENTORY marks Stop `injects:False` deliberately; injecting on the FORCED continuation would just
re-extend a turn the platform force-ended). The clean-turn disposition summary is therefore ASSISTANT-NARRATED
(computed here via `summary`, quiet when nothing needed action; the standing duty to relay it lives in the
deployed floor). The POST-HOC cap-stop / fail-open notices remain best-effort whose DURABLE record is the
logged Issue (re-surfaced at the next boot) — never dressed as a guaranteed operator surface. (The gate acts
only on the `stop_hook_active` boolean — the platform sets it on the forced continuation after the cap of
consecutive blocks — blocking while it is false and logging+proceeding when true; the within-turn block count
that times the loop-line / pre-announce is legibility bookkeeping only, never the block/proceed decision.)

CLI (the operator-runnable demo; the live gate is what the wired `Stop` hook invokes):
  python tools/close.py                                   # hook mode: run the Stop gate over stdin
  python tools/close.py record  --session S --message M   # record a raised concern (undispositioned)
  python tools/close.py dispose --session S --id F --kind fixed|logged|escalated
  python tools/close.py pending --session S               # the undispositioned findings (what holds a turn)
  python tools/close.py summary --session S               # the plain-language disposition summary
  python tools/close.py clear   --session S               # wipe the record (a fresh turn)
  python tools/close.py demo                              # a scripted held-then-ends demonstration
  python tools/close.py demo-relay                        # the ambient-capture relay inert->live demo (M1 seam)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks      # noqa: E402  (run_hook + block/proceed: the fail-open harness the gate rides)
import telemetry  # noqa: E402  (promote_finding: the out-of-band 'log it' relay; telemetry owns it)
import validate   # noqa: E402  (collect + local_ctx: the pre-close local full-suite advisory pass)


# ---- the block this owning system declares for the hook block budget ------------------------
# hooks.py "names no invariant itself", so the consumer (module_coherence.block_eligible_registrations)
# assembles the registry from each owner's declaration; the block-registry leg
# (validate.block_budget_findings) reads `event` (only PreToolUse/Stop may block) AND `modes` (the mode
# dimension declared as data). Stop is block-eligible. The
# findings-disposition gate fires on ANY turn-close, so it is active in every stance — and its close
# must stay satisfiable non-interactively in a routine run (an unattended run cannot answer a prompt).
# (This exact dict is fixtured by test_hooks.py's block-eligibility test.)
BLOCK_INVARIANT = {"event": "Stop", "name": "findings-disposition", "owner": "close",
                   "modes": ["explore", "build", "routine"]}

# The three durable dispositions every raised concern must reach (finding-disposition policy).
DISPOSITIONS = frozenset({"fixed", "logged", "escalated"})


# ---- the ephemeral findings record: OS-temp, session-keyed checklist ------------------------
# A session_id-keyed JSON checklist in OS-temp storage (the modes stance-signal pattern). NON-committed, never read across sessions, no repo footprint, no gitignore
# wire, no catalog entry — a committed or gitignored ledger would resurrect the dissolved session archive.
# It tracks what the session raised and whether each has a disposition; the gate reads the
# UNDISPOSITIONED subset. Distinct by construction from telemetry's cache and memory's ledger (the three
# "capture" records stay separate). The prefix differs from modes' `engine-stance-` (no collision).
_RECORD_PREFIX = "engine-findings-"


def _sanitize(session_id):
    """A filename-safe, length-bounded slug of the platform session id (it keys the OS-temp record). An
    empty/garbled id yields "" -> _record_path returns None -> the record is empty -> nothing pending ->
    the turn ENDS (degrade SAFE). A local copy of the modes stance-signal pattern (keeps boot's import
    surface off this turn-end hot path)."""
    if not session_id or not isinstance(session_id, str):
        return ""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:200]


def _record_path(session_id):
    """The OS-temp path for a session's findings record, or None when there is no usable session id."""
    slug = _sanitize(session_id)
    return os.path.join(tempfile.gettempdir(), f"{_RECORD_PREFIX}{slug}") if slug else None


def read_findings(session_id):
    """The session's findings checklist — a list of {id, message, disposition, location}. Absent /
    unreadable / malformed -> [] (degrade SAFE: an unreadable record means nothing pending, so the gate
    never HOLDS the turn on a record it cannot read — the fail-open direction)."""
    path = _record_path(session_id)
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — absent / unreadable / malformed -> empty, never a crash or a hold
        return []
    findings = data.get("findings") if isinstance(data, dict) else None
    return findings if isinstance(findings, list) else []


def _read_record(session_id):
    """The full session record dict (`{findings, blocks}`), or `{}` when absent / unreadable / malformed —
    the same fail-open direction as read_findings."""
    path = _record_path(session_id)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — absent / unreadable / malformed -> empty, never a crash
        return {}
    return data if isinstance(data, dict) else {}


def _blocks(session_id):
    """The per-turn consecutive-Stop-block count carried in the record (0 when absent / malformed). It is
    legibility bookkeeping only — it feeds the pushback REASON TEXT, never the block/proceed decision (which
    stays driven by `pending()` + `stop_hook_active`), so it can never make the gate fail closed."""
    n = _read_record(session_id).get("blocks")
    return n if isinstance(n, int) and n > 0 else 0


def _write_record(session_id, findings, blocks):
    path = _record_path(session_id)
    if not path:
        return False
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"findings": findings, "blocks": blocks}, fh)
        return True
    except Exception:  # noqa: BLE001 — a failed write degrades to "no record" -> nothing held, never a crash
        return False


def _write_findings(session_id, findings):
    """Write the findings list, PRESERVING the block counter — record_finding / dispose (a loop entry or
    exit) must not clobber the count that accumulates between the consecutive pure-block Stops in between."""
    return _write_record(session_id, findings, _blocks(session_id))


def _bump_blocks(session_id):
    """Increment the per-turn consecutive-block counter, returning the new count. Preserves findings.
    Best-effort: a failed read/write still returns a usable count, so the reason text is never withheld."""
    rec = _read_record(session_id)
    n = rec.get("blocks")
    n = (n if isinstance(n, int) and n > 0 else 0) + 1
    findings = rec.get("findings")
    _write_record(session_id, findings if isinstance(findings, list) else [], n)
    return n


def _reset_blocks(session_id):
    """Reset the consecutive-block counter to 0 — but ONLY when a record already exists, so a quiet turn
    (nothing raised) still leaves NO OS-temp footprint (the 'no record when nothing was raised' property)."""
    path = _record_path(session_id)
    if not path or not os.path.exists(path):
        return
    rec = _read_record(session_id)
    findings = rec.get("findings")
    _write_record(session_id, findings if isinstance(findings, list) else [], 0)


def record_finding(session_id, message, location=None):
    """Record a concern the session raised, UNDISPOSITIONED. The AI calls this under its standing pushback
    habit — the habit is POSTURE; the gate is mechanical on what was recorded. Idempotent on an identical
    still-open message (returns the existing id, never a duplicate). Returns the finding id, or None when
    there is no usable session id / the write failed."""
    message = (message or "").strip()
    if not message:
        return None
    findings = read_findings(session_id)
    for f in findings:
        if f.get("message") == message and f.get("disposition") is None:
            return f.get("id")           # idempotent: don't double-record the same open concern
    fid = f"f{len(findings) + 1}"
    findings.append({"id": fid, "message": message, "disposition": None, "location": location})
    return fid if _write_findings(session_id, findings) else None


def dispose(session_id, finding_id, kind):
    """Mark a recorded finding's durable disposition (fixed / logged / escalated). Returns True iff a
    matching still-open finding was marked."""
    if kind not in DISPOSITIONS:
        raise ValueError(f"unknown disposition {kind!r}; expected one of {sorted(DISPOSITIONS)}")
    findings = read_findings(session_id)
    changed = False
    for f in findings:
        if f.get("id") == finding_id and f.get("disposition") is None:
            f["disposition"] = kind
            changed = True
    return _write_findings(session_id, findings) if changed else False


def pending(session_id):
    """The undispositioned findings — exactly what the gate holds the turn on."""
    return [f for f in read_findings(session_id) if f.get("disposition") is None]


def clear(session_id):
    """Wipe the session's findings record (a fresh turn, or after a force-end logs the leftovers).
    Idempotent (a missing record is success); never raises."""
    path = _record_path(session_id)
    if not path:
        return False
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 — a failed delete is not fatal; an unreadable record holds nothing
        return False
    return True


def summary(session_id):
    """The plain-language disposition summary the operator reads instead of the transcript.
    Computed here, ASSISTANT-NARRATED (a clean Stop has no guaranteed in-transcript channel — see the
    module docstring). Quiet ("" ) when nothing this turn needed action; names what is still open while a
    turn is mid-disposition."""
    findings = read_findings(session_id)
    if not findings:
        return ""                                    # quiet — nothing was flagged
    still = [f for f in findings if f.get("disposition") is None]
    if still:
        n = len(still)
        return (f"{n} thing{'' if n == 1 else 's'} you raised this turn still "
                f"need{'s' if n == 1 else ''} a disposition before we finish.")
    counts = []
    fixed = sum(1 for f in findings if f.get("disposition") == "fixed")
    logged = sum(1 for f in findings if f.get("disposition") == "logged")
    escalated = sum(1 for f in findings if f.get("disposition") == "escalated")
    if fixed:
        counts.append(f"{fixed} fixed")
    if logged:
        counts.append(f"{logged} saved as a follow-up item{'' if logged == 1 else 's'}")
    if escalated:
        counts.append(f"{escalated} raised for your decision")
    return "everything I flagged this turn is handled — " + ", ".join(counts) + "."


# ---- operator-legible notices ----
# The RELIABLE surface is the pushback (the block reason, exit-2 stderr, fed to Claude, who relays it). The
# repeated-pushback LOOP line and the cap PRE-ANNOUNCEMENT ride that same reliable channel — folded into the
# block reason on the 2nd+ consecutive block and on the approach to the cap, so a non-engineer never meets an
# unexplained hang. The POST-HOC cap-stop / fail-open notices
# still ride exit-0 paths whose in-transcript surfacing is NOT guaranteed (a Stop hook does not inject; its
# exit-0 stdout is debug-log only), so their DURABLE record stays the logged Issue — never dressed as a
# guaranteed operator line. No backstage vocabulary leaks: no "Stop hook", "block budget", etc.
_LOOP_LINE = "sorting out where the open findings should go — one moment."
_CAP_APPROACH = "If we can't settle this, I'll save them as tracked follow-ups and finish up."
_CAP_STOP = "I've saved the open follow-up(s) as tracked items so they're not lost, and finished up."
# The cap-exhaustion, GitHub-offline case: the disposition check DID run (it found the open findings) — only the
# durable SAVE could not land, so the honest line is "couldn't save them as tracked yet", NOT "couldn't run the
# check". The kept findings re-surface next turn (the finding survives regardless).
_CAP_UNTRACKED = ("I've noted the open follow-up(s), but couldn't save them as tracked items just now — I'll try "
                  "again next turn, so nothing is lost. Review this turn's work with extra care.")
# The gate-CRASH notice (the gate fails open, and says so): the disposition check itself could not
# run. Passed to hooks.run_hook as fail_open_notice, so a crash speaks this plain line instead of the generic
# "a safety check on the Stop step could not run".
_FAIL_OPEN_NOTICE = ("I couldn't run the check that confirms nothing was dropped — review this turn's work "
                     "with extra care.")


def _effective_block_cap():
    """The operator-effective consecutive-Stop-block cap — hooks.STOP_HOOK_BLOCK_CAP, overridable via the env
    var the platform honors (hooks.STOP_HOOK_BLOCK_CAP_ENV). Used ONLY to time the cap pre-announcement, so
    the notice never promises an imminent finish the operator's own raised cap has not yet reached."""
    raw = os.environ.get(hooks.STOP_HOOK_BLOCK_CAP_ENV)
    try:
        n = int(raw)
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    return hooks.STOP_HOOK_BLOCK_CAP


def _pushback(open_findings, count=1):
    """The plain-language pushback — the reliable surface (the block reason fed back to Claude, who relays it).
    `count` is this turn's consecutive-block number. First block (count<2): the full ask — the way forward
    (fix / save as a follow-up / flag) plus what is still open, so the model knows what to settle. Repeated
    pushback (count>=2): one calm line instead of re-listing, and — on the approach to the effective cap — a pre-announcement that the open items will
    be saved and the turn finished, so the operator is told before the hang rather than meeting it unexplained.
    A clean turn ends without a block, so the disposition summary itself is assistant-narrated (see summary)."""
    if count < 2:
        head = ("Before we finish: something you raised this turn still needs a decision — fix it now, save it "
                "as a follow-up item, or flag it for me. I won't end the turn until it's settled.")
        items = "; ".join((f.get("message") or "") for f in open_findings)
        return f"{head} Still open: {items}" if items else head
    line = _LOOP_LINE
    if count >= _effective_block_cap() - 1:
        line = f"{line} {_CAP_APPROACH}"
    return line


# ---- the ambient-capture trigger (LIVE relay seam; memory's mechanism) ----------------------

def _trigger_ambient_capture(payload):
    """Relay the turn's delta to MEMORY's ambient capture (close only triggers; memory owns the mechanism
    and gates nothing). Memory-substrate has shipped, so
    this seam is now LIVE: `memory.capture_turn_delta` appends the completed turn's delta to the ledger. Still
    best-effort and fail-soft — any failure (including the import, on a repo without the memory module) is a
    silent no-op, so capture never gates close and never raises into the handler. (Operator-runnable proof:
    `close.py demo-relay` — the M1 inert→live crossover demo.)"""
    try:
        import memory  # noqa: F401,E402 — absent on a repo without memory-substrate; ImportError -> silent no-op
        memory.capture_turn_delta(payload)
    except Exception:  # noqa: BLE001 — capture is ambient and NEVER gates close; any failure is a no-op
        return


# ---- the telemetry relay: the "log it" disposition, applied at the cap on the AI's behalf ----

def _source_id(finding):
    """A content-derived, stable dedup key so the SAME concern, if it escapes the gate across turns,
    collapses onto ONE tracked Issue (telemetry dedups by source_id — content, never per-occurrence
    material)."""
    digest = hashlib.sha1((finding.get("message") or "").encode("utf-8")).hexdigest()[:12]
    return f"close/disposition/{digest}"


def _to_finding_record(finding, now):
    """A complete finding-record.v1 for telemetry's promotion path. severity=trust-critical so it promotes
    immediately (a dropped disposition is trust-relevant); `location` set EXPLICITLY (the schema requires
    the key — null when none); first_seen == last_seen == now (close is cache-free; the Issue carries its
    own history). The message self-frames: the engine's disposition gate is reporting that it logged an
    unsettled concern so it isn't lost."""
    concern = (finding.get("message") or "").strip()
    message = ("A concern raised while working wasn't given a disposition before the turn ended, so the "
               f"engine logged it here so it isn't lost. The concern: {concern}")
    return {"source_id": _source_id(finding), "severity": telemetry.TRUST_CRITICAL,
            "message": message, "location": finding.get("location"),
            "first_seen": now, "last_seen": now}


def _github():
    """The engine-Issue boundary for the RARE promote path. repo/token are reused from boot's single source
    via a LAZY import reached ONLY here (cap-exhaustion / fail-open) — so the common turn-end path imports
    neither boot's heavy stack nor the network. Returns None when
    repo/token are unavailable (offline) -> promotion degrades to surfaced-not-tracked, the merge wall the
    backstop. (The shared GitHub-context home is a later tidy when build-orch/24 becomes a second consumer.)"""
    try:
        from boot import repo_slug, gh_token  # lazy: keep boot off the hot path; reached only when promoting
        repo, token = repo_slug(), gh_token()
    except Exception:  # noqa: BLE001 — any failure obtaining GitHub context -> no durable tracking, wall holds
        return None
    if not repo or not token:
        return None
    return telemetry.GitHubIssues(repo, token)


_UNSET = object()   # sentinel: distinguishes "no boundary passed (resolve _github)" from "offline (None)"


def _promote(finding, now, github=_UNSET):
    """Log ONE undispositioned finding down telemetry's out-of-band promotion path — the policies "log it"
    disposition, applied by the gate on the AI's behalf when the cap is hit. Best-effort: returns the Issue
    number on success, or False when GitHub is unavailable/unreachable (the concern was already surfaced
    in-session; the protected-branch merge is the durable backstop). `github` is injectable for the demo
    and tests (only the network is faked; the relay logic is real) — passing None means OFFLINE (so a test
    can never reach live GitHub), while omitting it resolves the real boundary via _github()."""
    gh = _github() if github is _UNSET else github
    if gh is None:
        return False
    return telemetry.promote_finding(gh, _to_finding_record(finding, now), now)


# ---- the pre-close local full-suite advisory (validation's authoritative local pass) --------
# The `pre-close` suite is validation's authoritative LOCAL full-suite pass: the Stop hook always fires before work leaves the session, where the
# per-commit intercept can be missed on a manual commit. It is ADVICE — a hard finding surfaces to
# stderr, never a block. close's ONLY block stays the undispositioned-findings gate; this runs on the
# clean-proceed path AFTER that decision, in its OWN guard, so a broken rule can never propagate into
# run_hook's handler-level fail-open and swallow the disposition block. It runs every clean turn-close
# (spec-literal — a work-gate would skip exactly the manual-commit case pre-close exists to catch); on
# this repo the bound rules disclosed-no-op fast with no network (a local run reaches no GitHub event).

def _run_preclose_advisory() -> None:
    """Run the pre-close suite and surface any hard finding as an advisory stderr notice — never blocks,
    never raises into the Stop handler. A clean Stop has no guaranteed in-transcript channel, so this notice is best-effort; the merge-time CI run is the gate."""
    try:
        findings = validate.collect("pre-close", validate.local_ctx(), with_source=True)
        hard = [f for f in findings if f.get("severity") == "hard"]
        if hard:
            sys.stderr.write(
                "A local advisory check flagged the following as you finish — it does NOT block, and the "
                "automatic check that runs when a change is proposed for merge is the gate:\n"
                + "\n".join("  - " + validate.fmt(f) for f in hard) + "\n")
    except Exception:  # noqa: BLE001 — advisory only: nothing here (collect, format, or write) may reach
        return          # the disposition gate above; a broken suite/rule/finding degrades to silence


# ---- the turn-close Stop gate ---------------------------------------------------------------

def handler(payload):
    """The turn-close `Stop` gate. FIRST trigger ambient memory capture (live since memory shipped; never
    gates). Then read the undispositioned findings:
      - none            -> proceed (the turn ends; the summary, if any, is assistant-narrated);
      - pending, normal -> BLOCK with the plain pushback (exit-2 stderr, fed to Claude — the reliable
                           surface), looping the model back to disposition;
      - pending, FORCED -> (stop_hook_active: the platform is force-ending the turn at the block cap) LOG
                           each leftover down telemetry's promotion path (degrade recorded->logged, never
                           lost), clear the record, proceed — never re-block, so the cap can never deadlock.
    The whole handler rides hooks.run_hook's fail-open: a crash lets the turn END and is flagged."""
    payload = payload if isinstance(payload, dict) else {}
    _trigger_ambient_capture(payload)
    session_id = payload.get("session_id")
    open_findings = pending(session_id)
    if not open_findings:
        _reset_blocks(session_id)                         # a clean turn ends the block streak (no-op if no record)
        _run_preclose_advisory()                          # local full-suite ADVICE; never gates (see below)
        return hooks.proceed()                            # nothing undispositioned -> the turn ends
    if payload.get("stop_hook_active") is not True:
        count = _bump_blocks(session_id)                  # this turn's consecutive-block number (legibility only)
        return hooks.block(_pushback(open_findings, count))  # push back; loop-line on repeats, pre-announce near cap
    # Forced continuation: degrade recorded -> logged so nothing is lost, then proceed (run_hook would
    # downgrade a block here anyway; we never re-enter the gate THIS turn, so the loop can't deadlock).
    now = telemetry.utc_now()
    github = _github()
    # Log each leftover; KEEP any that could NOT be durably tracked (GitHub offline/unreachable) so it
    # re-surfaces next turn rather than being silently dropped — no consent is lost, the
    # finding survives regardless. A tracked leftover is removed; an untracked one stays in the record.
    kept = [f for f in open_findings if not _promote(f, now, github)]
    if kept:
        _write_findings(session_id, kept)
    else:
        clear(session_id)
    _reset_blocks(session_id)                             # the streak ends here; don't carry a stale count forward
    # Best-effort same-turn notice; the durable record is the logged Issue (or, for a kept leftover, its
    # re-appearance next turn). Honest: cap-stop only when EVERY leftover was tracked; otherwise the "couldn't
    # save them yet" line (the check RAN — only the durable write was offline; _FAIL_OPEN_NOTICE, "couldn't run
    # the check", belongs to a gate CRASH, now carried via run_hook's fail_open_notice).
    sys.stderr.write((_CAP_UNTRACKED if kept else _CAP_STOP) + "\n")
    return hooks.proceed()


# ---- the CLI (the operator-runnable demo; the live gate is the wired Stop hook) -------------

def _arg(argv, flag):
    """The value following `flag` in argv, or None."""
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _verdict(decision):
    """Render a handler decision as a one-line operator-facing verdict for the demo."""
    if isinstance(decision, dict) and decision.get("action") == "block":
        return f"HELD — {decision.get('reason')}"
    return "ENDS (proceed)"


def _demo(_argv):
    """A scripted held-then-ends demonstration over the REAL handler (only the session id, and the GitHub
    network for the cap step, are fixtures)."""
    sid = "engine-demo-close-session"
    clear(sid)
    print("The turn-close disposition gate — what the Stop hook decides (the real handler):\n")

    v1 = _verdict(handler({'session_id': sid}))
    print(f"(1) A turn that raised nothing: pending={len(pending(sid))} -> {v1}   (quiet; the turn ends)")

    fid = record_finding(sid, "The new endpoint has no rate limit.")
    print(f"\n(2) The session raises a concern (record -> {fid}); a turn-end is now HELD until it's settled:")
    v2 = _verdict(handler({'session_id': sid}))
    print(f"    pending={len(pending(sid))} -> {v2}")

    dispose(sid, fid, "logged")
    print(f"\n(3) Disposition it (saved as a follow-up); the turn now ENDS:")
    v3 = _verdict(handler({'session_id': sid}))
    print(f"    pending={len(pending(sid))} -> {v3}")
    print(f"    summary -> {summary(sid)!r}")

    clear(sid)
    record_finding(sid, "A config value looks wrong but I'm out of room to confirm it.")
    fake = telemetry._FakeGitHub()
    gh = telemetry.GitHubIssues("you/your-project", "demo-token", transport=fake.transport)
    orig = globals()["_github"]
    globals()["_github"] = lambda: gh                     # inject the faked boundary for the cap step
    try:
        verdict = _verdict(handler({"session_id": sid, "stop_hook_active": True}))
    finally:
        globals()["_github"] = orig
    open_issues = sum(1 for i in fake.issues.values() if i["state"] == "open")
    print(f"\n(4) Cap-exhaustion (a forced continuation) — the leftover is LOGGED, not lost; never deadlocks:")
    print(f"    pending-before=1 -> {verdict};  tracked items now: {open_issues};  "
          f"pending-after={len(pending(sid))}")
    print("    (this step used an in-memory stand-in for GitHub — NO real issue was created. The relay")
    print("     logic ran for real; that a logged item durably persists and re-appears at the next start,")
    print("     and that the engine only ever touches items it created itself, are confirmed live, not here.)")

    print("\nThe gate is posture + a strong local block over what was recorded — the merge wall is the only "
          "guarantee; the pushback is the reliable surface, the clean-turn summary is narrated.")
    # Self-check: a quiet turn ends, a raised concern HELDs the turn, disposition lets it end, and the
    # cap-exhausted forced continuation logs the leftover (never deadlocks).
    ok = "ENDS" in v1 and "HELD" in v2 and "ENDS" in v3 and open_issues >= 1
    if not ok:
        print("\nDEMO UNEXPECTED: the held-then-ends disposition gate or the cap-exhaustion logging did not "
              "behave as expected.", file=sys.stderr)
        return 1
    return 0


def _demo_relay(_argv=None):
    """Operator-runnable fail-then-pass for the AMBIENT-CAPTURE relay — the M1 seam close TRIGGERS and memory
    OWNS (the twin of the per-prompt scent; both seam demos gate M1). On throwaway stores (a temp ledger + a
    temp transcript) it shows the relay was a SAFE no-op while memory was unshipped (the inert state — and the
    turn still ENDS, because capture never gates close), and that it now CAPTURES the turn into memory's ledger
    (the live state). The REAL relay + real memory.capture run; only the session and transcript are fixtures."""
    import builtins
    import shutil
    from unittest import mock
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from memory import ledger, capture  # noqa: E402
    except Exception:  # noqa: BLE001 — no memory module: the relay's inert state, demonstrated by its absence
        print("This repo has no memory module installed — so the relay has nothing to capture into, which is")
        print("itself the safe INERT state this seam degrades to. Install memory-substrate to watch it light up.")
        return 0

    tmp = tempfile.mkdtemp(prefix="engine-close-relay-demo-")
    prev_dir = os.environ.get(ledger.ENV_DIR)
    prev_tx = os.environ.get(capture.TRANSCRIPT_DIR_ENV)
    os.environ[ledger.ENV_DIR] = tmp
    os.environ[capture.TRANSCRIPT_DIR_ENV] = tmp
    try:
        transcript = os.path.join(tmp, "transcript.jsonl")
        with open(transcript, "w", encoding="utf-8") as fh:
            for role, text in (("user", "we should ship the calendar sync on Friday"),
                               ("assistant", "agreed — the calendar sync ships Friday")):
                fh.write(json.dumps({"type": role, "message": {"role": role, "content": text}}) + "\n")
        payload = {"session_id": "demo-relay", "transcript_path": transcript}

        def stored():
            return sum(1 for _ in ledger.iter_records(path=ledger.ledger_path()))

        print("The end-of-turn AMBIENT-CAPTURE relay — close TRIGGERS it, memory OWNS it (an M1 seam):\n")

        # (1) INERT — memory unreachable (as it was before memory shipped): the relay no-ops AND the turn ends.
        real_import = builtins.__import__

        def no_memory(name, *a, **k):
            if name == "memory" or name.startswith("memory."):
                raise ImportError("memory not shipped")
            return real_import(name, *a, **k)

        before = stored()
        with mock.patch("builtins.__import__", side_effect=no_memory):
            _trigger_ambient_capture(payload)                            # the REAL relay, memory forced absent
            ended = _verdict(handler({"session_id": "demo-relay-x"}))    # the turn still closes
        inert = stored() - before
        print("(1) memory UNREACHABLE (the seam's inert state before memory shipped):")
        print(f"    captured this turn: {inert}   (the relay safely does nothing)")
        print(f"    the turn still:     {ended}   (capture NEVER gates the turn)")

        # (2) LIVE — memory present: the relay captures the turn-delta into the ledger.
        before = stored()
        _trigger_ambient_capture(payload)                                # the REAL relay, memory present
        live = stored() - before
        print("\n(2) memory PRESENT (now that it has shipped):")
        print(f"    captured this turn: {live}   (the turn's words landed in memory's ledger)")

        ok = inert == 0 and live >= 1 and "ENDS" in ended
        print("\n" + ("DEMO PASSED: dormant it was a safe no-op; live it captures — and never gates the turn."
                      if ok else "DEMO DID NOT BEHAVE AS EXPECTED — see above."))
        return 0 if ok else 1
    finally:
        for key, prev in ((ledger.ENV_DIR, prev_dir), (capture.TRANSCRIPT_DIR_ENV, prev_tx)):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    cmd = argv[0] if argv else "hook"
    if cmd == "hook":
        # Hook mode: what the wired Stop hook invokes. run_hook reads the event JSON from stdin, runs the
        # gate, translates block() -> exit 2 + stderr, downgrades a forced-continuation block, fail-open.
        # fail_open_notice: if the disposition gate itself CRASHES, the operator hears close's own plain line
        # (the gate fails open, and says so), not run_hook's generic wording.
        return hooks.run_hook("Stop", handler, fail_open_notice=_FAIL_OPEN_NOTICE)
    if cmd == "record":
        fid = record_finding(_arg(argv, "--session"), _arg(argv, "--message"))
        print(f"recorded: {fid}")
        return 0 if fid else 1
    if cmd == "dispose":
        try:
            ok = dispose(_arg(argv, "--session"), _arg(argv, "--id"), _arg(argv, "--kind") or "")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"disposed: {ok}")
        return 0 if ok else 1
    if cmd == "pending":
        for f in pending(_arg(argv, "--session")):
            print(f"  {f.get('id')}: {f.get('message')}")
        return 0
    if cmd == "summary":
        print(summary(_arg(argv, "--session")))
        return 0
    if cmd == "clear":
        print(f"cleared: {clear(_arg(argv, '--session'))}")
        return 0
    if cmd == "demo":
        return _demo(argv[1:])
    if cmd == "demo-relay":
        return _demo_relay(argv[1:])
    print("usage: close.py [hook | record --session S --message M | dispose --session S --id F --kind K | "
          "pending --session S | summary --session S | clear --session S | demo | demo-relay]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
