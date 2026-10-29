"""`bettermemory key show|export|import|adopt|rotate`: the store's key ring
from the command line (ENG-106), so a key is never placed in or deleted
from the keys directory by hand. The commands are `bettermemory.
key_lifecycle`'s; this module is the argparse registration and the
renderings. Each command reads the store the config resolves (without
the config's first-run write) and the keys directory the store reads.

Exit status: 0 done (or nothing to do; for `show`, the key is present),
1 refused or failed (for `show`, the key is not present), 2 no store or
a usage error, 3 a plan printed without ``--yes``."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path
from typing import Any


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register ``key`` and its ``show``, ``export``, ``import``, ``adopt``
    and ``rotate`` commands."""
    help_text = (
        "The store's key ring: the key outside the store that signs its log, "
        "the retired keys earlier segments verify under, the head checkpoint, "
        "the origin anchor and the binding to one store file. `key show` "
        "reads it; `key export` and `key import` keep and file back keys; "
        "`key adopt` binds the ring to this store file; `key rotate` writes a "
        "new key. Every change is a row in the store's log, signed with its "
        "key. No key file is placed or deleted by hand."
    )
    parser = sub.add_parser("key", help=help_text, description=help_text)
    key_sub = parser.add_subparsers(dest="key_cmd")

    show_text = (
        "Show the store's key as the open, doctor and the audit read it "
        "(present, unreadable, missing, foreign or bound elsewhere), the keys "
        "the ring holds by fingerprint (never a key), the head checkpoint and "
        "the origin anchor against the store's log, and the binding. "
        "Read-only. Exit 0 when the key is present, 1 when it is not, 2 when "
        "there is no store."
    )
    show = key_sub.add_parser("show", help=show_text, description=show_text)
    show.add_argument("--json", action="store_true", help="Emit the report as JSON.")

    export_text = (
        "Write every key the ring holds for the store, current and retired, "
        "to FILE (JSON, owner-only 0600, replaced atomically), to keep where "
        "the keys directory is not: `key import FILE` files them back. "
        "Whoever holds the file can sign the store's rows. Refused when the "
        "ring is bound to another store file, a key file cannot be read, or "
        "FILE is the store's own file or in the keys directory. Writes no "
        "row. Exit 0 written, 1 refused, 2 no store or a bad --out."
    )
    export = key_sub.add_parser("export", help=export_text, description=export_text)
    export.add_argument(
        "--out", required=True, metavar="FILE", help="Where the export goes."
    )
    export.add_argument("--json", action="store_true", help="Emit the report as JSON.")

    import_text = (
        "File the keys in FILE back into the ring: an export (`key export`) "
        "or a key file as a keys directory holds it (`<store_id>.key`, "
        "`<store_id>.<prefix>.key`). Each key is filed only when it signs a "
        "segment of this store's log; the key the store names becomes the "
        "current key when the ring has none or holds another (which is "
        "retired, never deleted), every other key a retired key, so the rows "
        "it signed verify again. Writes a key_imported row. Refused for "
        "another store's key, a key that signs nothing here, a ring bound "
        "elsewhere, a file that cannot be read, or an import that leaves the "
        "ring without the store's own key. Exit 0 filed (or held already), "
        "1 refused or failed, 2 no store."
    )
    imported = key_sub.add_parser("import", help=import_text, description=import_text)
    imported.add_argument("file", metavar="FILE", help="The export or key file.")
    imported.add_argument(
        "--json", action="store_true", help="Emit the report as JSON."
    )

    adopt_text = (
        "Bind the key ring to this store file, whatever it is bound to now, "
        "and write a key_adopted row naming the binding before and after: "
        "for a store copied or moved with its keys directory, which opens "
        "for reading only while the ring is bound to its original. The "
        "original, if it still holds the store id, opens for reading only "
        "from then on. Refused when the ring's key is not the store's, when "
        "this file's log does not carry the ring's origin anchor and reach "
        "its head (`bettermemory log accept-history` accepts such a history "
        "on purpose), or while a daemon serves this file or the bound one. "
        "Without --yes prints the plan. Exit 0 adopted (or bound here "
        "already), 1 refused or failed, 2 no store, 3 the plan."
    )
    adopt = key_sub.add_parser("adopt", help=adopt_text, description=adopt_text)
    adopt.add_argument("--yes", action="store_true", help="Adopt; without it, plan.")
    adopt.add_argument("--json", action="store_true", help="Emit the report as JSON.")

    rotate_text = (
        "Write a new key for the store and append the rekey row that opens "
        "its segment, signed with the new key, keeping the old key under its "
        "fingerprint so the rows it signed still verify. The current key file "
        "is never absent while it runs: the new key is written beside it, the "
        "rotation committed, then the new key renamed over it (an open that "
        "finds a committed rotation unfinished finishes it). Refused while a "
        "daemon serves the store (run `bettermemory down` first) and unless "
        "the key is present. Without --yes prints the plan. Exit 0 rotated, "
        "1 refused or failed, 2 no store, 3 the plan."
    )
    rotate = key_sub.add_parser("rotate", help=rotate_text, description=rotate_text)
    rotate.add_argument("--yes", action="store_true", help="Rotate; without it, plan.")
    rotate.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    return parser


