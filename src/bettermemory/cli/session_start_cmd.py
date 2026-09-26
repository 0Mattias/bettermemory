"""`bettermemory session-start` — Claude Code SessionStart-hook context block.

Prints a three-line "what is stored for this repository" hint on stdout.
Claude Code injects a SessionStart hook's stdout verbatim into the
model's context, so the model opens every session already knowing the
per-scope counts it would otherwise have had to ask the health report
for — or, far more often, never learned at all because
retrieval is opt-in and, between prompts the recall hook's high bar
doesn't fire on (the ~98% common case), nothing prompted it.

THE NEGATIVE MANDATE — this command constructs no `Recorder` and calls
no `.record()`, ever. Two independent failures follow from breaking it:

1. Anchor hijack. `hook._latest_in_process_session` picks the most
   recent session in the event log whose `triggered_from` is not in
   `hook._OUT_OF_PROCESS_TRIGGERS` — it never reads `attribution`. A
   session-start row
   would therefore become the anchor the Stop hook's turn audit
   attributes against, no matter how the row is stamped (this command
   has no roster entry to hide behind — the recall hook records, but
   it records AS a roster member). The familiar
   "just mark it `cli_*` like the admin CLI commands do" workaround
   fixes doctor's census and does NOT fix this.
2. Phantom sessions. Every session open would publish a fresh session id
   into the log with no turn that could ever produce `turn_audited`,
   which is the exact denominator corruption `ADMIN_RECORDED_EVENT_KINDS`
   was introduced to stop.

`tests/test_cli_smoke.py::test_session_start_records_nothing` is the
standing guard: it asserts a byte-identical event log across a run.
Without that test this docstring is only a wish.

The read is deliberately cheap, and every step of it is a gate rather
than a fallback:

* `load_config().resolved_directory()` instead of `cli_context()` —
  `cli_context()` opens the store through `Store.open()`, which
  provisions (mkdir + chmod) and can kick off a full index rebuild. None
  of that belongs in the critical path of opening a session. Since 7.17.0
  a bare `Store(...)` would be inert enough to be safe here, but resolving
  the directory directly stays the narrower dependency.
* Counts come from the FTS index's columnar scan
  (`index.scope_counts`), never from `Store.load_all()`. `load_all` is
  ~74 % of the equivalent handler's cost, all of it per-file opens and
  YAML parsing for bodies this surface does not print.
* Anything unusable — no store, empty store, absent/corrupt index, an
  index whose row count disagrees with what is on disk, or one whose
  rows name a different SET of files than the listing does (equal
  counts are the weaker claim: swap one memory for another out of band
  and both sides still read N) — degrades to EMPTY stdout, never to the
  expensive path. A session-start hook that stalls the session is worse
  than one that says nothing.

Diagnostics go to stderr (Claude Code routes it to the debug log, not
the model's context) and the process always exits 0. `hooks.json` still
appends `|| true` as belt-and-suspenders, because a `uvx` network
failure or an older published wheel without this subcommand would
otherwise surface as a hook-error banner.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Type-only so the runtime import cost stays where it belongs: inside
    # `run`. `cli/__init__.py` imports this module for every `bettermemory
    # <anything>` invocation, so a top-level `from ..origin import Origin`
    # would tax every other subcommand for a symbol only this one uses.

    pass

# How many scopes the context block names before collapsing the tail into
# "+N more". The block is injected into EVERY session, so its cost is
# paid per session forever; five is enough to characterise a store's
# shape and short enough that a 40-scope store cannot flood the opening
# context. The total is always exact — only the enumeration is capped.
_MAX_SCOPES_SHOWN = 5


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``session-start`` subparser on the parent parser."""
    help_text = (
        "Print the session-start memory hint on stdout. Intended as a "
        "Claude Code SessionStart hook target: emits the per-scope "
        "counts for the current repository so a fresh session starts "
        "with them in context instead of spending a tool call to get "
        "them (or never learning them at all). Reads the store's scope "
        "counts, never the memory bodies. Records NOTHING — no event, no "
        "session. Prints nothing when the store is empty or cannot be "
        "opened, and always exits 0 so a hook misfire never breaks "
        "session start."
    )
    parser = sub.add_parser("session-start", help=help_text, description=help_text)
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory session-start``.

    Always exits 0. The broad `except Exception` is the point: this runs
    on the session-open path of every conversation, so ANY unanticipated
    failure — an unreadable config, a permissions change under the store,
    a sqlite build without FTS5 — must degrade to a quiet stderr note
    rather than a hook-error banner in the user's face.

    TWO guarded arms, not one, because the WRITE can fail on its own and
    an unguarded one made the paragraph above false: under
    `PYTHONIOENCODING=ascii` the block's em dash raised
    `UnicodeEncodeError` out of `print` and the command exited 1 with a
    traceback. A closed pipe and a full disk are the same shape. The arms
    stay separate so each degrade says which half broke — "the read found
    nothing usable" and "the answer could not be delivered" are different
    problems for whoever reads the debug log.
    """
    import sys

    # `args` is unused: the subcommand takes no flags. It stays in the
    # signature because `cli/__init__.py`'s dispatch calls every `run`
    # the same way.
    try:
        block = _build_context_block()
    except Exception as exc:  # noqa: BLE001
        _note_degraded(f"skipped: {exc.__class__.__name__}: {exc}")
        raise SystemExit(0) from None

    if block:
        try:
            # Stdout is the context block and NOTHING else — Claude Code
            # feeds it to the model verbatim, so a stray progress line
            # would be indistinguishable from content the model should
            # act on.
            print(block)
            # Flushed HERE, while the guard is still up. A `print` alone
            # only fills the buffer, so an OS-level write failure (closed
            # pipe, full disk) would surface in the interpreter's
            # shutdown flush instead — past every handler, where CPython
            # prints "Exception ignored on flushing sys.stdout" and exits
            # **120** whatever `SystemExit` said. This line is what makes
            # such a failure catchable at all; `_blackhole_stdout` is
            # what then stops the shutdown retry from exiting 120 anyway.
            sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001
            _note_degraded(
                f"could not write the context block: {exc.__class__.__name__}: {exc}"
            )
            _blackhole_stdout()
            raise SystemExit(0) from None
    raise SystemExit(0)


def _note_degraded(detail: str) -> None:
    """Report a degrade on stderr — and swallow any failure to do so.

    The note is a courtesy to the debug log; the exit code is the
    contract. Whatever broke stdout has usually broken stderr too (one
    `PYTHONIOENCODING`, one full disk, one closed terminal), and a
    diagnostic that raised on its way out would turn a clean degrade
    back into the hook-error banner this whole module exists to avoid.
    """
    import sys

    try:
        print(f"[bettermemory] session-start {detail}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _blackhole_stdout() -> None:
    """Point stdout's descriptor at /dev/null after a failed write.

    A flush that fails leaves the data in the buffer, so CPython's
    shutdown flush retries it, fails again, and exits 120 — overruling
    the caller's `SystemExit(0)`. Catching the flush error is therefore
    not enough on its own; redirecting the underlying descriptor (the
    recipe from the `signal` docs' SIGPIPE note) is what makes the retry
    a silent no-op. Measured down a closed pipe: catch-only still exits
    120, catch-plus-redirect exits 0.

    Every failure here is swallowed and none is reported: `sys.stdout`
    may have no real descriptor at all (a test harness's capture object,
    an embedding host), and a salvage that raised would recreate exactly
    the failure it exists to prevent. Reached only on the write-failure
    path, one statement before the process exits, so redirecting a
    descriptor out from under the rest of the program is not a hazard
    here the way it would be in a library.
    """
    import os
    import sys

    try:
        fd = sys.stdout.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
    except Exception:  # noqa: BLE001
        return
    try:
        os.dup2(devnull, fd)
    except OSError:
        pass
    finally:
        os.close(devnull)


def _build_context_block() -> str | None:
    """Return the block to print, or None to stay silent.

    Split from `run` so the gates are testable without capturing stdout,
    and so `run` holds nothing but the never-raise / always-exit-0
    contract. The body is `hook.session_start_block`, shared with the
    daemon's session-start endpoint; this in-process shape opens the
    store read-only and prints the note on stderr.
    """
    import sys

    from ..config import load_config
    from ..hook import session_start_block
    from ..origin import capture as capture_origin
    from ..store import STORE_FILENAME, Store

    config = load_config()
    directory = config.resolved_directory()
    store_path = directory / STORE_FILENAME
    if not store_path.is_file():
        return None

    current = capture_origin()
    with Store.open(store_path, allow_rekey=False) as store:
        block, note = session_start_block(store, origin=current)
    if note:
        print(note, file=sys.stderr)
    return block


def _render_block(total: int, scopes: dict[str, int]) -> str:
    """The context block the model sees; `hook.render_session_start_block`
    is the one renderer, kept here under its 8.x name for the tests that
    hold the shape."""
    from ..hook import render_session_start_block

    return render_session_start_block(total, scopes)
