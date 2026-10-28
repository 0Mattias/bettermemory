"""Shared helpers for the CLI subcommand modules.

Folds the duplicated ``load_config()`` plus store-open pattern that
every ``_cli_*`` handler used to repeat (audit finding M8). One call to
:func:`cli_context` returns the resolved ``Config``, the storage
directory ``Path``, and the store opened there, the three
fixtures every subcommand needs.

Kept minimal: no logging configuration, no I/O beyond the config read.
The serve / ui subcommands need extra setup (``logging.basicConfig``,
extras imports) that doesn't belong here.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from ..config import Config, load_config
from ..store import STORE_FILENAME, Store

if TYPE_CHECKING:
    from ..events import Recorder
    from ..membership_migrate import MigrationReport

log = logging.getLogger("bettermemory.membership")


# ---------------------------------------------------------------------------
# The read-only commands' store
# ---------------------------------------------------------------------------


def _refuse(parser: argparse.ArgumentParser, message: str, exit_code: int) -> NoReturn:
    """End the command with `message` on stderr and `exit_code`: a usage
    error (`parser.error`, which prints the usage line and exits 2) when
    the code is 2, else the message alone."""
    if exit_code == 2:
        parser.error(message)
    sys.stderr.write(f"{parser.prog}: error: {message}\n")
    raise SystemExit(exit_code)


def read_only_store_path(
    parser: argparse.ArgumentParser, *, exit_code: int = 2
) -> Path:
    """The store file a read-only command reads, resolved from the config
    the daemon loads, without its first-run write.

    `load_config` creates a default config.toml where none exists, which a
    command documented as read-only must not do; ``create=False`` reads a
    missing file as the defaults and still applies the organisation-managed
    file's pins, so the store named is the one the daemon serves. A config
    that does not load ends the command with the loader's message, which
    names the file, and `exit_code` (2 by default, the code these commands
    give for "no store"), not with a traceback whose exit 1 each of them
    documents as something else."""
    try:
        config = load_config(create=False)
    except (ValueError, OSError) as exc:
        _refuse(parser, f"the config does not load: {exc}", exit_code)
    return config.resolved_directory() / STORE_FILENAME


def open_read_only(
    parser: argparse.ArgumentParser, path: Path, *, exit_code: int = 2
) -> Store:
    """The store at `path`, opened for reading: ``allow_rekey=False``, so
    a missing key is reported, never replaced, and no rekey row is
    written. No file at `path` is a usage error; a file that is not a
    store this build reads (not a database, no meta table, a store id or
    schema it refuses) ends the command with the reason and `exit_code`
    rather than a traceback."""
    if not path.is_file():
        parser.error(f"no bettermemory 9 store at {path}")
    try:
        return Store.open(path, allow_rekey=False)
    except (ValueError, sqlite3.DatabaseError) as exc:
        _refuse(parser, f"cannot read the store at {path}: {exc}", exit_code)


@dataclass(frozen=True)
class CliContext:
    """Resolved fixtures for a CLI subcommand invocation.

    The three fields cover the duplicated setup every ``_cli_*`` handler
    used to repeat by hand. ``frozen=True`` because the context is a
    snapshot — handlers that mutate state do so on ``store``, never on
    the context wrapper itself.
    """

    config: Config
    directory: Path
    store: Store


def cli_context(*, migrate: bool = True) -> CliContext:
    """Resolve the active config, storage directory, and Store.

    Replaces the ``config = load_config(); directory =
    config.resolved_directory(); store = Store(directory)`` triple that
    appeared in every CLI handler. Callers that only need a subset still
    pay the full triple — ``load_config`` re-reads the TOML on every
    call (it holds no cache), which is one small file parse per CLI
    invocation, nowhere near a hot path.

    With `migrate` (the default) the migration runs at open when a record
    has no home (`migrate_at_open`). `bettermemory membership import`
    passes False and runs it after its own homing, so the records its
    document names are homed by the document first.
    """
    config = load_config()
    directory = config.resolved_directory()
    # Entry point for every CLI command routed through here: provision
    # and run the startup checks. `bettermemory doctor` builds its own
    # Stores with the pure constructor instead, so inspecting a store
    # cannot rebuild the index it was asked to inspect.
    store = Store.open_or_create(directory / STORE_FILENAME)
    if migrate:
        try:
            migrate_at_open(store, config)
        except BaseException:
            store.close()
            raise
    return CliContext(config=config, directory=directory, store=store)


def migrate_at_open(store: Store, config: Config) -> None:
    """The migration's run at open (design 8.6): when a record has no
    home (the first open of an upgraded store, or a re-upgrade after a
    rollback, design 8.12), home every record before the command runs,
    and say so on stderr in one line. `bettermemory membership migrate`
    opens its store itself and never comes here, so its dry run is not
    preceded by a run. Without a machine id the command runs on the store
    as it is, after one warning (`run_migration_at_open`)."""
    report = run_migration_at_open(store, config, level=logging.WARNING)
    if report is None:
        return
    sys.stderr.write(
        f"bettermemory: membership migrated: {report.homed} records homed, "
        f"{len(report.proposals)} legacy questions opened\n"
    )


def run_migration_at_open(
    store: Store, config: Config, *, level: int = logging.INFO
) -> MigrationReport | None:
    """`membership_migrate.ensure_migrated` at an entry point's open
    (design 8.6): the report of a run that homed records, else None.

    A store opened for reading (an open without ``allow_rekey`` found no
    key) is left as it is; the next open with its key migrates it. When this process
    has no machine id (`location.process_machine` raises OSError: the state
    directory could not be opened), one line is logged at `level`, the
    store is left as it is, and the next open tries again. The hook
    commands pass INFO, the default: Claude Code shows a hook's stderr to
    the user, and no handler prints an INFO record in a hook process."""
    from .. import location
    from ..membership_migrate import ensure_migrated, settle_at_open, unhomed

    if not store.has_key:
        return None
    try:
        identity = location.process_machine()
    except OSError as exc:
        log.log(
            level,
            "membership migration not run for %s: no machine id (%s); the "
            "next open tries again",
            store.path,
            exc,
        )
        return None
    # The settle pass first (design 8.6, rc6): a legacy question an earlier
    # candidate opened on evidence that decides nothing closes as separate.
    settle_at_open(store, identity)
    if not unhomed(store):
        return None
    return ensure_migrated(store, identity, config=config)


def migration_line(report: MigrationReport) -> str:
    """The one log line of a run at open that homed records: its totals."""
    t = report.totals
    return (
        f"membership migrated: {report.homed} records homed ({t.memory_homes} "
        f"memories, {t.tombstone_homes} removed memories, {t.episode_homes} "
        f"episodes), {t.projects} projects, {t.checkouts} checkouts, "
        f"{t.proposals} legacy questions, {t.log_rows} log rows"
    )


def cli_recorder(
    ctx: CliContext,
    *,
    attribution: str,
    session_id: str | None = None,
    triggered_from: str = "",
) -> Recorder:
    """An event recorder for a CLI command that mutates the store.

    Mirrors the server's recorder construction (`builder.py`) so CLI
    writes land in the same audit log under the same telemetry posture:
    `[telemetry] enabled = false` turns the log off here too, and the
    rotation cap and verbatim-query redaction follow the same config.
    Every CLI path that creates or rewrites a memory records through
    this, because the provenance derivation at `index.rebuild` joins on
    those events: a mutation that records nothing reads as unaccounted
    on the next rebuild.

    `attribution` is required and must carry the admin prefix
    `eval.is_admin_recorded_event` reads: a CLI run records under a
    throwaway session id, often under kinds a live client session also
    emits, and the attribution is what keeps it out of the client
    session census and the tool-usage tallies. The prefix is not
    checked here on purpose (that classification has exactly one
    definition, in `eval`, and the parity scan there refuses a second
    copy anywhere in `src/`); `tests/test_cli_smoke.py` scans every
    call site against the real constant instead. `session_id` lets a
    command reuse the id it already stamped on tombstones (consolidate);
    the default mints a fresh one. `triggered_from` marks an
    out-of-process writer on every event (`AttributedRecorder` says why);
    only `bettermemory capture` sets it.
    """
    from ..events import Recorder
    from ..session import SessionState

    return Recorder(
        store=ctx.store,
        session_id=session_id or SessionState().session_id,
        enabled=ctx.config.telemetry.enabled,
        attribution=attribution,
        triggered_from=triggered_from,
    )


def parse_iso_cutoff(
    value: str,
    *,
    flag: str,
    far_future: str = "refuse",
) -> tuple[datetime, str]:
    """Parse an operator-supplied ISO timestamp, or exit 1 explaining why not.

    Shared by `consolidate --acknowledge-misses-before` and `rollback
    --since`. It lives here rather than inline in either caller because
    the careful part is the edge cases, and a second hand-rolled copy is
    how the two spellings drift apart: both accept `Z` and explicit
    offsets, both reject naive input, and both hint the midnight-UTC
    spelling for a bare date. Returns the parsed aware datetime plus its
    canonical UTC-`Z` rendering, so callers that persist the value all
    persist one representation.

    `flag` prefixes every message, so the error names the flag the
    operator actually typed.

    `far_future` is the one axis where the two callers genuinely
    differ, so it is a parameter rather than a hardcoded rule:

    * ``"refuse"`` — a far-future cutoff on `--acknowledge-misses-before`
      writes a marker that hides every audited event indefinitely, and
      the rollup then reads "clean" forever. A typo'd century is
      unrecoverable-looking, so refuse it up front.
    * ``"warn"`` — a far-future `--since` on a rollback selects NOTHING.
      The failure is visible in the very next line of output (a zero
      count) and nothing is destroyed, so a refusal would be noise
      where a warning is enough. The dangerous direction on a rollback
      is the ABSENT window, which the `--yes` gate covers instead.
    """
    from datetime import timedelta

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # Bare-date convenience hint: a tired oncall who types
        # `2026-05-25` (legitimate intent: midnight UTC of that day)
        # would otherwise just see "invalid ISO timestamp" and have to
        # guess the format. fromisoformat() *does* accept bare dates
        # since 3.11, so this branch only fires for genuinely malformed
        # input — but pointing out the midnight-UTC spelling is the
        # cheap-help.
        import re as _re

        bare_date_hint = ""
        if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            bare_date_hint = f" (or '{value}T00:00:00Z' if you meant midnight UTC)"
        sys.stderr.write(
            f"{flag}: invalid ISO timestamp "
            f"{value!r}. Expected e.g. '2026-05-25T05:25:35Z'"
            f"{bare_date_hint}.\n"
        )
        raise SystemExit(1) from None

    # Reject naive timestamps. A bare `2026-05-25T10:00:00` from a
    # non-UTC user produces a cutoff several hours off-by-zone with no
    # warning — the comparison is against aware datetimes, so the
    # discrepancy would only show up later as a confusing result set.
    # Forcing the user to spell out the offset (or write `Z`) makes the
    # assumption part of the input.
    if parsed.tzinfo is None:
        # If the user typed a bare date, the parse SUCCEEDED (3.11+
        # fromisoformat accepts it) but produced a naive midnight — so
        # the hint here is the same as the parse-error branch above:
        # spell out the offset.
        sys.stderr.write(
            f"{flag}: ISO timestamp {value!r} "
            f"is missing a UTC offset. Pass an explicit offset or "
            f"trailing `Z` (e.g. '{value}T00:00:00Z' for midnight "
            f"UTC, or '2026-05-25T01:25:35-04:00' for an explicit "
            f"offset) so the cutoff isn't silently interpreted as "
            f"your local zone.\n"
        )
        raise SystemExit(1)

    now_utc = datetime.now(timezone.utc)
    far_future_grace = timedelta(hours=24)
    if parsed > now_utc + far_future_grace:
        if far_future == "refuse":
            sys.stderr.write(
                f"{flag}: cutoff {value!r} is more "
                f"than 24 hours in the future (now is "
                f"{now_utc.isoformat().replace('+00:00', 'Z')}). This is "
                f"almost always a typo — a year-2126 cutoff would silently "
                f"hide every audited event in the log indefinitely. Pass a "
                f"timestamp at or before "
                f"{(now_utc + far_future_grace).isoformat().replace('+00:00', 'Z')}.\n"
            )
            raise SystemExit(1)
        sys.stderr.write(
            f"{flag}: warning — {value!r} is more than 24 hours in the "
            f"future (now is {now_utc.isoformat().replace('+00:00', 'Z')}), "
            f"so nothing was written after it and the selection below "
            f"will be empty. This is almost always a typo'd year.\n"
        )

    # Normalise to UTC-Z so every persisted cutoff uses one
    # representation, regardless of which offset the caller passed.
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return parsed, canonical
