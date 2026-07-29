#!/bin/sh
# The Codex hook launcher — the thin shim between a .codex/hooks.json command and the shared
# hook runner. Codex has no project-directory substitution token (Claude's ${CLAUDE_PROJECT_DIR}),
# so this shim locates the project root itself ($PWD when the hook runs at the root, else git),
# tags the process with the engine's provider marker, and execs the UNCHANGED shared runner
# (hook-runner.sh), which owns the venv wait and per-OS interpreter fallback. If no root can be
# found it prints one plain line and exits 1 — fail-open (never exit 2): a lost hook must never
# block the operator's action.
#
# Usage (rendered by hooks.hook_command(provider="codex"); drift-pinned by test):
#   sh .engine/tools/codex-hook-runner.sh ".engine/tools/<script>.py" [args...]
root="$PWD"
if [ ! -f "$root/.engine/tools/hook-runner.sh" ]; then
  root="$(git rev-parse --show-toplevel 2>/dev/null)"
fi
if [ -z "$root" ] || [ ! -f "$root/.engine/tools/hook-runner.sh" ]; then
  printf '%s\n' "The engine could not find its project folder from this hook, so this step was skipped (nothing was blocked)." >&2
  exit 1
fi
ENGINE_PROVIDER=codex
export ENGINE_PROVIDER
script="$1"
shift
exec sh "$root/.engine/tools/hook-runner.sh" "$root/.engine/.venv/bin/python" "$root/$script" "$@"
