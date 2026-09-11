"""Shared helpers for the CLI subcommand modules.

Folds the duplicated ``load_config() + Store(directory)`` pattern that
every ``_cli_*`` handler used to repeat (audit finding M8). One call to
:func:`cli_context` returns the resolved ``Config``, the storage
directory ``Path``, and a ``Store`` rooted at that directory — the three
fixtures every subcommand needs.

Kept minimal: no logging configuration, no I/O beyond the config read.
The serve / ui subcommands need extra setup (``logging.basicConfig``,
extras imports) that doesn't belong here.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Config, load_config
from ..store import Store

if TYPE_CHECKING:
    from ..events import AttributedRecorder


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


def cli_context() -> CliContext:
    """Resolve the active config, storage directory, and Store.

    Replaces the ``config = load_config(); directory =
    config.resolved_directory(); store = Store(directory)`` triple that
    appeared in every CLI handler. Callers that only need a subset still
    pay the full triple — ``load_config`` re-reads the TOML on every
    call (it holds no cache), which is one small file parse per CLI
    invocation, nowhere near a hot path.
    """
    config = load_config()
    directory = config.resolved_directory()
    store = Store(directory)
    return CliContext(config=config, directory=directory, store=store)


def cli_recorder(
    ctx: CliContext,
    *,
    attribution: str,
    session_id: str | None = None,
) -> AttributedRecorder:
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
    the default mints a fresh one.
    """
    from ..events import AttributedRecorder
    from ..session import SessionState

    return AttributedRecorder(
        root=ctx.directory,
        session_id=session_id or SessionState().session_id,
        enabled=ctx.config.telemetry.enabled,
        max_bytes=ctx.config.telemetry.max_bytes,
        log_queries_verbatim=ctx.config.telemetry.log_queries_verbatim,
        attribution=attribution,
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
