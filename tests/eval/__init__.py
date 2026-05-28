"""Comparative-evaluation harness for bettermemory.

This package is the home `docs/eval.md` designates for the comparative
publication work: a runnable, honest measurement of bettermemory's
retrieval + silent-miss lanes alongside a *capability matrix* that records
which competing memory systems can even compute the published trio
(``memory_helped_rate`` / ``endorsement_rate`` / ``silent_miss_rate``).

Three deliberate honesty constraints shape the design:

1. The retrieval and silent-miss lanes run the *real* ``search`` and
   ``probe_for_miss`` code over a fixed synthetic workload, then feed the
   genuinely-derived audit events through the published ``compute_eval``
   engine. No metric is hand-rolled for the harness.

2. ``memory_helped_rate`` and ``endorsement_rate`` are reported as
   ``n/a`` offline rather than as ``0.0``. They require a live agent
   deciding to *cite* a memory (``record_use`` with claim excerpts);
   fabricating those events from the gold labels would just relabel
   recall and is the circular "no-shit" implementation we refuse to ship.
   Feeding ``compute_eval`` only the audit events makes the two rates fall
   out as ``None`` (zero denominator) — honest by construction.

3. Competitor adapters do not invent numbers. In an environment without
   the competing package installed (or its API key) they raise
   ``SystemUnavailable``; their capability-matrix row is filled from
   public documentation, not from a run. The structural finding — only
   bettermemory logs all three signals the trio needs — stands without
   any competitor having to execute.
"""

import sys
from pathlib import Path

# Make `src/` importable for standalone runs (e.g.
# `python -m tests.eval.comparative`). runpy imports this package before the
# `comparative` submodule, so this lands before any `bettermemory` import.
# pytest gets the same path from conftest.py; this mirrors that
# belt-and-suspenders fix so the CLI works even when the editable install's
# .pth isn't honored — see tests/conftest.py for the iCloud UF_HIDDEN rationale.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
