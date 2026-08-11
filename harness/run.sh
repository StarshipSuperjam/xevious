#!/usr/bin/env bash
# One-command local loop: build the .sb3 from source, then run the harness.
# Uses `python` (expects 3.12, matching CI); with only an older system python, build
# with `uv run --python 3.12 -- python tools/scratch_project.py build` first, then
# `cd harness && node --test`.
set -euo pipefail
cd "$(dirname "$0")/.."
python tools/scratch_project.py build
cd harness
# Install deps through the guarded path (never a bare `npm install`) if they are absent.
[ -d node_modules ] || npm ci --ignore-scripts
node --test
