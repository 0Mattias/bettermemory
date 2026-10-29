"""`bettermemory export` — dump active (and optionally tombstoned) memories
to a self-describing JSON document.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

from ..models import Memory, TombstonedMemory, utcnow, validate_scope
from .._fsutil import atomic_write_bytes
from .._response import isoformat
from ..store import STORE_FILENAME, Store


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``export`` subparser on the parent parser."""
    help_text = (
        "Dump all active memories (and tombstones, by default) to a "
        "self-describing JSON document, with each record's home, for "
        "reading or for an external indexer. It is not a backup: it "
        "leaves out the signed log, episodes, conflicts and the other "
        "tables, and no command restores a store from it. `bettermemory "
        "backup create` makes a backup. Writes to stdout unless --output "
        "is given."
    )
    parser = sub.add_parser("export", help=help_text, description=help_text)
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Write the export to PATH instead of stdout "
            "(`bettermemory export -o memories.json`)."
        ),
    )
    parser.add_argument(
        "--no-tombstones",
        action="store_true",
        help=(
            "Skip tombstoned memories. By default the export includes "
            "them, with their removal reasons; use this when you only "
            "want the live set."
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
            "Exit non-zero when a memory or tombstone row could not be "
            "loaded and was left out of the export. The document is still "
            "written; only the exit status changes. Use this in a scheduled "
            "export so a short document fails the job instead of passing "
            "silently. Under --no-tombstones the tombstone half is never "
            "read, so --strict cannot fire on it — the export records null "
            "there rather than a zero nobody checked. The store loads every "
            "row it holds, so this fires only when a `memory_restore` lands "
            "between the export's count of the tombstones and its read of "
            "them; run the export again."
        ),
    )
    parser.add_argument(
        "--mirror",
        type=str,
        default=None,
        metavar="DIR",
        help=(
            "Write the bettermemory 9 store as a v8 directory under DIR: each "
            "active memory at its filename, tombstones under .tombstones/, "
            "episodes under episodes/<session>/, byte for byte what v8 wrote. "
            "DIR must be absent, empty, or a mirror this command made before; "
            "inside a mirror, unchanged files are left alone and files the "
            "store no longer holds are removed. Not combined with the JSON "
            "export's flags."
        ),
    )
    parser.add_argument(
        "--store",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "With --mirror: the store file to read. Default: memory.sqlite in "
            "the resolved store directory. A file inside a backup bundle, or "
            "one with other names (hard links), is refused: opening it would "
            "change it and fail the bundle's hash, so copy it out first."
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
    if args.mirror is not None:
        if args.output or args.no_tombstones or args.scope or args.strict:
            sub_parser.error(
                "--mirror writes the v8 directory and takes none of the JSON "
                "export's flags (--output, --no-tombstones, --scope, --strict)"
            )
        _cli_export_mirror(mirror=args.mirror, store=args.store, parser=sub_parser)
        return
    if args.store is not None:
        sub_parser.error("--store only applies to --mirror")
    _cli_export(
        output=args.output,
        include_tombstones=not args.no_tombstones,
        scopes=args.scope or None,
        strict=args.strict,
        parser=sub_parser,
    )


def _cli_export_mirror(
    *,
    mirror: str,
    store: str | None,
    parser: argparse.ArgumentParser,
) -> None:
    """`bettermemory export --mirror DIR` — the store as a v8 directory.
    The store is opened without its key when the key is absent: an
    export reads and never rekeys. A store file inside a backup bundle,
    and a `--store` file with other names (hard links), is refused with
    exit 2, the code for a store this command cannot read
    (`backup.bundle_refusal`): opening it would change the bundle's
    snapshot and fail its hash."""
    from ..backup import bundle_refusal
    from ..config import load_config as _load_config
    from ..mirror import MirrorRefused, write_mirror
    from ..store import STORE_FILENAME, Store

    store_path = (
        Path(store).expanduser()
        if store
        else _load_config().resolved_directory() / STORE_FILENAME
    )
    if not store_path.is_file():
        parser.error(f"no bettermemory 9 store at {store_path} ({STORE_FILENAME})")
    refusal = bundle_refusal(store_path, named=bool(store))
    if refusal is not None:
        parser.error(refusal)
    with Store.open(store_path, allow_rekey=False) as opened:
        try:
            report = write_mirror(opened, Path(mirror))
        except MirrorRefused as exc:
            parser.error(str(exc))
    sys.stdout.write(report.render_text())


def _membership_part(store: Store, record_ids: list[str]) -> dict[str, Any] | None:
    """The exported records' homes and their projects' portable identities
    with this store's id (design 8.7), or None for a store an earlier build
    left without membership tables or id."""
    from ..membership import portable_export

    try:
        return portable_export(store, record_ids)
    except (sqlite3.Error, RuntimeError):
        return None


def _open_export_store(parser: argparse.ArgumentParser | None) -> tuple[Path, Store]:
    """Open the configured store read-only, refusing a missing store or a bundle; returns its directory and the store."""
    # Lazy import keeps the module-import cost of ``bettermemory.cli``
    # off the hot path; ``load_config`` reads TOML and only matters when
    # ``export`` actually runs.
    from ..backup import bundle_refusal
    from ..config import load_config as _load_config

    config = _load_config()
    directory = config.resolved_directory()
    # Read-only, as the mirror is: no store is created where none exists,
    # and a store whose key is absent is read without rekeying it.
    path = directory / STORE_FILENAME
    if not path.is_file():
        msg = f"no bettermemory 9 store at {path} ({STORE_FILENAME})"
        if parser is not None:
            parser.error(msg)
        raise FileNotFoundError(msg)
    # A store directory that is a backup bundle: even this read-only open
    # switches the snapshot to WAL, and the bundle then fails its hash.
    # Exit 2, the code this command gives for a store it cannot read.
    refusal = bundle_refusal(path)
    if refusal is not None:
        if parser is not None:
            parser.error(refusal)
        raise ValueError(refusal)
    store = Store.open(path, allow_rekey=False)
    return directory, store


def _export_scope_set(
    scopes: list[str] | None, parser: argparse.ArgumentParser | None
) -> set[str] | None:
    """Validate the --scope values; returns the scope filter, or None for no filter."""
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
    return scope_set


def _export_active(
    store: Store, scope_set: set[str] | None
) -> tuple[list[Memory], int]:
    """Load the active memories and filter them by scope; returns them and the skipped count."""
    # The store's reader returns every row its table holds or raises, so
    # no active record is left out: `skipped_active_files` stays in the
    # document because format_version 1 froze it, and is always 0.
    active = store.load_all()
    skipped_active_files = 0
    if scope_set is not None:
        active = [m for m in active if scope_set.intersection(m.scopes)]
    return active, skipped_active_files


def _export_tombstones(
    store: Store, scope_set: set[str] | None
) -> tuple[list[TombstonedMemory], int]:
    """Count and load the tombstones, then filter them by scope; returns them and the skipped count."""
    # Count, then read. A `memory_restore` that lands between the two
    # takes a counted tombstone back before the read sees it, and the
    # difference reports it; a `memory_remove` there adds one the count
    # missed, which `max(0, …)` reads as the non-event it is.
    tombstone_files_before = store.count_tombstones()
    tombstoned = store.load_tombstones()
    skipped_tombstone_files = max(0, tombstone_files_before - len(tombstoned))
    if scope_set is not None:
        tombstoned = [t for t in tombstoned if scope_set.intersection(t.scopes)]
    return tombstoned, skipped_tombstone_files


def _warn_skipped_files(
    skipped_active_files: int, skipped_tombstone_files: int | None, directory: Path
) -> int:
    """Warn on stderr about the tombstones that changed while the export read them; returns how many rows are not in the export."""
    # Warn before writing, not after: an export that then fails on a bad
    # --output path has still already told the user their store is short.
    # The document is written either way — a partial backup beats none —
    # so this is a warning, and only --strict escalates it to an exit code.
    dropped = skipped_active_files + (skipped_tombstone_files or 0)
    if skipped_tombstone_files:
        # The one way a row is left out (the active half never drops one):
        # the tombstones changed while the export read them.
        sys.stderr.write(
            f"WARNING: {skipped_tombstone_files} tombstone(s) of the store at "
            f"{directory / STORE_FILENAME} changed while the export read them (a restore landed "
            "between its count and its read) and are NOT in this export. The "
            "export is a partial capture of the store, not a complete backup: "
            "run it again.\n"
        )
    return dropped


def _write_export(
    output: str | None,
    text: str,
    parser: argparse.ArgumentParser | None,
    active: list[Memory],
    include_tombstones: bool,
    tombstoned_count: int,
) -> None:
    """Write the document to ``output`` atomically, or to stdout."""
    if output:
        out_path = Path(output)
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
            # traceback / exit 1, mirroring the --scope ValueError arm in
            # `_export_scope_set` and the sibling rename-scope /
            # tombstones-restore commands. `parser is None` (programmatic /
            # test callers) keeps the raw exception so they still see the
            # exception type.
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
          "membership": {"store_id": ..., "projects": [...], "homes": {...}},
          "skipped_active_files": 0,
          "skipped_tombstone_files": 0
        }

    `membership` carries what an importer needs to home the exported
    records (design 8.7): this store's id, the portable identity of each
    exported record's project and of the parents on its chain (file ids,
    witness commits, remote keys; no local id and no path), and each
    record's home keyed by record id, naming its project by position
    (null for global). `bettermemory membership import` reads it. It is
    null for a store that carries no membership tables.

    `tombstoned_memories` is omitted entirely when --no-tombstones is
    passed (vs. emitted as []) so a consumer can distinguish "not
    requested" from "no tombstones present". Each memory dict mirrors
    the Pydantic model — id, created, updated, scopes, confidence,
    source, body, origin, last_verified_at — and tombstones add
    removed / removed_reason / removed_session.

    The two `skipped_*_files` counts are ALWAYS present, including on a
    clean store where both are 0, so a consumer can assert `== 0` rather
    than remember to probe for absence. `format_version: 1` froze them
    when the store was a directory of files and each was the files on
    disk minus the records the reader handed back. The store is one
    SQLite file now and its readers (`load_all`, `load_tombstones`)
    return every row its table holds or raise, so `skipped_active_files`
    is always 0, and `skipped_tombstone_files` is the tombstone count
    read before the load minus the rows the load returned: non-zero only
    when a `memory_restore` lands between the two reads.
    `skipped_tombstone_files` is `null`, not 0, when --no-tombstones was
    passed: that half was never examined, and reporting 0 there would
    assert an absence nobody checked.

    Round-trippability is therefore conditional: a consumer can recreate
    the active records and tombstones from this document with no loss
    only when both skipped counts are 0. When either is non-zero the
    document is a partial capture of the store, the missing rows are not
    represented in it, and the export says so on stderr.
    `--strict` turns any non-zero count into a non-zero exit for
    scripted backups; the default stays exit 0 with a
    stderr warning so existing cron callers do not start failing on an
    unchanged store. Bump format_version on any breaking change —
    including a rename of these keys, which `format_version: 1` freezes
    as surely as it freezes the record fields.
    """
    import json as _json

    directory, store = _open_export_store(parser)

    scope_set = _export_scope_set(scopes, parser)

    active, skipped_active_files = _export_active(store, scope_set)

    payload: dict[str, Any] = {
        "format_version": 1,
        "exported_at": isoformat(utcnow()),
        "source_directory": str(directory),
        "active_memories": [m.model_dump(mode="json") for m in active],
    }
    record_ids = [m.id for m in active]
    tombstoned_count = 0
    # `None` (JSON null) rather than 0 when the tombstone half was never
    # read — see the docstring: 0 would assert an absence nobody checked.
    skipped_tombstone_files: int | None = None
    if include_tombstones:
        tombstoned, skipped_tombstone_files = _export_tombstones(store, scope_set)
        payload["tombstoned_memories"] = [t.model_dump(mode="json") for t in tombstoned]
        tombstoned_count = len(tombstoned)
        record_ids.extend(t.id for t in tombstoned)

    payload["membership"] = _membership_part(store, record_ids)

    payload["skipped_active_files"] = skipped_active_files
    payload["skipped_tombstone_files"] = skipped_tombstone_files

    text = _json.dumps(payload, indent=2)

    dropped = _warn_skipped_files(
        skipped_active_files, skipped_tombstone_files, directory
    )

    _write_export(output, text, parser, active, include_tombstones, tombstoned_count)

    # The strict arm runs AFTER the document is written, in both output
    # modes: `--strict` is about the exit status a backup job checks, not
    # about withholding the partial capture. Opt-in because `export -o` is
    # advertised as the scripted-backup path — flipping the exit status
    # unconditionally would turn a long-broken file into a suddenly-red
    # cron job with no change on the user's part.
    if strict and dropped:
        raise SystemExit(1)
