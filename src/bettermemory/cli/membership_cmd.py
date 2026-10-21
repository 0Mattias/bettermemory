"""`bettermemory membership`: export and import of the projects' portable
identities (design 8.7, E27), and the migration of design 8.6.

`export` writes this store's id and the portable identity of every
project (its file ids, else its oldest and newest witness commits and its
remote keys; no local id and no path), with every record's home keyed by
record id. `bettermemory export` carries the same for the memories it
exports. `import FILE` reads either document and homes the records it
names that are present in this store and homed nowhere: each project is
mapped by `Membership.import_identity`, and each record first meets the
fork rule (`machine.check_import_fork`, E26 e). `migrate [--dry-run]`
runs the migration of design 8.6 over this store
(`membership_migrate.run_command`): every record homed once, with the
rule it was homed by, and the report printed; `--dry-run` prints the same
report from a run rolled back, writing nothing. The CLI's other commands
run it at open when a record has no home (`cli_context`); `import` runs
it after its own homing, so the document homes the records it names.

The health section of design 8.10 (`health_dict`, `health_lines`) lives
here too: `bettermemory health` prints it and the `memory_health` tool
returns it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from ..membership import ImportedProject, Membership, PortableIdentity
    from ..store import Store


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register ``membership`` and its commands."""
    help_text = (
        "Membership across stores: export the projects' portable identities "
        "and the records' homes, import them into this store, and migrate a "
        "store's records into projects."
    )
    parser = sub.add_parser("membership", help=help_text, description=help_text)
    membership_sub = parser.add_subparsers(dest="membership_cmd")

    text = (
        "Write this store's id, every project's portable identity (file ids, "
        "witness commits, remote keys; no local id and no path) and every "
        "record's home, as JSON to stdout or to FILE."
    )
    export = membership_sub.add_parser("export", help=text, description=text)
    export.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        metavar="FILE",
        help="Write the document to FILE instead of stdout.",
    )

    text = (
        "Home the records a membership export (or a `bettermemory export` "
        "document) names that are in this store and homed nowhere, mapping "
        "each project by its file id, or its history and remote together; one "
        "of them asks once in the checkout that holds it."
    )
    imported = membership_sub.add_parser("import", help=text, description=text)
    imported.add_argument("file", metavar="FILE", help="The exported document.")

    text = (
        "Build the membership tables from the store's own records (design "
        "8.6): every memory and episode homed once, with the rule it was "
        "homed by, writing no record row and no file into any repository; "
        "a second run homes nothing. Prints the report."
    )
    migrate = membership_sub.add_parser("migrate", help=text, description=text)
    migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report of a run rolled back; write nothing.",
    )
    return parser


def run(args: argparse.Namespace, *, sub_parser: argparse.ArgumentParser) -> None:
    """Dispatch handler for ``bettermemory membership ...``."""
    command = args.membership_cmd
    if command is None:
        sub_parser.print_help()
        raise SystemExit(2)
    if command == "export":
        _export(args.output)
    elif command == "import":
        _import(args.file)
    elif command == "migrate":
        from ..membership_migrate import run_command

        run_command(dry_run=args.dry_run)
    else:
        sub_parser.error(f"unknown membership command: {command!r}")


