#!/bin/sh
# hook-runner.sh — the engine's hook launcher.
#
# Every engine hook registered in .claude/settings.json runs through this one script instead of
# inlining a wall of shell in each registration. The command Claude Code DISPLAYS after a hook fires
# is the registration's command string verbatim, so collapsing the preamble here keeps that display
# short and legible to the non-engineer operator (it otherwise reads as a wall of code / an error).
#
# What it does:
#   - RESOLVES the engine tool-runtime interpreter for THIS machine's OS at fire time. The committed
#     command names the POSIX form ($1 = ${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python); if that
#     layout is absent — a Windows adopter, or a teammate on an OS other than the one that provisioned
#     the committed settings.json — the launcher falls back to the Windows sibling
#     (${CLAUDE_PROJECT_DIR}/.engine/.venv/Scripts/python.exe) under the SAME venv root. Both are
#     ${CLAUDE_PROJECT_DIR}-rooted engine-venv interpreters; it NEVER runs a bare/system Python. This
#     is what lets one committed repo work on every OS (including a mixed-OS team): it resolves the
#     per-OS venv-bin-path at fire time rather than baking one OS into the
#     committed command.
#   - a BOUNDED wait for that interpreter to appear — the fresh-worktree race (issue #83): the
#     gitignored .engine/.venv is provisioned a beat after a checkout, so a hook that fires in that
#     window finds no interpreter; the wait polls for either OS's layout, then runs the one present.
#   - it NEVER falls back to the operator's system Python — if neither layout appears within the bound,
#     it runs NOTHING (constraints: the engine "cannot manage a language runtime"). It also drops a
#     best-effort PRESENCE marker so that could-not-run leaves a durable trace: the `drain-inbox`
#     SessionStart driver promotes it into a tracked finding on the next healthy session (see the tail).
#
# INVARIANT (do not break): hook commands are ALWAYS rendered in the POSIX bin/python form by
# hooks.hook_command, and this launcher owns the per-OS resolution. Do NOT wire provisioning to
# re-render the Windows form into the committed command — the venv-root derivation below assumes $1 is
# the POSIX .../bin/python path, and a Windows-form $1 would strip wrong and resolve nothing (silent
# floor-only boot, the exact failure this launcher fixes). The single per-OS layout fact lives in
# hooks.interpreter_path; a drift test pins this launcher's bin/python + Scripts/python.exe literals to it.
#
# Usage (this exact form is rendered by hooks.hook_command, so it is byte-pinned by a drift test):
#   sh hook-runner.sh <venv-interpreter> <script> [args...]
#     $1        the explicit ${CLAUDE_PROJECT_DIR}-rooted venv interpreter in POSIX form
#               (.engine/.venv/bin/python). It is named in the command string itself, so the property that the
#               hook command names the interpreter explicitly stays mechanically witnessable in the
#               diff; the per-OS bin/ ÷ Scripts\ resolution is decided here.
#     $2 .. $#  the hook script (${CLAUDE_PROJECT_DIR}-rooted) and any trailing args (e.g. `hook`).
#
# The wait ceiling is 50 polls x 0.1s (~5s); ENGINE_HOOK_WAIT_POLLS / ENGINE_HOOK_WAIT_INTERVAL override
# it (the tests shrink it so they stay fast). Invoked as `sh hook-runner.sh ...`, so it needs no
# executable bit and travels via "Use this template" like every other committed engine file.
interp="$1"
shift
# Derive the venv root from the named POSIX interpreter, and the Windows sibling under it. On a POSIX
# machine $1 (bin/python) is present and used directly — byte-for-byte the prior behavior; the sibling
# is computed only as a fallback, and only when $1 has the expected .../bin/python shape.
venv="${interp%/bin/python}"
alt="$venv/Scripts/python.exe"
polls="${ENGINE_HOOK_WAIT_POLLS:-50}"
interval="${ENGINE_HOOK_WAIT_INTERVAL:-0.1}"
n=0
# Wait (bounded) for an EXECUTABLE interpreter of either layout — `-x`, not `-f`: a half-written file in
# the #83 provisioning window is not run before it is complete, and a present-but-non-runnable interpreter
# still reaches the fail-open readout below instead of a raw `exec` error. Git Bash reports a `.exe` as
# executable, so `-x` resolves the Windows layout too.
while [ ! -x "$interp" ] && [ ! -x "$alt" ] && [ "$n" -lt "$polls" ]; do
    sleep "$interval"
    n=$((n + 1))
done
# Prefer the named ($1) interpreter; fall back to the Windows sibling under the same venv root when the
# POSIX layout is absent (a Windows / mixed-OS machine). The winner resolves into $interp, so there is
# still a single run of one venv-rooted interpreter — never a bare/system Python.
if [ ! -x "$interp" ] && [ "$venv" != "$interp" ] && [ -x "$alt" ]; then
    interp="$alt"
fi
if [ -x "$interp" ]; then
    exec "$interp" "$@"
fi
# Neither the POSIX nor the Windows venv interpreter appeared within the wait bound (issue #83's
# fresh-worktree window elapsed, or .engine/.venv is not provisioned). There is no interpreter here to
# emit a structured finding, so — per the fail-open law's missing-runtime variant — NAME the absent
# runtime plainly for the operator instead of exiting silently, and exit NON-blocking (never 2, which
# the platform reads as a hard block: the #390 fail-closed stranding). Boot carries the standing DURABLE
# surfacing of an unhealthy runtime (the promotion is #391/#412).
# Fail-safe: this readout cannot fail the script closed.
printf '%s\n' "The engine could not run its own tools: its private Python runtime is not ready (looked for $interp and its Windows equivalent). I have not verified this step — this is not a block. If it keeps happening, the engine's environment needs to be set up (provisioning)." >&2
# Leave a durable trace: the interpreter never started, so nothing here can promote a telemetry finding (that
# needs Python + GitHub). Best-effort, drop a PRESENCE marker under the engine's gitignored cache — the
# `drain-inbox` SessionStart driver converts a present marker into ONE tracked "could-not-run" finding on the
# next session where the runtime is healthy (telemetry.RUNTIME_HEALTH_MARKER_PATH; fail-open-and-flag: a missing tool-runtime surfaces as a crash would). The marker path is derived from the venv root ($1),
# so it needs no env and a test can point it anywhere; it is EMPTY (presence-only) so no bytes can enter the
# finding. Every step is best-effort and MUST NOT change the exit code below — a marker-write failure can never
# turn this fail-open readout into a block. (`${venv%/.venv}` is the .engine dir; venv is derived from $1 above.)
cache_dir="${venv%/.venv}/telemetry/.cache"
mkdir -p "$cache_dir" 2>/dev/null && : > "$cache_dir/runtime-health.marker" 2>/dev/null || true
exit 1
