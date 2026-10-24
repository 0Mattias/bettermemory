"""`bettermemory session-start` — the session brief, in process.

Prints the session brief (`recall.session_brief`) on stdout: the project's
standing records in force and the records the conversation so far bears
on, eight pointers of each at most, under a header that says they are
pointers, not instructions. Claude Code injects a SessionStart hook's stdout
verbatim into the model's context. The installed hook runs `bettermemory
hook session-start`, which the process entry point routes to the daemon
before this package is imported; this subcommand is the same body run in
process, for a caller with no daemon.

It reads the SessionStart payload from stdin when one is piped
(`session_id`, `source`, `cwd`, `transcript_path`); `--session-id` and
`--source` stand in for it by hand. Without a session id it prints
nothing: the brief is recorded at show time as a `session_brief` event
under the session it was shown to, and an unrecorded injection is the
one thing the recall surface never makes.

The recording rules the old counts block lived by still hold, through
the event's stamp rather than through silence. The row is stamped
``triggered_from="session_hook"``, a member of
`hook._OUT_OF_PROCESS_TRIGGERS`, so the turn audit's server-session
anchor (`hook._latest_in_process_session`) skips it and cannot be
hijacked by it; and it is recorded under the harness's own session id,
the one that session's `turn_audited` rows carry, so it publishes no
phantom session. The recorder lives in `recall.py`; this module names
none (`tests/test_cli_smoke.py` reads its source to hold that).

Every gate degrades to EMPTY stdout, never to a traceback: no store, an
unreadable store, an empty one, a caller outside any repository, a
selection with nothing fresh. Diagnostics go to stderr (Claude Code
routes it to the debug log, not the model's context) and the process
always exits 0. `hooks.json` still appends `|| true` as
belt-and-suspenders, because a `uvx` network failure or an older
published wheel without this subcommand would otherwise surface as a
hook-error banner.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config import Config
    from ..store import Store

# The stdin payload's bound, the hooks' (`hook._STDIN_PAYLOAD_CAP_BYTES`).
_STDIN_CAP_BYTES = 64 * 1024


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``session-start`` subparser on the parent parser."""
    help_text = (
        "Print the session brief on stdout: the project's standing records "
        "in force and the records the conversation so far bears on, eight "
        "pointers of each at most. Intended as a Claude Code SessionStart "
        "hook target; reads the hook's stdin JSON (`session_id`, `source`, "
        "`cwd`, `transcript_path`). Records the show as a `session_brief` "
        "event under that session. Prints nothing without a session id, "
        "outside a project, or when nothing is admitted, and always exits 0 "
        "so a hook misfire never breaks session start."
    )
    parser = sub.add_parser("session-start", help=help_text, description=help_text)
    parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Override the session id from the SessionStart payload.",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help="Override the SessionStart source (startup, resume, compact, clear).",
    )
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

    try:
        block = _build_context_block(
            session_id=getattr(args, "session_id", None),
            source=getattr(args, "source", None),
        )
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


def _read_payload() -> dict[str, Any]:
    """The SessionStart payload piped on stdin, or ``{}``: nothing piped
    (a terminal), an unreadable or oversized stream, or not a JSON
    object."""
    import json
    import sys

    try:
        stream = sys.stdin
        if stream is None or stream.isatty():
            return {}
        raw = stream.buffer.read(_STDIN_CAP_BYTES + 1)
    except Exception:  # noqa: BLE001 - a captured or closed stdin reads as none
        return {}
    if len(raw) > _STDIN_CAP_BYTES:
        return {}
    try:
        loaded = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _build_context_block(
    *, session_id: str | None = None, source: str | None = None
) -> str | None:
    """Return the brief to print, or None to stay silent.

    Split from `run` so the gates are testable without capturing stdout,
    and so `run` holds nothing but the never-raise / always-exit-0
    contract. The body is `recall.session_brief`, shared with the
    daemon's session-start endpoint; this in-process shape opens the
    store without a rekey, holds no state between calls (the session's
    segment is read back from the log) and prints its note on stderr.
    """
    import sys
    from pathlib import Path

    from ..config import load_config
    from ..recall import session_brief
    from ..store import STORE_FILENAME, Store

    payload = _read_payload()
    config = load_config()
    directory = config.resolved_directory()
    store_path = directory / STORE_FILENAME
    if not store_path.is_file():
        return None

    def text(name: str) -> str | None:
        value = payload.get(name)
        return value if isinstance(value, str) and value else None

    cwd = text("cwd")
    with Store.open(store_path, allow_rekey=False) as store:
        _migrate(store, config)
        result = session_brief(
            session_id=session_id or text("session_id"),
            source=source or text("source"),
            harness=text("harness"),
            transcript_path=text("transcript_path"),
            cwd=Path(cwd) if cwd else None,
            config=config,
            store=store,
        )
    if result.stdout:
        print(
            f"[bettermemory] session-start: brief of {len(result.ids)} "
            f"pointer(s) from {store_path.parent}.",
            file=sys.stderr,
        )
    return result.stdout or None


def _migrate(store: Store, config: Config) -> None:
    """The migration's run at this open (design 8.6), for the path with no
    daemon: logged at INFO, never written to stderr, which Claude Code
    shows the user. A store opened without its key is left as it is
    (`_common.run_migration_at_open`)."""
    import logging

    from ._common import migration_line, run_migration_at_open

    report = run_migration_at_open(store, config)
    if report is not None:
        logging.getLogger("bettermemory.membership").info("%s", migration_line(report))