def _fail(line: str) -> NoReturn:
    sys.stderr.write(f"bettermemory: {line}\n")
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def _export(output: str | None) -> None:
    """Read-only, as `bettermemory export` is: no store is created and a
    store whose key is absent is read without rekeying it."""
    from .._fsutil import atomic_write_bytes
    from .._response import isoformat
    from ..config import load_config
    from ..membership import EXPORT_FORMAT, EXPORT_FORMAT_VERSION, portable_export
    from ..models import utcnow
    from ..store import STORE_FILENAME, Store

    path = load_config().resolved_directory() / STORE_FILENAME
    if not path.is_file():
        _fail(f"no bettermemory 9 store at {path}")
    store = Store.open(path, allow_rekey=False)
    try:
        body = portable_export(store)
    finally:
        store.close()
    doc = {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": isoformat(utcnow()),
        **body,
    }
    text = json.dumps(doc, indent=2) + "\n"
    if output is None:
        sys.stdout.write(text)
        return
    target = Path(output)
    if not target.parent.is_dir():
        _fail(f"--output parent directory does not exist: {target.parent}")
    try:
        atomic_write_bytes(target, text.encode("utf-8"))
    except OSError as exc:
        _fail(f"could not write {target}: {exc}")
    sys.stderr.write(
        f"Exported {len(doc['projects'])} projects and {len(doc['homes'])} homes "
        f"to {target}\n"
    )


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def _read_document(
    file: str,
) -> tuple[str | None, list[PortableIdentity], dict[str, tuple[str, int | None]]]:
    """The source store id, the identities and the homes a document
    carries: a membership export, or the ``membership`` part of a
    `bettermemory export` document. Exits 2 naming what is malformed."""
    from ..membership import PortableIdentity

    try:
        doc = json.loads(Path(file).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"cannot read {file}: {exc}")
    except ValueError as exc:
        _fail(f"{file} is not JSON: {exc}")
    body = doc.get("membership", doc) if isinstance(doc, dict) else None
    if not isinstance(body, dict):
        _fail(f"{file} carries no membership identities")
    store_id = body.get("store_id")
    projects = body.get("projects")
    homes = body.get("homes")
    if (
        not (store_id is None or isinstance(store_id, str))
        or not isinstance(projects, list)
        or not isinstance(homes, dict)
    ):
        _fail(f"{file} carries no membership identities (store_id, projects, homes)")
    try:
        identities = [PortableIdentity.from_json(raw) for raw in projects]
    except ValueError as exc:
        _fail(f"{file}: {exc}")
    for at, identity in enumerate(identities):
        if identity.parent is not None and not 0 <= identity.parent < len(identities):
            _fail(f"{file}: project {at} names a parent that is not in the document")
        if identity.parent == at:
            _fail(f"{file}: project {at} names itself as its parent")
    out: dict[str, tuple[str, int | None]] = {}
    for record_id, raw in homes.items():
        if not isinstance(raw, dict) or raw.get("kind") not in ("memory", "episode"):
            _fail(f"{file}: the home of {record_id} is malformed")
        where = raw.get("project")
        if where is not None and (
            not isinstance(where, int)
            or isinstance(where, bool)
            or not 0 <= where < len(identities)
        ):
            _fail(f"{file}: the home of {record_id} names no project of the document")
        out[str(record_id)] = (str(raw["kind"]), where)
    return store_id, identities, out


def _present(store: Store, record_id: str, kind: str) -> bool:
    from ..store import NotFoundError

    if kind == "memory":
        if store.has_memory(record_id):
            return True
        try:
            store.get_tombstone(record_id)
        except NotFoundError:
            return False
        return True
    try:
        store.get_episode(record_id)
    except NotFoundError:
        return False
    return True


def _import(file: str) -> None:
    from .. import location
    from ..machine import check_import_fork
    from ..membership import Membership
    from ._common import cli_context, migrate_at_open

    source_store, identities, homes = _read_document(file)
    source = Path(file).name
    # The document homes the records it names first; the migration then
    # homes any record still without a home, as at any other open.
    ctx = cli_context(migrate=False)
    store = ctx.store
    model = Membership.open(store, location.process_machine())
    forks: list[tuple[str, str, str]] = []
    for record_id in homes:
        forked = check_import_fork(
            store, record_store_id=source_store, record_id=record_id, source=file
        )
        if forked is not None:
            forks.append((record_id, *forked))
    needed: list[tuple[str, str, int | None]] = []
    already = absent = 0
    for record_id, (kind, where) in homes.items():
        if not _present(store, record_id, kind):
            absent += 1
        elif model.home(record_id) is not None:
            already += 1
        else:
            needed.append((record_id, kind, where))
    mapped: dict[int, ImportedProject] = {}

    def mapping(at: int, seen: frozenset[int] = frozenset()) -> ImportedProject:
        if at in mapped:
            return mapped[at]
        identity = identities[at]
        parent: str | None = None
        if identity.parent is not None and identity.parent not in seen:
            parent = mapping(identity.parent, seen | {at}).project
        mapped[at] = model.import_identity(
            identity, source=source, source_store=source_store, parent=parent
        )
        return mapped[at]

    homed = 0
    with store.batch():
        for record_id, kind, where in needed:
            imported = None if where is None else mapping(where)
            if model.import_home(
                record_id, kind, imported, source=source, source_store=source_store
            ):
                homed += 1
    print(f"bettermemory membership import: {file}")
    for at in sorted(mapped):
        _say_mapping(model, identities[at], mapped[at])
    print(
        f"  homed {homed} records; {already} already homed; {absent} not in this store"
    )
    for record_id, old, new in forks:
        print(
            f"  store forked: record {record_id} carries this store's id {old}, "
            f"which this store never wrote; its id is now {new}"
        )
    migrate_at_open(store, ctx.config)


