#!/usr/bin/env bash
# Run the four CI gates (lint, format, types, tests) the way ci.yml runs
# them, from any checkout: the main tree or an agent worktree.
#
# Every tool comes from the project's own environment through `uv run
# --extra dev`, never from PATH. A system ruff can carry a different rule
# set (0.17 changed its defaults), and a bare `uv run mypy` fails to spawn
# in a fresh worktree whose environment has not synced the dev extra.
#
# Extra arguments go to pytest, e.g. `tools/gates.sh tests/test_capture_*.py`.
# Exit status is the first failing gate's.
set -euo pipefail
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

run() { uv run --locked --extra dev "$@"; }

echo "== ruff check";  run ruff check .
echo "== ruff format"; run ruff format --check .
echo "== mypy";        run mypy
echo "== pytest";      run pytest -q -p no:cacheprovider "$@"
echo "== gates passed at $(git rev-parse --short HEAD)"
