"""`bettermemory export` — dump active (and optionally tombstoned) memories
to a self-describing JSON document.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..models import utcnow, validate_scope
from .._fsutil import atomic_write_bytes
from .._response import isoformat
from ..store import Store, count_active_memory_files


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``export`` subparser on the parent parser."""
    help_text = (
        "Dump all active memories (and tombstones, by default) to a "
        "self-describing JSON document. The format is round-trippable "
        "and intended for backup, migration between machines, or "
        "feeding an external indexer. Writes to stdout unless "
        "--output is given."
    )
    parser = sub.add_parser("export", help=help_text, description=help_text)
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Write the export to PATH instead of stdout. Use this for "
            "scripted backups (`bettermemory export -o backup.json`)."
        ),
    )
    parser.add_argument(
        "--no-tombstones",
        action="store_true",
        help=(
            "Skip tombstoned memories. By default the export includes "
            "them so a restored archive carries the same removal-reason "
            "audit trail; use this when you only want the live set."
        ),
    )
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="SCOPE",
        help=(
            "Filter to memories tagged with at least one of the given "
            "scopes. Repeat to widen the filter. Applies to both active "
            "and tombstoned records."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Exit non-zero when the loader skipped any memory or tombstone "
            "file on disk, leaving it out of the export. The document is "
            "still written; only the exit status changes. Use this in a "
            "backup cron so a short archive fails the job instead of "
            "passing silently. Under --no-tombstones the tombstone half is "
            "never read, so --strict cannot fire on it — the export records "
            "null there rather than a zero nobody checked. Every `.md` in "
            "the store root the loader skips counts, including one you put "
            "there yourself (a README, say) — the store makes no exception "
            "for those, so neither can this. `bettermemory doctor` re-reads "
            "the store and names the skipped ACTIVE files, reporting them as "
            "a warning where --strict escalates them to an exit code; no "
            "check anywhere COUNTS a skipped tombstone, so a dropped "
            "tombstone is reported here or not at all."
        ),
    )
    return parser