def _say_mapping(
    model: Membership, identity: PortableIdentity, imported: ImportedProject
) -> None:
    from .project_cmd import named

    label = named(model, imported.project)
    rule = imported.rule
    if rule.startswith("import:ask:"):
        print(f'  "{identity.name}": kept apart as {label} until answered')
        for proposal in imported.proposals:
            print(f"    {proposal}: {model.proposal(proposal).line}")
    elif rule == "import:new:no_live_checkout":
        print(
            f'  "{identity.name}": {label}, a project with no live checkout here; '
            "a checkout with its evidence joins it"
        )
    elif rule == "import:earlier":
        print(f'  "{identity.name}": {label}, as an earlier import mapped it')
    else:
        print(f'  "{identity.name}": {label} ({rule[len("import:") :]})')


# ---------------------------------------------------------------------------
# The membership section of health (design 8.10)
# ---------------------------------------------------------------------------


def health_model(store: Store) -> Membership:
    """The model `health` reads, for this process's machine; nothing is
    written."""
    from .. import location
    from ..membership import Membership

    return Membership(store, location.process_machine().id)


def _where(model: Membership, checkout_id: str | None) -> str | None:
    if checkout_id is None:
        return None
    try:
        newest = model.checkout(checkout_id).newest_view
    except KeyError:
        return None
    return newest.path if newest is not None else None


def health_dict(model: Membership) -> dict[str, Any]:
    """`Membership.health` as JSON: open questions with their lines,
    provisional and unresolved projects, suppressed recall turns per
    checkout, indeterminate and deferred captures, and the checkouts on
    this machine with no live view (retired ones excluded)."""
    h = model.health()
    return {
        "open_proposals": [
            {
                "id": pr.id,
                "checkout": pr.checkout,
                "path": _where(model, pr.checkout),
                "kind": pr.kind,
                "reason": pr.reason,
                "line": pr.line,
                "opened": pr.opened,
            }
            for pr in h.open_proposals
        ],
        "provisional_projects": [
            {"id": p.id, "name": p.name, "reason": p.provisional} for p in h.provisional
        ],
        "unresolved_projects": [
            {
                "id": u.project,
                "name": u.name,
                "state": u.state,
                "checkout": u.checkout,
                "path": u.path,
                "memories": u.memories,
                "episodes": u.episodes,
            }
            for u in h.unresolved
        ],
        "suppressed_recall_turns": dict(sorted(h.suppressed_turns.items())),
        "indeterminate_captures": h.indeterminate,
        "deferred_captures": h.deferred,
        "checkouts_without_live_view": [
            {"checkout": k, "last_path": _where(model, k)} for k in h.without_live_view
        ],
    }


def health_lines(d: dict[str, Any]) -> list[str]:
    """The membership section of `bettermemory health`'s text, from
    `health_dict`."""
    lines = [f"Membership: open questions ({len(d['open_proposals'])}):"]
    for pr in d["open_proposals"]:
        where = f" at {pr['path']}" if pr["path"] else ""
        lines.append(f"  {pr['id']} in checkout {pr['checkout']}{where}: {pr['line']}")
    if not d["open_proposals"]:
        lines.append("  (none)")
    lines.append(
        f"Membership: provisional projects ({len(d['provisional_projects'])}):"
    )
    for p in d["provisional_projects"]:
        lines.append(f'  {p["id"]} "{p["name"]}": {p["reason"]}')
    if not d["provisional_projects"]:
        lines.append("  (none)")
    lines.append(f"Membership: unresolved projects ({len(d['unresolved_projects'])}):")
    for u in d["unresolved_projects"]:
        lines.append(
            f'  {u["id"]} "{u["name"]}" ({u["state"]}) at {u["path"]}: '
            f"{u['memories']} memories, {u['episodes']} episodes"
        )
    if not d["unresolved_projects"]:
        lines.append("  (none)")
    turns = d["suppressed_recall_turns"]
    lines.append(f"Membership: suppressed recall turns ({sum(turns.values())}):")
    for checkout, count in turns.items():
        lines.append(f"  checkout {checkout}: {count}")
    if not turns:
        lines.append("  (none)")
    lines.append(
        f"Membership: captures {d['indeterminate_captures']} indeterminate, "
        f"{d['deferred_captures']} deferred"
    )
    without = d["checkouts_without_live_view"]
    lines.append(f"Membership: checkouts with no live view ({len(without)}):")
    for k in without:
        lines.append(f"  {k['checkout']}, last seen at {k['last_path']}")
    if not without:
        lines.append("  (none)")
    return lines


__all__ = ["add_subparser", "health_dict", "health_lines", "health_model", "run"]
