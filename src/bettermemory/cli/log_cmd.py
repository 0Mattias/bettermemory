"""`bettermemory log`: the store's hash-chained log. `log verify` checks
it against its key, its head checkpoint, a fold of its own rows and, on
request, anchors kept off the host; `log export` writes it as JSON Lines
for a SIEM (`bettermemory.audit_export`); `log anchor` takes one of those
anchors (`bettermemory.anchor`); `log accept-history` accepts a replaced
history on the record (`bettermemory.key_lifecycle.accept_history`).

Every command here but `accept-history` is read-only: the store opens
with ``read_only=True``, so a missing key is reported and never replaced,
and nothing is written into the log being read, nor to the file or
beside it (`store_schema.connect_read_only`). `accept-history` changes
nothing without ``--yes``."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from ..store import Store


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``log`` subparser and its ``verify``, ``export`` and
    ``anchor`` commands."""
    help_text = (
        "The bettermemory 9 store's hash-chained log. `log verify` checks "
        "every row's MAC against the key kept outside the store, the chain "
        "between rows, the head checkpoint against the last row, the origin "
        "anchor against the row where this host's history begins, and every "
        "table against a replay of the log. Exit 0 when the log verifies, "
        "1 when it is tampered or cannot be verified. `log export` writes "
        "the log as JSON Lines for an audit pipeline, `log anchor` takes an "
        "anchor to keep off this host for `log verify --anchor`, and `log "
        "accept-history` accepts a replaced history on the record."
    )
    parser = sub.add_parser("log", help=help_text, description=help_text)
    log_sub = parser.add_subparsers(dest="log_cmd")
    verify_text = (
        "Verify the log: MACs, chain, head, the origin anchor and fold, and "
        "every --anchor given. The origin anchor, kept beside the key outside "
        "the store (<store_id>.origin in the keys directory), names the row "
        "where this host's history of the store "
        "begins and the key that signed it: that row must still be in the "
        "log with the same MAC and verify under that key. A row gone or "
        "changed, or one that key does not verify while it is still held "
        "here, adds the problem origin_mismatch and the log reads tampered. "
        "When that key is no longer held (its key file was lost) or no key "
        "is held at all, nothing here can check the row: the origin reads "
        "unverifiable, and so does the log, until the key is filed back "
        "(`bettermemory key import`). `bettermemory log accept-history --yes` "
        "accepts a replaced history on purpose, on the record. Prints a "
        "report and exits 0 only when every check passes; "
        "1 when the log is tampered or cannot be verified (an anchor or an "
        "origin that no longer matches is tampering); 2 when there is no "
        "store or an anchor file cannot be read."
    )
    verify = log_sub.add_parser("verify", help=verify_text, description=verify_text)
    verify.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of text.",
    )
    verify.add_argument(
        "--anchor",
        action="append",
        default=None,
        metavar="FILE",
        help=(
            "Also check the anchors in FILE (written by `log anchor`; one JSON "
            "object, or JSON Lines of them): the log must still hold each "
            "anchor's seq with the same MAC and the same chain digest. A "
            "failed anchor adds the problem anchor_mismatch. An anchor of "
            "another store is listed as other_store and changes nothing. "
            "Repeat for several files. Without it the report is unchanged."
        ),
    )
    export_text = (
        "Write the log as JSON Lines, one object per row in seq order, for "
        "a SIEM or an audit archive: seq, ts, kind, class, session, actor, "
        "record ids, the payload, the MAC and the MAC it chains to, the "
        "segment's key fingerprint and whether the row's MAC verifies "
        "under the key held here (null when it is not held). Bodies and "
        "other free text are replaced by their sha256 and length unless "
        "--include-bodies. Read-only and deterministic: the same log gives "
        "the same bytes. Exit 0 when the export is written, 1 when the "
        "output closed before it was, 2 when there is no store it can read, "
        "an argument is wrong or --out cannot be written (or names the "
        "store's own files or the keys directory). The export is a trail, "
        "not a verdict: run `log verify` for that."
    )
    export = log_sub.add_parser("export", help=export_text, description=export_text)
    export.add_argument(
        "--since-seq",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Export only the rows after seq N. A cron job passes the last "
            "seq it exported (the through_seq --out reports). Default 0: "
            "from the first row."
        ),
    )
    export.add_argument(
        "--until-seq",
        type=int,
        default=None,
        metavar="M",
        help="Export only the rows up to and including seq M.",
    )
    export.add_argument(
        "--kinds",
        default=None,
        metavar="K1,K2",
        help=(
            "Export only rows of these kinds, comma-separated "
            "(memory_put,tombstone_put,rekey for example)."
        ),
    )
    export.add_argument(
        "--include-bodies",
        action="store_true",
        help=(
            "Keep every payload as the log holds it, memory bodies, "
            "takeaways and episode bodies included, every version of each. "
            "The output is then as sensitive as the store file. Warns on "
            "stderr."
        ),
    )
    export.add_argument(
        "--out",
        default=None,
        metavar="FILE",
        help=(
            "Write the lines to FILE (owner-only, 0600, replaced atomically "
            "when the export completes) instead of stdout, and print a "
            "summary: rows written, the seq range, and the through_seq to "
            "pass as the next --since-seq."
        ),
    )
    export.add_argument(
        "--json",
        action="store_true",
        help=(
            "With --out, print the summary as JSON. The rows themselves are "
            "always JSON Lines."
        ),
    )
    anchor_text = (
        "Take an anchor of the log: the store id, the last row's seq and "
        "MAC, a SHA-256 over every row's MAC up to it, and the time. Keep it "
        "off this host (a ticket, a git repository, a WORM bucket); `log "
        "verify --anchor FILE` then shows whether history up to that seq "
        "was rewritten since, even by a process that holds the key. Prints "
        "the anchor as one JSON line, or writes it to --out. Exit 0 when "
        "the anchor is taken, 2 when there is no store it can read, its log "
        "is empty or --out cannot be written (or names the store's own files "
        "or the keys directory)."
    )
    anchor = log_sub.add_parser("anchor", help=anchor_text, description=anchor_text)
    anchor.add_argument(
        "--out",
        default=None,
        metavar="FILE",
        help=(
            "Write the anchor to FILE (owner-only, 0600, replaced atomically) "
            "and print where it went, instead of printing the anchor."
        ),
    )
    anchor.add_argument(
        "--json",
        action="store_true",
        help=(
            "With --out, print {anchor, out} as JSON. Without it the anchor "
            "is printed as JSON either way."
        ),
    )
    accept_text = (
        "Accept the store file's history as it stands, on the record, when "
        "the origin anchor reads origin_mismatch or unverifiable or the head "
        "checkpoint names a row the log does not hold: a store file copied "
        "over this host's copy again, an anchor from before 9.0.0rc13 whose "
        "key was lost, an older copy put back by hand. With the daemon "
        "stopped, the anchor and the head are set aside, the key ring is "
        "bound to this file, and the store is opened as its owner opens it: "
        "a missing or foreign key is settled as any writing command settles "
        "it, and this host's history is anchored at the first row of the "
        "earliest segment a key it holds signs. A history_accepted row, "
        "signed with the store's key, names the anchor, head and binding "
        "before and after; the rewind record beside the head "
        "(<store_id>.backup_restore.json) gets the same entry. Refused "
        "while a daemon serves or starts for the store, or when the key "
        "file cannot be read. Without --yes prints the plan. Exit 0 "
        "accepted (or nothing to accept), 1 refused, failed or the log "
        "still tampered after it, 2 no store, 3 the plan."
    )
    accept = log_sub.add_parser(
        "accept-history", help=accept_text, description=accept_text
    )
    accept.add_argument(
        "--yes", action="store_true", help="Accept the history; without it, plan."
    )
    accept.add_argument(
        "--json", action="store_true", help="Emit the report as JSON instead of text."
    )
    return parser


