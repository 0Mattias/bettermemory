#!/usr/bin/env bash
# Maintainer lane: produce the committed live comparative artifact.
#
# Installs the competitor stack into a throwaway .eval-venv/ (gitignored,
# deleted at the end) so competitor pins never touch the dev venv,
# uv.lock, or published metadata. CI never runs this — the default
# harness keeps seeing the honest SystemUnavailable stub rows, and the
# live integration tests self-skip without BM_EVAL_LIVE=1.
#
# Prereqs: python3.11+, ~2 GB disk for the sentence-transformers download
# (first run only), and Node 20+ on PATH for the server-memory row (the
# row degrades to unavailable without it — honestly, not fatally).
set -euo pipefail
cd "$(dirname "$0")/../.."

echo "==> creating throwaway .eval-venv"
python3 -m venv .eval-venv
.eval-venv/bin/pip install --quiet --upgrade pip
.eval-venv/bin/pip install --quiet -e .
.eval-venv/bin/pip install --quiet 'mem0ai==2.0.*' qdrant-client sentence-transformers pytest

if command -v npx >/dev/null; then
  echo "==> pre-warming the npx cache for @modelcontextprotocol/server-memory"
  npx -y @modelcontextprotocol/server-memory </dev/null >/dev/null 2>&1 || true
else
  echo "==> npx not on PATH — the server-memory row will read unavailable"
fi

echo "==> live comparative run (text)"
BM_EVAL_LIVE=1 MEM0_TELEMETRY=False .eval-venv/bin/python -m tests.eval.comparative --live --k 5

mkdir -p docs/eval
artifact="docs/eval/comparative-live-$(date +%F).json"
echo "==> writing ${artifact}"
BM_EVAL_LIVE=1 MEM0_TELEMETRY=False .eval-venv/bin/python -m tests.eval.comparative --live --k 5 --json \
  > "${artifact}"

echo "==> gated integration tests"
BM_EVAL_LIVE=1 MEM0_TELEMETRY=False .eval-venv/bin/pytest tests/eval/test_live_adapters.py -q

echo "==> cleaning up .eval-venv"
rm -rf .eval-venv
echo "==> done: ${artifact} (write docs/eval-results.md against it; never re-run in CI)"