def run(
    args: argparse.Namespace,
    *,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory export``.

    ``sub_parser`` is forwarded into ``_cli_export`` so a malformed
    ``--scope`` surfaces through ``parser.error(...)`` (a clean
    ``bettermemory export: error: …`` + exit 2) instead of an uncaught
    ``ValueError`` traceback — mirroring how ``eval`` / ``episodes``
    thread their subparser through.
    """
    _cli_export(
        output=args.output,
        include_tombstones=not args.no_tombstones,
        scopes=args.scope or None,
        strict=args.strict,
        parser=sub_parser,
    )


def _count_tombstone_files(store: Store) -> int:
    """Count the tombstone ``.md`` files `load_tombstones` would try to read,
    without parsing them.

    The store's own iterator is counted directly, private though it is,
    because the number is only meaningful as the twin of the walk it is
    subtracted from: restating the filter here (regular file, not a symlink,
    `.md` suffix) would reproduce today's rule and diverge from it silently
    the day the store's rule changes. The active half avoids that with a
    shared helper (`count_active_memory_files`); the tombstone half has no
    such twin and this module does not own `store.py`, so counting
    `_iter_tombstone_paths` is how "counted here" and "skipped there" stay
    one definition. `_handlers.py` (`store._load_path`) and
    `handlers/episode_promote.py` (`episode_store._session_dir`) reach across
    the same boundary for the same reason.

    Returns 0 when the directory is absent (a store that has never
    tombstoned anything), matching the iterator's early return. An
    unlistable directory propagates OSError, like the active counter.
    """
    return sum(1 for _ in store._iter_tombstone_paths())


def _cli_export(
    *,
    output: str | None,
    include_tombstones: bool,
    scopes: list[str] | None,
    strict: bool = False,
    parser: argparse.ArgumentParser | None = None,
) -> None:
    """`bettermemory export` — dump active (and optionally tombstoned)
    memories to a self-describing JSON document.

    Format (`format_version: 1`):

        {
          "format_version": 1,
          "exported_at": "2026-05-09T12:34:56Z",
          "source_directory": "/Users/me/.claude-memory",
          "active_memories":     [<full Memory dict>, ...],
          "tombstoned_memories": [<full TombstonedMemory dict>, ...],
          "skipped_active_files": 0,
          "skipped_tombstone_files": 0
        }

    `tombstoned_memories` is omitted entirely when --no-tombstones is
    passed (vs. emitted as []) so a consumer can distinguish "not
    requested" from "no tombstones present". Each memory dict mirrors
    the Pydantic model — id, created, updated, scopes, confidence,
    source, body, origin, last_verified_at — and tombstones add
    removed / removed_reason / removed_session.

    The two `skipped_*_files` counts are ALWAYS present, including on a
    clean store where both are 0. Each is a two-walk delta — files on
    disk, minus the records the reader handed back — which is the same
    quantity doctor's memory_parse_health reports as `details["skipped"]`,
    and it is named after that walk rather than after a diagnosis for the
    same reason doctor is: the delta says the loader skipped the file, not
    why. Malformed frontmatter and a `schema_version` newer than this
    install (a `sync pull` from a machine on a newer bettermemory) are
    indistinguishable from a count, so neither these keys nor the warning
    claim the file "did not parse". `store.count_unparseable_memory_files`
    is where that stronger claim lives; it parses every file to earn it,
    and an export deliberately does not pay for a second parse of a store
    it has just read.

    The store's readers (`load_all`, `load_tombstones`) swallow every
    per-file failure, so without these counts a short export is
    indistinguishable from a small store — the worst failure mode a
    backup has: invisible at capture time, discovered when the source is
    gone. A key that appears only on failure is a key no consumer
    bothers to read, so both are unconditional and a consumer can assert
    `== 0` rather than remember to probe for absence.
    `skipped_tombstone_files` is `null`, not 0, when --no-tombstones was
    passed: that half was never examined, and reporting 0 there would
    assert an absence nobody checked.

    Round-trippability is therefore CONDITIONAL, not absolute: a future
    `bettermemory import` can recreate active records and tombstones
    from this document with no loss **only when both skipped counts are
    0**. When either is non-zero the document is a partial capture of
    the store — the skipped files are not represented anywhere in it,
    not even as placeholders, and `tombstoned_memories: []` under a
    non-zero `skipped_tombstone_files` means "none survived the read",
    not "none exist". `bettermemory doctor`'s memory_parse_health
    re-reads the store and names the offending ACTIVE files; nothing
    anywhere COUNTS a skipped tombstone, so a non-zero
    `skipped_tombstone_files` is a number this command is alone in
    reporting and the reader inspects `.tombstones/` by hand. The
    directory itself is not unread — doctor's `auto_memory_stranded`
    check calls `store.load_tombstones()` for ingest dedup (when a
    Claude Code auto-memory dir exists for the cwd) — but that reader
    swallows every per-file failure and returns only what parsed, so a
    file it dropped leaves no trace on that surface: it silently widens
    the set of sources that check calls stranded rather than reporting
    itself.
    `--strict` turns any non-zero count into a non-zero exit for
    scripted backups; the default stays exit 0 with a
    stderr warning so existing cron callers do not start failing on an
    unchanged store. Bump format_version on any breaking change —
    including a rename of these keys, which `format_version: 1` freezes
    as surely as it freezes the record fields.
    """
    import json as _json
    from pathlib import Path as _Path

    # Lazy import keeps the module-import cost of ``bettermemory.cli``
    # off the hot path; ``load_config`` reads TOML and only matters when
    # ``export`` actually runs.
    from ..config import load_config as _load_config

    config = _load_config()
    directory = config.resolved_directory()
    store = Store(directory)

    if scopes:
        # `validate_scope` raises ValueError on a malformed --scope
        # (uppercase, spaces, illegal chars). Route it through
        # `parser.error(...)` for a clean `bettermemory export: error: …`
        # + exit 2 instead of an uncaught traceback. The `parser is None`
        # fallback (direct `_cli_export` callers / tests) keeps the raw
        # ValueError so programmatic callers still see the exception.
        try:
            scopes = [validate_scope(s) for s in scopes]
        except ValueError as exc:
            if parser is not None:
                parser.error(str(exc))
            raise
    scope_set = set(scopes) if scopes else None

    # Count BEFORE the load, not after, and the order is the whole
    # protection. These are two walks of a directory a live server may be
    # writing to; whichever walk runs SECOND sees the newer state, and the
    # delta only accuses the loader when the file walk is the larger
    # number. Counting after the load meant a `memory_write` landing
    # between them was indistinguishable from a file the loader refused —
    # a backup reporting itself short, and under --strict a red cron job,
    # on a store that was never damaged. Counting first inverts that: a
    # memory written in the gap is loaded but never counted, so the delta
    # goes negative and `max(0, …)` reads it as the non-event it is.
    #
    # It does not make the pair atomic. A `memory_remove` in the gap moves
    # a counted file to `.tombstones/` before the load sees it and still
    # over-reports by one — the residual race, and the rarer of the two by
    # a wide margin (writes are a routine reflex; removals are curation).
    # `bettermemory doctor` re-reads and names files rather than
    # subtracting counts, which is why the warning below sends the reader
    # there instead of asserting which file is at fault.
    active_files_before = count_active_memory_files(directory)
    active = store.load_all()
    # Count the drop BEFORE the scope filter. A file the reader could not
    # parse has no scopes to test, so it can only be measured against the
    # unfiltered read — subtracting after the filter would blame the scope
    # filter for every out-of-scope memory.
    skipped_active_files = max(0, active_files_before - len(active))
    if scope_set is not None:
        active = [m for m in active if scope_set.intersection(m.scopes)]

    payload: dict[str, Any] = {
        "format_version": 1,
        "exported_at": isoformat(utcnow()),
        "source_directory": str(directory),
        "active_memories": [m.model_dump(mode="json") for m in active],
    }
    tombstoned_count = 0
    # `None` (JSON null) rather than 0 when the tombstone half was never
    # read — see the docstring: 0 would assert an absence nobody checked.
    skipped_tombstone_files: int | None = None
    if include_tombstones:
        # Same count-then-load ordering as the active half above, for the
        # same reason. The write that lands in this gap is a
        # `memory_remove` (it creates the tombstone), and the removal that
        # lands in it is a `memory_restore`; both are rarer here than a
        # `memory_write` is there, but the ordering costs nothing and
        # keeping the two halves symmetrical is what stops one of them
        # from being reasoned about again from scratch.
        tombstone_files_before = _count_tombstone_files(store)
        tombstoned = store.load_tombstones()
        skipped_tombstone_files = max(0, tombstone_files_before - len(tombstoned))
        if scope_set is not None:
            tombstoned = [t for t in tombstoned if scope_set.intersection(t.scopes)]
        payload["tombstoned_memories"] = [t.model_dump(mode="json") for t in tombstoned]
        tombstoned_count = len(tombstoned)

    payload["skipped_active_files"] = skipped_active_files
    payload["skipped_tombstone_files"] = skipped_tombstone_files

    text = _json.dumps(payload, indent=2)

    # Warn before writing, not after: an export that then fails on a bad
    # --output path has still already told the user their store is short.
    # The document is written either way — a partial backup beats none —
    # so this is a warning, and only --strict escalates it to an exit code.
    dropped = skipped_active_files + (skipped_tombstone_files or 0)
    if dropped:
        parts = []
        if skipped_active_files:
            parts.append(f"{skipped_active_files} active memory file(s)")
        if skipped_tombstone_files:
            parts.append(f"{skipped_tombstone_files} tombstone file(s)")
        # Where to go next, branched on which half dropped, because the
        # two have different answers: doctor re-reads the active files and
        # names them, and no check anywhere names a skipped tombstone. Its
        # `auto_memory_stranded` check does READ `.tombstones/` (via
        # `store.load_tombstones`, for ingest dedup), but that reader
        # swallows per-file failures, so a dropped tombstone shows up there
        # only as one more source file called stranded — never as itself.
        # A flat "run doctor" sent a user whose tombstone dropped to a
        # command that cannot name the file they need.
        if skipped_active_files and skipped_tombstone_files:
            pointer = (
                "Run `bettermemory doctor` to see the active files by name; "
                "no check names a skipped tombstone, so inspect "
                "`.tombstones/` by hand for that half."
            )
        elif skipped_active_files:
            pointer = "Run `bettermemory doctor` to see the files by name."
        else:
            pointer = (
                "No check names a skipped tombstone, so `doctor` will "
                "not report these — inspect `.tombstones/` by hand."
            )
        # Doctor's claim for the same delta: the loader skipped these, and
        # the two causes a count cannot tell apart are both named rather
        # than collapsed into "could not be read". Telling a user their
        # file is corrupt when it merely came from a newer install sends
        # them editing frontmatter that is already correct.
        sys.stderr.write(
            f"WARNING: {' and '.join(parts)} in {directory} were skipped by "
            f"the loader (malformed frontmatter, or a schema_version newer "
            f"than this install) and are NOT in this export. The export is a "
            f"partial capture of the store, not a complete backup. {pointer}\n"
        )

    if output:
        out_path = _Path(output)
        # Pre-check the parent is an existing directory. `atomic_write_bytes`
        # would otherwise silently create the parent tree via
        # `parent.mkdir(parents=True, exist_ok=True)` — that auto-mkdir
        # is intentional for fresh-install callers (init.py creating
        # ~/.claude.json under a missing ~/.config, sync.py creating a
        # .gitignore under a fresh sync root) but wrong here: a user
        # who typed `bettermemory export -o /typod/path/backup.json`
        # wants a loud error, not a silently-created
        # /typod/path/ tree with their backup buried inside. Pre-3.2.1
        # the bare `write_text` raised FileNotFoundError for missing
        # parents; this restores that contract while preserving the
        # atomic-write durability benefit. `out_path.parent` is
        # Path(".") when output is a bare filename, which always
        # exists as a directory — so this only fires on a genuinely
        # bad parent.
        #
        # `is_dir()` (not `exists()`): if the parent path is a regular
        # FILE, `exists()` returns True and the pre-check would pass,
        # but the helper's `mkdir(parents=True, exist_ok=True)` then
        # raises a confusing `FileExistsError` naming the internal
        # `.tmp` path. `is_dir()` catches both the missing-parent and
        # the parent-is-a-file cases here, so the export caller surfaces
        # one clean error pointing at the parent the user actually typed.
        parent = out_path.parent
        if not parent.is_dir():
            msg = (
                f"--output parent directory does not exist or is not a "
                f"directory: {parent}"
            )
            # Route through `parser.error` for a clean
            # `bettermemory export: error: …` + exit 2 instead of a raw
            # traceback / exit 1, mirroring the --scope ValueError arm
            # above and the sibling rename-scope / tombstones-restore
            # commands. `parser is None` (programmatic / test callers)
            # keeps the raw exception so they still see the exception type.
            if parser is not None:
                parser.error(msg)
            raise FileNotFoundError(msg)
        # Atomic + durable write via `_fsutil.atomic_write_bytes`: a plain
        # `out_path.write_text(...)` here would leave a truncated JSON on
        # power loss / process kill mid-write, defeating the point of a
        # backup. The helper writes to a tmp sibling, fsyncs, atomic-
        # renames into place, and fsyncs the parent directory. A genuine
        # filesystem failure here (read-only parent, ENOSPC, EACCES)
        # routes through `parser.error` for the same clean exit 2, rather
        # than leaking a raw OSError traceback.
        try:
            atomic_write_bytes(out_path, (text + "\n").encode("utf-8"))
        except OSError as exc:
            if parser is not None:
                parser.error(str(exc))
            raise
        summary = f"Exported {len(active)} active memories"
        if include_tombstones:
            summary += f" + {tombstoned_count} tombstones"
        summary += f" to {out_path}\n"
        # Status line goes to stderr so `-o` callers can still pipe
        # the file path on stdout if they want; consistent with how
        # most CLI tools split status from data.
        sys.stderr.write(summary)
    else:
        sys.stdout.write(text + "\n")

    # The strict arm runs AFTER the document is written, in both output
    # modes: `--strict` is about the exit status a backup job checks, not
    # about withholding the partial capture. Opt-in because `export -o` is
    # advertised as the scripted-backup path — flipping the exit status
    # unconditionally would turn a long-broken file into a suddenly-red
    # cron job with no change on the user's part.
    if strict and dropped:
        raise SystemExit(1)