def run(
    args: argparse.Namespace,
    *,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory log ...``."""
    if args.log_cmd is None:
        sub_parser.print_help()
        raise SystemExit(2)
    if args.log_cmd == "verify":
        _cli_log_verify(
            json_out=args.json, parser=sub_parser, anchor_files=args.anchor or ()
        )
        return
    if args.log_cmd == "export":
        _cli_log_export(args, parser=sub_parser)
        return
    if args.log_cmd == "anchor":
        _cli_log_anchor(args, parser=sub_parser)
        return
    if args.log_cmd == "accept-history":
        _cli_log_accept_history(args, parser=sub_parser)
        return
    sub_parser.error(f"unknown log subcommand: {args.log_cmd!r}")


def _cli_log_accept_history(
    args: argparse.Namespace, *, parser: argparse.ArgumentParser
) -> None:
    from ..key_lifecycle import DONE, EXIT_CODES, accept_history
    from .key_cmd import _store_file

    report = accept_history(_store_file(parser), apply=args.yes)
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_accept(report))
    if report["status"] == DONE and report.get("log") == "tampered":
        raise SystemExit(1)
    raise SystemExit(EXIT_CODES[report["status"]])


def _anchor_text(origin: dict[str, Any] | None) -> str:
    if origin is None:
        return "none"
    named = origin.get("key_fingerprint")
    return f"seq {origin['seq']}" + (f" (key {named[:16]})" if named else "")


def render_accept(report: dict[str, Any]) -> str:
    """The text form of a `key_lifecycle.accept_history` report."""
    from ..history import printable

    status = report["status"]
    heading = {
        "planned": "plan (nothing changed; run again with --yes to accept)",
        "unchanged": "nothing to accept",
        "done": "accepted",
    }.get(status, status)
    lines = [
        f"bettermemory log accept-history: {heading}",
        f"  store {report['store']}",
    ]
    if report["detail"]:
        lines.append(f"  {report['detail']}")
    fit = report.get("fit")
    if fit is not None:
        origin = fit["origin"]
        lines.append(
            f"  origin anchor {_anchor_text(report['origin']['before'])}: "
            f"{origin['status']}"
            + (f" ({origin['reason']})" if "reason" in origin else "")
        )
        end = report["log_end_before"]
        head = report["head"]["before"]
        lines.append(
            f"  head checkpoint {'none' if head is None else 'seq ' + str(head['seq'])}: "
            f"{fit['head']['status']}; the log ends at seq "
            f"{0 if end is None else end['seq']}"
        )
    if status == "planned":
        anchor_at = report.get("anchor_at")
        lines.append(
            "  the anchor and the head are set aside and the store opened as its "
            "owner opens it; "
            + (
                f"this host's history is anchored at seq {anchor_at['seq']}"
                if anchor_at
                else f"the key ({report.get('key_state')}) is settled first, and "
                "the row that settles it anchors this host's history"
            )
        )
    binding = report["binding"]
    if status in ("planned", "done") and binding["before"] != binding["after"]:
        lines.append(
            f"  key ring binding: {binding['before'] or 'none'} -> {binding['after']}"
        )
    if status == "done":
        lines.append(
            f"  origin anchor now {_anchor_text(report['origin']['after'])}; head "
            + (
                "none"
                if report["head"]["after"] is None
                else f"seq {report['head']['after']['seq']}"
            )
        )
        lines.append(f"  recorded in the log at seq {report['seq']}")
        if report.get("record"):
            lines.append(f"  and in {report['record']}")
        if report.get("log"):
            lines.append(f"  log verify: {report['log']}")
    return "\n".join(printable(line) for line in lines) + "\n"


def _open_read_only(parser: argparse.ArgumentParser, *, exit_code: int = 2) -> Store:
    """The resolved store, opened read-only (`read_only=True`): a
    command that reads the log must not write a rekey row into it. A
    missing key is then reported, not fixed. The config is read without
    its first-run write, and a config or a store file that cannot be read
    ends the command with the reason and `exit_code`
    (`_common.read_only_store_path`, `_common.open_read_only`)."""
    from ._common import open_read_only, read_only_store_path

    path = read_only_store_path(parser, exit_code=exit_code)
    return open_read_only(parser, path, exit_code=exit_code)


def _cli_log_verify(
    *,
    json_out: bool,
    parser: argparse.ArgumentParser,
    anchor_files: Sequence[str] = (),
) -> None:
    from ..anchor import (
        Anchor,
        AnchorFileError,
        check_anchors,
        load_anchors,
        merge_into_report,
    )
    from ..history import history_replaced

    # Read every anchor before the store: a file that is not one is a
    # usage error, reported before any work and never as a verdict.
    anchors: list[tuple[str, Anchor]] = []
    for name in anchor_files:
        try:
            anchors.extend((name, anchor) for anchor in load_anchors(name))
        except AnchorFileError as exc:
            parser.error(str(exc))
    # A config or store file that cannot be read is a log that cannot be
    # verified: exit 1, as before, with the reason instead of a traceback.
    store = _open_read_only(parser, exit_code=1)
    try:
        report = store.log_verify()
        # The rows that say the history was replaced, each kind in its
        # words (`log.HISTORY_REPLACED`): what a head or anchor that moved
        # back, or a range of seqs used twice, comes from. Present only
        # when there is one, so any other report is as before.
        replaced = history_replaced(store)
        if replaced:
            report["history_replaced"] = replaced
        if anchor_files:
            merge_into_report(
                report, check_anchors(store.conn, store.store_id, anchors)
            )
    finally:
        store.close()
    if json_out:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write(render_report(report))
    raise SystemExit(0 if report["status"] == "ok" else 1)


def _output_path(parser: argparse.ArgumentParser, name: str) -> Path:
    """``--out``'s path, its directory required to exist: a mistyped
    path is an error, not a tree created on the way (as `export -o`)."""
    path = Path(name).expanduser()
    if not path.parent.is_dir():
        parser.error(f"--out: no directory {path.parent}")
    if path.is_dir():
        parser.error(f"--out: {path} is a directory")
    return path


def _same_file(a: Path, b: Path) -> bool:
    """Whether ``a`` and ``b`` are one file or directory to the operating
    system (`os.path.samefile`); False when either does not exist or
    cannot be read."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _refuse_store_files(
    parser: argparse.ArgumentParser, out: Path, store: Store
) -> None:
    """Refuse an ``--out`` that names the store file, its ``-wal`` or
    ``-shm``, or a path in the keys directory. The output replaces its
    path atomically: a read-only command must not put JSON where the
    store was (a daemon still serving it would go on writing to the
    unlinked file), nor a file among the keys and the head checkpoint.

    Compared by identity, not by spelling: a symlink, a relative path, a
    macOS firmlink, or a spelling that differs only in case on a volume
    that folds case (macOS's and Windows' default) names the same file,
    and `Path.resolve` puts none of the last two right. A ``-wal`` or
    ``-shm`` that does not exist yet is caught by its name in the store's
    directory, compared folded when the store's volume folds case. Called
    with the store open, before anything is written."""
    target = out.expanduser().absolute()
    resolved = target.resolve()
    store_file = store.path
    keys_dir = store.keys_dir.expanduser()
    own = [store_file.with_name(store_file.name + s) for s in ("", "-wal", "-shm")]
    # The store file exists while it is open, so its case-flipped
    # spelling resolving to it says whether this volume folds case.
    folds = _same_file(store_file, store_file.with_name(store_file.name.swapcase()))

    def fold(name: str) -> str:
        return name.casefold() if folds else name

    own_names = {fold(path.name) for path in own}
    if any(_same_file(target, path) for path in own) or any(
        _same_file(path.parent, store_file.parent) and fold(path.name) in own_names
        for path in (target, resolved)
    ):
        store.close()
        parser.error(f"--out: {out} is the store's own file")
    if any(
        _same_file(directory, keys_dir)
        for directory in (target, resolved, *target.parents, *resolved.parents)
    ):
        store.close()
        parser.error(f"--out: {out} is in the keys directory {keys_dir}")