def run(args: argparse.Namespace, *, sub_parser: argparse.ArgumentParser) -> None:
    """Dispatch handler for ``bettermemory key ...``."""
    command = args.key_cmd
    if command is None:
        sub_parser.print_help()
        raise SystemExit(2)
    from .. import key_lifecycle as lifecycle

    store_file = _store_file(sub_parser)
    report: dict[str, Any]
    if command == "show":
        report = lifecycle.describe(store_file)
        _emit(report, args.json, render_show)
        if report["detail"] is not None:
            raise SystemExit(1)
        raise SystemExit(0 if report["status"] == "present" else 1)
    if command == "export":
        out = _export_target(sub_parser, args.out, store_file)
        report = lifecycle.export_keys(store_file, out)
    elif command == "import":
        report = lifecycle.import_keys(store_file, Path(args.file).expanduser())
    elif command == "adopt":
        report = lifecycle.adopt_ring(store_file, apply=args.yes)
    elif command == "rotate":
        report = lifecycle.rotate_key(store_file, apply=args.yes)
    else:
        sub_parser.error(f"unknown key command: {command!r}")
    _emit(report, args.json, render_change)
    raise SystemExit(lifecycle.EXIT_CODES[report["status"]])


def _store_file(parser: argparse.ArgumentParser) -> Path:
    """The store file the config resolves, read without the config's
    first-run write; no file there is a usage error (exit 2), as the
    read-only commands give it."""
    from ._common import read_only_store_path

    path = read_only_store_path(parser)
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        parser.error(f"no bettermemory 9 store at {path}")
    except OSError as exc:
        parser.error(f"cannot read the store at {path}: {exc}")
    if not stat.S_ISREG(mode):
        parser.error(f"no bettermemory 9 store at {path}")
    return path


def _export_target(
    parser: argparse.ArgumentParser, name: str, store_file: Path
) -> Path:
    """``--out``'s path, refused when its directory is missing, when it is
    a directory, the store's own file (its ``-wal`` or ``-shm`` included)
    or in the keys directory (`log_cmd._refuse_store_files`, which compares
    by identity): an export there would sit among the keys it copies."""
    from ._common import open_read_only
    from .log_cmd import _output_path, _refuse_store_files

    out = _output_path(parser, name)
    store = open_read_only(parser, store_file)
    try:
        _refuse_store_files(parser, out, store)
    finally:
        store.close()
    return out


def _emit(report: dict[str, Any], as_json: bool, render: Any) -> None:
    if as_json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render(report))


def _text(lines: list[str]) -> str:
    """The lines a report prints, each through `history.printable`: they
    quote paths and fingerprints a store file or a keys directory holds,
    which whoever wrote those files chose."""
    from ..history import printable

    return "\n".join(printable(line) for line in lines) + "\n"


def render_show(report: dict[str, Any]) -> str:
    """The text form of `key_lifecycle.describe`'s report."""
    if report["detail"] is not None:
        return _text([f"bettermemory key show: {report['detail']}"])
    lines = [
        f"bettermemory key show: {report['status']}",
        f"  store {report['store']}",
        f"  id {report['store_id']}",
        f"  keys directory {report['keys_dir']}",
        f"  {report['summary']}",
    ]
    if report["remedy"]:
        lines.append(f"  fix: {report['remedy']}")
    for key in report["keys"]:
        role = "current" if key["current"] else "retired"
        lines.append(f"  {role} key {key['fingerprint'][:16]}")
    for unread in report["unreadable"]:
        lines.append(f"  unreadable: {unread}")
    head = report["head"]
    head_text = head["status"] + (f" at seq {head['seq']}" if "seq" in head else "")
    lines.append(f"  head {head_text}; the log ends at seq {report['log_end'] or 0}")
    origin = report["origin"]
    origin_text = origin["status"]
    if "seq" in origin:
        origin_text += f" at seq {origin['seq']}"
    if "key_fingerprint" in origin:
        origin_text += f" (key {origin['key_fingerprint'][:16]})"
    if "reason" in origin:
        origin_text += f" ({origin['reason']})"
    lines.append(f"  origin {origin_text}")
    binding = report["binding"]
    if "unreadable" in binding:
        lines.append(f"  binding cannot be read: {binding['unreadable']}")
    elif binding["path"] is None:
        lines.append("  binding: none")
    elif binding["this_file"]:
        lines.append(f"  bound to this file ({binding['path']})")
    else:
        lines.append(f"  bound to another file: {binding['path']}")
    return _text(lines)


def render_change(report: dict[str, Any]) -> str:
    """The text form of an export, import, adopt or rotate report."""
    command, status = report["command"], report["status"]
    heading = {
        "planned": "plan (nothing changed; run again with --yes)",
        "unchanged": "nothing to do",
    }.get(status, status)
    lines = [f"bettermemory key {command}: {heading}", f"  store {report['store']}"]
    if report["detail"]:
        lines.append(f"  {report['detail']}")
    if command == "export" and status == "done":
        lines.append(
            f"  {len(report['keys'])} key(s) of store {report['store_id']} written "
            f"to {report['out']} (owner-only); whoever holds it can sign the "
            "store's rows"
        )
    if command == "import":
        for key in report["keys"]:
            lines.append(f"  key {key['fingerprint'][:16]}: {key['action']}")
        if report.get("retired_current"):
            lines.append(
                f"  the ring's other current key {report['retired_current'][:16]} "
                "was retired"
            )
    if command == "adopt" and report.get("binding"):
        binding = report["binding"]
        lines.append(f"  binding: {binding['before'] or 'none'} -> {binding['after']}")
        if report.get("previous_holds_store"):
            lines.append(
                "  the file it was bound to still holds the store id, and opens "
                "for reading only once the ring is bound here"
            )
    if command == "rotate" and report.get("old_fingerprint"):
        new = report.get("new_fingerprint")
        lines.append(
            f"  key {report['old_fingerprint'][:16]} -> "
            + (new[:16] if new else "a new key")
        )
    if "seq" in report:
        lines.append(f"  recorded in the log at seq {report['seq']}")
    if "log" in report:
        lines.append(f"  log verify: {report['log']}")
    return _text(lines)


__all__ = ["add_subparser", "render_change", "render_show", "run"]