@contextlib.contextmanager
def _owner_only_output(path: Path) -> Iterator[TextIO]:
    """A text stream whose contents land at ``path`` only when the block
    completes: written to a temporary file beside it, created owner-only
    (0600) before the first byte, flushed, fsynced, then renamed over
    ``path``. A failure part way leaves ``path`` as it was and removes the
    temporary file, so a cron job never reads half an export."""
    from .._fsutil import fsync_dir, fsync_file, replace_atomic

    fd, name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(name)
    done = False
    try:
        if sys.platform != "win32":
            # mkstemp creates 0600 already; the fchmod says so for a
            # platform whose default differs.
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            yield handle
            handle.flush()
            fsync_file(handle.fileno())
        replace_atomic(tmp, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        fsync_dir(path.parent)
        done = True
    finally:
        if not done:
            with contextlib.suppress(OSError):
                tmp.unlink()


def _parse_kinds(parser: argparse.ArgumentParser, raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    kinds = [name.strip() for name in raw.split(",") if name.strip()]
    if not kinds:
        parser.error("--kinds: name at least one kind")
    return kinds


def _blackhole_stdout() -> None:
    """Point stdout's descriptor at /dev/null after the reader went away,
    so the interpreter's own flush at exit does not fail again and
    overrule the exit code (the recipe from the `signal` docs' SIGPIPE
    note, as `session_start_cmd` uses it). Silent when stdout has no real
    descriptor (a capture object, an embedding host)."""
    try:
        fd = sys.stdout.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
    except (AttributeError, OSError, ValueError):
        return
    try:
        os.dup2(devnull, fd)
    except OSError:
        pass
    finally:
        os.close(devnull)


def _cli_log_export(
    args: argparse.Namespace, *, parser: argparse.ArgumentParser
) -> None:
    from ..audit_export import MAX_SEQ, export, rewinds_across
    from ..history import printable

    since = int(args.since_seq)
    until = None if args.until_seq is None else int(args.until_seq)
    if since < 0:
        parser.error("--since-seq must be 0 or more")
    if until is not None and until < since:
        parser.error("--until-seq must be at least --since-seq")
    if max(since, until or 0) > MAX_SEQ:
        parser.error(f"--since-seq and --until-seq are at most {MAX_SEQ}")
    kinds = _parse_kinds(parser, args.kinds)
    out = None if args.out is None else _output_path(parser, args.out)
    if args.include_bodies:
        sys.stderr.write(
            "bettermemory log export: --include-bodies exports every body, "
            "takeaway and other free text the log holds, every version of each, "
            "removed records' included; keep the output as you keep the store "
            "file\n"
        )
    store = _open_read_only(parser)
    if out is not None:
        _refuse_store_files(parser, out, store)
    try:
        if out is None:
            try:
                summary = export(
                    store,
                    sys.stdout.write,
                    since_seq=since,
                    until_seq=until,
                    kinds=kinds,
                    include_bodies=args.include_bodies,
                )
                sys.stdout.flush()
            except BrokenPipeError:
                _blackhole_stdout()
                raise SystemExit(1) from None
        else:
            try:
                with _owner_only_output(out) as handle:
                    summary = export(
                        store,
                        handle.write,
                        since_seq=since,
                        until_seq=until,
                        kinds=kinds,
                        include_bodies=args.include_bodies,
                    )
            except OSError as exc:
                parser.error(f"--out: cannot write {out}: {exc}")
    finally:
        store.close()
    # A log with no rows at all ends at seq 0: a cursor past it is the
    # same warning, the whole log being the tail that went.
    log_end = summary.log_last_seq or 0
    if log_end < since:
        empty = " (it holds no rows)" if summary.log_last_seq is None else ""
        sys.stderr.write(
            f"bettermemory log export: the log ends at seq {log_end}{empty}, "
            f"before --since-seq {since}: the store was restored or its tail "
            f"removed since the last export, or this is another store; "
            f"run `bettermemory log verify`\n"
        )
    # A cursor inside a range a restore rewound, or a history accepted
    # behind its head checkpoint: once the log grows past it again, the
    # rows that took those seqs are skipped, and nothing above would say so.
    rewinds = rewinds_across(store.keys_dir, store.store_id, since)
    for rewind in rewinds:
        sys.stderr.write(printable(_rewind_warning(rewind, since)) + "\n")
    if out is None:
        raise SystemExit(0)
    if args.json:
        document = {**summary.to_dict(), "out": str(out), "rewinds": rewinds}
        sys.stdout.write(json.dumps(document) + "\n")
    elif summary.rows:
        sys.stdout.write(
            f"bettermemory log export: {summary.rows} row(s), seq "
            f"{summary.first_seq} to {summary.last_seq}, written to {out}; "
            f"next --since-seq {summary.through_seq}\n"
        )
    else:
        sys.stdout.write(
            f"bettermemory log export: no rows to export after seq {since}, "
            f"written to {out}; next --since-seq {summary.through_seq}\n"
        )
    raise SystemExit(0)


def _rewind_warning(rewind: Mapping[str, Any], since: int) -> str:
    """`log export`'s warning for a cursor inside a range that a row of a
    `log.HISTORY_REPLACED` kind replaced (`audit_export.rewinds_across`), in
    that kind's words: a restore rewound the log; an accepted history (`log
    accept-history`) took the log as it stood, behind its head checkpoint."""
    from ..log import HISTORY_REPLACED, STORE_RESTORED

    at, to, start = rewind["restored_at"], rewind["to_seq"], rewind["from_seq"]
    restore = rewind["kind"] == STORE_RESTORED
    event = "restore" if restore else "acceptance"
    lead = f"bettermemory log export: {HISTORY_REPLACED[rewind['kind']][0]} at {at} "
    skipped = (
        f"a cursor taken before that {event} skips the new rows after seq {to} "
        f"up to seq {since}; export them with --since-seq {to} (if they were "
        f"exported after the {event}, "
    )
    if start is None:
        # A restore onto a host that held no copy of the store: where the
        # replaced log ended is not known here.
        return (
            f"{lead}put the log back to seq {to} on a host that held no copy of "
            "the store, so where the log it replaced ended is not known here, "
            f"and --since-seq {since} is past seq {to}: {skipped}this repeats "
            "for every cursor past that seq; the batches' prev_mac link tells "
            "the two apart)"
        )
    did = (
        f"rewound the log from seq {start} to seq {to}"
        if restore
        else f"accepted the log as it stood at seq {to}, where the head "
        f"checkpoint had said seq {start}"
    )
    return (
        f"{lead}{did}, and --since-seq {since} lies between: {skipped}this "
        f"repeats until the cursor passes seq {start})"
    )


def _cli_log_anchor(
    args: argparse.Namespace, *, parser: argparse.ArgumentParser
) -> None:
    from .._fsutil import atomic_write_bytes
    from ..anchor import take

    out = None if args.out is None else _output_path(parser, args.out)
    store = _open_read_only(parser)
    if out is not None:
        _refuse_store_files(parser, out, store)
    try:
        anchor = take(store)
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        store.close()
    if out is None:
        sys.stdout.write(anchor.to_line())
        raise SystemExit(0)
    try:
        atomic_write_bytes(
            out, anchor.to_line().encode("utf-8"), mode_before_rename=0o600
        )
    except OSError as exc:
        parser.error(f"--out: cannot write {out}: {exc}")
    if args.json:
        sys.stdout.write(
            json.dumps({"anchor": anchor.to_dict(), "out": str(out)}, indent=2) + "\n"
        )
    else:
        sys.stdout.write(
            f"bettermemory log anchor: seq {anchor.seq} of store "
            f"{anchor.store_id} anchored to {out}; keep it off this host\n"
        )
    raise SystemExit(0)


def render_report(report: dict[str, Any]) -> str:
    """The text form of a `log_verify` report. It quotes what the store
    file says about itself (the fingerprints rekey rows name, a row's
    kind, a record id), which an editor of the file chose: each line goes
    through `history.printable`, so an escape sequence in them stays
    text, as in `backup verify`'s rendering of the same report."""
    from ..history import printable

    head = report["head"]
    head_text = head["status"]
    if "seq" in head:
        head_text += f" at seq {head['seq']}"
    lines = [
        f"bettermemory log verify: {report['status']}",
        f"  store {report['path']}",
        f"  id {report['store_id']}",
        f"  rows {report['rows']} in {len(report['segments'])} segment(s), head {head_text}",
    ]
    # The origin anchor: where this host's history of the store begins
    # (`log.check_origin`). A report from before it existed has none.
    origin = report.get("origin")
    if origin is not None:
        origin_text = origin["status"]
        if "seq" in origin:
            origin_text += f" at seq {origin['seq']}"
        if "reason" in origin:
            origin_text += f" ({origin['reason']})"
        lines.append(f"  origin {origin_text}")
    for segment in report["segments"]:
        lines.append(
            f"  segment seq {segment['first_seq']} to {segment['last_seq']}: "
            f"{segment['status']} (key {segment['key_fingerprint'][:16]})"
        )
    fold = report["fold"]
    lines.append(f"  fold {fold['status']}")
    for table, counts in fold["tables"].items():
        if counts["unaccounted"] or counts["missing"]:
            lines.append(
                f"    {table}: {len(counts['unaccounted'])} unaccounted, "
                f"{len(counts['missing'])} missing"
            )
            for key in counts["unaccounted"]:
                lines.append(f"      unaccounted {key}")
            for key in counts["missing"]:
                lines.append(f"      missing {key}")
    # The membership tables of design 8.5, folded by the same replay
    # (`Store.refold` reports them under ``membership``).
    membership = fold.get("membership") or {}
    rows = sum(int(counts["rows"]) for counts in membership.values())
    lines.append(f"  membership {rows} rows in {len(membership)} tables")
    for table, counts in membership.items():
        if counts["unaccounted"] or counts["missing"]:
            lines.append(
                f"    {table}: {len(counts['unaccounted'])} unaccounted, "
                f"{len(counts['missing'])} missing"
            )
            for key in counts["unaccounted"]:
                lines.append(f"      unaccounted {key}")
            for key in counts["missing"]:
                lines.append(f"      missing {key}")
    # Present only when `--anchor` was given, so a report without anchors
    # renders exactly as it did before anchors existed.
    if "anchors" in report:
        lines.append(f"  anchors {len(report['anchors'])}")
        for entry in report["anchors"]:
            status = entry["status"]
            if "reason" in entry:
                status += f" ({entry['reason']})"
            lines.append(f"    seq {entry['seq']}: {status} ({entry['file']})")
    # Present only in `log verify`'s report, and only when a row replaced
    # the history, so any other report renders as before.
    replaced = report.get("history_replaced") or []
    if replaced:
        lines.append(f"  history replaced {len(replaced)} time(s)")
        lines.extend(f"    {_replaced_text(entry)}" for entry in replaced)
    if report["problems"]:
        lines.append("  problems")
        for problem in report["problems"]:
            lines.append(
                f"    seq {problem['seq']} ({problem['kind']}): {problem['problem']}"
            )
    return "\n".join(printable(line) for line in lines) + "\n"


def _replaced_text(entry: Mapping[str, Any]) -> str:
    """One line of `log verify`'s list of the rows that replaced the
    history (`history.history_replaced`), in its kind's words."""
    from ..log import HISTORY_REPLACED, STORE_RESTORED

    to, start = entry.get("to_seq"), entry.get("from_seq")
    said = f"seq {entry['seq']} {entry['kind']}: {HISTORY_REPLACED[entry['kind']][0]}"
    said += f" at {entry['ts']}"
    if entry["kind"] == STORE_RESTORED:
        said += f" put the log back to seq {to}"
        if start is not None:
            said += f" from seq {start}"
    else:
        said += f" accepted the log as it stood at seq {to}"
        if start is not None and start != to:
            said += f", where the head checkpoint had said seq {start}"
    return said
